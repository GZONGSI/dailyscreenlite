"""工单 06：AKShare 行情接入、数据更新与日线图。

真实临时 SQLite + 真实证券库快照 + 固定时钟；行情走注入的夹具来源（与 AKShare
同一 QuotesSource 边界）。覆盖：新导入后台补取、整段前复权口径一致、失败保留旧
数据、部分成功分别报告、防并发、按 16:30 与启动补更的调度判断、重启读回，以及
来源附带假行情不替代自有行情。真实 AKShare 实源结论单列在 Comments。
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
    BatchStatus,
    StockDataState,
    UpdateKind,
    UpdateStatus,
)
from dailyscreen_lite.quotes import scheduler as scheduler_mod
from dailyscreen_lite.quotes.source import (
    FixtureQuotesSource,
    QuoteSourceError,
    ProviderBar,
    ProviderSecurity,
)
from dailyscreen_lite.quotes.update import UpdateBusy, UpdateService
from dailyscreen_lite.settings import Settings


def _bar(day: str, close: float, *, volume: float = 1000.0, amount: float = 1.0e7) -> dict:
    return {
        "date": day,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume_lots": volume,
        "amount_yuan": amount,
    }


def write_fixture(tmp_path: Path, payload: dict, name: str = "quotes_fixture.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


# 默认状态夹具：交易日历覆盖用例里的两个日期，沪深已覆盖，北交所未知。
# 有了它，用例既不打真实来源，也能确定地判定「行情是否补齐」。
DEFAULT_STATUS = {
    "trade_dates": ["2026-09-10", "2026-09-11"],
    "covered_markets": ["SH", "SZ"],
    "uncovered_markets": ["BJ"],
}


def make_settings(
    tmp_path: Path, fixture: dict | None = None, status: dict | None = None
) -> Settings:
    """默认不启用调度，并注入离线状态夹具，避免任何用例落到真实来源。"""
    return Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=REAL_SNAPSHOT,
        quotes_fixture=write_fixture(tmp_path, fixture or {}),
        market_status_fixture=write_fixture(
            tmp_path, status if status is not None else DEFAULT_STATUS, "status_fixture.json"
        ),
        update_schedule_enabled=False,
    )


def build(tmp_path: Path, fixture: dict, *, status: dict | None = None, clock=None) -> Container:
    settings = make_settings(tmp_path, fixture, status)
    return build_container(settings, clock or FixedClock(datetime(2026, 9, 11, 17, 0, 0)))


def import_codes(container: Container, *codes: str) -> None:
    """导入并把「后台补取」改为同步执行。

    生产里补取跑在后台线程（导入回调在事务提交后触发），测试关心的是补取结果
    以及随后的手动更新能否拿到更新锁。同步执行让「导入后补取已完成」成为确定
    事实，不再赌后台线程是否已释放更新锁（此前会偶发 UpdateBusy）。
    后台线程接入本身由 test_data_status 的钩子用例覆盖。
    """
    container.imports._published_hook = lambda ids: container.updates.refresh_quotes(ids)
    container.imports.submit_file("a.csv", make_csv("代码", *codes))


def test_repeated_provider_connection_errors_stop_this_round_early(tmp_path):
    """同一来源连续断连时不让百只股票各自等待网络超时。"""
    container = build(tmp_path, {})

    class DisconnectedSource:
        source_id = "baostock"
        daily_markets = ("SH", "SZ")

        def __init__(self):
            self.calls = 0

        def declared_adjust(self):
            return "qfq"

        def daily_bars(self, **_kwargs):
            self.calls += 1
            raise QuoteSourceError("BaoStock 10002007: 网络接收错误。")

    source = DisconnectedSource()
    container.updates._daily_sources = [source]
    ids = [f"{code}.SZ" for code in ("000001", "000002", "000333", "000651", "000858")]

    results = container.updates._update_quotes(ids)

    assert source.calls == 3
    assert len(results) == len(ids)
    assert all(item.status.value == "failed" for item in results)
    assert "本轮未请求" in results[-1].message


def test_stock_specific_errors_do_not_stop_other_stocks(tmp_path):
    container = build(tmp_path, {})

    class StockSpecificFailure:
        source_id = "baostock"
        daily_markets = ("SH", "SZ")

        def __init__(self):
            self.calls = 0

        def declared_adjust(self):
            return "qfq"

        def daily_bars(self, **_kwargs):
            self.calls += 1
            raise QuoteSourceError("证券数据格式错误")

    source = StockSpecificFailure()
    container.updates._daily_sources = [source]
    ids = [f"{code}.SZ" for code in ("000001", "000002", "000333", "000651", "000858")]

    results = container.updates._update_quotes(ids)

    assert source.calls == len(ids)
    assert len(results) == len(ids)


def test_disconnected_primary_is_skipped_while_backup_continues(tmp_path):
    container = build(tmp_path, {})

    class Primary:
        source_id = "tencent"
        daily_markets = ("SH", "SZ")

        def __init__(self):
            self.calls = 0

        def declared_adjust(self):
            return "qfq"

        def daily_bars(self, **_kwargs):
            self.calls += 1
            raise QuoteSourceError("ConnectionError: connection closed")

    class Backup:
        source_id = "sina"
        daily_markets = ("SH", "SZ")

        def __init__(self):
            self.calls = 0

        def declared_adjust(self):
            return "qfq"

        def daily_bars(self, **_kwargs):
            self.calls += 1
            return [ProviderBar(date(2026, 9, 11), 10, 11, 9, 10.5, 1000, None)]

    primary, backup = Primary(), Backup()
    container.updates._daily_sources = [primary, backup]
    ids = [f"{code}.SZ" for code in ("000001", "000002", "000333", "000651", "000858")]

    results = container.updates._update_quotes(ids)

    assert primary.calls == 3
    assert backup.calls == len(ids)
    assert all(item.status.value == "ok" for item in results)
    assert container.quotes.view(ids[-1]).source == "sina"


# --- 新导入补取与序列读取 ---


def test_import_backfills_quotes_and_card_shows_latest_date(tmp_path):
    fixture = {
        "bars": {
            "000001.SZ": [
                _bar("2019-01-02", 6.21),
                _bar("2026-09-10", 11.85, volume=867632, amount=1.022544e9),
            ]
        }
    }
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    # 导入触发的补取在本用例里同步完成；等待条件仍写成有界轮询，不赌时序
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    view = container.quotes.view("000001.SZ")
    assert view.available
    assert view.latest_date == "2026-09-10"
    assert view.adjust == "qfq"
    assert view.source == "fixture"
    # 卡片与研究项不内联行情；行情经 /api/quotes 读取（此处用服务读取同一事实）
    assert container.quotes.view("000001.SZ").latest_date == "2026-09-10"


def test_volume_units_and_price_are_preserved(tmp_path):
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85, volume=867632, amount=1.022544e9)]}}
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    bar = container.quotes.view("000001.SZ").bars[-1]
    assert bar.close == 11.85
    # 成交量单位为手，成交额为元
    assert bar.volume_lots == 867632
    assert bar.amount_yuan == pytest.approx(1.022544e9)


# --- 前复权整段口径一致 ---


def test_full_history_refetch_does_not_mix_old_adjust(tmp_path):
    """除权后整段重取：旧口径的旧价不得残留。"""
    container = build(tmp_path, {
        "bars": {"000001.SZ": [_bar("2024-01-02", 10.0), _bar("2026-09-10", 12.0)]}
    })
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [10.0, 12.0]

    # 再次整段替换（模拟除权后历史被重算）：不追加、不残留旧价
    from dailyscreen_lite.domain.models import DailyBar
    from dailyscreen_lite.repository import Database, quotes_repo

    def bar(date_str: str, close: float) -> DailyBar:
        return DailyBar(
            security_id="000001.SZ",
            trade_date=date.fromisoformat(date_str),
            open=close,
            high=close,
            low=close,
            close=close,
            volume_lots=1000.0,
            amount_yuan=1.0e7,
        )

    with Database(container.settings.database_path).transaction() as conn:
        quotes_repo.replace_series(
            conn,
            security_id="000001.SZ",
            adjust="qfq",
            source="fixture",
            fetched_at=container.clock.now().isoformat(),
            bars=[bar("2024-01-02", 5.0), bar("2026-09-10", 12.0)],
        )

    bars = container.quotes.view("000001.SZ").bars
    # 历史低点随新口径更新（5.0），不再保留旧口径的 10.0
    assert [b.close for b in bars] == [5.0, 12.0]
    assert len(bars) == 2


# --- 失败保留旧数据、部分成功分别报告 ---


def test_failure_keeps_last_good_data_and_reports(tmp_path):
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85)]}}
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    # 注入该股票日线失败：更新失败不得清掉既有行情
    container.settings  # noqa: B018 - 保持结构清晰
    failing = UpdateService(
        container.db,
        container.clock,
        FixtureQuotesSource(write_fixture(tmp_path, {
            "bars": {"000001.SZ": [_bar("2026-09-10", 99.0)]},
            "fail_bars": ["000001.SZ"],
        })),
    )
    report = failing.run(UpdateKind.MANUAL)

    assert report.status is UpdateStatus.FAILED
    assert report.failed_securities == ("000001.SZ",)
    # 旧数据原样保留，未被失败清空
    assert container.quotes.view("000001.SZ").bars[-1].close == 11.85
    # 失败仍不阻止研究
    assert container.classification.list_candidates()


def test_no_data_is_skipped_not_failed(tmp_path):
    """来源成功但无该股票数据（新上市/停牌）：算「无数据」，不算失败，也不算已补齐。"""
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85)]}}
    status = {"trade_dates": ["2026-09-10", "2026-09-11"], "covered_markets": ["SH", "SZ"]}
    container = build(tmp_path, fixture, status=status)
    import_codes(container, "000001", "600519")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    # 600519 在夹具中无 bars：既非成功也非失败
    report = container.updates.run(UpdateKind.MANUAL)
    assert report.quotes_ok == 1
    assert report.quotes_failed == 0
    assert report.quotes_skipped == 1
    # 目标交易日 09-11 的行情确实没补齐：整次更新只能是「部分完成」，不是全部成功
    assert report.quotes_pending == 2
    assert report.status is UpdateStatus.PARTIAL
    stocks = {s.security_id: s for s in container.data_status.status().stocks}
    assert stocks["600519.SH"].state is StockDataState.PENDING


def test_import_backfill_does_not_suppress_daily_update(tmp_path):
    """定向补取（import）不代表整体更新完成，不得抑制 16:30/启动补更。"""
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85)]}}
    settings = make_settings(tmp_path, fixture)
    settings = Settings(
        data_dir=settings.data_dir,
        securities_snapshot=REAL_SNAPSHOT,
        quotes_fixture=settings.quotes_fixture,
        market_status_fixture=settings.market_status_fixture,
        update_schedule_enabled=True,
    )
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 16, 31, 0)))
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)
    # 导入触发的补取已写入一条 import 记录
    assert any(r["kind"] == "import" for r in container.updates.history(10))

    # 调度仍认为需要整体更新：import 记录不抑制补更
    assert container.scheduler.tick() is True


def test_partial_success_reported_separately(tmp_path):
    fixture = {
        "bars": {
            "000001.SZ": [_bar("2026-09-10", 11.85)],
            "600519.SH": [_bar("2026-09-10", 1500.0)],
        }
    }
    container = build(tmp_path, fixture)
    import_codes(container, "000001", "600519")
    _wait_for(lambda: container.quotes.view("600519.SH").available)

    partial = UpdateService(
        container.db,
        container.clock,
        FixtureQuotesSource(write_fixture(tmp_path, {
            "bars": {"600519.SH": [_bar("2026-09-10", 1500.0), _bar("2026-09-11", 1520.0)]},
            "fail_bars": ["000001.SZ"],
        })),
    )
    report = partial.run(UpdateKind.MANUAL)

    assert report.status is UpdateStatus.PARTIAL
    assert report.quotes_ok == 1
    assert report.failed_securities == ("000001.SZ",)
    # 成功部分写入，失败部分保留旧值
    assert container.quotes.view("600519.SH").latest_date == "2026-09-11"
    assert container.quotes.view("000001.SZ").latest_date == "2026-09-10"


def test_securities_failure_keeps_old_universe(tmp_path):
    container = build(tmp_path, {"securities_error": "交易所名单获取失败（夹具注入）"})
    before = container.securities_count
    report = container.updates.run(UpdateKind.MANUAL)
    assert report.securities_status == "failed"
    # 名单获取失败保留旧库，导入仍可识别
    assert container.securities_count == before
    import_codes(container, "000001")
    assert container.classification.list_candidates()


# --- 来源附带假行情不替代自有行情 ---


def test_source_attached_price_does_not_override_own_quotes(tmp_path):
    """导入携带的假价格只作存档，工作台行情仍取自有来源。"""
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85)]}}
    container = build(tmp_path, fixture)
    # CSV 附带一列明显矛盾的价格与来源日期
    container.imports.submit_file(
        "a.csv", make_csv("代码,名称,最新价,日期", "000001,平安银行,999.99,2020-01-01")
    )
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    bar = container.quotes.view("000001.SZ").bars[-1]
    assert bar.close == 11.85  # 自有行情
    assert bar.trade_date.isoformat() == "2026-09-10"
    # 假价格保留在只读存档里，可查看但不参与行情
    batch = container.imports.list_batches()[0]
    detail = container.imports.get_batch(batch.batch_id)
    stored = next(s for s in detail.stocks if s.normalized_code == "000001")
    assert stored.raw_extras.get("最新价") == "999.99"


# --- 防并发 ---


def test_concurrent_update_is_rejected_not_duplicated(tmp_path):
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85)]}}
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    service = container.updates
    # 模拟另一路（手动/定时）已占用更新：直接置位内部锁
    service._lock.acquire()  # noqa: SLF001 - 测试并发分支
    try:
        with pytest.raises(UpdateBusy):
            service.run(UpdateKind.SCHEDULED)
    finally:
        service._lock.release()  # noqa: SLF001

    # 释放后仍可正常更新，且更新记录不因并发尝试而重复写入
    before = len(container.updates.history(50))
    service.run(UpdateKind.MANUAL)
    assert len(container.updates.history(50)) == before + 1


# --- 调度：16:30 与启动补更 ---


def test_scheduler_triggers_after_1630_and_skips_when_done(tmp_path):
    settings = make_settings(tmp_path, {})
    settings = Settings(
        data_dir=settings.data_dir,
        securities_snapshot=REAL_SNAPSHOT,
        quotes_fixture=settings.quotes_fixture,
        market_status_fixture=settings.market_status_fixture,
        update_schedule_enabled=True,
    )
    container = build_container(
        settings, FixedClock(datetime(2026, 9, 11, 16, 31, 0))
    )
    assert container.scheduler.tick() is True  # 首次（启动补更）
    last = container.updates.last_completed()
    assert last is not None
    # 同一天再次判断：已完成，不重复执行
    assert container.scheduler.tick() is False


def test_automatic_slot_boundaries():
    for hour, minute, startup, expected in [
        (9, 0, True, 0), (9, 0, False, None),
        (16, 29, False, None), (16, 30, False, 1),
        (17, 29, False, 1), (17, 30, False, 2),
        (22, 30, False, 7), (22, 31, True, None), (23, 0, True, None),
    ]:
        now = datetime(2026, 9, 18, hour, minute, tzinfo=scheduler_mod.BEIJING)
        assert scheduler_mod.automatic_slot(now, startup=startup) == expected

# --- 向前浏览与重启读回 ---


def test_history_browsing_and_restart_readback(tmp_path):
    bars = [_bar(f"2026-08-{d:02d}", 10.0 + d) for d in range(1, 21)]
    fixture = {"bars": {"000001.SZ": bars}}
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    # 默认取最近 N 条
    page = container.quotes.view("000001.SZ", limit=5)
    assert [b.trade_date.isoformat() for b in page.bars] == [
        "2026-08-16", "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20",
    ]
    # 用 end 向前翻页浏览更早历史
    older = container.quotes.view("000001.SZ", limit=5, end="2026-08-15")
    assert older.bars[-1].trade_date.isoformat() == "2026-08-15"
    assert older.total == 20  # 总量仍是全序列

    # 重启后行情与更新记录都可读回
    reopened = build_container(
        Settings(
            data_dir=container.settings.data_dir,
            securities_snapshot=REAL_SNAPSHOT,
            quotes_fixture=container.settings.quotes_fixture,
            update_schedule_enabled=False,
        ),
        FixedClock(datetime(2026, 9, 12, 9, 0, 0)),
    )
    assert reopened.quotes.view("000001.SZ").latest_date == "2026-08-20"
    assert reopened.updates.status()["lastRun"] is not None


# --- HTTP 边界 ---


def test_incremental_quote_pages_share_whole_series_version_after_restart(tmp_path):
    """旧历史与增量新段的采集时间不同，同一条当前曲线的分页版本必须一致。"""
    from fastapi.testclient import TestClient
    from dailyscreen_lite.app.main import create_app
    from dailyscreen_lite.domain.models import DailyBar
    from dailyscreen_lite.repository import Database, quotes_repo

    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 17))
    old_stamp = "2026-09-10T17:00:00+08:00"
    new_stamp = "2026-09-11T17:00:00+08:00"
    def bar(day, close):
        return DailyBar("000001.SZ", date.fromisoformat(day), close, close, close, close, 1000, None)

    with TestClient(create_app(settings, clock)) as client:
        with Database(settings.database_path).transaction() as conn:
            quotes_repo.replace_series(conn, security_id="000001.SZ", adjust="qfq", source="fixture",
                fetched_at=old_stamp, bars=[bar("2026-09-07", 10), bar("2026-09-08", 11), bar("2026-09-09", 12)])
            quotes_repo.merge_incremental_series(conn, security_id="000001.SZ", adjust="qfq", source="fixture",
                fetched_at=new_stamp, bars=[bar("2026-09-09", 12), bar("2026-09-10", 13)])
        current = client.get("/api/quotes/000001.SZ", params={"limit": 2}).json()
        older = client.get("/api/quotes/000001.SZ", params={"limit": 2, "end": "2026-09-08"}).json()
        assert [b["close"] for b in older["bars"]] == [10, 11]
        assert current["fetchedAt"] == older["fetchedAt"] == new_stamp
        with Database(settings.database_path).transaction() as conn:
            quotes_repo.merge_incremental_series(conn, security_id="000001.SZ", adjust="qfq", source="fixture",
                fetched_at="2026-09-12T17:00:00+08:00", bars=[bar("2026-09-10", 13), bar("2026-09-11", 14)])
        updated = client.get("/api/quotes/000001.SZ", params={"limit": 2, "end": "2026-09-08"}).json()
        assert updated["fetchedAt"] == "2026-09-12T17:00:00+08:00"
    with TestClient(create_app(settings, clock)) as reopened:
        assert reopened.get("/api/quotes/000001.SZ", params={"limit": 2}).json()["fetchedAt"] == updated["fetchedAt"]
        assert reopened.get("/api/quotes/000001.SZ", params={"limit": 2, "end": "2026-09-08"}).json()["fetchedAt"] == updated["fetchedAt"]


def test_quote_and_update_endpoints(tmp_path):
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85, volume=867632)]}}
    settings = make_settings(tmp_path, fixture)
    from fastapi.testclient import TestClient

    from dailyscreen_lite.app.main import create_app

    with TestClient(create_app(settings, FixedClock(datetime(2026, 9, 11, 17, 0, 0)))) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        )
        # 后台补取可能尚未完成：等待 /api/quotes 可用
        _wait_for(lambda: client.get("/api/quotes/000001.SZ").json()["available"])
        # 补取会短暂持有更新锁（还要落记录与终态），等它结束再触发手动更新，
        # 否则接口按「更新正在进行中」拒绝，用例会拿到 409 而不是更新结果
        _wait_for(
            lambda: any(
                run["kind"] == "import"
                for run in client.get("/api/updates").json()["history"]
            )
        )
        _wait_for(lambda: client.get("/api/updates").json()["running"] is False)

        quote = client.get("/api/quotes/000001.SZ").json()
        assert quote["adjust"] == "qfq"
        assert quote["latestDate"] == "2026-09-10"
        assert quote["bars"][-1]["close"] == 11.85
        assert quote["bars"][-1]["volumeLots"] == 867632

        updated = client.post("/api/updates").json()
        assert updated["status"] in {"success", "partial"}
        status = client.get("/api/updates").json()
        assert status["lastRun"]["status"] in {"success", "partial"}


# --- A25：历史样本经同一行情边界加载 ---


REPLAY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "quote_replay_sample.json"


@pytest.mark.skipif(
    not REPLAY_FIXTURE.exists(),
    reason="未生成历史回放夹具（tools/build_quote_fixture.py）",
)
def test_authorized_history_replay_through_quote_boundary(tmp_path):
    """授权历史库样本经正常行情接入边界载入临时库，页面可核对价格/量/日期。

    夹具由 backend/tools/build_quote_fixture.py 只读抽取生成，记录来源与样本指纹；
    样本口径为通达信原始日线（不复权），故如实标注为「不复权」而非前复权。
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=REAL_SNAPSHOT,
        quotes_fixture=REPLAY_FIXTURE,
        market_status_fixture=write_fixture(tmp_path, DEFAULT_STATUS, "status_fixture.json"),
        update_schedule_enabled=False,
    )
    container = build_container(settings, FixedClock(datetime(2026, 9, 12, 9, 0, 0)))
    # 000001.SZ 在该历史样本中
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    _wait_for(lambda: container.quotes.view("000001.SZ", limit=5).available)

    # 服务按来源声明的口径读取（不复权），不误标前复权
    view = container.quotes.view("000001.SZ", limit=5)
    assert view.available
    assert view.adjust == "raw"
    assert view.adjust_label == "不复权"
    assert view.source == "tdx.hsjdy" or view.source == "tdx.hsjday"
    # 真实价格与成交量：2026-09-03 收 11.88，量 1105134.39 手（由股换算）
    last = view.bars[-1]
    assert last.trade_date.isoformat() == "2026-09-03"
    assert last.close == pytest.approx(11.88, abs=0.001)
    assert last.volume_lots == pytest.approx(1105134.39, rel=1e-4)
    # 默认读取最近三年（保留真实样本原有的日期与价格）
    assert view.total >= 700
    full = container.quotes.view("000001.SZ", limit=5000)
    assert "2023-09-11" <= full.earliest_date <= "2023-09-15"

    # 重启后保持一致（样本已落临时库）
    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    again = reopened.quotes.view("000001.SZ", limit=5)
    assert again.bars[-1].close == pytest.approx(11.88, abs=0.001)
    assert again.adjust == "raw"


