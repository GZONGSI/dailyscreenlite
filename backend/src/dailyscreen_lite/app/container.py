"""应用装配：连接数据库、加载证券库、构造领域服务。

测试与生产共用同一装配入口，只通过 Settings 指定不同数据目录。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field

from datetime import datetime

from dailyscreen_lite.domain.clock import BeijingClock, Clock, FixedClock
from dailyscreen_lite.imports.service import ImportService
from dailyscreen_lite.observations.service import ObservationService
from dailyscreen_lite.notes.service import NoteService
from dailyscreen_lite.quotes.scheduler import UpdateScheduler
from dailyscreen_lite.quotes.calendar_source import CalendarSource, build_calendar_source
from dailyscreen_lite.quotes.market_status import MarketStatusSource, build_market_status_source
from dailyscreen_lite.quotes.service import QuoteService
from dailyscreen_lite.quotes.source import (
    QuotesSource,
    build_daily_sources,
    build_source,
    declared_adjust_of,
)
from dailyscreen_lite.quotes.status import DataStatusService
from dailyscreen_lite.quotes.update import UpdateService
from dailyscreen_lite.repository import Database
from dailyscreen_lite.repository import quotes_repo, securities_repo
from dailyscreen_lite.repository.securities_repo import SnapshotMeta
from dailyscreen_lite.classification.service import ClassificationService
from dailyscreen_lite.securities import SnapshotLoadError, load_snapshot
from dailyscreen_lite.settings import Settings
from dailyscreen_lite.wencai.cookie_store import CookieStore
from dailyscreen_lite.wencai.http_session import HttpWencaiSession
from dailyscreen_lite.wencai.token import NodeTokenProvider

logger = logging.getLogger(__name__)


@dataclass
class _BackfillState:
    """导入后后台补取的协调状态：在途标记 + 完成信号（测试可等待线程收尾）。"""

    running: bool = False
    done: threading.Event = field(default_factory=threading.Event)


@dataclass
class Container:
    settings: Settings
    db: Database
    clock: Clock
    imports: ImportService
    classification: ClassificationService
    observations: ObservationService
    notes: NoteService
    quotes: QuoteService
    data_status: DataStatusService
    market_status: MarketStatusSource
    updates: UpdateService
    scheduler: UpdateScheduler
    cookies: CookieStore
    wencai_available: bool
    wencai_message: str | None
    securities_loaded: bool
    securities_message: str | None
    securities_count: int
    #: 后台补取的并发护栏与完成信号：同时在跑就只留一条在途记录，测试可等待它结束。
    #: 用协调对象而不是布尔字段：closure 与 Container 必须看到同一份状态。
    backfill: "_BackfillState" = field(default_factory=lambda: _BackfillState())
    def start_background(self) -> None:
        """启动应用内调度：启动补更与每日 16:30 尝试更新。"""
        self.scheduler.start()

    def stop_background(self) -> None:
        self.scheduler.stop()


def build_container(settings: Settings, clock: Clock | None = None, *, quotes_source: QuotesSource | None = None,
                    calendar_source: CalendarSource | None = None, status_source: MarketStatusSource | None = None) -> Container:
    settings.ensure_dirs()
    db = Database(settings.database_path)
    db.initialize()
    active_clock = clock or _configured_clock(settings)
    # 上次进程被中断会留下永久「更新中」的记录，启动时按失败收尾；
    # 先查有无遗留，无事发生时不读时钟（提交时的导入日期依赖它保持稳定）
    with db.transaction() as conn:
        if quotes_repo.count_running(conn):
            quotes_repo.fail_stale_runs(
                conn,
                finished_at=active_clock.now().isoformat(),
                message="上次更新未结束（进程中断），已按失败收尾",
            )
    cookies = CookieStore(settings.secrets_path)

    wencai_available = False
    wencai_message: str | None = None
    wencai_session = None
    bundle = settings.wencai_token_bundle
    if bundle is not None:
        try:
            tokens = NodeTokenProvider(bundle)
            # 令牌生成器存在即认为链接获取可用；Cookie 缺失在提交时单独提示
            wencai_available = bundle.exists()
            wencai_session = _WencaiSessionProxy(tokens, cookies, settings.wencai_base_url)
            if not wencai_available:
                wencai_message = f"缺少问财令牌生成器：{bundle}"
        except Exception as exc:  # pragma: no cover - 装配兜底
            wencai_message = f"问财获取装配失败：{type(exc).__name__}"
            # 页面只说装配失败，具体原因留在日志里便于排查令牌生成器与 Node
            logger.exception("问财令牌生成器装配失败：%s", bundle)

    loaded = False
    message: str | None = None
    count = 0
    try:
        snapshot = load_snapshot(settings.securities_snapshot)
        # 只在库为空时用交付快照初始化；已有库（含应用内更新后的名单）保持不动，
        # 否则重启会用旧快照覆盖今天的更新，用户还得再手动更新一次。
        with db.transaction() as conn:
            current = securities_repo.current_meta(conn)
            existing = securities_repo.count(conn)
            if current is not None and existing > 0:
                loaded = True
                count = existing
            else:
                securities_repo.replace_snapshot(
                    conn,
                    SnapshotMeta(
                        snapshot_id=snapshot.snapshot_id,
                        loaded_at=snapshot.loaded_at,
                        record_count=snapshot.record_count,
                        identity_fingerprint=snapshot.identity_fingerprint,
                        effective_date=snapshot.effective_date,
                        source_json=json.dumps(snapshot.source, ensure_ascii=False),
                    ),
                    snapshot.securities,
                )
                loaded = True
                count = snapshot.record_count
    except SnapshotLoadError as exc:
        # 交付快照不可用不阻止启动：库中已有证券仍可用于识别
        with db.read() as conn:
            existing = securities_repo.count(conn)
        if existing > 0:
            loaded = True
            count = existing
        else:
            message = str(exc)

    classification = ClassificationService(db, active_clock)
    observations = ObservationService(db, active_clock, classification)
    notes = NoteService(db, active_clock, classification)
    # 首次启动即提供默认观察组；此后用户重命名或归档都不覆盖已有行
    observations.ensure_default_group()

    quotes_source = quotes_source or build_source(settings)
    active_adjust = declared_adjust_of(quotes_source) or "qfq"
    quotes = QuoteService(db, adjust=active_adjust)
    # 每日状态来源与完整性判定：目标交易日、逐股状态、待补齐集合
    status_source = status_source or build_market_status_source(settings)
    data_status = DataStatusService(db, active_clock, adjust=active_adjust)
    updates = UpdateService(
        db,
        active_clock,
        quotes_source,
        status_source=status_source,
        calendar_source=calendar_source or build_calendar_source(settings),
        daily_sources=build_daily_sources(settings, quotes_source),
        # 自动重试只针对「状态正常但行情落后」的股票，待确认的不自动重试
        pending_ids=data_status.pending_ids,
        # 收尾时对本次请求的股票逐股判定完整性（历史股票不在持续范围内也要如实判定）
        incomplete_ids=data_status.incomplete_among,
    )

    # 后台补取的并发护栏与完成信号；closure 与 Container 共用同一份状态
    backfill = _BackfillState()

    def refresh_published(security_ids: list[str]) -> None:
        """新导入或主动重新归类的股票立即后台补行情；失败不影响候选发布。"""
        # 逐轮提交之间同一批股票可能被再次触发；不并发补取，也不为重复触发排队
        # （下一次导入或重新归类仍会重新触发），只留一条在途记录。
        if backfill.running:
            logger.info("后台补取已在进行中，跳过本次触发（%d 只）", len(security_ids))
            return
        backfill.running = True
        backfill.done.clear()

        def worker() -> None:
            try:
                updates.refresh_quotes(security_ids)
            except Exception:
                # 补取失败只影响行情可用性，不阻塞候选发布；但要留下可诊断的记录，
                # 否则页面只说「行情暂未取得」，日志里查不到是哪些股票、为什么失败。
                logger.exception(
                    "导入后后台补取行情失败（%d 只：%s）；候选发布不受影响，缺失可由数据中心重试",
                    len(security_ids),
                    ", ".join(security_ids[:10]) + ("…" if len(security_ids) > 10 else ""),
                )
            finally:
                backfill.running = False
                backfill.done.set()

        threading.Thread(target=worker, name="dslite-quote-backfill", daemon=True).start()

    # 主动重新归类使股票回到持续更新范围，同样补齐行情
    classification.attach_reclassified_hook(refresh_published)

    scheduler = UpdateScheduler(
        updates,
        active_clock,
        enabled=settings.update_schedule_enabled,
    )

    return Container(
        settings=settings,
        db=db,
        clock=active_clock,
        imports=ImportService(
            db,
            active_clock,
            settings.archive_dir,
            cookie_store=cookies,
            wencai_session=wencai_session,
            published_hook=refresh_published,
        ),
        classification=classification,
        observations=observations,
        notes=notes,
        quotes=quotes,
        data_status=data_status,
        market_status=status_source,
        updates=updates,
        scheduler=scheduler,
        cookies=cookies,
        wencai_available=wencai_available,
        wencai_message=wencai_message,
        securities_loaded=loaded,
        securities_message=message,
        securities_count=count,
        backfill=backfill,
    )


def _configured_clock(settings: Settings) -> Clock:
    """时钟来源：显式注入 > DSLITE_NOW > 真实北京时间。

    DSLITE_NOW 只用于开发与端到端测试固定"今天"（跨日入选等场景），生产不设置。
    """
    if settings.fixed_now:
        return FixedClock(datetime.fromisoformat(settings.fixed_now))
    return BeijingClock()


class _WencaiSessionProxy:
    """每次获取都读取当前 Cookie，使设置保存后立即生效。"""

    def __init__(
        self,
        tokens: NodeTokenProvider,
        cookies: CookieStore,
        base_url: str | None = None,
    ) -> None:
        self._tokens = tokens
        self._cookies = cookies
        self._base_url = base_url

    def _session(self) -> HttpWencaiSession:
        cookie = self._cookies.load()
        if not cookie:
            from dailyscreen_lite.domain.errors import WencaiCookieMissing

            raise WencaiCookieMissing("未配置问财 Cookie，无法获取链接结果")
        return HttpWencaiSession(cookie, self._tokens, base_url=self._base_url)

    def parse(self, query: str):
        return self._session().parse(query)

    def page(self, context, *, page: int, perpage: int):
        return self._session().page(context, page=page, perpage=perpage)
