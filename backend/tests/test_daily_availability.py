from test_quotes_update import build, import_codes, _bar
import pytest
from dailyscreen_lite.quotes.baostock import query_worker


def test_production_daily_sources_use_tencent_then_sina_for_sh_sz():
    from types import SimpleNamespace
    from dailyscreen_lite.quotes.source import AkshareQuotesSource, build_daily_sources

    settings = SimpleNamespace(quotes_fixture=None, quotes_history_db=None)
    sources = build_daily_sources(settings, AkshareQuotesSource())
    assert [s.source_id for s in sources] == ["tencent", "sina", "akshare"]
    assert [s.source_id for s in sources if "SH" in s.daily_markets] == ["tencent", "sina"]
    assert [s.source_id for s in sources if "BJ" in s.daily_markets] == ["sina", "akshare"]


def test_tencent_and_sina_qfq_daily_units(monkeypatch):
    from datetime import date
    import json
    from types import SimpleNamespace
    import pandas as pd
    from dailyscreen_lite.quotes import akshare_worker
    from dailyscreen_lite.quotes.source import TencentHttpQuotesSource, AkshareSinaQuotesSource

    calls = []

    def fake(method, **kwargs):
        calls.append((method, kwargs))
        volume = 1000 if method == "tencent" else 100000
        return pd.DataFrame([{
            "date": "2026-09-23", "open": 10, "high": 11, "low": 9,
            "close": 10.5, "volume": volume, "amount": 250000,
        }])

    monkeypatch.setattr(akshare_worker, "bounded_akshare", lambda: SimpleNamespace(
        stock_zh_a_daily=lambda **kw: fake("sina", **kw),
    ))

    class TencentSession:
        def get(self, _url, *, params, timeout):
            calls.append(("tencent", params))
            assert timeout == (3, 10)
            symbol = params["param"].split(",")[0]
            payload = {"code": 0, "data": {symbol: {"qfqday": [
                ["2026-09-23", "10", "10.5", "11", "9", "1000", {}, "", "25"]
            ]}}}
            return SimpleNamespace(
                text=params["_var"] + "=" + json.dumps(payload),
                raise_for_status=lambda: None,
            )

    for source in (TencentHttpQuotesSource(session=TencentSession()), AkshareSinaQuotesSource()):
        bars = source.daily_bars(code="002594", exchange="SZ", start=date(2026, 9, 22),
                                 end=date(2026, 9, 23), adjust="qfq")
        assert [(bar.trade_date, bar.volume_lots, bar.amount_yuan) for bar in bars] == [
            (date(2026, 9, 23), 1000, 250000)
        ]
    assert [method for method, _ in calls] == ["tencent", "sina"]
    assert calls[0][1]["param"].startswith("sz002594,day,")
    assert calls[1][1]["symbol"] == "sz002594" and calls[1][1]["adjust"] == "qfq"
    sz_main = TencentHttpQuotesSource(session=TencentSession()).daily_bars(
        code="000001", exchange="SZ", start=date(2026, 9, 22),
        end=date(2026, 9, 23), adjust="qfq",
    )
    assert sz_main[0].volume_lots == 10  # sz000 原始量为股，转换为手
    bj_bars = AkshareSinaQuotesSource().daily_bars(
        code="920961", exchange="BJ", start=date(2026, 9, 22),
        end=date(2026, 9, 23), adjust="qfq",
    )
    assert bj_bars[-1].trade_date == date(2026, 9, 23)
    assert calls[-1][1]["symbol"] == "bj920961"


