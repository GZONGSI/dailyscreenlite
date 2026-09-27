"""数据更新：证券库、每日状态快照与行情，手动/定时/启动/导入后复用同一流程。

规则要点（对应规格「数据中心与可靠性」）：
- 证券库取交易所名单，行情取日线，每日状态快照取停复牌与交易日历；三者来源身份分别记录；
- 持续更新范围是待归类股票与现存观察组成员的去重并集，不下载全市场历史；默认请求最近三年或上市日起，旧历史保留；
- 目标是最新已收盘交易日的完整日线：抓取截止取目标交易日（不取今天，避免写入盘中未收盘的日线），
  请求成功但日期落后仍算未补齐，整次更新的终态按实际完整性判定，不用请求成功代替完整性；
- 同源且重叠价格一致时保留旧段；来源或复权基准改变则旧曲线留档，避免混拼；
- 失败保留最后可用数据，只对未完成部分有限重试，不做无限重试；
- 同一时间只允许一个更新在跑，重复触发直接返回「进行中」，不并发写同一数据。
"""

from __future__ import annotations

import logging
import threading
import sqlite3
import time
import uuid
from dataclasses import dataclass, replace
from collections.abc import Callable
from datetime import date, timedelta

from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.models import (
    SECURITIES_FAILED,
    SECURITIES_OK,
    SECURITIES_SKIPPED,
    DailyBar,
    MarketStatusSnapshot,
    Security,
    UpdateItemResult,
    UpdateItemStatus,
    UpdateKind,
    UpdateRun,
    UpdateStatus,
)
from dailyscreen_lite.quotes.calendar import resolve_target
from dailyscreen_lite.quotes.calendar_source import CalendarSource
from dailyscreen_lite.quotes.daily import fetch_daily, source_name, validate_bars
from dailyscreen_lite.quotes.market_status import (
    MarketStatusError,
    MarketStatusSource,
    NoMarketStatusSource,
)
from dailyscreen_lite.repository import (
    Database,
    market_status_repo,
    quotes_repo,
    securities_repo,
)
from dailyscreen_lite.quotes.source import (
    DailyQuotesSource,
    MarketScopedSource,
    ProviderBar,
    ProviderSecurity,
    QuoteSourceError,
    QuotesSource,
    covered_markets,
    declared_adjust_of,
    market_scoped,
)
from dailyscreen_lite.repository.securities_repo import SnapshotMeta
from dailyscreen_lite.securities.resolver import normalize_code

ADJUST = "qfq"

# 未完成部分的自动重试次数；仍失败保留手动重试入口，不无限重试
MAX_RETRIES = 2

# 同一日线来源连续断连后，本轮停止向它发请求；下一轮仍重新尝试。
_SOURCE_FAILURE_LIMIT = 3
_CONNECTION_FAILURES = (
    "10002007",  # BaoStock 网络接收错误
    "网络接收错误",
    "请求超过 45 秒",
    "ConnectionError",
    "ProxyError",
    "RemoteDisconnected",
    "ConnectTimeout",
    "ReadTimeout",
)


def _connection_failed(message: str | None) -> bool:
    return bool(message and any(marker in message for marker in _CONNECTION_FAILURES))

# 定向补取、单股更新与重试都不代表整体更新完成，不计入调度判断
TARGETED_KINDS = (UpdateKind.IMPORT, UpdateKind.RETRY, UpdateKind.SINGLE)

_TARGETED_MESSAGE = {
    UpdateKind.IMPORT: "仅补新导入股票的行情",
    UpdateKind.RETRY: "仅重试未完成部分",
    UpdateKind.SINGLE: "单股手动更新（不加入持续更新范围）",
}


logger = logging.getLogger(__name__)


class UpdateBusy(RuntimeError):
    """已有更新在进行中，本次不重复执行。"""



@dataclass(frozen=True)
class RecoveryPlan:
    markets: tuple[str, ...]
    months: tuple[date, ...]
    status: bool
    stocks: tuple[str, ...]

    @property
    def needed(self) -> bool:
        return bool(self.markets or self.months or self.status or self.stocks)


def _bar_from_provider(security_id: str, bar: ProviderBar) -> DailyBar:
    return DailyBar(
        security_id=security_id,
        trade_date=bar.trade_date,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume_lots=bar.volume_lots,
        amount_yuan=bar.amount_yuan,
    )


def _tally(results: list[UpdateItemResult]) -> tuple[int, list[str], int]:
    """按最终逐股结果统计成功/失败/真实无数据，重试后不会重复计数。"""
    ok = sum(1 for item in results if item.status is UpdateItemStatus.OK)
    failed = [item.security_id for item in results if item.status is UpdateItemStatus.FAILED]
    skipped = sum(1 for item in results if item.status is UpdateItemStatus.NO_DATA)
    return ok, failed, skipped


