"""导入服务：接收来源 → 解析/获取 → 证券库识别 → 事务内发布。

规则要点：
- 每个文件、文本块或链接独立成批次，一个来源失败不影响其他来源；
- 歧义（多工作表/多代码列）保留为待选择，用户确认后继续，不判为失败；
- 问财链接先判定获取完整性，再按查询身份决定自动发布或等待条件确认；
- 同一股票与同一导入日期唯一，重复提交/重试只关联来源、不重复建项；
- 重新识别只处理原批次跳过项，成功后按原导入日期补入，仍未知的明细删除。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import replace
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.errors import (
    AmbiguousSelection,
    ImportRejected,
    ParseFailed,
    UnsupportedFormat,
    WencaiCookieMissing,
    WencaiUnexpected,
)
from dailyscreen_lite.domain.models import (
    BatchStatus,
    BatchStock,
    ImportBatch,
    ImportDate,
    ParsedCandidate,
    ParseOptions,
    ParseResult,
    Security,
    SourceKind,
    StockOutcome,
)
from dailyscreen_lite.imports.parsers.csv_parser import parse_csv
from dailyscreen_lite.imports.parsers.text_parser import parse_text
from dailyscreen_lite.imports.parsers.xlsx_parser import parse_xlsx
from dailyscreen_lite.imports.publishing import publish_selections
from dailyscreen_lite.repository import Database
from dailyscreen_lite.repository import classification_repo, imports_repo, securities_repo
from dailyscreen_lite.securities.resolver import normalize_code, resolve_normalized
from dailyscreen_lite.wencai.acquisition import (
    COMPLETENESS_ZERO,
    AcquisitionResult,
    WencaiProtocol,
    acquire,
)
from dailyscreen_lite.wencai.condition import condition_fingerprint
from dailyscreen_lite.wencai.cookie_store import CookieStore
from dailyscreen_lite.wencai.url import decode_link

_UNSAFE = re.compile(r"[^0-9A-Za-z._\u4e00-\u9fff-]+")

_CSV_SUFFIXES = {".csv", ".tsv"}
_XLSX_SUFFIXES = {".xlsx", ".xlsm"}
_TEXT_SUFFIXES = {".txt"}
MAX_BYTES = 20 * 1024 * 1024


def _safe_name(name: str, *, fallback: str) -> str:
    base = Path(name).name or fallback
    return _UNSAFE.sub("_", base)[:120] or fallback


def _looks_like_link(text: str) -> bool:
    """单行且看起来是问财结果链接时按链接来源处理。"""
    if not text or "\n" in text:
        return False
    lowered = text.lower()
    return lowered.startswith(("http://", "https://")) and "iwencai.com" in lowered


@dataclass(frozen=True)
class SourceDraft:
    """一次来源接收的固定上下文：批次身份、来源信息与接收时间。"""

    batch_id: str
    source_kind: SourceKind
    source_name: str
    source_ref: str | None
    archive_path: str
    received_at: datetime
    import_date: ImportDate


@dataclass(frozen=True)
class Receipt:
    """一次提交的接收时刻。

    同一提交的多个来源共用同一个 Receipt：日期与接收时间在提交时固定一次，
    避免逐来源读时钟时前一个来源耗时跨午夜，把后续来源归入次日。
    """

    received_at: datetime
    import_date: ImportDate


@dataclass(frozen=True)
class LinkMetadata:
    query_text: str
    condition_labels: tuple[str, ...]
    identity_fingerprint: str
    completeness: str | None
    condition_fingerprint: str | None = None


def _source_kind_for_suffix(suffix: str) -> SourceKind:
    if suffix in _XLSX_SUFFIXES:
        return SourceKind.XLSX
    if suffix in _TEXT_SUFFIXES:
        return SourceKind.TEXT
    return SourceKind.CSV


def _parser_for_suffix(suffix: str):
    if suffix in _CSV_SUFFIXES:
        return parse_csv
    if suffix in _XLSX_SUFFIXES:
        return parse_xlsx
    if suffix in _TEXT_SUFFIXES:
        return parse_text
    raise UnsupportedFormat(f"暂不支持的文件格式：{suffix or '未知'}")


def _read_batch(db: Database, batch_id: str) -> ImportBatch:
    """按 id 读取批次，不存在时抛 KeyError。"""
    with db.read() as conn:
        batch = imports_repo.get_batch(conn, batch_id)
    if batch is None:
        raise KeyError(batch_id)
    return batch


def _draft_from_batch(batch: ImportBatch) -> SourceDraft:
    """复用已接收批次的固定上下文（重试/选择/确认/重识别不改日期与来源）。"""
    return SourceDraft(
        batch_id=batch.batch_id,
        source_kind=batch.source_kind,
        source_name=batch.source_name,
        source_ref=batch.source_ref,
        archive_path=batch.archive_path or "",
        received_at=batch.received_at,
        import_date=batch.import_date,
    )


class ImportService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        archive_dir: Path,
        *,
        cookie_store: CookieStore | None = None,
        wencai_session: WencaiProtocol | None = None,
        published_hook=None,
    ) -> None:
        self._db = db
        self._clock = clock
        self._archive_dir = archive_dir
        self._cookie_store = cookie_store
        self._wencai = wencai_session
        # 发布成功后回调（security_id 列表）：容器用它触发新股票的行情后台补取，
        # 导入本身不等待行情，缺行情也能立即归类。
        self._published_hook = published_hook

    # --- 来源提交 ---

    def _receipt(self) -> Receipt:
        """固定一次接收时刻，并由同一瞬间取北京时间日期，避免两次读时钟跨午夜。"""
        now = self._clock.now()
        return Receipt(now, ImportDate(now.date()))

    def submit_sources(
        self,
        files: list[tuple[str, bytes]],
        texts: list[str],
    ) -> list[ImportBatch]:
        """一次混合提交：文件与文本块各自成批次，共用同一个接收时刻。

        保证同一次提交的全部来源归入同一导入日期，即使处理跨越午夜。
        """
        receipt = self._receipt()
        batches = [self.submit_file(name, data, receipt=receipt) for name, data in files]
        for text in texts:
            if (text or "").strip():
                batches.append(self.submit_text(text, receipt=receipt))
        return batches

    def submit_file(
        self, filename: str, data: bytes, *, receipt: Receipt | None = None
    ) -> ImportBatch:
        suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
        name = _safe_name(filename, fallback=f"source{suffix or '.txt'}")
        kind = _source_kind_for_suffix(suffix)
        draft = self._draft(uuid.uuid4().hex, kind, name, None, data, receipt=receipt)
        return self._ingest_file(draft, suffix=suffix, data=data)

    def submit_text(self, text: str, *, receipt: Receipt | None = None) -> ImportBatch:
        """提交文本块；单独的问财链接按链接获取处理，不当作股票代码正文。"""
        stripped = (text or "").strip()
        if _looks_like_link(stripped):
            return self.submit_link(stripped, receipt=receipt)
        data = stripped.encode("utf-8")
        draft = self._draft(uuid.uuid4().hex, SourceKind.TEXT, "粘贴文本", None, data, receipt=receipt)
        return self._process(draft, parser=parse_text, data=data)

    def submit_link(self, url: str, *, receipt: Receipt | None = None) -> ImportBatch:
        stripped = (url or "").strip()
        data = stripped.encode("utf-8")
        draft = self._draft(uuid.uuid4().hex, SourceKind.LINK, "问财链接", stripped, data, receipt=receipt)
        return self._ingest_link(draft)

    def retry(self, batch_id: str) -> ImportBatch:
        """重试失败的批次：保留原批次身份、来源与导入日期，不新建候选项日期。"""
        batch = _read_batch(self._db, batch_id)
        if batch.status is not BatchStatus.REJECTED:
            raise ValueError("只有未发布的失败批次可以重试")
        draft = _draft_from_batch(batch)
        if batch.source_kind is SourceKind.LINK:
            return self._ingest_link(draft)
        archive = Path(batch.archive_path) if batch.archive_path else None
        if archive is None or not archive.exists():
            raise ValueError("原件存档缺失，无法重试")
        suffix = ("." + batch.source_name.rsplit(".", 1)[-1].lower()) if "." in batch.source_name else ""
        return self._ingest_file(draft, suffix=suffix, data=archive.read_bytes())

    # --- 内部：来源接收 ---

    def _ingest_file(self, draft: SourceDraft, *, suffix: str, data: bytes) -> ImportBatch:
        if len(data) > MAX_BYTES:
            return self._persist_rejected(
                draft,
                error=ImportRejected(f"{draft.source_name} 超过 20MB 上限", detail="file_too_large"),
            )
        try:
            parser = _parser_for_suffix(suffix)
        except ImportRejected as exc:
            # 一个来源格式不支持只影响该批次，不阻止同一提交中的其他来源
            return self._persist_rejected(draft, error=exc)
        return self._process(draft, parser=parser, data=data)

    def _ingest_link(self, draft: SourceDraft) -> ImportBatch:
        url = draft.source_ref or ""
        try:
            link = decode_link(url)
        except ImportRejected as exc:
            return self._persist_rejected(draft, error=exc)

        if self._wencai is None:
            return self._persist_rejected(
                draft,
                error=WencaiCookieMissing("问财获取未配置，无法导入链接"),
            )
        cookie = self._cookie_store.load() if self._cookie_store else None
        if not cookie:
            return self._persist_rejected(
                draft,
                error=WencaiCookieMissing(
                    "未配置问财 Cookie，无法获取链接结果；请在设置中保存 Cookie 后重试"
                ),
            )
        try:
            result = acquire(url, self._wencai)
        except ImportRejected as exc:
            return self._persist_rejected(draft, error=exc)
        except Exception as exc:  # noqa: BLE001 - 获取意外异常不能拖垮同次提交的其他来源
            return self._persist_rejected(
                draft,
                error=WencaiUnexpected(f"问财获取失败：{type(exc).__name__}"),
            )

        metadata = LinkMetadata(
            query_text=result.query,
            condition_labels=result.condition_labels,
            identity_fingerprint=link.query_fingerprint,
            completeness=result.completeness,
            condition_fingerprint=result.condition_fingerprint,
        )
        if result.completeness == COMPLETENESS_ZERO:
            return self._persist_empty(draft, metadata=metadata, parsed_count=0)

        return self._publish_candidates(
            draft,
            candidates=result.candidates,
            declared_total=result.declared_total,
            metadata=metadata,
            require_confirmation=True,
        )

    # --- 歧义选择与条件确认 ---

    def resolve_selection(
        self,
        batch_id: str,
        *,
        sheet: str | None = None,
        code_column: int | None = None,
    ) -> ImportBatch:
        """用户确认工作表/代码列后继续解析；仍歧义则再次返回待选择。"""
        batch = _read_batch(self._db, batch_id)
        if batch.status is not BatchStatus.AWAITING_SELECTION:
            raise ValueError("该批次不在待选择状态")
        if batch.source_kind is SourceKind.LINK:
            raise ValueError("链接批次不需要列选择")
        archive = Path(batch.archive_path) if batch.archive_path else None
        if archive is None or not archive.exists():
            raise ValueError("原件存档缺失，无法继续解析")
        data = archive.read_bytes()
        options = ParseOptions(sheet=sheet, code_column=code_column)
        parser = parse_text if batch.source_kind is SourceKind.TEXT else (
            parse_xlsx if batch.source_kind is SourceKind.XLSX else parse_csv
        )
        draft = _draft_from_batch(batch)
        return self._process(draft, parser=parser, data=data, options=options)

    def confirm_link(self, batch_id: str) -> ImportBatch:
        """用户确认问财解析条件后发布；同时记录查询身份供后续自动发布。"""
        with self._db.read() as conn:
            batch = imports_repo.get_batch(conn, batch_id)
            candidates = imports_repo.load_candidates(conn, batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if batch.status is not BatchStatus.AWAITING_CONFIRMATION:
            raise ValueError("该批次不在待确认状态")
        if not batch.identity_fingerprint:
            raise ValueError("批次缺少查询身份，无法确认")
        with self._db.transaction() as conn:
            imports_repo.confirm_query(
                conn,
                query_fingerprint=batch.identity_fingerprint,
                query_text=batch.query_text or "",
                condition_fingerprint=batch.condition_fingerprint,
                confirmed_at=self._clock.now().isoformat(),
            )
        metadata = LinkMetadata(
            query_text=batch.query_text or "",
            condition_labels=batch.condition_labels,
            identity_fingerprint=batch.identity_fingerprint,
            completeness=batch.completeness,
            condition_fingerprint=batch.condition_fingerprint,
        )
        return self._publish_candidates(
            batch,
            candidates=candidates,
            declared_total=batch.declared_total,
            metadata=metadata,
        )

    # --- 读取 ---

    def list_batches(self, import_date: str | None = None) -> list[ImportBatch]:
        with self._db.read() as conn:
            return imports_repo.list_batches(conn, import_date)

    def get_batch(self, batch_id: str) -> ImportBatch | None:
        with self._db.read() as conn:
            return imports_repo.get_batch(conn, batch_id)

    def batch_security_names(self, batch: ImportBatch) -> dict[str, str]:
        """批次明细里已识别股票的名称，来自权威证券库（不是来源附带名称）。"""
        ids = [stock.security_id for stock in batch.stocks if stock.security_id]
        with self._db.read() as conn:
            return securities_repo.names_for(conn, ids)

    def reidentify(self, batch_id: str) -> ImportBatch:
        """重新识别原批次跳过项；成功项按原导入日期补入，仍未知的明细删除。"""
        with self._db.read() as conn:
            batch = imports_repo.get_batch(conn, batch_id)
        if batch is None:
            raise KeyError(batch_id)
        if batch.status is not BatchStatus.ALL_UNKNOWN and batch.skipped_count == 0:
            raise ValueError("该批次没有被跳过的股票，无需重新识别")

        with self._db.transaction() as conn:
            kept: list[BatchStock] = []
            newly_recognized: list[Security] = []
            removed = 0
            for stock in batch.stocks:
                if stock.outcome is not StockOutcome.SKIPPED:
                    kept.append(stock)
                    continue
                normalized = normalize_code(stock.raw_code)
                lookup = securities_repo.lookup_many(conn, [normalized.code]) if normalized.valid else {}
                resolved = resolve_normalized([normalized], lookup)[0]
                _, security = resolved
                if security is None:
                    removed += 1  # 再次未识别：明细直接删除，不再列入重试
                    continue
                newly_recognized.append(security)
                kept.append(
                    BatchStock(
                        position=stock.position,
                        raw_code=stock.raw_code,
                        normalized_code=stock.normalized_code,
                        outcome=StockOutcome.IMPORTED,
                        security_id=security.security_id,
                        reason=None,
                        raw_extras=stock.raw_extras,
                    )
                )

            published = publish_selections(
                conn,
                securities=newly_recognized,
                batch_id=batch.batch_id,
                import_date=batch.import_date,
                published_at=self._clock.now(),
            )
            counts = published.counts
            # 重新识别也可能建出候选项或让已处理项重新待归类：同事务推进列表版本。
            classification_repo.bump_list_revision(conn)
            # 补入的明细标注本次影响：原有明细保留自己首次发布时的标注
            kept = [
                stock
                if published.effects.get(stock.security_id or "") is None
                else replace(stock, effect=published.effects[stock.security_id or ""])
                for stock in kept
            ]

            recognized_now = sum(1 for s in kept if s.outcome is StockOutcome.IMPORTED)
            if recognized_now > 0:
                status = BatchStatus.PUBLISHED
                error_code = error_message = None
            else:
                # 重新识别仍全部未识别：仍属"全部未识别"，不是来源真实零结果
                status = BatchStatus.ALL_UNKNOWN
                error_code = "all_securities_unknown"
                error_message = "未导入任何股票：全部未识别"

            # 保留原批次的数量汇总：重新识别不改变来源总数与解析条数，也不抹掉
            # 首次发布已建的候选项；明细按当前状态重算，新增/合并/重新归类在原基础上累加。
            updated = ImportBatch(
                batch_id=batch.batch_id,
                source_kind=batch.source_kind,
                source_name=batch.source_name,
                source_ref=batch.source_ref,
                archive_path=batch.archive_path,
                received_at=batch.received_at,
                import_date=batch.import_date,
                status=status,
                declared_total=batch.declared_total,
                parsed_count=batch.parsed_count,
                # 去重股票数是来源属性，重新识别只改识别/跳过，不改来源曾有多少只
                unique_count=batch.unique_count,
                recognized_count=recognized_now,
                skipped_count=0,
                new_candidate_count=batch.new_candidate_count + counts.new,
                merged_candidate_count=batch.merged_candidate_count + counts.merged,
                reopened_candidate_count=batch.reopened_candidate_count + counts.reopened,
                error_code=error_code,
                error_message=error_message,
                stocks=tuple(kept),
                query_text=batch.query_text,
                condition_labels=batch.condition_labels,
                condition_fingerprint=batch.condition_fingerprint,
                identity_fingerprint=batch.identity_fingerprint,
                completeness=batch.completeness,
                reidentified_at=self._clock.now(),
                reidentified_imported=len(newly_recognized),
                reidentified_removed=removed,
            )
            imports_repo.insert_batch(conn, updated, batch.received_at.isoformat())
        # 事务提交后再回调：重新识别补入的股票同样立即后台补行情，失败不影响已发布的候选项
        if newly_recognized and self._published_hook:
            self._published_hook([s.security_id for s in newly_recognized])
        return updated

    # --- 内部实现 ---

    def _draft(
        self,
        batch_id: str,
        source_kind: SourceKind,
        source_name: str,
        source_ref: str | None,
        data: bytes,
        *,
        receipt: Receipt | None = None,
    ) -> SourceDraft:
        active = receipt or self._receipt()
        return SourceDraft(
            batch_id=batch_id,
            source_kind=source_kind,
            source_name=source_name,
            source_ref=source_ref,
            archive_path=self._archive(batch_id, source_name, data),
            received_at=active.received_at,
            import_date=active.import_date,
        )

    def _archive(self, batch_id: str, source_name: str, data: bytes) -> str:
        self._archive_dir.mkdir(parents=True, exist_ok=True)
        target = self._archive_dir / f"{batch_id}-{source_name}"
        target.write_bytes(data)
        return str(target)

    def _process(
        self,
        draft: SourceDraft,
        *,
        parser,
        data: bytes,
        options: ParseOptions | None = None,
    ) -> ImportBatch:
        try:
            parsed = parser(data, source_name=draft.source_name, options=options)
        except AmbiguousSelection as exc:
            return self._persist_selection(draft, exc)
        except ImportRejected as exc:
            return self._persist_rejected(draft, error=exc)
        except Exception as exc:  # noqa: BLE001 - 解析器意外异常不能拖垮同次提交的其他来源
            # 未预期的解析错误记为该批次未发布，不当作零结果，也不影响其他来源
            return self._persist_rejected(
                draft,
                error=ParseFailed(
                    f"{draft.source_name} 读取失败：{type(exc).__name__}",
                    detail="parse_error",
                ),
            )
        return self._publish(draft, parsed)

    def _persist_selection(self, draft: SourceDraft, exc: AmbiguousSelection) -> ImportBatch:
        batch = self._terminal_batch(
            draft,
            status=BatchStatus.AWAITING_SELECTION,
            declared_total=None,
            parsed_count=0,
            error_code=exc.code,
            error_message=exc.message,
            selection=exc.request,
        )
        self._write_batch(batch)
        return batch

    def _persist_rejected(self, draft: SourceDraft, *, error: ImportRejected) -> ImportBatch:
        batch = self._terminal_batch(
            draft,
            status=BatchStatus.REJECTED,
            declared_total=None,
            parsed_count=0,
            error_code=error.code,
            error_message=error.message,
        )
        self._write_batch(batch)
        return batch

    def _persist_empty(
        self,
        draft: SourceDraft,
        *,
        metadata: LinkMetadata | None = None,
        parsed_count: int = 0,
    ) -> ImportBatch:
        batch = self._terminal_batch(
            draft,
            status=BatchStatus.EMPTY,
            declared_total=0,
            parsed_count=parsed_count,
            error_code="empty_source",
            error_message="来源没有任何记录（真实零结果）",
            metadata=metadata,
        )
        self._write_batch(batch)
        return batch

    def _publish(self, draft: SourceDraft, parsed: ParseResult) -> ImportBatch:
        if parsed.declared_total == 0:
            return self._persist_empty(draft)
        if not parsed.candidates:
            return self._persist_rejected(
                draft, error=ImportRejected("未从来源中解析出任何股票代码")
            )
        return self._publish_candidates(
            draft,
            candidates=parsed.candidates,
            declared_total=parsed.declared_total,
        )

    def _publish_candidates(
        self,
        draft: SourceDraft,
        *,
        candidates: tuple[ParsedCandidate, ...],
        declared_total: int | None,
        metadata: LinkMetadata | None = None,
        require_confirmation: bool = False,
    ) -> ImportBatch:
        if require_confirmation and metadata is not None and self._needs_confirmation(metadata):
            return self._persist_awaiting_confirmation(
                draft, candidates=candidates, declared_total=declared_total, metadata=metadata
            )

        with self._db.transaction() as conn:
            stocks, recognized = self._classify(conn, candidates)
            skipped_count = sum(1 for s in stocks if s.outcome is StockOutcome.SKIPPED)
            all_unknown = not recognized and bool(stocks)
            batch = ImportBatch(
                batch_id=draft.batch_id,
                source_kind=draft.source_kind,
                source_name=draft.source_name,
                source_ref=draft.source_ref,
                archive_path=draft.archive_path,
                received_at=draft.received_at,
                import_date=draft.import_date,
                status=BatchStatus.ALL_UNKNOWN if all_unknown else BatchStatus.PUBLISHED,
                declared_total=declared_total,
                parsed_count=len(candidates),
                unique_count=len(recognized) + skipped_count,
                recognized_count=len(recognized),
                skipped_count=skipped_count,
                new_candidate_count=0,
                merged_candidate_count=0,
                reopened_candidate_count=0,
                error_code="all_securities_unknown" if all_unknown else None,
                error_message="未导入任何股票：全部未识别" if all_unknown else None,
                stocks=tuple(stocks),
                query_text=metadata.query_text if metadata else None,
                condition_labels=metadata.condition_labels if metadata else (),
                condition_fingerprint=metadata.condition_fingerprint if metadata else None,
                identity_fingerprint=metadata.identity_fingerprint if metadata else None,
                completeness=metadata.completeness if metadata else None,
            )
            # 批次行先落库，每日入选与来源追溯才能引用它；数量随后回填
            imports_repo.insert_batch(conn, batch, draft.received_at.isoformat())
            published = publish_selections(
                conn,
                securities=recognized,
                batch_id=draft.batch_id,
                import_date=draft.import_date,
                published_at=self._clock.now(),
            )
            counts = published.counts
            # 这次发布可能新增、合并或重开候选项：在同一事务里推进列表版本，
            # 使返回工作区时依据版本重读，而不是让前端猜列表是否过期。
            classification_repo.bump_list_revision(conn)
            # 数量回填 + 明细逐行标注本次影响：结果表据此显示
            # 「新增 / 合并 / 重新归类 / 仅追加来源」
            batch.new_candidate_count = counts.new
            batch.merged_candidate_count = counts.merged
            batch.reopened_candidate_count = counts.reopened
            batch.stocks = tuple(
                replace(
                    stock,
                    effect=published.effects.get(stock.security_id or "", None),
                )
                for stock in batch.stocks
            )
            imports_repo.update_candidate_counts(
                conn,
                draft.batch_id,
                new=counts.new,
                merged=counts.merged,
                reopened=counts.reopened,
            )
            imports_repo.replace_batch_stocks(conn, batch)
        # 事务提交后再回调：行情补取失败不影响已发布的候选项
        if batch.status is BatchStatus.PUBLISHED and recognized and self._published_hook:
            self._published_hook([s.security_id for s in recognized])
        return batch

    def _needs_confirmation(self, metadata: LinkMetadata) -> bool:
        """未确认查询、口径缺失或口径变化时需要确认；普通日期滚动不重复确认。

        条件口径取不到时不认为"已确认"：无法核对口径就不能自动发布。
        """
        if metadata.condition_fingerprint is None:
            return True
        with self._db.read() as conn:
            confirmed = imports_repo.confirmed_condition(conn, metadata.identity_fingerprint)
        return confirmed != metadata.condition_fingerprint

    def _persist_awaiting_confirmation(
        self,
        draft: SourceDraft,
        *,
        candidates: tuple[ParsedCandidate, ...],
        declared_total: int | None,
        metadata: LinkMetadata,
    ) -> ImportBatch:
        batch = self._terminal_batch(
            draft,
            status=BatchStatus.AWAITING_CONFIRMATION,
            declared_total=declared_total,
            parsed_count=len(candidates),
            error_code="awaiting_confirmation",
            error_message="新查询或条件口径变化，请确认实际解析条件后发布",
            metadata=metadata,
        )
        with self._db.transaction() as conn:
            imports_repo.insert_batch(conn, batch, draft.received_at.isoformat())
            imports_repo.store_candidates(conn, draft.batch_id, candidates)
        return batch

    @staticmethod
    def _classify(
        conn, candidates: tuple[ParsedCandidate, ...]
    ) -> tuple[list[BatchStock], list[Security]]:
        """按权威证券库识别候选，返回批次明细与去重后的已识别证券。"""
        normalized = [normalize_code(c.raw_code) for c in candidates]
        valid_codes = [n.code for n in normalized if n.valid]
        lookup = securities_repo.lookup_many(conn, valid_codes)
        resolved = resolve_normalized(normalized, lookup)

        stocks: list[BatchStock] = []
        seen: set[str] = set()
        recognized: list[Security] = []
        for cand, (code, security) in zip(candidates, resolved):
            identity = security.security_id if security is not None else f"unknown:{code}"
            if identity in seen:
                stocks.append(
                    BatchStock(
                        position=cand.position,
                        raw_code=cand.raw_code,
                        normalized_code=code,
                        outcome=StockOutcome.DUPLICATE,
                        security_id=security.security_id if security else None,
                        reason="批次内重复",
                        raw_extras=cand.raw_extras,
                    )
                )
                continue
            seen.add(identity)
            if security is None:
                stocks.append(
                    BatchStock(
                        position=cand.position,
                        raw_code=cand.raw_code,
                        normalized_code=code,
                        outcome=StockOutcome.SKIPPED,
                        security_id=None,
                        reason="未在权威证券库中识别",
                        raw_extras=cand.raw_extras,
                    )
                )
                continue
            recognized.append(security)
            stocks.append(
                BatchStock(
                    position=cand.position,
                    raw_code=cand.raw_code,
                    normalized_code=code,
                    outcome=StockOutcome.IMPORTED,
                    security_id=security.security_id,
                    reason=None,
                    raw_extras=cand.raw_extras,
                )
            )
        return stocks, recognized

    def _terminal_batch(
        self,
        draft: SourceDraft,
        *,
        status: BatchStatus,
        declared_total: int | None,
        parsed_count: int,
        error_code: str | None,
        error_message: str | None,
        selection=None,
        metadata: LinkMetadata | None = None,
    ) -> ImportBatch:
        """构造不产生候选项的批次（拒绝/空来源/待选择/待确认）。"""
        return ImportBatch(
            batch_id=draft.batch_id,
            source_kind=draft.source_kind,
            source_name=draft.source_name,
            source_ref=draft.source_ref,
            archive_path=draft.archive_path,
            received_at=draft.received_at,
            import_date=draft.import_date,
            status=status,
            declared_total=declared_total,
            parsed_count=parsed_count,
            unique_count=0,
            recognized_count=0,
            skipped_count=0,
            new_candidate_count=0,
            merged_candidate_count=0,
            reopened_candidate_count=0,
            error_code=error_code,
            error_message=error_message,
            stocks=(),
            query_text=metadata.query_text if metadata else None,
            condition_labels=metadata.condition_labels if metadata else (),
            condition_fingerprint=metadata.condition_fingerprint if metadata else None,
            identity_fingerprint=metadata.identity_fingerprint if metadata else None,
            completeness=metadata.completeness if metadata else None,
            selection=selection,
        )

    def _write_batch(self, batch: ImportBatch) -> None:
        with self._db.transaction() as conn:
            imports_repo.insert_batch(conn, batch, batch.received_at.isoformat())
