"""离线约束自身的验证：违规必须让用例变红，且伪来源必须放行。

这组用例锁住的是测试基础设施本身：`conftest._no_live_sources` 抛的断言会被
更新服务与后台补取的 `except Exception` 吞掉，单靠抛异常无法保证违规用例失败
（曾实测：不检查结果的违规用例仍然通过）。因此约束改为「先记录、收尾统一断言」，
这里验证记录在两种被吞路径下都成立。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from conftest import REAL_SNAPSHOT, live_source_violations

from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import UpdateKind
from dailyscreen_lite.quotes.source import AkshareQuotesSource
from dailyscreen_lite.settings import Settings


def _live_container(tmp_path: Path):
    """故意不注入任何夹具的容器：让来源落到真实 AKShare/BaoStock（应被封印记录）。"""
    settings = Settings(
        data_dir=tmp_path / "data",
        securities_snapshot=REAL_SNAPSHOT,
        update_schedule_enabled=False,
    )
    container = build_container(settings, FixedClock(datetime(2026, 9, 18, 17, 0, 0)))
    container.imports._published_hook = None
    return container


def test_violation_is_recorded_when_update_service_swallows_it(tmp_path):
    """整体更新：断言被 `except Exception` 吞成一次失败更新，但违规仍被记录。"""
    container = _live_container(tmp_path)
    container.imports.submit_file("a.csv", "代码\n600519\n".encode())

    record = container.updates.run(UpdateKind.MANUAL)

    # 异常被吞：更新只落成失败终态，用例若不看结果就不会察觉
    assert record.status.value in {"failed", "partial"}
    # 但违规已被记录 → 夹具收尾会让这个用例红
    assert set(live_source_violations(reset=True)) == {
        "AkshareQuotesSource", "SzseCalendar", "AkshareMarketStatusSource",
        "Tencent", "AkshareWorker",
    }


def test_violation_is_recorded_when_backfill_swallows_it(tmp_path):
    """单股补取：同样被 `except Exception` 吞掉，违规仍被记录。"""
    container = _live_container(tmp_path)

    container.updates.refresh_security("600519.SH")

    assert live_source_violations(reset=True) == ["Tencent", "AkshareWorker"]


def test_guard_raises_immediately_for_direct_callers(tmp_path):
    """直接调用适配器时不吞异常：立刻断言失败，错误信息给出修法。"""
    with pytest.raises(AssertionError, match="不得调用真实 akshare"):
        AkshareQuotesSource().daily_bars(
            code="000001",
            exchange="SZ",
            start=datetime(2026, 9, 1).date(),
            end=datetime(2026, 9, 11).date(),
            adjust="qfq",
        )
    assert live_source_violations(reset=True) == ["AkshareQuotesSource"]


def test_fake_akshare_is_allowed_not_recorded(monkeypatch):
    """用伪 akshare（`_install_fake_akshare`）的适配器测试是合法离线测试，必须放行。"""
    from test_quotes_update import _install_fake_akshare

    _install_fake_akshare(monkeypatch)

    securities = AkshareQuotesSource().security_list()

    assert securities, "伪模块应被放行并返回数据"
    assert live_source_violations(reset=True) == []
