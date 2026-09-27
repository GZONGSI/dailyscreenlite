"""数据完整性判定：目标交易日、逐股状态与分项结论。

判定只依据两件事实：已入库的最新行情日期、以及带目标交易日的状态快照。
空行情或过期行情都不能单独证明停牌；没有覆盖目标交易日的新快照时，
「不在停牌清单里」也不当作正常交易，缺失行情记「未补齐，状态待确认」。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.models import (
    SECURITIES_OK,
    MarketStatus,
    MarketStatusSnapshot,
    StockDataState,
    StockDataStatus,
    UpdateItemResult,
    UpdateRun,
    UpdateStatus,
)
from dailyscreen_lite.quotes.calendar import previous_trade_date, resolve_target
from dailyscreen_lite.quotes.market_status import (
    covering_suspension,
    resolve_market_status,
)
from dailyscreen_lite.repository import (
    Database,
    market_status_repo,
    quotes_repo,
    securities_repo,
)

ITEM_SECURITIES = "securities"
ITEM_MARKET_STATUS = "market_status"
ITEM_QUOTES = "quotes"


@dataclass(frozen=True)
class DataStatusItem:
    """数据中心的分项状态：证券库、股票状态、日线。"""

    key: str
    label: str
    state: str  # ok | partial | failed | unknown | running
    message: str


@dataclass(frozen=True)
class DataStatus:
    """一次完整性判定的完整结论，供接口与页面直接展示。"""

    target_trade_date: date | None
    target_source: str
    calendar_available: bool
    snapshot: MarketStatusSnapshot | None
    scope_count: int
    items: tuple[DataStatusItem, ...]
    stocks: tuple[StockDataStatus, ...]
    last_run: UpdateRun | None
    history: tuple[UpdateRun, ...]
    # 最近一次真正动过证券库的更新：定向补取与单股更新的 skipped 不能抹掉它的结论
    securities_run: UpdateRun | None
    quote_diagnostics: tuple[UpdateItemResult, ...]

    @property
    def incomplete(self) -> tuple[StockDataStatus, ...]:
        return tuple(
            stock
            for stock in self.stocks
            if stock.state in {StockDataState.PENDING, StockDataState.UNCONFIRMED}
        )

    @property
    def complete(self) -> bool:
        """行情是否已补齐：证券库失败单独提示，不把完整行情判成未更新。"""
        return not any(not stock.security_id.endswith(".BJ") for stock in self.incomplete)

    @property
    def reminder(self) -> str | None:
        """未补齐时的顶部提醒文案；日期用实际目标交易日。"""
        if self.complete or self.target_trade_date is None:
            return None
        return (
            f"截至最近交易日（{self.target_trade_date.strftime('%m-%d')}），"
            "数据尚未更新完整！"
        )


@dataclass(frozen=True)
class _Judgement:
    """一只股票的完整性结论；状态规则只在一个地方产出它。"""

    state: StockDataState
    market_status: MarketStatus
    reason: str | None


@dataclass(frozen=True)
class _MarketContext:
    """逐股判定所依据的共同事实：目标交易日、状态快照与交易日历。

    只包含判定本身需要的项，使同一套规则既能判持续更新范围，也能判单次请求的股票。
    """

    target: date
    trusted: bool
    suspensions: tuple
    covered: tuple
    calendar: tuple


def _market_context(
    target: date, calendar_available: bool, snapshot_ok, calendar
) -> _MarketContext:
    """没有覆盖目标交易日的新快照时状态不可信：缺失行情一律记「状态待确认」。"""
    trusted = (
        calendar_available
        and snapshot_ok is not None
        and snapshot_ok.target_trade_date == target
    )
    return _MarketContext(
        target=target,
        trusted=trusted,
        suspensions=snapshot_ok.suspensions if snapshot_ok else (),
        covered=snapshot_ok.covered_markets if snapshot_ok else (),
        calendar=calendar,
    )


class DataStatusService:
    """完整性判定与更新范围：待归类股票 ∪ 现存观察组成员。"""

    def __init__(self, db: Database, clock: Clock, *, adjust: str = "qfq") -> None:
        self._db = db
        self._clock = clock
        self._adjust = adjust

    def target_trade_date(self) -> tuple[date, str, bool]:
        """目标交易日与来源。没有可信日历时按工作日回落并标注不可信。"""
        with self._db.read() as conn:
            calendar = market_status_repo.trade_dates(conn)
            coverage = market_status_repo.calendar_coverage(conn)
        return resolve_target(self._clock.now(), calendar, coverage)

    def scope_ids(self) -> list[str]:
        with self._db.read() as conn:
            return quotes_repo.target_security_ids(conn)

    def incomplete_ids(self) -> list[str]:
        """持续更新范围里未完成的部分：待补齐与状态待确认，供手动重试与逐股列表。"""
        return self._ids_with((StockDataState.PENDING, StockDataState.UNCONFIRMED))

    def pending_ids(self) -> list[str]:
        """可自动重试的部分：状态正常但行情落后目标交易日。

        状态待确认的股票不在此列：既不知道当天是否应有行情，重试也不会改变结论。
        """
        return self._ids_with((StockDataState.PENDING,))

    def incomplete_among(self, security_ids: list[str]) -> list[str]:
        """给定股票中未补齐的子集，按与 status() 相同的规则判定。

        用于单股更新与定向补取的收尾：这些股票可能不在持续更新范围内
        （已处理且离组的历史股票），但仍须逐一如实判定，否则会把未补齐说成成功。
        """
        ids = sorted({sid for sid in security_ids if sid})
        if not ids:
            return []
        target, _, calendar_available = self.target_trade_date()
        with self._db.read() as conn:
            latest_dates = quotes_repo.latest_quote_dates(conn, ids, self._adjust)
            snapshot_ok = market_status_repo.latest_snapshot(conn, only_ok=True)
            calendar = market_status_repo.trade_dates(conn)
        market = _market_context(target, calendar_available, snapshot_ok, calendar)
        judged = self._judge(ids, market, latest_dates)
        return [
            security_id
            for security_id, verdict in judged.items()
            if verdict.state in {StockDataState.PENDING, StockDataState.UNCONFIRMED}
        ]

    def _ids_with(self, states: tuple[StockDataState, ...]) -> list[str]:
        return [stock.security_id for stock in self.status().stocks if stock.state in states]

    def _judge(
        self,
        security_ids: list[str],
        market: _MarketContext,
        latest_dates: dict[str, str],
    ) -> dict[str, _Judgement]:
        """逐股结论；状态与停牌规则只在这一处出现。"""
        judged: dict[str, _Judgement] = {}
        for security_id in security_ids:
            market_status = resolve_market_status(
                security_id,
                market.target,
                market.suspensions,
                market.covered,
                trusted=market.trusted,
            )
            state, reason = _stock_state(
                security_id=security_id,
                market_status=market_status,
                latest_date=latest_dates.get(security_id),
                target=market.target,
                suspensions=market.suspensions,
                calendar=market.calendar,
            )
            judged[security_id] = _Judgement(state, market_status, reason)
        return judged

    def status(self) -> DataStatus:
        target, target_source, calendar_available = self.target_trade_date()
        with self._db.read() as conn:
            scope = quotes_repo.target_security_ids(conn)
            latest_dates = quotes_repo.latest_quote_dates(conn, scope, self._adjust)
            names = securities_repo.names_for(conn, scope)
            snapshot_row = market_status_repo.latest_snapshot(conn, only_ok=False)
            snapshot_ok = market_status_repo.latest_snapshot(conn, only_ok=True)
            calendar = market_status_repo.trade_dates(conn)
            last_run = quotes_repo.latest_run(conn)
            quote_diagnostics = tuple(sorted(
                (item for item in quotes_repo.item_results(conn, last_run.run_id)
                 if item.elapsed_ms is not None),
                key=lambda item: item.elapsed_ms or 0, reverse=True,
            )[:5]) if last_run else ()
            securities_run = quotes_repo.latest_securities_run(conn)
            history = tuple(quotes_repo.recent_runs(conn, 10))
            failures = quotes_repo.latest_item_messages(conn)
            sources = quotes_repo.latest_sources(conn, self._adjust)
            market_updates = securities_repo.market_updates(conn)
            calendar_result = market_status_repo.calendar_update(conn)
            calendar_months = market_status_repo.calendar_months(conn)

        # 没有可信交易日历时缺失行情一律记「状态待确认」：连当天是否应有行情都不确定
        market = _market_context(target, calendar_available, snapshot_ok, calendar)

        stocks: list[StockDataStatus] = []
        for security_id, verdict in self._judge(scope, market, latest_dates).items():
            stocks.append(
                StockDataStatus(
                    security_id=security_id,
                    name=names.get(security_id, ""),
                    state=verdict.state,
                    market_status=verdict.market_status,
                    latest_date=latest_dates.get(security_id),
                    reason=verdict.reason,
                    last_error=failures.get(security_id),
                    source=sources.get(security_id),
                )
            )

        items = _items(
            snapshot=snapshot_row,
            snapshot_trusted=market.trusted,
            calendar_available=calendar_available,
            last_run=last_run,
            securities_run=securities_run,
            stocks=stocks,
            missing=sum(
                1
                for stock in stocks
                if stock.state in {StockDataState.PENDING, StockDataState.UNCONFIRMED}
            ),
        )
        if calendar_result:
            coverage_text = "、".join(f"{r['month']}（{r['source']}，{r['updated_at']}）" for r in calendar_months)
            uncertainty = "；未覆盖当前日期，目标日仅按工作日估算" if not calendar_available else ""
            state = "unknown" if not calendar_available and calendar_result['status'] == "ok" else calendar_result['status']
            items += (DataStatusItem("calendar", "交易日历", state,
                f"{calendar_result['message']}；已缓存完整月份：{coverage_text or '无'}{uncertainty}"),)
        return DataStatus(
            target_trade_date=target,
            target_source=target_source,
            calendar_available=calendar_available,
            snapshot=snapshot_ok,
            scope_count=len(scope),
            items=items + tuple(DataStatusItem(
                f"securities_{r['exchange']}",
                {"SH": "沪市名单", "SZ": "深市名单", "BJ": "北交所名单"}[r['exchange']],
                r['status'],
                f"{r['message']}（来源 {r['source']}，最近成功 {r['succeeded_at'] or '尚无更新，使用交付资料'}）",
            ) for r in market_updates),
            stocks=tuple(stocks),
            last_run=last_run,
            history=history,
            securities_run=securities_run,
            quote_diagnostics=quote_diagnostics,
        )


def _stock_state(
    *,
    security_id: str,
    market_status: MarketStatus,
    latest_date: str | None,
    target: date,
    suspensions: tuple,
    calendar: tuple,
) -> tuple[StockDataState, str | None]:
    """逐股结论。

    停牌只豁免停牌区间覆盖的日期，且不掩盖停牌之前的行情缺口：
    停牌前一天应当有日线，缺了就仍记待补齐，并把缺口如实写进原因。
    """
    if latest_date is not None and latest_date >= target.isoformat():
        # 目标日或更晚的日线都算已补齐：不因「最新日期不等于目标日」而判缺失
        return StockDataState.UPDATED, None
    if market_status is MarketStatus.SUSPENDED:
        suspension = covering_suspension(security_id, target, suspensions)
        expected = previous_trade_date(suspension.start_date, calendar) if suspension else None
        if expected is not None and (latest_date is None or latest_date < expected.isoformat()):
            return (
                StockDataState.PENDING,
                f"停牌前行情缺失：最后行情 {latest_date or '无'}，停牌起 {suspension.start_date}",
            )
        return (
            StockDataState.SUSPENDED,
            f"全天停牌，最后行情 {latest_date}" if latest_date else "全天停牌，暂无行情",
        )
    if market_status is MarketStatus.TRADING:
        return (
            StockDataState.PENDING,
            f"最新行情 {latest_date}，落后目标交易日" if latest_date else "暂无行情",
        )
    return (
        StockDataState.UNCONFIRMED,
        "未补齐，状态待确认"
        if latest_date
        else "缺行情且状态无法确认（未补齐，状态待确认）",
    )


def _items(
    *,
    snapshot: MarketStatusSnapshot | None,
    snapshot_trusted: bool,
    calendar_available: bool,
    last_run: UpdateRun | None,
    securities_run: UpdateRun | None,
    stocks: list[StockDataStatus],
    missing: int,
) -> tuple[DataStatusItem, ...]:
    # 证券库结论只看真正动过证券库的那次更新：其后的定向补取/单股更新记 skipped，
    # 不能把「证券库更新失败，当前使用上次数据」抹成「本次未更新证券库」
    if securities_run is None:
        securities_state, securities_message = "unknown", "尚未更新"
    elif securities_run.securities_failed:
        securities_state, securities_message = (
            "failed",
            "证券库更新失败，当前使用上次数据",
        )
    elif securities_run.securities_status == SECURITIES_OK:
        securities_state = "ok"
        securities_message = f"已更新 {securities_run.securities_count} 只"
    elif securities_run.securities_status == "partial":
        securities_state, securities_message = "partial", securities_run.securities_message or "部分市场更新失败"
    else:
        securities_state, securities_message = "unknown", "本次未更新证券库"

    if snapshot is None:
        status_state, status_message = "unknown", "尚无状态快照"
    elif not snapshot.ok:
        status_state = "failed"
        status_message = snapshot.message or "状态获取失败，保留上次快照"
    elif snapshot_trusted:
        status_state = "ok"
        covered = "、".join(snapshot.covered_markets) or "无"
        status_message = f"{snapshot.target_trade_date.strftime('%m-%d')} 已更新（覆盖 {covered}）"
        if snapshot.uncovered_markets:
            # 未覆盖市场必须显式说明，不宣称全市场已验证
            uncovered = "、".join(snapshot.uncovered_markets)
            status_message += f"，{uncovered} 状态未知"
    elif not calendar_available:
        status_state, status_message = "unknown", "无交易日历，状态待确认"
    else:
        status_state = "unknown"
        status_message = (
            snapshot.message
            or f"快照仍为 {snapshot.target_trade_date.strftime('%m-%d')}，状态待确认"
        )

    if not stocks:
        quotes_state, quotes_message = "ok", "更新范围内暂无股票"
    elif missing == 0:
        quotes_state, quotes_message = "ok", "数据正常"
    elif missing == len(stocks):
        quotes_state = "failed"
        quotes_message = f"{missing} 只股票尚未补齐"
    else:
        quotes_state = "partial"
        quotes_message = f"仍有 {missing} 只股票待更新"
    if last_run is not None and last_run.status is UpdateStatus.RUNNING:
        quotes_state = "running"
        quotes_message = "更新进行中"

    return (
        DataStatusItem(ITEM_SECURITIES, "证券库", securities_state, securities_message),
        DataStatusItem(ITEM_MARKET_STATUS, "股票状态", status_state, status_message),
        DataStatusItem(ITEM_QUOTES, "日线", quotes_state, quotes_message),
    )
