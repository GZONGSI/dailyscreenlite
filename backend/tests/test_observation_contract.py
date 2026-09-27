"""观察工作表 HTTP 契约：请求边界与响应形状由 DTO 声明。

迁移前这些响应是手写字典、请求是裸 dict。这里逐端点固定「实际返回的键 == 迁移前的线格式
== 声明的响应模型字段」，使 `response_model` 不会静默过滤成员数量、观察关系或浏览上下文；
请求侧固定沿用原有的 400 提示语、缺省与显式 null 的区别、可空语义，
并逐条记录本片显式收紧的输入（非字符串组名与筛选值、含非字符串的组列表）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import assert_wire_contract, make_csv, offline_settings
from dailyscreen_lite.app.contract import FailurePayload
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.app.routes.observations import (
    DeleteGroupResponse,
    GroupListPayload,
    GroupNameCommand,
    MembershipPayload,
    ObservationGroupPayload,
    ObservationStatePayload,
    ObservationStockListPayload,
    ObservationViewChanges,
    ObservationViewPayload,
    ObservedStockPayload,
    StockDetailPayload,
)
from dailyscreen_lite.domain.clock import FixedClock

# 迁移前的线格式（用基线 `671bdea` 的手写装配对新 DTO 装配做过逐字对照，见证据日志）。
GROUP_KEYS = frozenset({"groupId", "name", "isDefault", "memberCount"})
STOCK_KEYS = frozenset({"securityId", "groupIds", "joinedAt", "security"})
STATE_KEYS = frozenset({"groupId", "sort", "currentSecurityId"})
VIEW_KEYS = frozenset({"groups", "stocks", "state"})
DETAIL_KEYS = frozenset({"securityId", "security", "groupIds", "candidate"})
MEMBERSHIP_KEYS = frozenset({"securityId", "groupIds"})


@contextmanager
def _observation_client(tmp_path: Path):
    """两只已归类为观察的股票 + 两个组，用于观察工作表契约用例。"""
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001", "600519"), "text/csv")},
        )
        candidates = client.get(
            "/api/classification/candidates?scope=unprocessed"
        ).json()["candidates"]
        default = client.get("/api/observations/groups").json()["groups"][0]
        extra = client.post("/api/observations/groups", json={"name": "核心"}).json()
        for candidate in candidates:
            client.post(
                f"/api/observations/candidates/{candidate['candidateId']}/observe",
                json={"groupIds": [default["groupId"]]},
            )
        yield client, default["groupId"], extra["groupId"]


# --- 响应形状 ---


def test_group_endpoints_declare_their_payloads(tmp_path):
    """组列表、建组与重命名都用同一个组模型；删除只回执被删的组。"""
    with _observation_client(tmp_path) as (client, default_id, extra_id):
        listed = client.get("/api/observations/groups").json()
        assert set(listed) == {"groups"}
        assert all(set(group) == set(GROUP_KEYS) for group in listed["groups"])
        assert GroupListPayload.model_validate(listed)

        created = client.post("/api/observations/groups", json={"name": "新组"}).json()
        assert_wire_contract(ObservationGroupPayload, created, GROUP_KEYS)
        assert created["memberCount"] == 0

        renamed = client.patch(
            f"/api/observations/groups/{created['groupId']}", json={"name": "改名"}
        ).json()
        assert_wire_contract(ObservationGroupPayload, renamed, GROUP_KEYS)
        assert renamed["name"] == "改名"

        removed = client.delete(f"/api/observations/groups/{created['groupId']}").json()
        assert_wire_contract(DeleteGroupResponse, removed, frozenset({"deleted"}))
        assert removed["deleted"] == created["groupId"]


def test_view_declares_groups_stocks_and_state(tmp_path):
    """观察工作表：组、观察列表与浏览上下文逐键与声明一致。"""
    with _observation_client(tmp_path) as (client, default_id, _):
        view = client.get("/api/observations/view").json()

    assert_wire_contract(ObservationViewPayload, view, VIEW_KEYS)
    assert_wire_contract(ObservationStatePayload, view["state"], STATE_KEYS)
    assert all(set(stock) == set(STOCK_KEYS) for stock in view["stocks"])
    assert all(
        set(group) == set(GROUP_KEYS) for group in view["groups"]
    )
    # 观察列表按证券去重：两只股票各一行，携带所属全部现存组
    assert {stock["securityId"] for stock in view["stocks"]} == {"000001.SZ", "600519.SH"}
    assert all(stock["groupIds"] == [default_id] for stock in view["stocks"])
    assert ObservedStockPayload.model_validate(view["stocks"][0]).joined_at


def test_stock_list_and_detail_declare_their_payloads(tmp_path):
    """轻量股票表按可选组筛选；共用详情带证券身份、观察关系与候选项。"""
    with _observation_client(tmp_path) as (client, default_id, extra_id):
        listed = client.get("/api/observations/securities").json()
        assert set(listed) == {"stocks"}
        assert len(listed["stocks"]) == 2
        assert ObservationStockListPayload.model_validate(listed)

        scoped = client.get(
            "/api/observations/securities", params={"groupId": extra_id}
        ).json()
        assert scoped["stocks"] == []

        detail = client.get("/api/observations/securities/000001.SZ").json()
        assert_wire_contract(StockDetailPayload, detail, DETAIL_KEYS)
        assert detail["groupIds"] == [default_id]
        assert detail["candidate"]["securityId"] == "000001.SZ"
        # 普通详情没有动作专属字段（与归类的详情模型同一口径）
        assert "listRevision" not in detail["candidate"]
        assert detail["security"]["securityId"] == "000001.SZ"


def test_memberships_and_focus_declare_their_payloads(tmp_path):
    """成员关系读写与跨模块打开都用同一份浏览上下文／关系模型。"""
    with _observation_client(tmp_path) as (client, default_id, extra_id):
        read = client.get(
            "/api/observations/memberships", params={"securityId": "000001.SZ"}
        ).json()
        assert_wire_contract(MembershipPayload, read, MEMBERSHIP_KEYS)
        assert read["groupIds"] == [default_id]

        saved = client.put(
            "/api/observations/memberships",
            json={"securityId": "000001.SZ", "groupIds": [extra_id]},
        ).json()
        assert_wire_contract(MembershipPayload, saved, MEMBERSHIP_KEYS)
        assert saved["groupIds"] == [extra_id]

        focused = client.post("/api/observations/securities/000001.SZ/focus").json()
        assert_wire_contract(ObservationViewPayload, focused, VIEW_KEYS)
        assert focused["state"]["currentSecurityId"] == "000001.SZ"
        assert focused["state"]["groupId"] is None


# --- 请求边界 ---


VIEW_CHANGES_MATRIX: tuple[tuple[dict, int, str | None], ...] = (
    ({}, 200, None),
    ({"unexpected": 1}, 200, None),
    ({"sort": "name"}, 200, None),
    ({"sort": None}, 200, None),
    ({"groupId": "all"}, 200, None),
    ({"groupId": None}, 200, None),
    ({"currentSecurityId": None}, 200, None),
    # 不存在的组照迁移前回落到汇总视图，不报错
    ({"groupId": "group-missing"}, 200, None),
    # 不可见的当前股票被清空，不报错
    ({"currentSecurityId": "600036.SH"}, 200, None),
    ({"sort": "unknown"}, 400, "未知排序方式：unknown"),
)


def test_view_changes_keep_legacy_judgements(tmp_path):
    """工作位置的部分修改：未提交与显式 null 的区别、未知排序的 400 都照旧。"""
    with _observation_client(tmp_path) as (client, default_id, extra_id):
        for payload, status, message in VIEW_CHANGES_MATRIX:
            response = client.put("/api/observations/view", json=payload)
            assert response.status_code == status, (payload, response.text)
            if message is not None:
                assert response.json()["detail"]["message"] == message, payload

        # 未提交 groupId：保持原筛选；显式 null：回到汇总视图
        selected = client.put("/api/observations/view", json={"groupId": extra_id}).json()
        assert selected["state"]["groupId"] == extra_id
        for payload, expected in (({"sort": "name"}, extra_id), ({"groupId": None}, None)):
            changed = client.put("/api/observations/view", json=payload).json()
            assert changed["state"]["groupId"] == expected, payload
        assert ObservationViewChanges.model_validate({}).model_dump(exclude_unset=True) == {}
        assert ObservationViewChanges.model_validate({"groupId": None}).model_dump(
            by_alias=True, exclude_unset=True, mode="json"
        ) == {"groupId": None}


def test_group_names_and_membership_boundaries(tmp_path):
    """组名与成员关系的既有 400／404 与提示语不变；空组列表仍是退出全部组。"""
    with _observation_client(tmp_path) as (client, default_id, extra_id):
        for payload, message in (
            ({}, "观察组名称不能为空"),
            ({"name": "   "}, "观察组名称不能为空"),
            ({"name": "核心"}, "已存在同名观察组：核心"),
        ):
            response = client.post("/api/observations/groups", json=payload)
            assert response.status_code == 400, (payload, response.text)
            assert response.json()["detail"]["message"] == message

        missing = client.patch(
            "/api/observations/groups/group-missing", json={"name": "新名"}
        )
        assert missing.status_code == 404
        assert missing.json()["detail"]["message"].startswith("观察组不存在")

        no_security = client.put("/api/observations/memberships", json={"groupIds": []})
        assert no_security.status_code == 400
        assert no_security.json()["detail"]["message"] == "缺少证券标识"

        # 空组列表（缺省、显式 null 与 []）都是退出全部组
        for payload in ({"securityId": "000001.SZ"}, {"securityId": "000001.SZ", "groupIds": None}):
            cleared = client.put("/api/observations/memberships", json=payload)
            assert cleared.status_code == 200, (payload, cleared.text)
            assert cleared.json()["groupIds"] == []

        unknown_group = client.put(
            "/api/observations/memberships",
            json={"securityId": "000001.SZ", "groupIds": ["group-missing"]},
        )
        assert unknown_group.status_code == 400
        assert unknown_group.json()["detail"]["message"] == "观察组不存在：group-missing"

        not_imported = client.put(
            "/api/observations/memberships",
            json={"securityId": "999999.SZ", "groupIds": [default_id]},
        )
        assert not_imported.status_code == 404
        assert not_imported.json()["detail"]["message"].startswith("股票未经导入与识别")

        assert GroupNameCommand.model_validate({"name": None}).name is None


def test_non_string_values_are_structural_errors(tmp_path):
    """本片显式收紧的输入：类型不对的组名与工作位置值改由结构校验拒绝。

    迁移前它们会被 `str()` 成字符串再使用（数字组名会真的建成一个组）。
    """
    with _observation_client(tmp_path) as (client, default_id, _):
        assert client.post("/api/observations/groups", json={"name": 5}).status_code == 422
        assert client.patch(
            f"/api/observations/groups/{default_id}", json={"name": 5}
        ).status_code == 422
        assert client.put("/api/observations/view", json={"groupId": 5}).status_code == 422
        assert client.put(
            "/api/observations/view", json={"currentSecurityId": 5}
        ).status_code == 422
        assert client.put("/api/observations/view", json={"sort": 5}).status_code == 422
        assert client.put(
            "/api/observations/memberships",
            json={"securityId": "000001.SZ", "groupIds": [7]},
        ).status_code == 422


def test_failure_envelopes_are_unchanged(tmp_path):
    """业务失败仍是 `{"detail": {"message": ...}}`，且与声明的模型一致。"""
    with _observation_client(tmp_path) as (client, default_id, _):
        unknown = client.get("/api/observations/securities/999999.SZ")
        assert unknown.status_code == 404
        unknown_group_list = client.get(
            "/api/observations/securities", params={"groupId": "group-missing"}
        )
        assert unknown_group_list.status_code == 404

    assert set(unknown.json()) == {"detail"}
    assert isinstance(FailurePayload.model_validate(unknown.json()).detail.message, str)
    assert unknown_group_list.json()["detail"]["message"].startswith("观察组不存在")


def test_non_object_bodies_are_structural_errors(tmp_path):
    """没有请求体、JSON null 与非对象 JSON 仍由框架拒绝：422 且定位到请求体。"""
    frames: tuple[dict[str, object], ...] = (
        {},
        {"content": b"null", "headers": {"Content-Type": "application/json"}},
        {"content": b"[1,2]", "headers": {"Content-Type": "application/json"}},
        {"content": b'"x"', "headers": {"Content-Type": "application/json"}},
    )
    with _observation_client(tmp_path) as (client, default_id, _):
        for kwargs in frames:
            for call in (
                lambda: client.post("/api/observations/groups", **kwargs),
                lambda: client.put("/api/observations/view", **kwargs),
                lambda: client.put("/api/observations/memberships", **kwargs),
            ):
                response = call()
                assert response.status_code == 422, (kwargs, response.text)
                detail = response.json()["detail"]
                assert detail[0]["loc"] == ["body"], (kwargs, detail)
                assert set(detail[0]) == {"type", "loc", "msg", "input"}, (kwargs, detail)