def test_existing_stock_requests_five_bar_overlap_and_preserves_history(tmp_path):
    from datetime import date, datetime
    from dailyscreen_lite.app.container import build_container
    from dailyscreen_lite.domain.clock import FixedClock
    from dailyscreen_lite.quotes.source import ProviderBar
    from test_quotes_update import make_settings

    days = [date.fromisoformat(day) for day in (
        "2026-09-07", "2026-09-08", "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14",
    )]

    class RecordingSource:
        source_id = "recording"

        def __init__(self):
            self.requests = []

        def daily_bars(self, **request):
            self.requests.append(request)
            return [ProviderBar(day, 10, 10, 10, 10, 100, None)
                    for day in days if request["start"] <= day <= request["end"]]

    clock = FixedClock(datetime(2026, 9, 11, 17))
    source = RecordingSource()
    settings = make_settings(tmp_path, status={
        "trade_dates": [day.isoformat() for day in days],
        "covered_markets": ["SH", "SZ"], "uncovered_markets": ["BJ"],
    })
    container = build_container(settings, clock, quotes_source=source)
    import_codes(container, "000001")
    assert source.requests[0]["start"] == date(2023, 9, 11)
    requests_after_import = len(source.requests)
    container.updates.refresh_quotes(["000001.SZ"])
    assert len(source.requests) == requests_after_import  # 定向与整体更新都跳过已补齐股票
    container.updates.refresh_security("000001.SZ")
    assert source.requests[-1]["start"] == date(2026, 9, 7)
    clock.set(datetime(2026, 9, 14, 17))
    container.updates.refresh_security("000001.SZ")
    assert source.requests[-1]["start"] == date(2026, 9, 7)
    assert [bar.trade_date for bar in container.quotes.view("000001.SZ").bars] == days


def test_missing_amount_is_stored_and_survives_restart(tmp_path):
    container = build(tmp_path, {"bars": {"000001.SZ": [_bar("2026-09-11", 12, amount=None)]}})
    import_codes(container, "000001")
    view = container.quotes.view("000001.SZ")
    assert view.available
    assert view.bars[-1].amount_yuan is None
    from dailyscreen_lite.app.container import build_container
    restarted = build_container(container.settings, container.clock)
    assert restarted.quotes.view("000001.SZ").bars[-1].amount_yuan is None


def test_short_history_keeps_old_rows_and_changed_basis_is_archived(tmp_path):
    import json
    from dailyscreen_lite.repository import quotes_repo
    container = build(tmp_path, {"source": "one", "bars": {"000001.SZ": [
        _bar("2024-01-02", 10), _bar("2026-09-11", 12)]}})
    import_codes(container, "000001")
    def refresh(rows):
        container.settings.quotes_fixture.write_text(json.dumps({"source": "one", "bars": {"000001.SZ": rows}}), encoding="utf-8")
        container.updates.refresh_security("000001.SZ")
    refresh([_bar("2026-09-11", 12)])
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [10, 12]

    refresh([_bar("2026-09-11", 6)])
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [10, 12]
    refresh([_bar("2024-01-02", 8), _bar("2026-09-11", 6)])
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [8, 6]
    with container.db.read() as conn:
        archives = quotes_repo.archived_series(conn, "000001.SZ", "qfq")
    assert [row["close"] for row in archives[-1]] == [10, 12]


def test_baostock_real_capture_units_and_suspension(tmp_path):
    import json
    from pathlib import Path
    from datetime import date
    from dailyscreen_lite.quotes.baostock import BaostockQuotesSource
    captures = Path(__file__).parent / "fixtures/baostock"
    def transport(request):
        name = "star" if request["code"] == "sh.688981" else "suspension"
        return json.loads((captures / (name + ".json")).read_text(encoding="utf-8"))
    source = BaostockQuotesSource(transport=transport)
    bars = source.daily_bars(code="688981", exchange="SH", start=date(2026, 9, 14), end=date(2026, 9, 18), adjust="qfq")
    assert bars[-1].volume_lots == 387219.91
    assert bars[-1].close == 122
    bars = source.daily_bars(code="601995", exchange="SH", start=date(2026, 9, 14), end=date(2026, 9, 18), adjust="qfq")
    assert [b.trade_date.isoformat() for b in bars] == ["2026-09-14"]


