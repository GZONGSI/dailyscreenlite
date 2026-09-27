"""固定时钟 → 真实更新服务 → 临时 SQLite → 重启，供应商边界记录请求。"""
from dataclasses import replace
from datetime import datetime

from test_data_status import make_settings, import_codes, trg, _write, _bar
from test_market_availability import market_rows, captured_calendar
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.quotes.source import FixtureQuotesSource, QuoteSourceError


class RecordingSource(FixtureQuotesSource):
    def __init__(self, path):
        super().__init__(path)
        self.calls = []

    def daily_bars(self, **kwargs):
        self.calls.append((kwargs["code"], kwargs["end"]))
        raise QuoteSourceError("尚未发布")


def test_seven_slots_attempt_once_each_and_restart_does_not_reset(tmp_path):
    settings = replace(make_settings(tmp_path, {}, trg()), update_schedule_enabled=True)
    clock = FixedClock(datetime(2026, 9, 18, 16, 29))
    source = RecordingSource(settings.quotes_fixture)
    app = build_container(settings, clock, quotes_source=source)
    import_codes(app, "000001", "920001")
    for hour in range(16, 23):
        clock.set(datetime(2026, 9, 18, hour, 30))
        assert app.scheduler.tick() is True
        assert app.scheduler.tick() is False
    assert [code for code, _ in source.calls].count("000001") == 7
    assert [code for code, _ in source.calls].count("920001") == 7
    reopened = build_container(settings, clock, quotes_source=source)
    assert reopened.scheduler.tick() is False
    clock.set(datetime(2026, 9, 18, 23))
    assert reopened.scheduler.tick() is False
    assert len(source.calls) == 14


def test_later_rounds_request_only_missing_capabilities_and_stocks(tmp_path):
    from dailyscreen_lite.quotes.calendar_source import FixtureCalendarSource
    from dailyscreen_lite.quotes.market_status import FixtureMarketStatusSource
    class Quotes(FixtureQuotesSource):
        def __init__(self, path):
            super().__init__(path)
            self.markets, self.stocks = [], []
        def security_list_for_market(self, exchange):
            self.markets.append(exchange)
            return super().security_list_for_market(exchange)
        def daily_bars(self, **kwargs):
            self.stocks.append(kwargs['code'])
            return super().daily_bars(**kwargs)
    class Calendar(FixtureCalendarSource):
        def __init__(self, path):
            super().__init__(path)
            self.calls = []
        def month(self, year, month):
            self.calls.append(month)
            return super().month(year, month)
    class Status(FixtureMarketStatusSource):
        calls = 0
        def snapshot(self, **kwargs):
            self.calls += 1
            return super().snapshot(**kwargs)
    rows = market_rows()
    quotes = {'security_markets': {
        'SH': {'securities': [r for r in rows if r['exchange'] == 'SH']},
        'SZ': {'error': '离线'}, 'BJ': {'error': '尽力获取失败'},
    }, 'bars': {'000001.SZ': [_bar('2026-09-18', 12)]}, 'fail_bars': ['600519.SH', '920001.BJ']}
    status = {'calendar_months': {'2026-09': captured_calendar()}, 'error': '状态离线'}
    settings = replace(make_settings(tmp_path, quotes, status), update_schedule_enabled=True)
    clock = FixedClock(datetime(2026, 9, 18, 16, 30))
    source, calendar, states = Quotes(settings.quotes_fixture), Calendar(settings.market_status_fixture), Status(settings.market_status_fixture)
    app = build_container(settings, clock, quotes_source=source, calendar_source=calendar, status_source=states)
    import_codes(app, '000001', '600519', '920001')
    assert app.scheduler.tick()
    quotes['security_markets']['SZ'] = {'securities': [r for r in rows if r['exchange'] == 'SZ']}
    quotes['bars']['600519.SH'] = [_bar('2026-09-18', 1500)]
    quotes['fail_bars'] = ['920001.BJ']
    status.pop('error')
    status['calendar_months']['2026-08'] = {'data': [
        {'jyrq': f'2026-08-{d:02}', 'jybz': '1' if datetime(2026, 8, d).weekday() < 5 else '0'} for d in range(1, 32)]}
    _write(tmp_path, 'quotes_fixture.json', quotes)
    _write(tmp_path, 'status_fixture.json', status)
    for hour in (17, 18):
        clock.set(datetime(2026, 9, 18, hour, 30))
        assert app.scheduler.tick()
    assert source.markets.count('SH') == 1
    assert source.markets.count('SZ') == 2
    assert source.markets.count('BJ') == 3
    assert calendar.calls == [8, 9, 8]
    assert states.calls == 2
    assert source.stocks.count('000001') == 1
    assert source.stocks.count('600519') == 2
    assert source.stocks.count('920001') == 3
    assert app.data_status.status().complete