def test_history_replay_source_loads_through_quote_boundary(tmp_path):
    """历史库只读回放：样本经正常行情接入写入临时库，页面可核对。

    直接使用 FixtureQuotesSource 表达历史样本（真实抽取见 tools 脚本）；
    这里验证「历史样本 → 同一边界 → 库 → 视图」而非绕过被测流程。
    """
    historical = {
        "bars": {
            "000001.SZ": [
                _bar("2026-09-01", 11.92, volume=1523164, amount=1.8072544e9),
                _bar("2026-09-02", 11.91, volume=892247, amount=1.063261824e9),
                _bar("2026-09-03", 11.88, volume=1105134, amount=1.324230272e9),
            ]
        }
    }
    container = build(tmp_path, historical)
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    view = container.quotes.view("000001.SZ")
    assert [b.trade_date.isoformat() for b in view.bars] == [
        "2026-09-01", "2026-09-02", "2026-09-03",
    ]
    assert view.bars[-1].close == 11.88
    assert view.bars[-1].volume_lots == 1105134
    assert view.latest_date == "2026-09-03"


def _wait_for(predicate, *, timeout: float = 10.0, interval: float = 0.05) -> None:
    """有界轮询后台结果，不用固定 sleep 猜测时长。"""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("等待条件超时")