def test_fallback_is_per_stock_and_sticky_across_restart(tmp_path):
    import json
    from dailyscreen_lite.app.container import build_container
    fixture = {"providers": [
        {"source": "primary", "fail_bars": ["000001.SZ"], "bars": {"600519.SH": [_bar("2026-09-11", 100)]}},
        {"source": "backup", "bars": {"000001.SZ": [_bar("2026-09-11", 12, amount=None)]}},
    ]}
    container = build(tmp_path, fixture)
    import_codes(container, "000001", "600519")
    assert container.quotes.view("000001.SZ").source == "backup"
    assert container.quotes.view("600519.SH").source == "primary"
    stock = next(s for s in container.data_status.status().stocks if s.security_id == "000001.SZ")
    assert "primary" in stock.last_error
    fixture["providers"][0]["fail_bars"] = []
    fixture["providers"][0]["bars"]["000001.SZ"] = [_bar("2026-09-11", 99)]
    container.settings.quotes_fixture.write_text(json.dumps(fixture), encoding="utf-8")
    restarted = build_container(container.settings, container.clock)
    restarted.updates.refresh_security("000001.SZ")
    assert restarted.quotes.view("000001.SZ").source == "backup"
    assert restarted.quotes.view("000001.SZ").bars[-1].close == 12


def test_default_window_is_three_years_and_respects_listing(tmp_path):
    container = build(tmp_path, {"bars": {"000001.SZ": [
        _bar("2023-09-10", 9), _bar("2023-09-11", 10), _bar("2026-09-11", 12)]}})
    import_codes(container, "000001")
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [10, 12]


def test_http_reports_actual_source_and_nullable_amount(tmp_path):
    from fastapi.testclient import TestClient
    from dailyscreen_lite.app.main import create_app
    from test_quotes_update import make_settings
    settings = make_settings(tmp_path, {"source": "admitted-backup", "bars": {"000001.SZ": [_bar("2026-09-11", 12, amount=None)]}})
    from datetime import datetime
    from dailyscreen_lite.domain.clock import FixedClock
    app = create_app(settings, FixedClock(datetime(2026, 9, 11, 17)))
    import_codes(app.state.container, "000001")
    with TestClient(app) as client:
        assert client.get("/api/quotes/000001.SZ").json()["bars"][-1]["amountYuan"] is None
        assert client.get("/api/data").json()["stocks"][0]["source"] == "admitted-backup"
        diagnostics = client.get("/api/data").json()["quoteDiagnostics"]
        assert diagnostics[0]["securityId"] == "000001.SZ"
        assert diagnostics[0]["fetchMode"] == "initial"
        assert diagnostics[0]["requestStart"] == "2023-09-11"
        assert diagnostics[0]["attempts"][0]["source"] == "admitted-backup"
        assert isinstance(diagnostics[0]["elapsedMs"], int)
    restarted = create_app(settings, FixedClock(datetime(2026, 9, 11, 17)))
    with TestClient(restarted) as client:
        assert client.get("/api/data").json()["quoteDiagnostics"] == diagnostics


def test_old_update_records_gain_nullable_diagnostics_on_restart(tmp_path):
    import sqlite3
    from dailyscreen_lite.app.container import build_container

    container = build(tmp_path, {"bars": {"000001.SZ": [_bar("2026-09-11", 12)]}})
    import_codes(container, "000001")
    with sqlite3.connect(container.settings.database_path) as conn:
        for column in ("fetch_mode", "request_start", "elapsed_ms", "attempts_json"):
            conn.execute(f"alter table update_stock_results drop column {column}")
        conn.execute("alter table update_runs drop column elapsed_ms")
    reopened = build_container(container.settings, container.clock)
    with reopened.db.read() as conn:
        columns = {row[1] for row in conn.execute("pragma table_info(update_stock_results)")}
        run = reopened.updates.last_completed()
        items = reopened.updates.run_items(run.run_id)
    assert {"fetch_mode", "request_start", "elapsed_ms", "attempts_json"} <= columns
    assert items[0].fetch_mode is None and items[0].attempts == ()
    assert run.elapsed_ms is None