def test_busy_scheduler_defers_and_import_queue_is_drained(tmp_path, monkeypatch):
    from threading import Event, Thread
    from test_quotes_update import _wait_for
    from dailyscreen_lite.quotes import scheduler
    entered, release, fetched = Event(), Event(), Event()

    class BlockingSource(FixtureQuotesSource):
        calls = 0
        def daily_bars(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(5)
            else:
                fetched.set()
            return super().daily_bars(**kwargs)

    settings = replace(make_settings(tmp_path, {'bars': {'600519.SH': [_bar('2026-09-18', 1500)]}}, trg()), update_schedule_enabled=True)
    clock = FixedClock(datetime(2026, 9, 18, 16, 30))
    source = BlockingSource(settings.quotes_fixture)
    app = build_container(settings, clock, quotes_source=source)
    import_codes(app, '000001')
    worker = Thread(target=app.updates.refresh_security, args=('000001.SZ',))
    worker.start()
    assert entered.wait(3)
    monkeypatch.setattr(scheduler, '_BUSY_INTERVAL_SECONDS', 0.02)
    try:
        assert not app.scheduler.tick()
        assert app.updates.automatic_rounds() == []
        app.updates.refresh_quotes(['600519.SH'])
        app.updates.refresh_quotes(['600519.SH'])
        app.updates.refresh_security('000001.SZ')
        app.scheduler.start()
        release.set()
        assert fetched.wait(3)
        _wait_for(lambda: len(app.updates.automatic_rounds()) == 1, timeout=3)
        assert app.quotes.view('600519.SH').available
    finally:
        release.set()
        worker.join(5)
        app.scheduler.stop()
    assert not app.updates.running
    assert source.calls == 3  # 手动 000001 + 合并的 600519 + 后续自动恢复 000001


def test_interruption_keeps_claim_and_next_slot_recovers(tmp_path):
    import pytest

    class Interrupted(FixtureQuotesSource):
        def daily_bars(self, **kwargs):
            raise SystemExit('模拟进程退出')

    settings = replace(make_settings(tmp_path, {}, trg()), update_schedule_enabled=True)
    clock = FixedClock(datetime(2026, 9, 18, 16, 30))
    app = build_container(settings, clock, quotes_source=Interrupted(settings.quotes_fixture))
    import_codes(app, '000001')
    with pytest.raises(SystemExit):
        app.scheduler.tick()
    reopened = build_container(settings, clock)
    assert reopened.updates.status()['lastRun']['status'] == 'failed'
    assert not reopened.updates.running
    assert not reopened.scheduler.tick()
    clock.set(datetime(2026, 9, 18, 17, 30))
    _write(tmp_path, 'quotes_fixture.json', {'bars': {'000001.SZ': [_bar('2026-09-18', 12)]}})
    assert reopened.scheduler.tick()
    assert reopened.data_status.status().complete
    assert [r['slot'] for r in reopened.updates.automatic_rounds()] == [1, 2]


def test_missed_slots_manual_cutoff_and_next_day_startup(tmp_path):
    from dailyscreen_lite.domain.models import UpdateKind
    settings = replace(make_settings(tmp_path, {}, trg()), update_schedule_enabled=True)
    clock = FixedClock(datetime(2026, 9, 18, 20, 45))
    source = RecordingSource(settings.quotes_fixture)
    app = build_container(settings, clock, quotes_source=source)
    import_codes(app, '000001')
    assert app.scheduler.tick()
    assert len(source.calls) == 1  # 错过的四轮合并为一轮
    assert [r['slot'] for r in app.updates.status()['automaticRounds']] == [5]
    clock.set(datetime(2026, 9, 18, 23))
    reopened = build_container(settings, clock, quotes_source=source)
    assert not reopened.scheduler.tick()
    reopened.updates.run(UpdateKind.MANUAL)
    reopened.updates.refresh_security('600519.SH')
    assert [r['slot'] for r in reopened.updates.status()['automaticRounds']] == [5]
    assert '600519.SH' not in reopened.updates.recovery_plan().stocks
    before = len(source.calls)
    clock.set(datetime(2026, 9, 19, 9))  # 周六仍恢复周五目标
    reopened = build_container(settings, clock, quotes_source=source)
    assert reopened.scheduler.tick()
    assert not build_container(settings, clock, quotes_source=source).scheduler.tick()
    assert len(source.calls) == before + 1
    assert source.calls[-1][1].isoformat() == '2026-09-18'
    assert [r['slot'] for r in reopened.updates.status()['automaticRounds']] == [0]


def test_completed_target_stops_requests_even_with_short_history(tmp_path):
    settings = replace(make_settings(tmp_path, {'bars': {'000001.SZ': [_bar('2026-09-18', 12)]}}, trg()), update_schedule_enabled=True)
    clock = FixedClock(datetime(2026, 9, 18, 16, 30))
    app = build_container(settings, clock)
    import_codes(app, '000001')
    assert app.scheduler.tick()
    clock.set(datetime(2026, 9, 18, 17, 30))
    assert not app.scheduler.tick()
    assert len(app.updates.history()) == 1
    assert app.data_status.status().complete
