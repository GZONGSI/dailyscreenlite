"""工单 03：每日状态快照、目标交易日完整性与数据中心。

真实临时 SQLite + 真实证券库快照 + 固定时钟；行情与状态都走注入的夹具来源
（与 AKShare 同一来源边界）。覆盖：16:30 与周末/节假日边界、停牌只豁免对应日期、
盘中停牌不豁免、北交所状态未知、状态失败保留旧快照、更新范围、逐股失败原因、
有限重试与手动重试、证券库失败独立提示、提醒文案与重启读回。
供应商实源结论单列在 evidence/20260920-market-status。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest
from conftest import REAL_SNAPSHOT, make_csv

from dailyscreen_lite.app.container import Container, build_container
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import (
    StockDataState,
    UpdateKind,
    UpdateStatus,
)
from dailyscreen_lite.quotes.calendar import (
    latest_closed_trade_date,
    previous_weekday,
)
from dailyscreen_lite.quotes.market_status import (
    MarketStatusError,
    resolve_market_status,
)
from dailyscreen_lite.quotes.source import ProviderBar, QuoteSourceError
from dailyscreen_lite.settings import Settings


def _bar(day: str, close: float) -> dict:
    return {
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume_lots": 1000.0,
        "amount_yuan": 1.0e7,
    }


@pytest.mark.parametrize("fail", [False, True])
def test_import_backfill_reminder_tracks_running_and_terminal_state(tmp_path, fail):
    from threading import Event
    from time import monotonic, sleep
    from fastapi.testclient import TestClient
    from dailyscreen_lite.app.main import create_app

    settings = make_settings(tmp_path, {
        "bars": {"000001.SZ": [_bar("2026-09-18", 11.5)]},
        "fail_bars": ["000001.SZ"] if fail else [],
    }, trg())
    app = create_app(settings, FixedClock(datetime(2026, 9, 18, 17, 0, 0)))
    source = app.state.container.updates._source
    original = source.daily_bars
    started, release = Event(), Event()

    def gated_bars(**request):
        started.set()
        assert release.wait(10)
        return original(**request)

    source.daily_bars = gated_bars
    with TestClient(app) as client:
        try:
            response = client.post("/api/imports", data={"texts": "000001"})
            assert response.status_code == 200
            assert started.wait(5)
            active = client.get("/api/data/status").json()
            assert active["updating"] is True
            assert active["reminder"] == "导入股票数据更新中"
        finally:
            release.set()
        deadline = monotonic() + 10
        while client.get("/api/data/status").json()["updating"]:
            assert monotonic() < deadline
            sleep(0.02)
        final = client.get("/api/data/status").json()
        assert final["reminder"] == (
            "截至最近交易日（09-18），数据尚未更新完整！" if fail else None
        )
        # 后续单股更新不能沿用上一轮导入的进度文案。
        from concurrent.futures import ThreadPoolExecutor
        started.clear()
        release.clear()
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(client.post, "/api/securities/000001.SZ/refresh")
            try:
                assert started.wait(5)
                single = client.get("/api/data/status").json()
                assert single["updating"] is True
                assert single["reminder"] == final["reminder"]
            finally:
                release.set()
            assert request.result(timeout=10).status_code == 200


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def make_settings(
    tmp_path: Path,
    quotes: dict | None = None,
    status: dict | None = None,
) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=REAL_SNAPSHOT,
        quotes_fixture=_write(tmp_path, "quotes_fixture.json", quotes or {}),
        market_status_fixture=_write(tmp_path, "status_fixture.json", status or {}),
        update_schedule_enabled=False,
    )


def build(
    tmp_path: Path,
    quotes: dict | None = None,
    status: dict | None = None,
    *,
    clock: FixedClock | None = None,
) -> Container:
    settings = make_settings(tmp_path, quotes, status)
    return build_container(
        settings, clock or FixedClock(datetime(2026, 9, 18, 17, 0, 0))
    )


def import_codes(container: Container, *codes: str) -> list[str]:
    """导入并返回候选项标识。

    关掉后台补取钩子：测试显式触发更新，才能确定性地统计来源调用次数。
    钩子本身的行为由 test_reclassify_and_import_hooks_trigger_backfill 单独覆盖。
    """
    container.imports._published_hook = None
    container.classification._reclassified_hook = None
    container.imports.submit_file("a.csv", make_csv("代码", *codes))
    return [v.candidate.candidate_id for v in container.classification.list_candidates()]


def status_of(container: Container, security_id: str) -> StockDataState:
    stocks = {s.security_id: s for s in container.data_status.status().stocks}
    return stocks[security_id].state


def trg(trade_dates: list[str] | None = None, **kwargs) -> dict:
    """默认状态夹具：交易日历含 09-17、09-18。"""
    payload = {"trade_dates": trade_dates or ["2026-09-17", "2026-09-18"]}
    payload.update(kwargs)
    return payload


# --- 目标交易日：16:30、周末与节假日 ---


def test_latest_closed_trade_date_boundaries():
    calendar = [date(2026, 9, 17), date(2026, 9, 18)]

    # 当天未到 16:30：目标仍是上一交易日（盘中允许上一交易日）
    assert latest_closed_trade_date(
        datetime(2026, 9, 18, 10, 0), calendar
    ) == date(2026, 9, 17)
    # 16:30 起当天成为已收盘交易日
    assert latest_closed_trade_date(
        datetime(2026, 9, 18, 16, 30), calendar
    ) == date(2026, 9, 18)
    # 周末回落到最近一个已过去交易日，不把周末当作缺当日日线
    assert latest_closed_trade_date(
        datetime(2026, 9, 19, 10, 0), calendar
    ) == date(2026, 9, 18)
    assert latest_closed_trade_date(
        datetime(2026, 9, 20, 23, 0), calendar
    ) == date(2026, 9, 18)
    # 日历含未来交易日时不会被误取
    future = calendar + [date(2026, 9, 21)]
    assert latest_closed_trade_date(
        datetime(2026, 9, 18, 17, 0), future
    ) == date(2026, 9, 18)
    # 无日历：返回 None，由调用方标注不可信
    assert latest_closed_trade_date(datetime(2026, 9, 18, 17, 0), []) is None


def test_previous_weekday_fallback_skips_weekend():
    assert previous_weekday(datetime(2026, 9, 20, 23, 0)) == date(2026, 9, 18)
    assert previous_weekday(datetime(2026, 9, 19, 10, 0)) == date(2026, 9, 18)


def test_target_uses_calendar_and_notes_non_trading_day(tmp_path):
    container = build(
        tmp_path, {"bars": {}}, trg(), clock=FixedClock(datetime(2026, 9, 20, 23, 0))
    )
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)

    status = container.data_status.status()
    # 周日晚上：目标仍是最近已收盘交易日 09-18，不当作缺当日日线
    assert status.target_trade_date == date(2026, 9, 18)
    assert status.calendar_available
    assert status.target_source == "calendar"


def test_without_calendar_target_is_flagged_untrusted(tmp_path):
    container = build(tmp_path, {}, trg(calendar_error="日历获取失败"), clock=FixedClock(datetime(2026, 9, 18, 17, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    status = container.data_status.status()
    assert status.calendar_available is False
    assert status.target_source == "weekday_fallback"
    # 没有可信日历时缺失行情记「状态待确认」，不宣称未补齐也不宣称完整
    assert status_of(container, "000001.SZ") is StockDataState.UNCONFIRMED


# --- 停牌判定 ---


def test_suspension_only_exempts_the_dates_it_covers(tmp_path):
    # 停牌前一天（09-17）有日线：停牌当日是干净豁免，不是掩盖缺口
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-17", 11.0)]}}
    status = trg(
        suspensions=[
            {
                "code": "000001",
                "exchange": "SZ",
                "name": "平安银行",
                "kind": "continuous",
                "start": "2026-09-18",
                "end": None,
                "market": "深交所主板",
            }
        ]
    )
    container = build(tmp_path, quotes, status)
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)

    # 09-18 起停牌：目标日 09-18 豁免，显示全天停牌与最后行情日期
    stock = {s.security_id: s for s in container.data_status.status().stocks}["000001.SZ"]
    assert stock.state is StockDataState.SUSPENDED
    assert "最后行情 2026-09-17" in (stock.reason or "")

    # 目标日回到 09-17（停牌前一天）时缺口不被停牌掩盖
    later = build(
        tmp_path / "other",
        {"bars": {"000001.SZ": [_bar("2026-09-16", 11.0)]}},
        trg(suspensions=[{"code": "000001", "exchange": "SZ", "kind": "continuous", "start": "2026-09-18"}]),
        clock=FixedClock(datetime(2026, 9, 17, 17, 0)),
    )
    import_codes(later, "000001")
    later.updates.run(UpdateKind.MANUAL)
    assert later.data_status.status().target_trade_date == date(2026, 9, 17)
    assert status_of(later, "000001.SZ") is StockDataState.PENDING


def test_intraday_suspension_is_not_exempt(tmp_path):
    status = trg(
        suspensions=[
            {
                "code": "600519",
                "exchange": "SH",
                "kind": "intraday",
                "start": "2026-09-18",
                "end": "2026-09-18",
                "market": "上交所主板",
            }
        ]
    )
    container = build(tmp_path, {"bars": {"600519.SH": [_bar("2026-09-17", 1500.0)]}}, status)
    import_codes(container, "600519")
    container.updates.run(UpdateKind.MANUAL)
    assert status_of(container, "600519.SH") is StockDataState.PENDING


def test_bj_market_is_uncovered_and_missing_data_is_unconfirmed(tmp_path):
    container = build(tmp_path, {"bars": {"920001.BJ": [_bar("2026-09-17", 10.0)]}}, trg())
    import_codes(container, "920001")
    container.updates.run(UpdateKind.MANUAL)

    status = container.data_status.status()
    stock = {s.security_id: s for s in status.stocks}["920001.BJ"]
    assert stock.state is StockDataState.UNCONFIRMED
    assert stock.reason == "未补齐，状态待确认"
    assert status.complete
    assert status.reminder is None
    assert [s.security_id for s in status.incomplete] == ["920001.BJ"]
    import_codes(container, "600519")
    assert not container.data_status.status().complete
    # 状态分项如实说明北交所未知，不宣称全市场已验证
    item = {i.key: i for i in status.items}["market_status"]
    assert "BJ" in item.message


def test_empty_quotes_do_not_prove_suspension(tmp_path):
    container = build(tmp_path, {"bars": {}}, trg())
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)
    # 来源成功但无数据（空响应）不能单独证明停牌
    assert status_of(container, "000001.SZ") is StockDataState.PENDING


def test_market_status_resolution_rules():
    """状态判定只看停牌区间与覆盖范围，不看行情。"""
    from dailyscreen_lite.domain.models import MarketStatus, Suspension, SuspensionKind

    target = date(2026, 9, 18)
    suspension = Suspension(
        security_id="000001.SZ",
        code="000001",
        name="平安银行",
        kind=SuspensionKind.CONTINUOUS,
        start_date=date(2026, 9, 15),
        end_date=None,
        expected_resume=None,
        market="深交所主板",
        reason=None,
    )
    assert resolve_market_status("000001.SZ", target, (), ("SH", "SZ")) is MarketStatus.TRADING
    assert resolve_market_status("920001.BJ", target, (), ("SH", "SZ")) is MarketStatus.UNKNOWN
    assert resolve_market_status("000001.SZ", target, (suspension,), ("SH", "SZ")) is MarketStatus.SUSPENDED
    # 不可信快照下连停牌也不豁免：旧区间不能用来推断新的目标交易日
    assert (
        resolve_market_status("000001.SZ", target, (suspension,), ("SH", "SZ"), trusted=False)
        is MarketStatus.UNKNOWN
    )


# --- 状态失败与旧快照 ---


def test_status_failure_keeps_old_snapshot_without_flagging_complete_stocks(tmp_path):
    quotes = {
        "bars": {
            "000001.SZ": [_bar("2026-09-18", 11.0)],
            "600519.SH": [_bar("2026-09-17", 1500.0)],
        }
    }
    container = build(tmp_path, quotes, trg())
    import_codes(container, "000001", "600519")
    container.updates.run(UpdateKind.MANUAL)
    assert status_of(container, "000001.SZ") is StockDataState.UPDATED
    assert status_of(container, "600519.SH") is StockDataState.PENDING

    # 状态来源失败：沿用当天旧快照，已完整的不被判缺失，落后的仍记待补齐
    _write(tmp_path, "status_fixture.json", {"error": "停复牌状态获取失败（夹具注入）"})
    container.updates.run(UpdateKind.MANUAL)
    result = container.data_status.status()
    assert status_of(container, "000001.SZ") is StockDataState.UPDATED
    assert status_of(container, "600519.SH") is StockDataState.PENDING
    item = {i.key: i for i in result.items}["market_status"]
    assert item.state == "failed"
    assert "夹具注入" in item.message


def test_next_trading_day_without_new_snapshot_marks_missing_unconfirmed(tmp_path):
    # 交易日历覆盖 09-21，使目标日能推进到下一交易日
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]}}
    clock = FixedClock(datetime(2026, 9, 18, 17, 0, 0))
    container = build(tmp_path, quotes, trg(["2026-09-17", "2026-09-18", "2026-09-21"]), clock=clock)
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)
    assert status_of(container, "000001.SZ") is StockDataState.UPDATED

    # 进入下一个交易日但没有新快照：不能拿昨天的状态推断今天
    clock.set(datetime(2026, 9, 21, 17, 0, 0))
    _write(tmp_path, "status_fixture.json", {"error": "状态获取失败（夹具注入）"})
    container.updates.run(UpdateKind.MANUAL)
    status = container.data_status.status()
    assert status.target_trade_date == date(2026, 9, 21)
    assert status_of(container, "000001.SZ") is StockDataState.UNCONFIRMED
    assert status.reminder == "截至最近交易日（09-21），数据尚未更新完整！"


# --- 更新范围 ---


def test_scope_is_pending_candidates_and_observation_members(tmp_path):
    quotes = {
        "bars": {
            "000001.SZ": [_bar("2026-09-18", 11.0)],
            "600519.SH": [_bar("2026-09-18", 1500.0)],
            "300750.SZ": [_bar("2026-09-18", 200.0)],
        }
    }
    container = build(tmp_path, quotes, trg())
    ids = import_codes(container, "000001", "600519", "300750")
    # 先整体更新一次，让三只都有行情
    container.updates.run(UpdateKind.MANUAL)
    by_code = {
        v.candidate.security_id: v.candidate.candidate_id
        for v in container.classification.list_candidates()
    }

    # 一只保持待归类、一只加入观察组、一只暂不关注（未加入任何组）
    container.observations.observe_candidate(by_code["600519.SH"], ["default"])
    container.classification.dismiss(by_code["300750.SZ"])

    assert container.data_status.scope_ids() == ["000001.SZ", "600519.SH"]

    container.updates.run(UpdateKind.MANUAL)
    # 离组且已处理的历史股票不进持续范围，但旧行情保留可用
    assert container.quotes.view("300750.SZ").available
    assert "300750.SZ" not in [s.security_id for s in container.data_status.status().stocks]


def test_reclassify_brings_stock_back_into_scope(tmp_path):
    quotes = {"bars": {"300750.SZ": [_bar("2026-09-18", 200.0)]}}
    container = build(tmp_path, quotes, trg())
    ids = import_codes(container, "300750")
    container.classification.dismiss(ids[0])
    assert container.data_status.scope_ids() == []

    container.classification.reclassify(ids[0])
    assert container.data_status.scope_ids() == ["300750.SZ"]


def test_single_security_refresh_does_not_join_continuous_scope(tmp_path):
    quotes = {"bars": {"300750.SZ": [_bar("2026-09-10", 200.0)]}}
    container = build(tmp_path, quotes, trg())
    ids = import_codes(container, "300750")
    container.classification.dismiss(ids[0])
    assert container.data_status.scope_ids() == []

    container.updates.refresh_security("300750.SZ")
    # 单股更新只取一次，不加入持续范围
    assert container.data_status.scope_ids() == []
    runs = container.updates.history(5)
    assert runs[0]["kind"] == "single"
    # 单股更新不计入调度判断（不算「整体更新已完成」）
    assert container.updates.last_completed(exclude_kinds=(UpdateKind.SINGLE,)) is None


# --- 部分失败、逐股原因与有限重试 ---


class CountingSource:
    """记录调用次数的夹具来源；可让某只股票前 N 次调用失败。"""

    source_id = "counting"

    def __init__(self, quotes: dict, *, fail_first: dict[str, int] | None = None) -> None:
        self._quotes = quotes
        self._fail_first = dict(fail_first or {})
        self.calls: list[str] = []
        # 每次抓取的截止日期：用于核对「取到目标交易日为止」而不是取到今天
        self.ends: list[date] = []
        self.progress_snapshots: list = []

    def security_list(self):
        return []

    def daily_bars(self, *, code, exchange, start: date, end: date, adjust: str):
        security_id = f"{code}.{exchange}"
        self.calls.append(security_id)
        self.ends.append(end)
        remaining = self._fail_first.get(security_id, 0)
        if remaining > 0:
            self._fail_first[security_id] = remaining - 1
            raise QuoteSourceError(f"{security_id} 行情获取失败（夹具注入）")
        bars = [
            ProviderBar(
                trade_date=date.fromisoformat(row["date"]),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume_lots=row["volume_lots"],
                amount_yuan=row["amount_yuan"],
            )
            for row in self._quotes.get(security_id, [])
        ]
        # 与真实来源同一约定：只返回 [start, end] 内的日线
        return [bar for bar in bars if start <= bar.trade_date <= end]


def _build_with_source(
    tmp_path: Path, source, status: dict, *, clock: FixedClock | None = None
) -> Container:
    settings = Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=REAL_SNAPSHOT,
        market_status_fixture=_write(tmp_path, "status_fixture.json", status),
        update_schedule_enabled=False,
    )
    container = build_container(
        settings, clock or FixedClock(datetime(2026, 9, 18, 17, 0, 0)), quotes_source=source
    )
    return container


def test_partial_failure_keeps_old_data_and_records_reason_per_stock(tmp_path):
    source = CountingSource(
        {"000001.SZ": [_bar("2026-09-18", 11.5)]},
        fail_first={"600519.SH": 99},
    )
    container = _build_with_source(tmp_path, source, trg())
    import_codes(container, "000001", "600519")

    record = container.updates.run(UpdateKind.MANUAL)
    assert record.status is UpdateStatus.PARTIAL
    assert record.quotes_ok == 1
    assert record.quotes_failed == 1
    assert list(record.failed_securities) == ["600519.SH"]

    items = {i.security_id: i for i in container.updates.run_items(record.run_id)}
    assert items["000001.SZ"].status.value == "ok"
    assert items["000001.SZ"].trade_date == "2026-09-18"
    assert items["600519.SH"].status.value == "failed"
    assert "夹具注入" in (items["600519.SH"].message or "")

    result = container.data_status.status()
    stock = {s.security_id: s for s in result.stocks}["600519.SH"]
    assert stock.state is StockDataState.PENDING
    assert "夹具注入" in (stock.last_error or "")


def test_bounded_retry_retries_only_unfinished_stock(tmp_path):
    source = CountingSource(
        {"000001.SZ": [_bar("2026-09-18", 11.5)]},
        fail_first={"600519.SH": 99},
    )
    container = _build_with_source(tmp_path, source, trg())
    import_codes(container, "000001", "600519")
    container.updates.run(UpdateKind.MANUAL)

    # 重试只覆盖未完成部分且次数有限：成功的股票只取一次
    assert source.calls.count("000001.SZ") == 1
    assert source.calls.count("600519.SH") == 1 + 2


def test_retry_recovers_when_source_succeeds_later(tmp_path):
    import time

    source = CountingSource(
        {"600519.SH": [_bar("2026-09-18", 1500.0)]},
        fail_first={"600519.SH": 1},
    )
    original = source.daily_bars

    def slow_attempt(**request):
        time.sleep(0.025)
        return original(**request)

    source.daily_bars = slow_attempt
    container = _build_with_source(tmp_path, source, trg())
    import_codes(container, "600519")

    record = container.updates.run(UpdateKind.MANUAL)
    # 第一次失败、重试成功：最终是成功，不把部分成功冒充为全失败
    assert record.quotes_ok == 1
    assert record.quotes_failed == 0
    assert status_of(container, "600519.SH") is StockDataState.UPDATED
    item = container.updates.run_items(record.run_id)[0]
    assert [attempt["outcome"] for attempt in item.attempts] == ["failed", "data"]
    assert item.attempts[0]["error"]
    assert item.elapsed_ms >= 50  # 两次请求都计入逐股总耗时


def test_pending_stock_missing_bar_is_retried(tmp_path):
    """来源成功但行情日期落后目标日的股票，也在有限重试范围内。"""
    source = CountingSource({"600519.SH": [_bar("2026-09-17", 1500.0)]})
    container = _build_with_source(tmp_path, source, trg())
    import_codes(container, "600519")

    container.updates.run(UpdateKind.MANUAL)
    # 行情一直落后：待补齐的股票在本次运行内被重试（1 + MAX_RETRIES），仍不虚构日期
    assert source.calls.count("600519.SH") == 3
    assert status_of(container, "600519.SH") is StockDataState.PENDING


def test_progress_is_reported_while_running(tmp_path):
    source = CountingSource({"000001.SZ": [_bar("2026-09-18", 11.5)]})
    container = _build_with_source(tmp_path, source, trg())
    service = container.updates
    original = source.daily_bars

    seen: list[dict] = []

    def spy(*, code, exchange, start, end, adjust):
        snapshot = service.progress()
        if snapshot is not None:
            seen.append(snapshot)
        return original(code=code, exchange=exchange, start=start, end=end, adjust=adjust)

    source.daily_bars = spy
    import_codes(container, "000001")
    service.run(UpdateKind.MANUAL)

    assert seen, "进行中应能读到进度"
    assert seen[0]["total"] == 1
    assert service.progress() is None  # 结束后不再报进行中


def test_reclassify_and_import_hooks_trigger_backfill(tmp_path):
    """新入选与主动重新归类都要补齐行情，二者复用同一补取入口。"""
    container = build(tmp_path, {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]}}, trg())
    seen: list[list[str]] = []
    container.classification.attach_reclassified_hook(lambda ids: seen.append(ids))
    container.imports._published_hook = lambda ids: seen.append(ids)

    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    ids = [v.candidate.candidate_id for v in container.classification.list_candidates()]
    assert seen == [["000001.SZ"]]

    container.classification.dismiss(ids[0])
    container.classification.reclassify(ids[0])

    # 新入选与重新归类复用同一补取入口，都传该股票身份
    assert seen[-1] == ["000001.SZ"]
    assert len(seen) == 2


def test_background_backfill_failure_is_logged_for_diagnosis(tmp_path, caplog):
    """后台补取失败要留下可诊断记录：页面只说「行情暂未取得」，日志得能查到原因。"""
    container = build(tmp_path, {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]}}, trg())

    def boom(_ids):
        raise QuoteSourceError("夹具注入：后台补取失败")

    container.updates.refresh_quotes = boom
    with caplog.at_level("ERROR", logger="dailyscreen_lite.app.container"):
        container.imports.submit_file("a.csv", make_csv("代码", "000001"))
        assert container.backfill.done.wait(10), "后台补取线程未在 10 秒内结束"

    records = [r for r in caplog.records if r.name == "dailyscreen_lite.app.container"]
    assert records, "后台补取失败没有留下任何日志记录"
    assert "后台补取行情失败" in records[0].getMessage()
    assert "000001.SZ" in records[0].getMessage()
    # 具体原因在堆栈里（logger.exception），页面只显示「行情暂未取得」
    assert records[0].exc_info is not None, "应带堆栈，便于定位失败点"
    assert isinstance(records[0].exc_info[1], QuoteSourceError)


def test_securities_failure_is_reported_separately(tmp_path):
    quotes = {
        "bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]},
        "securities_error": "交易所名单获取失败（夹具注入）",
    }
    container = build(tmp_path, quotes, trg())
    import_codes(container, "000001")

    record = container.updates.run(UpdateKind.MANUAL)
    assert record.securities_status == "failed"
    assert record.securities_failed

    result = container.data_status.status()
    item = {i.key: i for i in result.items}["securities"]
    assert item.state == "failed"
    assert item.message == "证券库更新失败，当前使用上次数据"
    # 行情完整时不因证券库失败而说成未更新
    assert result.complete is True
    assert result.reminder is None


# --- 复核修复：抓取截止、终态与提示不被后续任务改写 ---


def test_lagging_quotes_do_not_report_a_successful_update(tmp_path):
    """来源始终返回 09-17（目标 09-18）：重试之后也不得把整次更新记成成功。"""
    source = CountingSource({"600519.SH": [_bar("2026-09-17", 1500.0)]})
    container = _build_with_source(tmp_path, source, trg())
    import_codes(container, "600519")

    record = container.updates.run(UpdateKind.MANUAL)
    assert source.calls.count("600519.SH") == 1 + 2  # 仍在有限重试范围内
    assert record.quotes_ok == 1  # 抓取本身成功
    assert record.quotes_failed == 0  # 也不是失败
    assert record.quotes_pending == 1  # 但目标交易日行情没到
    assert record.status is UpdateStatus.PARTIAL

    # 与完整性接口同一结论：未补齐，不能用「更新完成」覆盖
    result = container.data_status.status()
    assert result.complete is False
    assert result.reminder is not None


def test_intraday_fetch_stops_at_the_target_trade_date(tmp_path):
    """盘中（目标仍是上一交易日）：不得把今天未收盘的日线写进序列。"""
    source = CountingSource(
        {"600519.SH": [_bar("2026-09-17", 1500.0), _bar("2026-09-18", 1510.0)]}
    )
    container = _build_with_source(
        tmp_path, source, trg(), clock=FixedClock(datetime(2026, 9, 18, 10, 0, 0))
    )
    import_codes(container, "600519")
    container.updates.run(UpdateKind.MANUAL)

    # 抓取截止取目标交易日（09-17），不取今天：未收盘的 09-18 日线不入库
    assert set(source.ends) == {date(2026, 9, 17)}
    assert container.quotes.view("600519.SH").latest_date == "2026-09-17"
    # 已有目标日完整行情，不因「最新日期不等于目标日」被判缺失
    assert status_of(container, "600519.SH") is StockDataState.UPDATED


def test_single_refresh_keeps_securities_failure_notice(tmp_path):
    """证券库仍未恢复时，单股更新（不动证券库）不得抹掉失败提示。"""
    quotes = {
        "bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]},
        "securities_error": "交易所名单获取失败（夹具注入）",
    }
    container = build(tmp_path, quotes, trg())
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)
    assert {
        i.key: i.state for i in container.data_status.status().items
    }["securities"] == "failed"

    container.updates.refresh_security("000001.SZ")

    result = container.data_status.status()
    item = {i.key: i for i in result.items}["securities"]
    assert item.state == "failed"
    assert item.message == "证券库更新失败，当前使用上次数据"
    # 提示取自最近一次真正动过证券库的更新，而不是最近一次任务
    assert result.last_run.kind is UpdateKind.SINGLE
    assert result.securities_run is not None and result.securities_run.securities_failed


def test_single_refresh_of_departed_stock_reports_not_complete(tmp_path):
    """历史股票（已处理且不在观察组）的单股更新也必须如实判定完整性。

    它不在持续更新范围里，但这次请求确实动了它：来源只给到 09-17（目标 09-18）
    时必须记「部分完成 + 1 只未补齐」，不能因为「不在范围」就说成成功。
    前置一次整体更新，与真实使用顺序一致（状态快照由整体更新写入，单股补取不写）。
    """
    source = CountingSource({"600519.SH": [_bar("2026-09-17", 1500.0)]})
    container = _build_with_source(tmp_path, source, trg())
    ids = import_codes(container, "600519")
    container.updates.run(UpdateKind.MANUAL)
    container.classification.dismiss(ids[0])
    assert container.data_status.scope_ids() == []  # 离组历史股票

    container.updates.refresh_security("600519.SH")

    run = container.updates.history(1)[0]
    assert run["kind"] == "single"
    assert run["quotesOk"] == 1  # 抓取本身成功
    assert run["quotesPending"] == 1  # 但目标交易日行情没到
    assert run["status"] == "partial"
    # 判定逐股给出，且不因此把该股拉进持续范围
    assert container.data_status.incomplete_among(["600519.SH"]) == ["600519.SH"]
    assert container.data_status.scope_ids() == []


def test_single_refresh_reports_success_when_target_date_arrives(tmp_path):
    """补齐目标交易日后，历史股票的单股更新如实记成功。"""
    source = CountingSource({"600519.SH": [_bar("2026-09-18", 1510.0)]})
    container = _build_with_source(tmp_path, source, trg())
    ids = import_codes(container, "600519")
    container.updates.run(UpdateKind.MANUAL)
    container.classification.dismiss(ids[0])

    container.updates.refresh_security("600519.SH")

    run = container.updates.history(1)[0]
    assert run["quotesPending"] == 0
    assert run["status"] == "success"
    assert container.data_status.scope_ids() == []


def test_single_refresh_of_suspended_history_stock_is_not_incomplete(tmp_path):
    """确认全天停牌的历史股票仍算补齐：不为「目标日无日线」虚构缺口。"""
    status = trg(
        suspensions=[
            {
                "code": "600519",
                "exchange": "SH",
                "kind": "continuous",
                "start": "2026-09-18",
            }
        ]
    )
    source = CountingSource({"600519.SH": [_bar("2026-09-17", 1500.0)]})
    container = _build_with_source(tmp_path, source, status)
    ids = import_codes(container, "600519")
    container.updates.run(UpdateKind.MANUAL)
    container.classification.dismiss(ids[0])

    container.updates.refresh_security("600519.SH")

    run = container.updates.history(1)[0]
    assert run["quotesPending"] == 0
    assert run["status"] == "success"
    assert container.data_status.incomplete_among(["600519.SH"]) == []


def test_single_refresh_without_snapshot_is_unconfirmed_not_success(tmp_path):
    """没有当日状态快照时判定不可信：单股更新记「状态待确认」，不说成成功。

    快照由整体更新写入；单股补取不采集全市场停牌状态，因此不伪造可信结论。
    """
    source = CountingSource({"600519.SH": [_bar("2026-09-17", 1500.0)]})
    container = _build_with_source(tmp_path, source, trg())
    ids = import_codes(container, "600519")
    container.classification.dismiss(ids[0])  # 直接单股更新，此前没有任何整体更新

    container.updates.refresh_security("600519.SH")

    run = container.updates.history(1)[0]
    assert run["quotesPending"] == 1
    assert run["status"] == "partial"


def test_securities_notice_is_absent_before_any_securities_update(tmp_path):
    """只有定向任务、从未动过证券库时如实说「尚未更新」，不假称失败或已更新。"""
    source = CountingSource({"000001.SZ": [_bar("2026-09-18", 11.0)]})
    container = _build_with_source(tmp_path, source, trg())
    import_codes(container, "000001")
    container.updates.refresh_security("000001.SZ")

    result = container.data_status.status()
    item = {i.key: i for i in result.items}["securities"]
    assert (item.state, item.message) == ("unknown", "尚未更新")
    assert result.securities_run is None


def test_reminder_appears_with_actual_target_date_and_clears_when_complete(tmp_path):
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]}}
    container = build(tmp_path, quotes, trg())
    import_codes(container, "000001", "600519")
    container.updates.run(UpdateKind.MANUAL)

    result = container.data_status.status()
    assert result.reminder == "截至最近交易日（09-18），数据尚未更新完整！"
    assert result.complete is False

    # 缺的那只补齐后提醒解除（按实际完整性重新判断）
    _write(tmp_path, "quotes_fixture.json", {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)], "600519.SH": [_bar("2026-09-18", 1500.0)]}})
    container.updates.run(UpdateKind.MANUAL)
    assert container.data_status.status().reminder is None


# --- 重启读回 ---


def test_snapshot_and_stock_results_survive_restart(tmp_path):
    quotes = {
        "bars": {
            "000001.SZ": [_bar("2026-09-18", 11.0)],
            "600519.SH": [_bar("2026-09-17", 1500.0)],
        }
    }
    status = trg(
        suspensions=[
            {"code": "600519", "exchange": "SH", "name": "贵州茅台", "kind": "continuous", "start": "2026-09-18"}
        ]
    )
    settings = make_settings(tmp_path, quotes, status)
    first = build_container(settings, FixedClock(datetime(2026, 9, 18, 17, 0, 0)))
    import_codes(first, "000001", "600519")
    first.updates.run(UpdateKind.MANUAL)
    first_record = first.updates.history(1)[0]

    # 重启：同一数据目录重建容器
    reopened = build_container(settings, FixedClock(datetime(2026, 9, 18, 18, 0, 0)))
    result = reopened.data_status.status()
    assert result.target_trade_date == date(2026, 9, 18)
    assert result.calendar_available
    stocks = {s.security_id: s for s in result.stocks}
    assert stocks["000001.SZ"].state is StockDataState.UPDATED
    assert stocks["600519.SH"].state is StockDataState.SUSPENDED
    assert result.reminder is None
    assert reopened.updates.history(1)[0]["runId"] == first_record["runId"]
    items = reopened.updates.run_items(first_record["runId"])
    assert {i.security_id for i in items} == {"000001.SZ", "600519.SH"}


def test_snapshot_records_source_and_collected_at(tmp_path):
    container = build(tmp_path, {}, trg())
    container.updates.run(UpdateKind.MANUAL)
    with container.db.read() as conn:
        from dailyscreen_lite.repository import market_status_repo

        snapshot = market_status_repo.latest_snapshot(conn)
    assert snapshot is not None
    assert snapshot.source == "fixture.market_status"
    assert snapshot.target_trade_date == date(2026, 9, 18)
    assert snapshot.covered_markets == ("SH", "SZ")
    assert snapshot.uncovered_markets == ("BJ",)
    assert snapshot.collected_at.tzinfo is not None


def test_stale_snapshot_does_not_exempt_suspension(tmp_path):
    """旧快照的停牌区间不得用来豁免新的目标交易日（不拿昨天的状态推断今天）。"""
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]}}
    clock = FixedClock(datetime(2026, 9, 18, 17, 0, 0))
    container = build(
        tmp_path,
        quotes,
        trg(
            ["2026-09-17", "2026-09-18", "2026-09-21"],
            suspensions=[
                {
                    "code": "000001",
                    "exchange": "SZ",
                    "kind": "continuous",
                    "start": "2026-09-17",
                    "end": None,
                    "market": "深交所主板",
                }
            ],
        ),
        clock=clock,
    )
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)
    assert status_of(container, "000001.SZ") is StockDataState.UPDATED

    # 第二天状态来源失败：09-17 起未设截止的停牌区间仍在旧快照里，
    # 但它不能用来豁免 09-21，该股按「未补齐，状态待确认」处理
    clock.set(datetime(2026, 9, 21, 17, 0, 0))
    _write(tmp_path, "status_fixture.json", {"error": "状态获取失败（夹具注入）"})
    container.updates.run(UpdateKind.MANUAL)
    status = container.data_status.status()
    assert status.target_trade_date == date(2026, 9, 21)
    assert status_of(container, "000001.SZ") is StockDataState.UNCONFIRMED


def test_suspension_does_not_hide_gap_before_it(tmp_path):
    """停牌豁免对应的日期，但不掩盖停牌之前的行情缺口。"""
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.0)]}}
    status = trg(
        ["2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18"],
        suspensions=[
            {
                "code": "000001",
                "exchange": "SZ",
                "kind": "continuous",
                "start": "2026-09-17",
                "end": None,
                "market": "深交所主板",
            }
        ],
    )
    container = build(tmp_path, quotes, status)
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)

    # 停牌前一交易日（09-16）应当有日线；最后行情停在 09-10 → 缺口不被停牌掩盖
    stock = {s.security_id: s for s in container.data_status.status().stocks}["000001.SZ"]
    assert stock.state is StockDataState.PENDING
    assert "停牌前行情缺失" in (stock.reason or "")
    assert not container.data_status.status().complete


def test_suspension_exempts_when_no_gap_before_it(tmp_path):
    """停牌前一天行情齐全时，停牌当日正常豁免。"""
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-16", 11.0)]}}
    status = trg(
        ["2026-09-16", "2026-09-17", "2026-09-18"],
        suspensions=[
            {
                "code": "000001",
                "exchange": "SZ",
                "kind": "continuous",
                "start": "2026-09-17",
                "end": None,
                "market": "深交所主板",
            }
        ],
    )
    container = build(tmp_path, quotes, status)
    import_codes(container, "000001")
    container.updates.run(UpdateKind.MANUAL)
    assert status_of(container, "000001.SZ") is StockDataState.SUSPENDED
    assert container.data_status.status().complete


def test_interrupted_running_record_is_closed_on_startup(tmp_path):
    """进程中断留下的「更新中」必须在启动时收成终态，不出现永久运行中。"""
    quotes = {"bars": {"000001.SZ": [_bar("2026-09-18", 11.0)]}}
    settings = make_settings(tmp_path, quotes, trg())
    first = build_container(settings, FixedClock(datetime(2026, 9, 18, 17, 0, 0)))
    import_codes(first, "000001")
    # 直接写入一条永远不会被收尾的 RUNNING 记录，模拟进程被杀
    from dailyscreen_lite.domain.models import UpdateKind, UpdateRun, UpdateStatus
    from dailyscreen_lite.repository import quotes_repo

    with first.db.transaction() as conn:
        quotes_repo.insert_run(
            conn,
            UpdateRun(
                run_id="update-interrupted",
                kind=UpdateKind.MANUAL,
                status=UpdateStatus.RUNNING,
                started_at=FixedClock(datetime(2026, 9, 18, 16, 31, 0)).now(),
                finished_at=None,
                securities_status=None,
                securities_message=None,
                securities_count=0,
                quotes_ok=0,
                quotes_failed=0,
            ),
        )

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 18, 18, 0, 0)))
    latest = reopened.updates.history(1)[0]
    assert latest["runId"] == "update-interrupted"
    assert latest["status"] == "failed"
    assert latest["finishedAt"] is not None
    assert "中断" in (latest["securitiesMessage"] or "")
    item = {i.key: i for i in reopened.data_status.status().items}["quotes"]
    assert item.state != "running"