def test_http_reports_stock_in_progress(tmp_path):
    from datetime import date, datetime
    from threading import Event, Thread
    from fastapi.testclient import TestClient
    from dailyscreen_lite.app.main import create_app
    from dailyscreen_lite.domain.clock import FixedClock
    from dailyscreen_lite.quotes.source import ProviderBar
    from test_quotes_update import make_settings

    arrived, release = Event(), Event()

    class SlowSource:
        source_id = "slow-fixture"

        def daily_bars(self, **_request):
            arrived.set()
            assert release.wait(5)
            return [ProviderBar(date(2026, 9, 11), 10, 10, 10, 10, 100, None)]

    app = create_app(make_settings(tmp_path, {"bars": {"000001.SZ": [_bar("2026-09-11", 10)]}}),
                     FixedClock(datetime(2026, 9, 11, 17)))
    import_codes(app.state.container, "000001")
    app.state.container.updates._daily_sources = [SlowSource()]
    with TestClient(app) as client:
        worker = Thread(target=app.state.container.updates.refresh_security,
                        args=("000001.SZ",), daemon=True)
        worker.start()
        try:
            assert arrived.wait(5)
            progress = client.get("/api/data").json()["progress"]
            assert (progress["currentSecurityId"], progress["done"], progress["total"]) == (
                "000001.SZ", 0, 1)
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()
        assert client.get("/api/data").json()["progress"] is None


def test_old_database_migration_preserves_business_and_earlier_quotes(tmp_path):
    import json
    import sqlite3
    from datetime import datetime
    from fastapi.testclient import TestClient
    from dailyscreen_lite.app.main import create_app
    from dailyscreen_lite.app.container import build_container
    from dailyscreen_lite.domain.clock import FixedClock
    from dailyscreen_lite.repository.database import SCHEMA
    from test_quotes_update import make_settings
    settings = make_settings(tmp_path, {"bars": {"000001.SZ": [_bar("2020-10-09", 8), _bar("2023-09-11", 10)]}})
    settings.ensure_dirs()
    # A real previous-schema database, not a mock migration or empty fresh DB.
    with sqlite3.connect(settings.database_path) as conn:
        conn.executescript(SCHEMA.replace("amount_yuan real,", "amount_yuan real not null,"))
    # Populate through normal business services, then restore the old quote constraint
    # to model an installed old version at upgrade time.
    old = build_container(settings, FixedClock(datetime(2023, 9, 11, 17)))
    import_codes(old, "000001")
    candidate_id = old.classification.list_candidates()[0].candidate.candidate_id
    old.observations.observe_candidate(candidate_id, ["default"])
    old.notes.create_for_security("000001.SZ", "迁移后保留")
    with sqlite3.connect(settings.database_path) as conn:
        conn.executescript("""
            alter table daily_quotes rename to previous_quotes;
            drop index idx_daily_quotes_lookup;
        """)
        conn.executescript(SCHEMA.replace("amount_yuan real,", "amount_yuan real not null,"))
        conn.execute("insert into daily_quotes select * from previous_quotes")
        conn.execute("drop table previous_quotes")
    clock = FixedClock(datetime(2026, 9, 11, 17))
    settings.quotes_fixture.write_text(json.dumps({"bars": {"000001.SZ": [
        _bar("2023-09-11", 10), _bar("2026-09-11", 12, amount=None)]}}), encoding="utf-8")
    for _ in range(2):
        with TestClient(create_app(settings, clock)) as client:
            assert client.get(f"/api/classification/candidates/{candidate_id}").json()["state"] == "observed"
            assert client.get("/api/observations/memberships?securityId=000001.SZ").json()["groupIds"] == ["default"]
            assert client.get("/api/notes/securities/000001.SZ").json()["notes"][0]["body"] == "迁移后保留"
            assert client.post("/api/securities/000001.SZ/refresh").json()["updated"]
            bars = client.get("/api/quotes/000001.SZ").json()["bars"]
            assert [b["date"] for b in bars] == ["2020-10-09", "2023-09-11", "2026-09-11"]
            assert bars[-1]["amountYuan"] is None