def _write_snapshot(tmp_path: Path, rows: list[dict]) -> Path:
    path = tmp_path / "custom_snapshot.json"
    path.write_text(
        json.dumps(
            {
                "securities": rows,
                "record_count": len(rows),
                "identity_fingerprint": "test-fingerprint",
                "source": {"batch_id": "test-snapshot", "effective_trade_date": "2026-08-28"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_reidentify_backfills_quotes_for_newly_recognized(tmp_path):
    """用户触发的重新识别补入的股票，也要立即后台补行情（与首次导入同一条链路）。

    否则证券库更新后补卡的股票会一直显示「行情暂未取得」，必须等下一次整体更新。
    """
    fixture = {
        "securities": [
            {"code": "000002", "exchange": "SZ", "board": "main", "name": "万科A"}
        ],
        "bars": {"000002.SZ": [_bar("2026-09-10", 7.51)]},
    }
    settings = Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=_write_snapshot(
            tmp_path,
            [
                {
                    "security_id": "000001.SZ",
                    "code": "000001",
                    "exchange": "SZ",
                    "board": "main",
                    "name": "平安银行",
                    "listing_date": None,
                    "is_st": False,
                }
            ],
        ),
        quotes_fixture=write_fixture(tmp_path, fixture),
        market_status_fixture=write_fixture(tmp_path, DEFAULT_STATUS, "status_fixture.json"),
        update_schedule_enabled=False,
    )
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 17, 0, 0)))
    batch = container.imports.submit_file("a.csv", make_csv("代码", "000002"))
    assert batch.status is BatchStatus.ALL_UNKNOWN  # 当前库中没有 000002

    # 用户触发证券库更新（名单已含 000002），再重新识别原批次跳过项
    container.updates.run(UpdateKind.MANUAL)
    after = container.imports.reidentify(batch.batch_id)
    assert after.status is BatchStatus.PUBLISHED
    assert after.reidentified_imported == 1

    # 补入的股票立即补行情，不等下一次整体更新
    _wait_for(lambda: container.quotes.view("000002.SZ").available)
    assert container.quotes.view("000002.SZ").latest_date == "2026-09-10"


