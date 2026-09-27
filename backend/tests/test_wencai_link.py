"""问财获取适配器测试：完整性判定、查询身份、条件确认与鉴权异常。

使用符合协议的固定提供器，不访问网络；实源验证另行记录。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from conftest import make_csv
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.errors import (
    UnsupportedLink,
    WencaiIncomplete,
    WencaiUnexpected,
)
from dailyscreen_lite.domain.models import BatchStatus, SourceKind
from dailyscreen_lite.wencai.acquisition import QueryContext, acquire
from dailyscreen_lite.wencai.condition import condition_fingerprint, condition_labels

URL = "https://www.iwencai.com/screener/result?w=%E5%88%9B%E6%96%B0%E9%AB%98&querytype=stock&sign=1"
QUERY = "创新高"


def _condition(window: str = "20250101-20260911") -> str:
    return json.dumps(
        [
            {
                "node_type": "op",
                "uiText": f"最高价:后复权创120日新高[{window}]",
                "opPropertiesMap": {"date": window},
                "dateText": window,
            }
        ],
        ensure_ascii=False,
    )


class FakeWencai:
    """固定提供器：按声明总数与逐页数据返回，可注入异常。"""

    def __init__(self, *, total: int, pages: list[list[dict]], condition=None, urls=None):
        self._total = total
        self._pages = pages
        self._condition = _condition() if condition is None else condition
        self._page_context = {"query": QUERY, "comp_id": "c1", "uuid": "u1"}

    def parse(self, query: str) -> QueryContext:
        return QueryContext(
            query=query,
            condition=self._condition,
            declared_total=self._total,
            page_context=self._page_context,
            comp_id="c1",
            uuid="u1",
        )

    def page(self, context, *, page: int, perpage: int) -> list[dict]:
        if page - 1 < len(self._pages):
            return self._pages[page - 1]
        return []


def _rows(*codes: str) -> list[dict]:
    return [{"股票代码": c, "股票简称": "x"} for c in codes]


def test_condition_fingerprint_ignores_date_rolling():
    """同一口径、日期窗口滚动时指纹不变，普通滚动不重复确认。"""
    a = condition_fingerprint(_condition("20250101-20260911"))
    b = condition_fingerprint(_condition("20250102-20260912"))
    assert a == b


def test_condition_fingerprint_changes_when_bounds_change():
    """新增限制（额外下限）会改变指纹，需重新确认。"""
    base = condition_fingerprint(_condition())
    extra = json.dumps(
        [
            {"node_type": "op", "uiText": "创120日新高"},
            {"node_type": "op", "uiText": "区间涨跌幅>0%"},
        ],
        ensure_ascii=False,
    )
    assert condition_fingerprint(extra) != base


def test_condition_labels_extract_ui_text():
    labels = condition_labels(_condition())
    assert labels and "创120日新高" in labels[0]


def test_acquire_is_complete_when_pages_match_declared_total():
    session = FakeWencai(total=3, pages=[_rows("000001.SZ", "600519.SH", "300750.SZ")])
    result = acquire(URL, session, perpage=100)
    assert result.completeness == "internally_consistent"
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519", "300750"]


def test_acquire_rejects_truncated_pages():
    session = FakeWencai(total=5, pages=[_rows("000001.SZ", "600519.SH")])
    with pytest.raises(WencaiIncomplete):
        acquire(URL, session, perpage=100)


def test_acquire_rejects_missing_declared_total():
    session = FakeWencai(total=None, pages=[_rows("000001.SZ")])  # type: ignore[arg-type]
    with pytest.raises(WencaiUnexpected):
        acquire(URL, session, perpage=100)


def test_acquire_confirms_true_zero_result_only_with_condition():
    session = FakeWencai(total=0, pages=[])
    result = acquire(URL, session, perpage=100)
    assert result.completeness == "confirmed_zero_result"
    assert result.candidates == ()

    no_condition = FakeWencai(total=0, pages=[], condition="")
    # 空字符串条件归一化后为空 → 无法确认口径，不能当作零结果
    with pytest.raises(WencaiUnexpected):
        acquire(URL, no_condition, perpage=100)


def test_rejects_non_wencai_link():
    with pytest.raises(UnsupportedLink):
        acquire("https://example.com/result?w=abc", FakeWencai(total=0, pages=[]))


# --- 服务与 HTTP 集成 ---

@pytest.fixture
def app_env(tmp_path):
    """离线配置：提交链接会触发导入后补取，不能让它落到真实来源。"""
    from conftest import offline_settings

    settings = offline_settings(tmp_path)
    clock = FixedClock(__import__("datetime").datetime(2026, 9, 11, 10, 0, 0))
    return settings, clock


def _install_fake(container, session: FakeWencai) -> None:
    container.imports._wencai = session
    container.cookies.save("fake-cookie-value")


def test_link_requires_cookie_then_publishes_after_confirmation(app_env):
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    _install_fake(container, FakeWencai(total=2, pages=[_rows("000001.SZ", "600519.SH")]))

    # 第一次：新查询 → 待确认
    batch = container.imports.submit_link(URL)
    assert batch.status is BatchStatus.AWAITING_CONFIRMATION
    assert batch.query_text == QUERY
    assert batch.condition_labels
    assert container.classification.list_candidates() == []  # 未确认前不发布

    # 用户确认 → 发布
    published = container.imports.confirm_link(batch.batch_id)
    assert published.status is BatchStatus.PUBLISHED
    assert published.recognized_count == 2
    assert {v.candidate.security_id for v in container.classification.list_candidates()} == {
        "000001.SZ",
        "600519.SH",
    }

    # 同一查询、同一口径再次导入 → 自动发布；同日重复只追加来源，不重复建项
    again = container.imports.submit_link(URL)
    assert again.status is BatchStatus.PUBLISHED
    assert again.new_candidate_count == 0 and again.merged_candidate_count == 0
    again_items = container.classification.list_candidates()
    assert len(again_items) == 2
    assert all(len(v.sources) == 2 for v in again_items)


def test_link_condition_change_requires_reconfirmation(app_env):
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    _install_fake(container, FakeWencai(total=1, pages=[_rows("000001.SZ")]))
    batch = container.imports.submit_link(URL)
    container.imports.confirm_link(batch.batch_id)

    # 口径变化（新增额外下限）→ 同一查询也要重新确认
    changed = json.dumps(
        [
            {"node_type": "op", "uiText": "创120日新高"},
            {"node_type": "op", "uiText": "区间涨跌幅>0%"},
        ],
        ensure_ascii=False,
    )
    container.imports._wencai = FakeWencai(total=1, pages=[_rows("000001.SZ")], condition=changed)
    recofirm = container.imports.submit_link(URL)
    assert recofirm.status is BatchStatus.AWAITING_CONFIRMATION


def test_different_queries_with_same_codes_do_not_share_confirmation(app_env):
    """冲突样本：两个不同查询返回完全相同的代码集合，也不得复用彼此的确认。

    查询身份取原句 sha256、与集合无关；集合偶然相同不构成同一查询。
    """
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    codes = ["000001.SZ", "600519.SH"]
    _install_fake(container, FakeWencai(total=2, pages=[_rows(*codes)]))

    # 查询 A：首次确认后发布
    first = container.imports.submit_link(URL)
    assert first.status is BatchStatus.AWAITING_CONFIRMATION
    published = container.imports.confirm_link(first.batch_id)
    assert published.status is BatchStatus.PUBLISHED

    # 查询 B：原句不同（不同 identity），但供应商返回集合完全相同
    other_url = URL.replace("sign=1", "sign=2").replace("%E5%88%9B%E6%96%B0%E9%AB%98", "%E5%88%9B%E6%96%B0%E4%BD%8E")
    other = container.imports.submit_link(other_url)
    assert other.identity_fingerprint != first.identity_fingerprint
    # 关键：不能因集合相同就复用 A 的确认自动发布
    assert other.status is BatchStatus.AWAITING_CONFIRMATION
    assert other.new_candidate_count == 0
    # 确认后仍归并到同一候选项（同一天同一股票只自动入选一次），不重复建项
    confirmed = container.imports.confirm_link(other.batch_id)
    assert confirmed.status is BatchStatus.PUBLISHED
    assert confirmed.new_candidate_count == 0
    assert confirmed.merged_candidate_count == 0
    assert len(container.classification.list_candidates()) == 2



def test_link_incomplete_batch_kept_with_reason_and_no_items(app_env):
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    _install_fake(container, FakeWencai(total=5, pages=[_rows("000001.SZ")]))
    batch = container.imports.submit_link(URL)
    assert batch.status is BatchStatus.REJECTED
    assert batch.error_code == "wencai_incomplete"
    assert container.classification.list_candidates() == []
    # 批次保留并可读回
    assert container.imports.get_batch(batch.batch_id) is not None


def test_link_zero_result_is_empty_not_all_unknown(app_env):
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    _install_fake(container, FakeWencai(total=0, pages=[]))
    batch = container.imports.submit_link(URL)
    assert batch.status is BatchStatus.EMPTY
    assert batch.error_code == "empty_source"


def test_link_without_cookie_is_rejected_with_hint(app_env):
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    # 未保存 Cookie
    batch = container.imports.submit_link(URL)
    assert batch.status is BatchStatus.REJECTED
    assert batch.error_code == "wencai_cookie_missing"


def test_http_link_flow_with_fake_session_and_restart_readback(app_env):
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    app = create_app(settings, clock)
    _install_fake(app.state.container, FakeWencai(total=2, pages=[_rows("000001.SZ", "600519.SH")]))

    with TestClient(app) as client:
        first = client.post("/api/imports", data={"texts": URL}).json()["batches"][0]
        assert first["status"] == "awaiting_confirmation"
        assert first["queryText"] == QUERY
        assert first["conditionLabels"]

        confirmed = client.post(f"/api/imports/{first['batchId']}/confirm").json()
        assert confirmed["status"] == "published"
        assert confirmed["recognizedCount"] == 2

    # 重启后读回
    app2 = create_app(settings, clock)
    with TestClient(app2) as client2:
        detail = client2.get(f"/api/imports/{first['batchId']}").json()
        assert detail["status"] == "published"
        assert detail["recognizedCount"] == 2
        assert len(client2.get("/api/classification/candidates").json()["candidates"]) == 2


def test_http_mixed_sources_independent_batches(app_env):
    """文件与文本混合提交：一个失败不阻止其他成功批次。"""
    settings, clock = app_env
    app = create_app(settings, clock)
    with TestClient(app) as client:
        response = client.post(
            "/api/imports",
            files=[("files", ("a.csv", make_csv("代码", "000001"), "text/csv"))],
            data={"texts": ["000001\n600519", "https://example.com/not-wencai?w=x"]},
        )
        batches = response.json()["batches"]
        assert len(batches) == 3
        assert batches[0]["status"] == "published"  # CSV 成功
        assert batches[1]["status"] == "published"  # 文本成功
        assert batches[2]["status"] == "rejected"  # 非问财链接失败
        # 成功的两个批次照常建项，失败的不影响
        items = client.get("/api/classification/candidates").json()["candidates"]
        assert {i["security"]["code"] for i in items} == {"000001", "600519"}


def test_http_selection_flow_awaits_then_resolves(app_env):
    settings, clock = app_env
    app = create_app(settings, clock)
    with TestClient(app) as client:
        response = client.post(
            "/api/imports",
            files=[("files", ("amb.csv", make_csv("a,b", "000001,600000"), "text/csv"))],
        )
        batch = response.json()["batches"][0]
        assert batch["status"] == "awaiting_selection"
        assert batch["selection"]["kind"] == "column"
        assert len(batch["selection"]["options"]) == 2

        resolved = client.post(
            f"/api/imports/{batch['batchId']}/selection", json={"codeColumn": 0}
        ).json()
        assert resolved["status"] == "published"
        assert resolved["recognizedCount"] == 1


def test_http_reidentify_removes_unknown_and_adds_recognized(app_env):
    settings, clock = app_env
    app = create_app(settings, clock)
    with TestClient(app) as client:
        batch = client.post(
            "/api/imports/csv",
            files={"file": ("r.csv", make_csv("代码", "000001", "999999"), "text/csv")},
        ).json()
        assert batch["skippedCount"] == 1

        # 名单未变：重新识别后仍未知的明细被删除，汇总保留
        after = client.post(f"/api/imports/{batch['batchId']}/reidentify").json()
        assert after["recognizedCount"] == 1
        assert after["skippedCount"] == 0
        assert after["reidentifiedRemoved"] == 1
        assert [s["rawCode"] for s in after["stocks"]] == ["000001"]
        # 导入事实（批次）仍可读回
        assert client.get(f"/api/imports/{batch['batchId']}").json()["recognizedCount"] == 1


def test_new_query_without_condition_does_not_auto_publish(app_env):
    """条件口径取不到时不能自动发布；即使查询曾被确认过也要重新确认。"""
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    _install_fake(container, FakeWencai(total=1, pages=[_rows("000001.SZ")], condition=""))
    batch = container.imports.submit_link(URL)
    assert batch.status is BatchStatus.AWAITING_CONFIRMATION
    assert container.classification.list_candidates() == []


def test_rejected_batch_can_be_retried_keeping_identity(app_env):
    """失败批次保留并可重试：不新建批次，导入日期不变。"""
    from dailyscreen_lite.app.container import build_container

    settings, clock = app_env
    container = build_container(settings, clock)
    # 首次未配 Cookie → 未发布
    failed = container.imports.submit_link(URL)
    assert failed.status is BatchStatus.REJECTED

    # 配置 Cookie 后重试同一批次：仍是原 batchId 与原导入日期
    _install_fake(container, FakeWencai(total=1, pages=[_rows("000001.SZ")]))
    container.cookies.save("fake-cookie-value")
    retried = container.imports.retry(failed.batch_id)
    assert retried.batch_id == failed.batch_id
    assert retried.import_date.iso == failed.import_date.iso
    assert retried.status is BatchStatus.AWAITING_CONFIRMATION


def test_http_retry_endpoint_keeps_batch_id(app_env):
    settings, clock = app_env
    app = create_app(settings, clock)
    with TestClient(app) as client:
        failed = client.post(
            "/api/imports",
            data={"texts": ["https://www.iwencai.com/screener/result?w=%E5%88%9B%E6%96%B0%E9%AB%98&querytype=stock&sign=1"]},
        ).json()["batches"][0]
        assert failed["status"] == "rejected"

        # 配置 Cookie 与固定获取器后重试
        _install_fake(app.state.container, FakeWencai(total=1, pages=[_rows("000001.SZ")]))
        client.put("/api/settings/wencai-cookie", json={"cookie": "fake"})
        retried = client.post(f"/api/imports/{failed['batchId']}/retry").json()
        assert retried["batchId"] == failed["batchId"]
        assert retried["importDate"] == failed["importDate"]
        assert retried["status"] == "awaiting_confirmation"


def test_http_reidentify_all_unknown_is_not_true_zero(app_env):
    settings, clock = app_env
    app = create_app(settings, clock)
    with TestClient(app) as client:
        batch = client.post(
            "/api/imports/csv",
            files={"file": ("u.csv", make_csv("代码", "999998", "999999"), "text/csv")},
        ).json()
        assert batch["status"] == "all_unknown"
        after = client.post(f"/api/imports/{batch['batchId']}/reidentify").json()
        # 仍未知：保持"全部未识别"，不得变成"真实零结果"
        assert after["status"] == "all_unknown"
        assert after["skippedCount"] == 0
        assert after["reidentifiedRemoved"] == 2


def test_settings_cookie_is_saved_without_exposing_value(app_env):
    settings, clock = app_env
    app = create_app(settings, clock)
    with TestClient(app) as client:
        saved = client.put("/api/settings/wencai-cookie", json={"cookie": "my-secret-cookie"}).json()
        assert saved["wencaiCookie"]["configured"] is True
        assert "my-secret-cookie" not in json.dumps(saved)
        status = client.get("/api/settings").json()
        assert status["wencaiCookie"]["configured"] is True
        assert "my-secret-cookie" not in json.dumps(status)