@pytest.mark.parametrize("bad_rows", [[], [_bar("2026-09-10", 11)],
    [_bar("2026-09-11", float("nan"))], [_bar("2026-09-11", 12, volume=-1)],
    [_bar("2026-09-11", 12), _bar("2026-09-11", 12)]])
def test_empty_stale_and_malformed_sources_fall_back(tmp_path, bad_rows):
    container = build(tmp_path, {"providers": [
        {"source": "bad", "bars": {"000001.SZ": bad_rows}},
        {"source": "good", "bars": {"000001.SZ": [_bar("2026-09-11", 13)]}},
    ]})
    import_codes(container, "000001")
    assert container.quotes.view("000001.SZ").source == "good"
    assert "bad" in container.data_status.status().stocks[0].last_error


def test_failed_fallback_keeps_old_curve_and_cross_source_change_archives(tmp_path):
    import json
    from dailyscreen_lite.repository import quotes_repo
    fixture = {"providers": [
        {"source": "first", "bars": {"000001.SZ": [_bar("2024-01-02", 8), _bar("2026-09-11", 12)]}},
        {"source": "second", "bars": {}},
    ]}
    container = build(tmp_path, fixture)
    import_codes(container, "000001")
    fixture["providers"][0]["fail_bars"] = ["000001.SZ"]
    fixture["providers"][1]["bars"] = {"000001.SZ": [_bar("2026-09-10", 9)]}
    container.settings.quotes_fixture.write_text(json.dumps(fixture), encoding="utf-8")
    container.updates.refresh_security("000001.SZ")
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [8, 12]
    fixture["providers"][1]["bars"] = {"000001.SZ": [_bar("2026-09-11", 12)]}
    container.settings.quotes_fixture.write_text(json.dumps(fixture), encoding="utf-8")
    container.updates.refresh_security("000001.SZ")
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [8, 12]
    fixture["providers"][1]["bars"] = {"000001.SZ": [_bar("2024-01-02", 7), _bar("2026-09-11", 12)]}
    container.settings.quotes_fixture.write_text(json.dumps(fixture), encoding="utf-8")
    container.updates.refresh_security("000001.SZ")
    # Even equal latest prices cannot establish equivalent cross-source adjustment.
    assert [b.close for b in container.quotes.view("000001.SZ").bars] == [7, 12]
    with container.db.read() as conn:
        assert [b["close"] for b in quotes_repo.archived_series(conn, "000001.SZ", "qfq")[-1]] == [8, 12]


@pytest.mark.parametrize("field,value", [("adjustflag", "3"), ("volume", ""), ("tradestatus", "unknown"), ("code", "sz.000001")])
def test_baostock_invalid_rows_are_not_complete_bars(field, value):
    import json
    from pathlib import Path
    from datetime import date
    from dailyscreen_lite.quotes.baostock import BaostockQuotesSource
    from dailyscreen_lite.quotes.source import QuoteSourceError
    capture = Path(__file__).parent / "fixtures/baostock/star.json"
    payload = json.loads(capture.read_text(encoding="utf-8"))
    payload["rows"][-1][payload["fields"].index(field)] = value
    with pytest.raises(QuoteSourceError):
        BaostockQuotesSource(transport=lambda request: payload).daily_bars(
            code="688981", exchange="SH", start=date(2026, 9, 14), end=date(2026, 9, 18), adjust="qfq")


def test_raw_provider_cannot_impersonate_qfq(tmp_path):
    container = build(tmp_path, {"providers": [
        {"source": "raw", "adjust": "raw", "bars": {"000001.SZ": [_bar("2026-09-11", 99)]}},
        {"source": "qfq", "adjust": "qfq", "bars": {"000001.SZ": [_bar("2026-09-11", 12)]}},
    ]})
    import_codes(container, "000001")
    assert container.quotes.view("000001.SZ").bars[-1].close == 12