def test_securities_update_survives_restart(tmp_path):
    """应用内更新过的证券库重启后不被交付快照覆盖。

    否则更新后能识别的新股票，重启又变回未识别，且当天更新记录会阻止再次补更。
    """
    initial = [
        {
            "security_id": "000001.SZ",
            "code": "000001",
            "exchange": "SZ",
            "board": "main",
            "name": "平安银行",
            "listing_date": None,
            "is_st": False,
        }
    ]
    # 更新来源名单改为含 000002（整名单替换）
    fixture = {
        "securities": [{"code": "000002", "exchange": "SZ", "board": "main", "name": "万科A"}],
        "bars": {"000002.SZ": [_bar("2026-09-10", 7.51)]},
    }
    settings = Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=_write_snapshot(tmp_path, initial),
        quotes_fixture=write_fixture(tmp_path, fixture),
        market_status_fixture=write_fixture(tmp_path, DEFAULT_STATUS, "status_fixture.json"),
        update_schedule_enabled=False,
    )
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 17, 0, 0)))
    assert container.imports.submit_file("a.csv", make_csv("代码", "000002")).status is (
        BatchStatus.ALL_UNKNOWN
    )

    container.updates.run(UpdateKind.MANUAL)
    assert container.securities_count == 1
    published = container.imports.submit_file("b.csv", make_csv("代码", "000002"))
    assert published.status is BatchStatus.PUBLISHED

    # 重启：沿用同一数据目录与同一交付快照路径，更新后的名单必须保留
    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 9, 0, 0)))
    assert reopened.securities_loaded
    assert reopened.securities_count == 1
    again = reopened.imports.submit_file("c.csv", make_csv("代码", "000002"))
    assert again.status is BatchStatus.PUBLISHED
    assert again.recognized_count == 1


