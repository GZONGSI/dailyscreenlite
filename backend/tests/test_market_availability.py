"""分市场失败保旧，通过应用服务、真实 SQLite 与重新装配读回验证。"""
import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

import pytest

from conftest import REAL_SNAPSHOT
from test_data_status import build, import_codes, trg, _write
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.domain.models import UpdateKind
from dailyscreen_lite.securities import load_snapshot


def market_rows():
    return [asdict(s) for s in load_snapshot(REAL_SNAPSHOT).securities]


def test_market_failure_preserves_identity_while_other_markets_refresh(tmp_path):
    rows = market_rows()
    sz = [s for s in rows if s["exchange"] == "SZ"]
    next(s for s in sz if s["code"] == "000001")["name"] = "深市更新名称"
    payload = {"security_markets": {
        "SH": {"error": "沪市超时"},
        "SZ": {"securities": sz},
        "BJ": {"securities": []},
    }}
    app = build(tmp_path, payload, trg())
    app.updates.run(UpdateKind.MANUAL)
    restarted = build_container(app.settings, app.clock)
    import_codes(restarted, "000001", "600519", "920001", "999999")
    candidates = {v.candidate.security_id: v for v in restarted.classification.list_candidates()}
    assert set(candidates) == {"000001.SZ", "600519.SH", "920001.BJ"}
    items = {i.key: i for i in restarted.data_status.status().items}
    assert items["securities_SZ"].state == "ok"
    assert items["securities_SH"].state == "failed"
    assert items["securities_BJ"].state == "failed"
    assert "沪市超时" in items["securities_SH"].message
    assert candidates["000001.SZ"].security.name == "深市更新名称"


def test_calendar_updates_despite_status_failure_and_survives_failed_refresh(tmp_path):
    from dailyscreen_lite.domain.clock import FixedClock
    # 独立日历明确 09-18 休市；不能用工作日猜测或失败状态覆盖它。
    rows = [{"jyrq": f"2026-09-{day:02}", "jybz": "1" if day == 17 else "0"} for day in range(1, 31)]
    payload = {"calendar_months": {"2026-09": {"data": rows}}, "error": "停牌来源离线"}
    app = build(tmp_path, {}, payload)
    app.updates.run(UpdateKind.MANUAL)
    assert app.data_status.target_trade_date() == (date(2026, 9, 17), "calendar", True)
    _write(tmp_path, "status_fixture.json", {"calendar_months": {}, "error": "仍离线"})
    app.updates.run(UpdateKind.MANUAL)
    restarted = build_container(app.settings, app.clock)
    assert restarted.data_status.target_trade_date() == (date(2026, 9, 17), "calendar", True)
    items = {i.key: i for i in restarted.data_status.status().items}
    assert items["calendar"].state == "failed"
    assert items["market_status"].state == "failed"
    # 跨月旧日历不能冻结目标至九月。
    future = build_container(app.settings, FixedClock(datetime(2026, 10, 12, 17)))
    assert future.data_status.target_trade_date() == (date(2026, 10, 12), "weekday_fallback", False)


def captured_calendar():
    return json.loads((Path(__file__).parent / "fixtures/szse-calendar-2026-09.json").read_text())


@pytest.mark.parametrize("damage", ["missing", "duplicate", "wrong_month", "flag", "unpublished", "error"])
def test_szse_adapter_rejects_incomplete_month(damage):
    from dailyscreen_lite.quotes.calendar_source import SzseCalendarSource, CalendarError
    payload = captured_calendar()
    if damage == "missing":
        payload["data"].pop()
    elif damage == "duplicate":
        payload["data"][-1] = payload["data"][0]
    elif damage == "wrong_month":
        payload["data"][0]["jyrq"] = "2026-08-01"
    elif damage == "flag":
        payload["data"][0]["jybz"] = "unknown"
    elif damage == "unpublished":
        payload["data"] = []
    def transport(year, month):
        if damage == "error":
            raise TimeoutError("timeout")
        return payload
    with pytest.raises(CalendarError):
        SzseCalendarSource(transport).month(2026, 9)


