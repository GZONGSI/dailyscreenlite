"""测试公共夹具：真实临时 SQLite 库 + 真实证券库快照 + 固定时钟。

测试一律走**离线**来源：行情给一份空夹具（来源成功但无该股票数据），后台补取立即返回，
既不访问外网、也不留下可能拖住临时目录清理的后台线程。需要真实行情的用例
（`test_quotes_update`、`test_data_status`）各自注入明确夹具；需要计数来源调用的用例
另注入计数来源。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from dailyscreen_lite.app.container import build_container  # noqa: E402
from dailyscreen_lite.domain.clock import FixedClock  # noqa: E402
from dailyscreen_lite.settings import Settings  # noqa: E402

REPO_ROOT = BACKEND_ROOT.parent
REAL_SNAPSHOT = REPO_ROOT / "data" / "securities" / "initial_snapshot.json"


# 伪 akshare 模块的标记：`_install_fake_akshare` 会打上它，封印据此放行
FAKE_AKSHARE_MARK = "__dslite_test_fake__"

# 本次用例里「触到真实来源」的记录。抛异常挡不住违规用例：更新服务与后台补取都有
# `except Exception`，会把断言吞掉当成一次更新失败，用例若不看结果就仍是绿的。
# 因此这里先**记下来**，再由夹具收尾统一断言——被吞掉也算数。
_LIVE_SOURCE_VIOLATIONS: list[str] = []


def live_source_violations(*, reset: bool = False) -> list[str]:
    """本用例记录的违规调用；只需给「故意验证封印」的用例读一次并清空。"""
    recorded = list(_LIVE_SOURCE_VIOLATIONS)
    if reset:
        _LIVE_SOURCE_VIOLATIONS.clear()
    return recorded


@pytest.fixture(autouse=True)
def _no_live_sources(monkeypatch: pytest.MonkeyPatch):
    """测试绝不拿到真实 akshare：记下违规并在收尾统一断言。

    拦在「获取 akshare」这一层而不是适配器方法上：用伪 akshare 装进 `sys.modules`
    的适配器测试是合法的离线测试，必须放行；只有真的要用外网才算违规。
    收尾断言而非「只抛异常」是关键：更新服务与后台补取会捕获异常，单靠抛异常
    无法保证违规用例变红（已实测：不检查结果的用例仍会通过）。
    """
    import sys

    from dailyscreen_lite.quotes import market_status as status_mod
    from dailyscreen_lite.quotes import source as source_mod

    _LIVE_SOURCE_VIOLATIONS.clear()

    def guarded_ak(self: object) -> object:
        module = sys.modules.get("akshare")
        if module is None or not getattr(module, FAKE_AKSHARE_MARK, False):
            _LIVE_SOURCE_VIOLATIONS.append(type(self).__name__)
            raise AssertionError(
                "测试不得调用真实 akshare：请注入 quotes_fixture / market_status_fixture，"
                "用 conftest 的 offline_settings / build_offline，或用 _install_fake_akshare 装伪模块"
            )
        return module

    for owner in (source_mod.AkshareQuotesSource, status_mod.AkshareMarketStatusSource):
        monkeypatch.setattr(owner, "_ak", guarded_ak, raising=False)

    # Production SH/SZ daily quotes now use a direct Tencent HTTP session and
    # the Sina subprocess fallback; keep the offline test boundary current.
    import requests
    from dailyscreen_lite.quotes import akshare_worker
    original_get = requests.Session.get
    original_fetch_frame = akshare_worker.fetch_frame

    def guarded_get(self, url, *args, **kwargs):
        if str(url).startswith("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"):
            _LIVE_SOURCE_VIOLATIONS.append("Tencent")
            raise AssertionError("Inject a Tencent session or quotes_fixture in offline tests")
        return original_get(self, url, *args, **kwargs)

    def guarded_worker(method, **kwargs):
        if method in {"stock_zh_a_daily", "stock_zh_a_hist_tx"}:
            _LIVE_SOURCE_VIOLATIONS.append("AkshareWorker")
            raise AssertionError("Inject quotes_fixture instead of a live AKShare subprocess")
        return original_fetch_frame(method, **kwargs)

    monkeypatch.setattr(requests.Session, "get", guarded_get)
    monkeypatch.setattr(akshare_worker, "fetch_frame", guarded_worker)

    from dailyscreen_lite.quotes import baostock as bao_mod
    def guarded_bao(request):
        _LIVE_SOURCE_VIOLATIONS.append("BaoStock")
        raise AssertionError("Inject BaoStock transport or quotes_fixture in offline tests")
    monkeypatch.setattr(bao_mod, "query_worker", guarded_bao)
    from dailyscreen_lite.quotes import calendar_source
    def guarded_calendar(year, month):
        _LIVE_SOURCE_VIOLATIONS.append("SzseCalendar")
        raise AssertionError("Inject calendar transport or market_status_fixture in offline tests")
    monkeypatch.setattr(calendar_source, "fetch_month", guarded_calendar)

    yield

    if _LIVE_SOURCE_VIOLATIONS:
        owners = "、".join(sorted(set(_LIVE_SOURCE_VIOLATIONS)))
        _LIVE_SOURCE_VIOLATIONS.clear()
        pytest.fail(
            f"本用例触到了真实来源（{owners}）：测试必须离线——注入夹具，"
            "或用 offline_settings / build_offline"
        )


@pytest.fixture
def fixed_clock() -> FixedClock:
    # 2026-09-11 北京时间 22:30，便于验证导入日期固定
    return FixedClock(datetime(2026, 9, 11, 22, 30, 0))


def offline_quotes(tmp_path: Path) -> Path:
    """空行情夹具：来源成功但无数据，不访问外网。"""
    path = tmp_path / "offline_quotes.json"
    path.write_text("{}", encoding="utf-8")
    return path


def offline_settings(tmp_path: Path, **overrides: object) -> Settings:
    """隔离数据目录 + 离线行情来源；证券库沿用真实交付快照。

    用例需要真实行情时传 `quotes_fixture=...` 覆盖本默认值。
    """
    data_dir = Path(overrides.pop("data_dir", tmp_path / "data"))
    (data_dir / "securities").mkdir(parents=True, exist_ok=True)
    return Settings(
        data_dir=data_dir,
        securities_snapshot=REAL_SNAPSHOT,
        quotes_fixture=offline_quotes(tmp_path),
        **overrides,
    )


def build_offline(settings: Settings, clock: FixedClock):
    """构建容器并停用「导入后后台补取」。

    补取走的是后台线程；测试要的是同步、可判定的事实，留一个在测试结束后仍可能
    收尾的线程既会让临时目录删不掉（Windows 上 `PermissionError`），也会让
    「导入后行情摘要为空」这类断言变成竞态。补取本身由 `test_quotes_update` 与
    浏览器验收覆盖，这里停用不损失覆盖。
    """
    container = build_container(settings, clock)
    container.imports._published_hook = None
    return container


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """隔离数据目录：runtime 建在临时目录，证券库沿用真实交付快照。"""
    return offline_settings(tmp_path)


@pytest.fixture
def container(settings: Settings, fixed_clock: FixedClock):
    return build_offline(settings, fixed_clock)


def make_csv(*rows: str) -> bytes:
    return ("\n".join(rows) + "\n").encode("utf-8")


def declared_fields(model) -> set[str]:
    """声明的线格式字段名（别名）：响应的键集合必须与它逐字一致。"""
    return {field.alias or name for name, field in model.model_fields.items()}


def assert_wire_contract(model, payload: dict, expected: frozenset[str]) -> None:
    """一处固定「实际返回 == 迁移前的线格式 == 模型声明的字段」。

    只比「响应 == 模型」时，模型漏声明一个字段会让两边一起少，悄悄过滤看不出来；
    因此还要与迁移前逐字固定的键清单对齐。各片的 HTTP 契约用例共用这一处判定。
    """
    assert set(payload) == set(expected), (sorted(set(payload) ^ set(expected)),)
    assert declared_fields(model) == set(expected), (
        sorted(declared_fields(model) ^ set(expected)),
    )
    model.model_validate(payload)