@pytest.mark.parametrize("now,code,expected_start", [
    ("2024-02-29T17:00:00", "000001", "2021-02-28"),
    ("2021-09-10T17:00:00", "688981", "2020-07-16"),
])
def test_provider_request_honors_leap_year_and_listing(tmp_path, now, code, expected_start):
    from datetime import datetime
    from dailyscreen_lite.app.container import build_container
    from dailyscreen_lite.domain.clock import FixedClock
    from dailyscreen_lite.quotes.source import ProviderBar
    from test_quotes_update import make_settings
    class RecordingSource:
        source_id = "recording"
        def daily_bars(self, **request):
            self.request = request
            return [ProviderBar(request["end"], 12, 12, 12, 12, 100, None)]
    source = RecordingSource()
    container = build_container(make_settings(tmp_path), FixedClock(datetime.fromisoformat(now)), quotes_source=source)
    import_codes(container, code)
    assert source.request["start"].isoformat() == expected_start


def test_baostock_worker_rejects_late_interval_failure():
    from dailyscreen_lite.quotes.baostock_worker import collect, FIELDS
    class Result:
        fields = FIELDS.split(",")
        error_code = "0"
        error_msg = "success"
        def next(self):
            return False
    class Client:
        def __init__(self):
            self.calls = []
        def query_history_k_data_plus(self, code, fields, **query):
            self.calls.append(query)
            result = Result()
            if len(self.calls) == 2:
                result.error_code = "network-error"
                result.error_msg = "second interval failed"
            return result
    client = Client()
    result = collect(client, {"code": "sh.600519", "start": "2023-09-21", "end": "2026-09-21"})
    assert result["code"] == "network-error"
    assert "rows" not in result
    assert client.calls[0]["end_date"] == "2024-09-20"
    assert client.calls[1]["start_date"] == "2024-09-21"


def test_baostock_transport_timeout_is_an_explicit_source_failure(monkeypatch):
    import subprocess
    from dailyscreen_lite.quotes.source import QuoteSourceError
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 45
        raise subprocess.TimeoutExpired(args[0], 45)
    monkeypatch.setattr(subprocess, "run", timeout)
    # Test the captured original function: the global offline guard prevents any real process.
    with pytest.raises(QuoteSourceError, match="45"):
        query_worker({"code": "sh.600519", "start": "2023-09-21", "end": "2026-09-21"})


@pytest.mark.parametrize("volume", [None, ""])
def test_eastmoney_missing_volume_is_not_a_zero_volume_bar(monkeypatch, volume):
    from datetime import date
    from test_quotes_update import _install_fake_akshare
    from dailyscreen_lite.quotes.source import AkshareQuotesSource, QuoteSourceError
    _install_fake_akshare(monkeypatch, bars=[{
        "日期": "2026-09-11", "开盘": 12, "最高": 12, "最低": 12,
        "收盘": 12, "成交量": volume, "成交额": None,
    }])
    with pytest.raises(QuoteSourceError):
        AkshareQuotesSource().daily_bars(code="920001", exchange="BJ", start=date(2026, 9, 11), end=date(2026, 9, 11), adjust="qfq")


def test_database_write_failure_is_visible_and_preserves_previous_curve(tmp_path):
    container = build(tmp_path, {"bars": {"000001.SZ": [_bar("2026-09-11", 12)]}})
    import_codes(container, "000001")
    with container.db.transaction() as conn:
        conn.execute("""create trigger fail_quote_insert before insert on daily_quotes
            begin select raise(abort, 'quote write failed'); end""")
    container.updates.refresh_security("000001.SZ")
    assert container.quotes.view("000001.SZ").bars[-1].close == 12
    assert "quote write failed" in container.data_status.status().stocks[0].last_error