class UpdateService:
    def __init__(
        self,
        db: Database,
        clock: Clock,
        source: QuotesSource,
        *,
        daily_sources: list[DailyQuotesSource] | None = None,
        status_source: MarketStatusSource | None = None,
        calendar_source: CalendarSource | None = None,
        pending_ids: Callable[[], list[str]] | None = None,
        incomplete_ids: Callable[[list[str]], list[str]] | None = None,
        history_start: date | None = None,
        adjust: str = ADJUST,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._db = db
        self._clock = clock
        self._source = source
        self._daily_sources = daily_sources if daily_sources is not None else [source]
        self._status_source = status_source or NoMarketStatusSource()
        self._calendar_source = calendar_source
        self._pending_ids = pending_ids
        # 终态按完整性接口的同一处判定给出，不出现「任务成功但行情未补齐」
        self._incomplete_ids = incomplete_ids
        self._history_start = history_start
        self._adjust = adjust
        self._max_retries = max_retries
        # 进程内互斥：同一时间只跑一个更新，重复触发返回「进行中」
        self._lock = threading.Lock()
        self._running = False
        self._backfill_kind: UpdateKind | None = None
        # 定向补取的待办队列：整体更新在跑时新导入的股票不能被丢弃，
        # 先排队，待当前更新结束后由同一把锁串行补取。
        self._pending_lock = threading.Lock()
        self._pending: list[tuple[UpdateKind, list[str]]] = []
        self._active_targets: set[str] = set()
        # 进行中更新的进度；仅用于界面展示，不参与判定
        self._progress_lock = threading.Lock()
        self._progress: dict | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def import_updating(self) -> bool:
        """当前正在执行导入后的补取；仅供顶部进度文案使用。"""
        return self._running and self._backfill_kind is UpdateKind.IMPORT

    def progress(self) -> dict | None:
        with self._progress_lock:
            return dict(self._progress) if self._progress else None

    def _begin_progress(self, total: int) -> None:
        with self._progress_lock:
            self._progress = {"total": total, "done": 0, "attempt": 1,
                              "currentSecurityId": None, "currentStartedAt": None}

    def _current_stock(self, security_id: str) -> None:
        with self._progress_lock:
            if self._progress is not None:
                self._progress["currentSecurityId"] = security_id
                self._progress["currentStartedAt"] = self._clock.now().isoformat()

    def _tick_progress(self, done: int) -> None:
        with self._progress_lock:
            if self._progress is not None:
                self._progress["done"] = done
                self._progress["currentSecurityId"] = None
                self._progress["currentStartedAt"] = None

    def _end_progress(self) -> None:
        with self._progress_lock:
            self._progress = None

    def _active_adjust(self) -> str:
        """来源自带口径优先（如历史回放样本为不复权），否则用配置口径。

        如实标注可避免把不复权样本写进前复权序列、在页面上标成「前复权」。
        """
        value = declared_adjust_of(self._source)
        return str(value) if value else self._adjust

    def status(self) -> dict:
        """当前更新状态与最近一次记录，供界面展示与重启后读回。"""
        with self._db.read() as conn:
            latest = quotes_repo.latest_run(conn)
        return {
            "running": self._running,
            "lastRun": run_json(latest),
            "progress": self.progress(),
            "automaticRounds": self.automatic_rounds(),
        }

    def automatic_rounds(self) -> list[dict]:
        with self._db.read() as conn:
            return quotes_repo.automatic_rounds(conn, self._clock.now().date().isoformat())

    def last_completed(self, exclude_kinds: tuple[UpdateKind, ...] = ()) -> UpdateRun | None:
        """最近一次已结束的整体更新；调度据此判断是否需要补更。"""
        with self._db.read() as conn:
            return quotes_repo.last_completed_run(conn, exclude_kinds)

    def history(self, limit: int = 10) -> list[dict]:
        with self._db.read() as conn:
            return [run_json(r) for r in quotes_repo.recent_runs(conn, limit)]

    def run_items(self, run_id: str) -> list[UpdateItemResult]:
        """某次更新的逐股结果，供数据中心逐股展示失败原因与日期。"""
        with self._db.read() as conn:
            return quotes_repo.item_results(conn, run_id)

    def refresh_quotes(self, security_ids: list[str], kind: UpdateKind = UpdateKind.IMPORT) -> int:
        """只补指定股票的行情（新导入后立即后台补取），仍走同一行情边界。

        与整体更新用同一把锁：锁被占用时不丢弃本次请求，而是排队等待当前更新结束后
        串行补取；本次补取不写入证券库，记录 kind 也不计入「整体更新已完成」。
        """
        if not security_ids:
            return 0
        with self._pending_lock:
            if not self._lock.acquire(blocking=False):
                existing = self._active_targets | {sid for _, ids in self._pending for sid in ids}
                missing = [sid for sid in dict.fromkeys(security_ids) if sid not in existing]
                if missing:
                    self._pending.append((kind, missing))
                return 0
        self._running = True
        try:
            ok = self._run_backfill(security_ids, kind)
            ok += self._drain_pending()
            return ok
        finally:
            self._finish_pending()

    def _finish_pending(self) -> None:
        """队列检查与释放更新锁不可分开，否则最后一刻入队的请求会遗留。"""
        self._end_progress()
        with self._pending_lock:
            self._active_targets.clear()
        try:
            while True:
                with self._pending_lock:
                    if not self._pending:
                        self._end_progress()
                        self._backfill_kind = None
                        self._running = False
                        self._lock.release()
                        return
                self._drain_pending()
        except BaseException:
            self._backfill_kind = None
            self._running = False
            self._lock.release()
            raise

    def refresh_security(self, security_id: str) -> int:
        """历史详情的单股手动更新：只更新这一只，不加入持续更新范围。"""
        return self.refresh_quotes([security_id], UpdateKind.SINGLE)

    def _drain_pending(self) -> int:
        """在持有更新锁的前提下清空待办队列，返回补取成功数。"""
        ok = 0
        while True:
            with self._pending_lock:
                if not self._pending:
                    return ok
                kind, ids = self._pending.pop(0)
                self._active_targets = set(ids)
            ok += self._run_backfill(ids, kind)

    def _run_backfill(self, security_ids: list[str], kind: UpdateKind) -> int:
        """在持有更新锁的前提下补取一批股票的行情并写一条更新记录。"""
        self._backfill_kind = kind
        run_id = f"update-{uuid.uuid4().hex[:12]}"
        started = self._clock.now()
        started_perf = time.perf_counter()
        try:
            results = self._update_quotes(security_ids, force=kind is UpdateKind.SINGLE)
            ok, failed, skipped = _tally(results)
            pending = self._incomplete_count(results)
            status = _final_status(SECURITIES_SKIPPED, ok, failed, pending)
        except Exception:  # noqa: BLE001 - 异常也要落终态并继续处理队列
            results, ok, failed, skipped, pending = [], 0, list(security_ids), 0, 0
            status = UpdateStatus.FAILED
        finished = self._clock.now()
        with self._db.transaction() as conn:
            quotes_repo.insert_run(
                conn,
                UpdateRun(
                    run_id=run_id,
                    kind=kind,
                    status=status,
                    started_at=started,
                    finished_at=finished,
                    securities_status=SECURITIES_SKIPPED,
                    securities_message=_TARGETED_MESSAGE.get(kind, "仅更新指定股票的行情"),
                    securities_count=0,
                    quotes_ok=ok,
                    quotes_failed=len(failed),
                    quotes_skipped=skipped,
                    quotes_pending=pending,
                    failed_securities=tuple(failed),
                    elapsed_ms=round((time.perf_counter() - started_perf) * 1000),
                ),
            )
            quotes_repo.insert_item_results(conn, run_id, results)
        return ok

    def _incomplete_count(self, results: list[UpdateItemResult]) -> int:
        """本次请求的股票中仍未补齐的数量（待补齐与状态待确认）。

        结论取自与完整性接口同一处的逐股判定，且判的是**本次真正处理过的股票**：
        历史股票不在持续更新范围内，但单股更新它时仍须如实判定，
        否则会把「来源只给到旧日期」说成更新成功。
        """
        if self._incomplete_ids is None:
            return 0
        return len(self._incomplete_ids([item.security_id for item in results]))

    def _missing_stocks(self) -> list[str]:
        ids = self._resolve_targets(None)
        return self._incomplete_ids(ids) if self._incomplete_ids else ids

    def recovery_plan(self) -> RecoveryPlan:
        """按持久化的成功事实恢复，完整性仍由既有逐股规则判断。"""
        target, _, trusted = self._resolved_target()
        now = self._clock.now()
        previous = now.date().replace(day=1) - timedelta(days=1)
        with self._db.read() as conn:
            markets = {r['exchange']: r for r in securities_repo.market_updates(conn)}
            months = {r['month'] for r in market_status_repo.calendar_months(conn)}
            snapshot = market_status_repo.latest_snapshot(conn)
            latest = quotes_repo.last_completed_run(conn, TARGETED_KINDS)
        missing_markets = tuple(m for m in ('SH', 'SZ', 'BJ') if m not in markets
            or markets[m]['status'] != 'ok'
            or markets[m]['succeeded_at'][:10] < target.isoformat())
        # 旧整体夹具/历史回放可声明 skipped；其成功事实不影响生产分市场结果。
        if not markets and latest and latest.started_at.date() >= target and latest.securities_status in (SECURITIES_OK, SECURITIES_SKIPPED):
            missing_markets = ()
        missing_months = tuple(d for d in (previous, now.date()) if d.strftime('%Y-%m') not in months) if self._calendar_source else ()
        missing_status = not isinstance(self._status_source, NoMarketStatusSource) and (
            not snapshot or snapshot.target_trade_date != target or (not trusted and not self._calendar_source))
        return RecoveryPlan(missing_markets, missing_months, missing_status, tuple(self._missing_stocks()))

    def run(self, kind: UpdateKind, *, automatic_slot: int | None = None) -> UpdateRun | None:
        """执行一次整体更新，返回终态记录。已在运行时抛 UpdateBusy。

        任何未预期异常都必须把已写入的 RUNNING 记录收为 FAILED 终态，
        否则界面与调度会一直把该次更新当作「进行中」而不再触发。
        """
        if not self._lock.acquire(blocking=False):
            raise UpdateBusy("已有更新在进行中")
        self._running = True
        run_id = f"update-{uuid.uuid4().hex[:12]}"
        started = self._clock.now()
        started_perf = time.perf_counter()
        try:
            plan = self.recovery_plan() if automatic_slot is not None else None
            if plan is not None and not plan.needed:
                return None
            with self._db.transaction() as conn:
                if automatic_slot is not None and not quotes_repo.claim_automatic_round(
                    conn, run_date=started.date().isoformat(), slot=automatic_slot,
                    run_id=run_id, started_at=started.isoformat(),
                ):
                    return None
                quotes_repo.insert_run(
                    conn,
                    UpdateRun(
                        run_id=run_id,
                        kind=kind,
                        status=UpdateStatus.RUNNING,
                        started_at=started,
                        finished_at=None,
                        securities_status=None,
                        securities_message=None,
                        securities_count=0,
                        quotes_ok=0,
                        quotes_failed=0,
                    ),
                )
            try:
                sec_status, sec_message, sec_count = self._update_securities(plan.markets if plan else None)
                calendar_ok = self._update_calendar(plan.months if plan else None)
                # 日历恢复可能改变目标日，状态与行情按恢复后的目标重新判断。
                market_ok = self._update_market_status() if plan is None or self.recovery_plan().status else True
                results = self._update_quotes(self._missing_stocks() if plan else None)
                if automatic_slot is None:
                    results = self._retry_incomplete(results)
                ok, failed, skipped = _tally(results)
                # 重试之后按实际完整性收尾：请求结束不代表行情已补齐
                pending = self._incomplete_count(results)
                status = _final_status(sec_status, ok, failed, pending)
                if status is UpdateStatus.SUCCESS and (not calendar_ok or not market_ok):
                    status = UpdateStatus.PARTIAL
            except Exception as exc:  # noqa: BLE001 - 未预期异常不得留下永久进行中
                sec_status = SECURITIES_FAILED
                sec_message = f"更新异常：{type(exc).__name__}: {exc}"
                sec_count = 0
                results, ok, failed, skipped, pending = [], 0, [], 0, 0
                status = UpdateStatus.FAILED
            finished = self._clock.now()
            record = UpdateRun(
                run_id=run_id,
                kind=kind,
                status=status,
                started_at=started,
                finished_at=finished,
                securities_status=sec_status,
                securities_message=sec_message,
                securities_count=sec_count,
                quotes_ok=ok,
                quotes_failed=len(failed),
                quotes_skipped=skipped,
                quotes_pending=pending,
                failed_securities=tuple(failed),
                elapsed_ms=round((time.perf_counter() - started_perf) * 1000),
            )
            with self._db.transaction() as conn:
                quotes_repo.finish_run(conn, record)
                quotes_repo.insert_item_results(conn, run_id, results)
            # 整体更新期间排队的新导入补取在这里串行完成，不丢请求
            self._drain_pending()
            return record
        finally:
            self._finish_pending()

    def _retry_incomplete(self, results: list[UpdateItemResult]) -> list[UpdateItemResult]:
        """只对未完成部分有限重试：获取失败与「状态正常但行情落后」的股票。

        状态待确认的股票不自动重试：既不知道当天是否应有行情，重试也不会改变结论。
        """
        merged = {item.security_id: item for item in results}
        if self._max_retries <= 0:
            return list(merged.values())
        for attempt in range(1, self._max_retries + 1):
            pending = {
                item.security_id
                for item in merged.values()
                if item.status is UpdateItemStatus.FAILED
            }
            if self._pending_ids is not None:
                try:
                    pending.update(sid for sid in self._pending_ids() if sid in merged)
                except Exception:  # noqa: BLE001 - 判定失败不影响已完成的更新
                    logger.exception("重试前统计未补齐股票失败；本轮只重试已确认失败的股票")
            if not pending:
                break
            with self._progress_lock:
                if self._progress is not None:
                    self._progress["attempt"] = attempt + 1
            for item in self._update_quotes(sorted(pending)):
                previous = merged.get(item.security_id)
                if previous is not None:
                    elapsed_parts = [ms for ms in (previous.elapsed_ms, item.elapsed_ms) if ms is not None]
                    starts = [day for day in (previous.request_start, item.request_start) if day]
                    item = replace(
                        item,
                        elapsed_ms=sum(elapsed_parts) if elapsed_parts else None,
                        request_start=min(starts) if starts else None,
                        attempts=previous.attempts + item.attempts,
                    )
                merged[item.security_id] = item
        return list(merged.values())

    # --- 证券库 ---

    def _update_securities(self, markets: tuple[str, ...] | None = None) -> tuple[str, str | None, int]:
        """整名单获取成功才替换；获取失败保留旧库，不阻止导入。"""
        if markets == ():
            return SECURITIES_SKIPPED, "证券库已完成，本轮无需重取", 0
        scoped = market_scoped(self._source)
        if scoped is not None:
            result = self._update_security_markets(scoped, markets or ('SH', 'SZ', 'BJ'))
            if result is not None:
                return result
        try:
            providers = self._source.security_list()
        except QuoteSourceError as exc:
            return SECURITIES_FAILED, str(exc), 0
        if not providers:
            # 夹具空名单表示「本次不动证券库」；不算失败也不替换
            return SECURITIES_SKIPPED, "本次未更新证券库", 0
        securities = [_to_security(p) for p in providers]
        now = self._clock.now()
        with self._db.transaction() as conn:
            securities_repo.replace_snapshot(
                conn,
                SnapshotMeta(
                    snapshot_id=f"akshare-{now.date().isoformat()}",
                    loaded_at=now.isoformat(),
                    record_count=len(securities),
                    identity_fingerprint=_fingerprint(securities),
                    effective_date=now.date().isoformat(),
                    source_json=_source_json(self._source.source_id),
                ),
                securities,
            )
        return SECURITIES_OK, f"证券库已更新为 {len(securities)} 只", len(securities)

    def _update_security_markets(
        self, source: MarketScopedSource, markets: tuple[str, ...]
    ) -> tuple[str, str | None, int] | None:
        """每个市场单独提交；无删除凭据时，名单缺项不能当作退市。

        返回 None 表示该来源不支持分市场（旧离线快照入口），由调用方回落到整体名单。
        """
        now = self._clock.now()
        messages, succeeded, count = [], 0, 0
        for exchange in markets:
            try:
                providers = source.fetch_market(exchange)
                if providers is None:
                    return None  # 旧离线快照入口
                securities = [_to_security(p) for p in providers]
                with self._db.read() as conn:
                    old_ids = securities_repo.market_ids(conn, exchange)
                ids = {s.security_id for s in securities}
                missing = old_ids - ids
                confirmed = set()
                if securities and missing:
                    confirmed = source.confirmed_delistings(exchange, now.date())
                with self._db.transaction() as conn:
                    if not securities or len(ids) != len(securities):
                        raise QuoteSourceError("名单为空或有重复证券，保留旧名单")
                    if any(s.exchange != exchange or len(s.code) != 6 or not s.code.isascii()
                           or not s.code.isdigit() or not s.name.strip() for s in securities):
                        raise QuoteSourceError("证券身份字段不完整或市场错位，保留旧名单")
                    if missing - confirmed:
                        raise QuoteSourceError(f"名单缺少旧证券 {len(missing - confirmed)} 只，无法确认完整，保留旧名单")
                    for security in securities:
                        if security.listing_date:
                            date.fromisoformat(security.listing_date)
                    securities_repo.replace_snapshot(conn, SnapshotMeta(
                        snapshot_id=f"{exchange}-{uuid.uuid4().hex[:12]}",
                        loaded_at=now.isoformat(), record_count=len(securities),
                        identity_fingerprint=_fingerprint(securities),
                        effective_date=now.date().isoformat(), source_json=_source_json(self._source.source_id),
                    ), securities, exchange=exchange)
                    message = f"已更新 {len(securities)} 只"
                    if missing:
                        message += f"，交易所已确认终止上市 {len(missing)} 只"
                    securities_repo.record_market_update(conn, exchange, status="ok", message=message,
                        source=self._source.source_id, attempted_at=now.isoformat())
                succeeded += 1
                count += len(securities)
            except Exception as exc:  # 单个供应商/市场异常不能中断其他市场
                message = f"{type(exc).__name__}: {exc}"
                with self._db.transaction() as conn:
                    securities_repo.record_market_update(conn, exchange, status="failed", message=message,
                        source=self._source.source_id, attempted_at=now.isoformat())
            messages.append(f"{exchange}：{message}")
        state = SECURITIES_OK if succeeded == len(markets) else "partial" if succeeded else SECURITIES_FAILED
        return state, "；".join(messages), count

    # --- 每日状态快照 ---

    def _update_calendar(self, months: tuple[date, ...] | None = None) -> bool:
        if self._calendar_source is None:
            return True
        now = self._clock.now()
        previous = now.date().replace(day=1) - timedelta(days=1)
        months = (previous, now.date()) if months is None else months
        if not months:
            return True
        messages, succeeded = [], 0
        for day in months:
            try:
                month = self._calendar_source.month(day.year, day.month)
                with self._db.transaction() as conn:
                    market_status_repo.replace_calendar_month(conn, month.month, source=month.source,
                        days=month.trade_dates, updated_at=now.isoformat())
                succeeded += 1
                messages.append(f"{month.month} 已更新（{month.source}）")
            except Exception as exc:
                messages.append(f"{day:%Y-%m} 获取失败：{exc}；保留可信缓存")
        with self._db.transaction() as conn:
            market_status_repo.record_calendar_update(conn,
                status="ok" if succeeded == len(months) else "partial" if succeeded else "failed",
                message="；".join(messages), attempted_at=now.isoformat())
        return succeeded == len(months)

    def _update_market_status(self) -> bool:
        """采集并保存每日状态快照与交易日历；失败只记失败快照，不影响行情。

        目标交易日由交易日历算出（日历缺失时按工作日回落并标注不可信），
        因此「已取得目标日完整日线」的股票不会因为状态接口失败被判为缺失。
        """
        now = self._clock.now()
        snapshot_id = f"status-{uuid.uuid4().hex[:12]}"
        try:
            provider = self._status_source.snapshot(for_date=now.date())
        except Exception as exc:
            target, _, _ = self._resolved_target()
            with self._db.transaction() as conn:
                market_status_repo.insert_snapshot(
                    conn,
                    MarketStatusSnapshot(
                        snapshot_id=snapshot_id,
                        target_trade_date=target,
                        collected_at=now,
                        source=self._status_source.source_id,
                        calendar_source=None,
                        covered_markets=(),
                        uncovered_markets=(),
                        suspensions=(),
                        status="failed",
                        message=str(exc),
                    ),
                )
                market_status_repo.prune_snapshots(conn)
            return False
        with self._db.transaction() as conn:
            if self._calendar_source is None:
                market_status_repo.replace_trade_dates(
                    conn, provider.trade_dates,
                    source=provider.calendar_source or provider.source, updated_at=now.isoformat(),
                )
            target, _, _ = self._resolved_target(conn)
            market_status_repo.insert_snapshot(
                conn,
                MarketStatusSnapshot(
                    snapshot_id=snapshot_id,
                    target_trade_date=target,
                    collected_at=now,
                    source=provider.source,
                    calendar_source=provider.calendar_source,
                    covered_markets=provider.covered_markets,
                    uncovered_markets=provider.uncovered_markets,
                    suspensions=provider.suspensions,
                    status="ok",
                ),
            )
            market_status_repo.prune_snapshots(conn)
        return True

    def _resolved_target(self, conn=None) -> tuple[date, str, bool]:
        """目标交易日与来源；conn 传入时复用同一连接。"""
        if conn is None:
            with self._db.read() as reader:
                calendar = market_status_repo.trade_dates(reader)
                coverage = market_status_repo.calendar_coverage(reader)
        else:
            calendar = market_status_repo.trade_dates(conn)
            coverage = market_status_repo.calendar_coverage(conn)
        return resolve_target(self._clock.now(), calendar, coverage)

    # --- 行情 ---

    def _resolve_targets(self, security_ids: list[str] | None) -> list[str]:
        if security_ids is not None:
            return security_ids
        with self._db.read() as conn:
            return quotes_repo.target_security_ids(conn)

    def _stored_latest(self, security_id: str) -> str | None:
        with self._db.read() as conn:
            return quotes_repo.latest_date(conn, security_id, self._active_adjust())

    def _update_quotes(self, security_ids: list[str] | None = None, *, force: bool = False) -> list[UpdateItemResult]:
        """更新行情，逐股记录结果（成功/获取失败/真实无数据）。

        失败（获取异常）与真实无数据（新上市/停牌，来源成功但空）分别记录：
        前者保留旧数据并提示，后者不当作失败，也不写空序列覆盖旧数据。
        抓取截止取目标交易日而不是「今天」：盘中抓今天只会写进一根未收盘的日线，
        随后还会因最新日期超过目标日而被判成未补齐。
        """
        requested = list(dict.fromkeys(self._resolve_targets(security_ids)))
        if not requested:
            return []
        target, _, _ = self._resolved_target()
        adjust = self._active_adjust()
        with self._db.read() as conn:
            latest = quotes_repo.latest_quote_dates(conn, requested, adjust)
        targets = [sid for sid in requested if force or latest.get(sid, "") < target.isoformat()]
        with self._pending_lock:
            self._active_targets = set(requested)
            # 整体更新取范围前排入的新股票若已被本轮包含，就共用这次请求。
            self._pending = [(kind, remaining) for kind, ids in self._pending
                             if (remaining := [sid for sid in ids if sid not in self._active_targets])]
        end = target
        results: list[UpdateItemResult] = []
        consecutive_failures: dict[str, int] = {}
        unavailable_sources: dict[str, str] = {}
        self._begin_progress(len(targets))
        for done, security_id in enumerate(targets, start=1):
            self._current_stock(security_id)
            item_started = time.perf_counter()
            code, _, exchange = security_id.partition(".")
            eligible = [
                source_name(source) for source in self._daily_sources
                if exchange in covered_markets(source)
            ]
            if eligible and all(name in unavailable_sources for name in eligible):
                results.append(UpdateItemResult(
                    security_id=security_id, status=UpdateItemStatus.FAILED,
                    message="；".join(unavailable_sources[name] for name in eligible) + "；本轮未请求",
                    trade_date=self._stored_latest(security_id),
                    elapsed_ms=round((time.perf_counter() - item_started) * 1000),
                ))
                self._tick_progress(done)
                continue
            with self._db.read() as conn:
                old = quotes_repo.series(conn, security_id, adjust, limit=5)
                security = securities_repo.get(conn, security_id)
            try:
                full_start = self._history_start or end.replace(year=end.year - 3)
            except ValueError:  # February 29 -> February 28
                full_start = end.replace(year=end.year - 3, day=28)
            if security and security.listing_date:
                full_start = max(full_start, date.fromisoformat(security.listing_date))
            start = old.bars[0].trade_date if old else full_start
            mode = "incremental" if old else "initial"
            fetched = fetch_daily(
                self._daily_sources, preferred=old.source if old else None,
                code=code, exchange=exchange, start=start, end=end, adjust=adjust,
                excluded_sources=set(unavailable_sources),
            )
            bars = fetched.bars
            attempts = list(fetched.attempt_details)
            errors = dict(fetched.errors)
            for name in fetched.attempted:
                if name in errors and _connection_failed(errors[name]):
                    consecutive_failures[name] = consecutive_failures.get(name, 0) + 1
                    if consecutive_failures[name] >= _SOURCE_FAILURE_LIMIT:
                        unavailable_sources[name] = f"{name} 连续 {_SOURCE_FAILURE_LIMIT} 次连接失败"
                else:
                    consecutive_failures[name] = 0
            if not bars:
                results.append(UpdateItemResult(
                    security_id=security_id,
                    status=UpdateItemStatus.FAILED if fetched.failed else UpdateItemStatus.NO_DATA,
                    message=fetched.message, trade_date=self._stored_latest(security_id),
                    fetch_mode=mode, request_start=start.isoformat(),
                    elapsed_ms=round((time.perf_counter() - item_started) * 1000),
                    attempts=tuple(attempts),
                ))
                self._tick_progress(done)
                continue
            source_id = fetched.source
            fetched_at = self._clock.now().isoformat()
            try:
                if old is None:
                    with self._db.transaction() as conn:
                        quotes_repo.replace_series(
                            conn, security_id=security_id, adjust=adjust, source=source_id,
                            fetched_at=fetched_at,
                            bars=[_bar_from_provider(security_id, bar) for bar in bars],
                        )
                elif bars[-1].trade_date >= old.bars[-1].trade_date:
                    rebuild = source_id != old.source
                    if not rebuild:
                        try:
                            with self._db.transaction() as conn:
                                quotes_repo.merge_incremental_series(
                                    conn, security_id=security_id, adjust=adjust,
                                    source=source_id, fetched_at=fetched_at,
                                    bars=[_bar_from_provider(security_id, bar) for bar in bars],
                                )
                        except quotes_repo.IncompatibleIncrement:
                            rebuild = True
                    if rebuild:
                        mode = "rebuild"
                        start = full_start
                        chosen = next(source for source in self._daily_sources
                                      if source_name(source) == source_id)
                        rebuild_started = time.perf_counter()
                        try:
                            complete = chosen.daily_bars(
                                code=code, exchange=exchange, start=full_start,
                                end=end, adjust=adjust,
                            )
                            validate_bars(complete, full_start, end)
                            if not complete or complete[-1].trade_date < bars[-1].trade_date:
                                raise QuoteSourceError("完整重抓未覆盖短区间已取得的最新行情")
                            with self._db.read() as conn:
                                old_full = quotes_repo.series(conn, security_id, adjust)
                            new_days = {bar.trade_date for bar in complete}
                            missing = [bar.trade_date for bar in old_full.bars
                                       if full_start <= bar.trade_date <= end
                                       and bar.trade_date not in new_days]
                            if missing:
                                raise QuoteSourceError(f"完整重抓缺少已有历史日期 {missing[0]}")
                            attempts.append({
                                "source": source_id,
                                "elapsedMs": round((time.perf_counter() - rebuild_started) * 1000),
                                "outcome": "rebuild", "latestDate": complete[-1].trade_date.isoformat(),
                            })
                            with self._db.transaction() as conn:
                                quotes_repo.replace_series(
                                    conn, security_id=security_id, adjust=adjust,
                                    source=source_id, fetched_at=fetched_at,
                                    bars=[_bar_from_provider(security_id, bar) for bar in complete],
                                )
                        except Exception:
                            attempts.append({
                                "source": source_id,
                                "elapsedMs": round((time.perf_counter() - rebuild_started) * 1000),
                                "outcome": "failed_rebuild", "latestDate": None,
                            })
                            raise
            except Exception as exc:  # 单股失败不能中断其余股票；旧曲线仍在事务中保护。
                results.append(UpdateItemResult(
                    security_id=security_id, status=UpdateItemStatus.FAILED,
                    message=f"行情保存或完整重抓失败：{exc}",
                    trade_date=self._stored_latest(security_id),
                    fetch_mode=mode, request_start=start.isoformat(),
                    elapsed_ms=round((time.perf_counter() - item_started) * 1000),
                    attempts=tuple(attempts),
                ))
                self._tick_progress(done)
                continue
            results.append(
                UpdateItemResult(
                    security_id=security_id,
                    status=UpdateItemStatus.OK,
                    message=fetched.message,
                    trade_date=self._stored_latest(security_id),
                    fetch_mode=mode, request_start=start.isoformat(),
                    elapsed_ms=round((time.perf_counter() - item_started) * 1000),
                    attempts=tuple(attempts),
                )
            )
            self._tick_progress(done)
        self._end_progress()
        with self._pending_lock:
            self._active_targets.clear()
        return results


def _to_security(provider: ProviderSecurity) -> Security:
    normalized = normalize_code(provider.code)
    code = normalized.code if normalized.valid else provider.code
    exchange = provider.exchange
    return Security(
        security_id=f"{code}.{exchange}",
        code=code,
        exchange=exchange,
        board=provider.board,
        name=provider.name,
        listing_date=provider.listing_date,
        is_st=provider.is_st,
    )


def _fingerprint(securities: list[Security]) -> str:
    import hashlib

    joined = "\n".join(sorted(s.security_id for s in securities))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _source_json(source_id: str) -> str:
    import json

    return json.dumps(
        {
            "provider": source_id,
            "securities": "交易所名单（经 AKShare）" if source_id == "akshare" else source_id,
            "note": "证券身份来自交易所名单；行情来源见各自序列记录。",
        },
        ensure_ascii=False,
    )


def _final_status(
    securities_status: str, ok: int, failed: list[str], pending: int
) -> UpdateStatus:
    """成功/失败如实区分：全部成功为 SUCCESS，部分成功为 PARTIAL，全失败为 FAILED。

    真实无数据不算失败，因此不参与这里的成功/失败判定；但「仍未补齐」的股票
    （请求成功却落后目标交易日、或状态无法确认）同样不算全部完成，只能记 PARTIAL。
    pending 由调用方按完整性判定给出，不设默认值：漏传就等于把未补齐当成功。
    """
    securities_ok = securities_status in {SECURITIES_OK, SECURITIES_SKIPPED}
    if securities_status == "partial":
        return UpdateStatus.PARTIAL
    if securities_ok and not failed and pending == 0:
        return UpdateStatus.SUCCESS
    if ok == 0 and not securities_ok:
        return UpdateStatus.FAILED
    if ok == 0 and (len(failed) > 0 or securities_status == SECURITIES_FAILED):
        return UpdateStatus.FAILED
    return UpdateStatus.PARTIAL


def run_json(run: UpdateRun | None) -> dict | None:
    """更新记录的序列化形式；接口与状态读取共用，避免两处字段漂移。"""
    if run is None:
        return None
    return {
        "runId": run.run_id,
        "kind": run.kind.value,
        "status": run.status.value,
        "startedAt": run.started_at.isoformat(),
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "durationMs": (run.elapsed_ms if run.elapsed_ms is not None else
                       round((run.finished_at - run.started_at).total_seconds() * 1000)
                       if run.finished_at else None),
        "securitiesStatus": run.securities_status,
        "securitiesMessage": run.securities_message,
        "securitiesCount": run.securities_count,
        "quotesOk": run.quotes_ok,
        "quotesFailed": run.quotes_failed,
        "quotesSkipped": run.quotes_skipped,
        "quotesPending": run.quotes_pending,
        "failedSecurities": list(run.failed_securities),
    }


__all__ = [
    "ADJUST",
    "MAX_RETRIES",
    "UpdateBusy",
    "UpdateService",
    "run_json",
    "TARGETED_KINDS",
]