def test_backfill_queued_while_lock_held_is_not_dropped(tmp_path):
    """更新锁被占用时，新导入的补取请求排队，锁释放后串行完成，不丢股票。

    否则连续导入 A、B 时，只有先到的 A 拿到行情，B 永久留在无行情状态。
    """
    fixture = {
        "bars": {
            "000001.SZ": [_bar("2026-09-10", 11.85)],
            "600519.SH": [_bar("2026-09-10", 1500.0)],
        }
    }
    container = build(tmp_path, fixture)
    service = container.updates

    # 模拟另一个更新正在运行：锁被占用
    service._lock.acquire()  # noqa: SLF001 - 测试排队分支
    try:
        # 锁被占用时不报错、不丢请求，而是排队
        assert service.refresh_quotes(["600519.SH"]) == 0
    finally:
        service._lock.release()  # noqa: SLF001

    # 下一次补取取得锁后，先补自己，再清空排队的 600519
    service.refresh_quotes(["000001.SZ"])
    assert container.quotes.view("000001.SZ").available
    assert container.quotes.view("600519.SH").available
    assert container.quotes.view("600519.SH").latest_date == "2026-09-10"


# --- AKShare 适配器格式与完整性边界 ---


def _install_fake_akshare(monkeypatch, *, sh=("600000",), star=("688001",), sz=("000001",), bj=("920001",), bars=None):
    """安装伪 akshare 模块：名单按交易所拆分，便于复现单个交易所为空。"""
    import sys
    import types

    import pandas as pd

    def name_frame(codes, code_col, name_col):
        return pd.DataFrame({code_col: list(codes), name_col: ["示例"] * len(codes), "上市日期": ["1999-11-10"] * len(codes)})

    def empty_name_frame(code_col, name_col):
        return pd.DataFrame({code_col: [], name_col: [], "上市日期": []})

    module = types.ModuleType("akshare")

    def stock_info_sh_name_code(*, symbol):
        if symbol == "主板A股":
            return name_frame(sh, "证券代码", "证券简称") if sh else empty_name_frame("证券代码", "证券简称")
        return name_frame(star, "证券代码", "证券简称") if star else empty_name_frame("证券代码", "证券简称")

    def stock_info_sz_name_code(*, symbol):
        frame = name_frame(sz, "A股代码", "A股简称") if sz else empty_name_frame("A股代码", "A股简称")
        frame["板块"] = ["主板"] * len(frame)
        return frame

    def stock_info_bj_name_code():
        return name_frame(bj, "证券代码", "证券简称") if bj else empty_name_frame("证券代码", "证券简称")

    def stock_zh_a_hist(**_kwargs):
        return pd.DataFrame(bars if bars is not None else [])

    module.stock_info_sh_name_code = stock_info_sh_name_code
    module.stock_info_sz_name_code = stock_info_sz_name_code
    module.stock_info_bj_name_code = stock_info_bj_name_code
    module.stock_zh_a_hist = stock_zh_a_hist
    # 标记为伪模块：conftest 的封印据此放行「用假 akshare 的适配器测试」
    module.__dslite_test_fake__ = True
    monkeypatch.setitem(sys.modules, "akshare", module)