@pytest.mark.parametrize(("now", "expected"), [
    (datetime(2026, 9, 22, 16, 29), date(2026, 9, 21)),
    (datetime(2026, 9, 22, 16, 30), date(2026, 9, 22)),
    (datetime(2026, 9, 20, 20), date(2026, 9, 18)),
    (datetime(2026, 9, 25, 20), date(2026, 9, 24)),
])
def test_captured_complete_calendar_controls_target(tmp_path, now, expected):
    from dailyscreen_lite.domain.clock import FixedClock
    app = build(tmp_path, {}, {"calendar_months": {"2026-09": captured_calendar()}}, clock=FixedClock(now))
    app.updates.run(UpdateKind.MANUAL)
    assert app.data_status.target_trade_date() == (expected, "calendar", True)


@pytest.mark.parametrize("damage", ["missing", "duplicate", "identity", "empty_name", "date"])
def test_partial_market_payload_cannot_replace_old_identity(tmp_path, damage):
    rows = [r for r in market_rows() if r["exchange"] == "SZ"]
    if damage == "missing":
        rows = [r for r in rows if r["code"] != "000001"]
    elif damage == "duplicate":
        rows.append(rows[0])
    elif damage == "identity":
        rows[0]["exchange"] = "SH"
    elif damage == "empty_name":
        rows[0]["name"] = ""
    elif damage == "date":
        rows[0]["listing_date"] = "not-a-date"
    app = build(tmp_path, {"security_markets": {"SZ": {"securities": rows}}}, trg())
    app.updates.run(UpdateKind.MANUAL)
    restarted = build_container(app.settings, app.clock)
    import_codes(restarted, "000001")
    assert restarted.classification.list_candidates()[0].security.name == "平安银行"
    assert {i.key: i for i in restarted.data_status.status().items}["securities_SZ"].state == "failed"


def test_repeated_status_failures_never_prune_last_success(tmp_path):
    from dailyscreen_lite.domain.clock import FixedClock
    app = build(tmp_path, {}, trg())
    app.updates.run(UpdateKind.MANUAL)
    _write(tmp_path, "status_fixture.json", {"error": "offline"})
    for _ in range(32):
        app.updates.run(UpdateKind.MANUAL)
    restarted = build_container(app.settings, FixedClock(datetime(2026, 9, 21, 17)))
    snapshot = restarted.data_status.status().snapshot
    assert snapshot is not None
    assert snapshot.target_trade_date == date(2026, 9, 18)


def test_confirmed_delisting_does_not_freeze_future_market_updates(tmp_path):
    rows = [r for r in market_rows() if r["exchange"] == "SZ" and r["code"] != "000001"]
    rows.append({"code": "001999", "exchange": "SZ", "name": "新上市测试", "board": "main"})
    app = build(tmp_path, {"security_markets": {"SZ": {
        "securities": rows, "delistings": [{"code": "000001", "date": "2026-09-17"}],
    }}}, trg())
    app.updates.run(UpdateKind.MANUAL)
    restarted = build_container(app.settings, app.clock)
    import_codes(restarted, "001999")
    assert restarted.classification.list_candidates()[0].security.name == "新上市测试"
    assert {i.key: i for i in restarted.data_status.status().items}["securities_SZ"].state == "ok"


def test_legacy_calendar_without_month_coverage_is_not_trusted(tmp_path):
    from dailyscreen_lite.domain.clock import FixedClock
    from dailyscreen_lite.repository import market_status_repo
    app = build(tmp_path, {}, trg())
    # 模拟升级前的真实缓存（旧表没有自然月覆盖声明）。
    with app.db.transaction() as conn:
        market_status_repo.replace_trade_dates(conn, (date(2026, 9, 18),),
            source="akshare.tool_trade_date_hist_sina", updated_at="2026-09-18T17:00:00+08:00")
    restarted = build_container(app.settings, FixedClock(datetime(2026, 9, 22, 17)))
    assert restarted.data_status.target_trade_date() == (date(2026, 9, 22), "weekday_fallback", False)