@pytest.mark.parametrize("empty", ["bj", "star"])
def test_partial_empty_exchange_is_not_a_complete_list(monkeypatch, empty):
    """任一交易所返回空名单即视为获取不完整，不能当作成功名单覆盖完整旧库。"""
    from dailyscreen_lite.quotes.source import AkshareQuotesSource

    kwargs = {"bj": (), "star": ()} if empty == "bj" else {"star": ()}
    _install_fake_akshare(monkeypatch, **kwargs)
    with pytest.raises(QuoteSourceError):
        AkshareQuotesSource().security_list()


def test_complete_list_still_returned_when_all_exchanges_present(monkeypatch):
    from dailyscreen_lite.quotes.source import AkshareQuotesSource

    _install_fake_akshare(monkeypatch)
    securities = AkshareQuotesSource().security_list()
    assert {s.security_id for s in securities} == {
        "600000.SH",
        "688001.SH",
        "000001.SZ",
        "920001.BJ",
    }


def test_non_numeric_price_is_source_error(monkeypatch):
    """行情格式异常必须成为来源失败，不能逃逸成未捕获异常。"""
    from dailyscreen_lite.quotes.source import AkshareQuotesSource

    _install_fake_akshare(
        monkeypatch,
        bars=[{"日期": "2026-09-10", "开盘": "abc", "最高": "1", "最低": "1", "收盘": "1", "成交量": "1", "成交额": "1"}],
    )
    with pytest.raises(QuoteSourceError):
        AkshareQuotesSource().daily_bars(
            code="000001", exchange="SZ", start=date(2019, 1, 1), end=date(2026, 9, 11), adjust="qfq"
        )


def test_unexpected_source_error_leaves_failed_terminal_state(tmp_path):
    """来源抛未预期异常时，更新记录不得永久停在 running。"""
    fixture = {"bars": {"000001.SZ": [_bar("2026-09-10", 11.85)]}}
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    _wait_for(lambda: container.quotes.view("000001.SZ").available)

    class ExplodingSource:
        source_id = "boom"

        def security_list(self):
            raise QuoteSourceError("挂牌失败")

        def daily_bars(self, **_kwargs):
            raise ValueError("非预期格式")

    service = UpdateService(container.db, container.clock, ExplodingSource())
    record = service.run(UpdateKind.MANUAL)

    assert record.status is UpdateStatus.FAILED
    assert record.finished_at is not None
    assert service.running is False
    # 该次记录已落终态，不再停在 running
    persisted = next(r for r in service.history(20) if r["runId"] == record.run_id)
    assert persisted["status"] == "failed"
    assert persisted["finishedAt"] is not None
