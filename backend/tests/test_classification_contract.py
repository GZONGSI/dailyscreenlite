"""候选归类 HTTP 契约：请求边界与响应形状由 DTO 声明。

迁移前这些响应是手写字典、请求是裸 dict。这里逐端点固定「实际返回的键 == 声明的响应
模型字段」，使 `response_model` 不会静默过滤列表版本、导航修订号、移除与行序字段；
请求侧固定沿用原有的 400 提示语、严格整数与「未提交 vs 显式 null」的区别，
并逐条记录本片显式收紧的输入（非字符串筛选值、非数组 groupIds）。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import assert_wire_contract, make_csv, offline_settings
from dailyscreen_lite.app.contract import FailurePayload
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.app.routes.classification import (
    BrowsePayload,
    CandidateActionPayload,
    CandidatePayload,
    CleanupResult,
    CommandPayload,
    ObservationCandidatePayload,
    ObservationSavePayload,
    ViewChanges,
)
from dailyscreen_lite.domain.clock import FixedClock

ACTION_KEYS = {
    "listRevision",
    "listRevisionBefore",
    "listRemoved",
    "navigationRevision",
    "listOrder",
}

# 迁移前的线格式（用基线 `952a8ae` 的手写装配对新 DTO 装配做过逐字对照，见
# `.scratch/wire-format-compare.log`）。固定成清单而不是只比对模型本身：
# 只比「响应 == 模型」时，模型漏声明一个字段会让两边一起少，悄悄过滤看不出来。
DETAIL_KEYS = frozenset(
    {
        "actionResult",
        "candidateId",
        "firstSeenAt",
        "groupIds",
        "history",
        "importDates",
        "lastActionAt",
        "latestImportDate",
        "noteCount",
        "observed",
        "security",
        "securityId",
        "selections",
        "sourceBatchIds",
        "sources",
        "state",
        "viewedAt",
    }
)
ROW_KEYS = frozenset(
    {
        "candidateId",
        "latestImportDate",
        "observed",
        "security",
        "securityId",
        "sourceCount",
        "state",
        "viewedAt",
    }
)
STATE_KEYS = frozenset(
    {
        "currentCandidate",
        "currentCandidateId",
        "cursor",
        "ended",
        "filterScope",
        "hasNext",
        "importDate",
        "inFilter",
        "listRevision",
        "navigationRevision",
        "path",
        "result",
        "round",
        "roundStartedAt",
        "scope",
        "search",
        "summary",
        "viewMode",
    }
)
BROWSE_KEYS = STATE_KEYS | {"rows", "pending", "dates"}
COMMAND_KEYS = STATE_KEYS | {"changes"}
DELTA_KEYS = frozenset({"changed", "removed", "order"})
CLEANUP_KEYS = frozenset({"clearedCount"})
ACTION_KEYS_FULL = DETAIL_KEYS | ACTION_KEYS
LINKAGE_KEYS = frozenset(
    {
        "groupIds",
        "candidate",
        "listRevision",
        "listRevisionBefore",
        "listRemoved",
        "navigationRevision",
    }
)


@contextmanager
def _classification_client(tmp_path: Path):
    """三只已导入候选 + 一个观察组，用于归类与联动契约用例。"""
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={
                "file": ("a.csv", make_csv("代码", "000001", "600519", "300750"), "text/csv")
            },
        )
        listed = client.get("/api/classification/candidates").json()["candidates"]
        ids = [item["candidateId"] for item in listed]
        group = client.post("/api/observations/groups", json={"name": "核心"}).json()
        yield client, ids, group["groupId"]


def _open(client, candidate_id: str) -> dict:
    """就地打开某只候选并返回新的浏览结果。"""
    response = client.put(
        "/api/classification/view", json={"currentCandidateId": candidate_id}
    )
    assert response.status_code == 200, response.text
    return response.json()


# --- 响应形状：普通详情、浏览、导航增量、动作与联动各是完整声明 ---


def test_candidate_detail_declares_the_plain_fields(tmp_path):
    """普通详情是 CandidatePayload：没有动作专属字段，也没有字段被响应模型过滤。"""
    with _classification_client(tmp_path) as (client, ids, _):
        detail = client.get(f"/api/classification/candidates/{ids[0]}").json()

    assert_wire_contract(CandidatePayload, detail, DETAIL_KEYS)
    assert not (ACTION_KEYS & set(detail))
    assert CandidatePayload.model_validate(detail).candidate_id == ids[0]


def test_browse_response_declares_lists_and_versions(tmp_path):
    """完整浏览带当前卡、完整轻量列表、数量与列表版本，逐键与声明一致。"""
    with _classification_client(tmp_path) as (client, ids, _):
        browse = _open(client, ids[0])

    assert_wire_contract(BrowsePayload, browse, BROWSE_KEYS)
    assert all(set(row) == set(ROW_KEYS) for row in browse["rows"])
    assert BrowsePayload.model_validate(browse).current_candidate_id == ids[0]


def test_command_response_declares_delta_and_no_rows(tmp_path):
    """导航增量只带列表变化，不含整份列表；列表版本与浏览状态同键。"""
    with _classification_client(tmp_path) as (client, ids, _):
        opened = _open(client, ids[0])
        command = client.post(
            "/api/classification/view/navigate",
            json={
                "direction": "next",
                "expectedCursor": opened["cursor"],
                "expectedRevision": opened["navigationRevision"],
            },
        ).json()

    assert_wire_contract(CommandPayload, command, COMMAND_KEYS)
    assert set(command["changes"]) == set(DELTA_KEYS)
    assert CommandPayload.model_validate(command).changes.order == command["changes"]["order"]


def test_action_response_declares_every_version_field(tmp_path):
    """归类动作结果带写入前后的列表版本、本次移除的行与写入后的行序。"""
    with _classification_client(tmp_path) as (client, ids, _):
        # 「稍后处理」移尾：仍留在待归类池（removed 为空），但真的改变了池内顺序
        later = client.post(f"/api/classification/candidates/{ids[0]}/later").json()
        # 「暂不关注」结束本次归类：这一只离开待归类列表
        dismissed = client.post(f"/api/classification/candidates/{ids[1]}/dismiss").json()

    assert_wire_contract(CandidateActionPayload, later, ACTION_KEYS_FULL)
    assert later["listRemoved"] == []
    assert later["listOrder"] == [ids[1], ids[2], ids[0]]
    assert dismissed["listRemoved"] == [ids[1]]
    assert CandidateActionPayload.model_validate(later).list_order == later["listOrder"]


def test_linkage_action_reuses_the_action_contract(tmp_path):
    """观察联动沿用同一份动作字段：嵌套候选项与顶层给出同一批版本号，但没有行序。"""
    with _classification_client(tmp_path) as (client, ids, group_id):
        detail = client.get(f"/api/classification/candidates/{ids[0]}").json()
        saved = client.post(
            f"/api/observations/candidates/{detail['candidateId']}/observe",
            json={"groupIds": [group_id]},
        ).json()

    assert_wire_contract(ObservationSavePayload, saved, LINKAGE_KEYS)
    assert_wire_contract(
        ObservationCandidatePayload, saved["candidate"], DETAIL_KEYS | ACTION_KEYS - {"listOrder"}
    )
    for key in ("listRevision", "listRevisionBefore", "listRemoved", "navigationRevision"):
        assert saved["candidate"][key] == saved[key]
    assert ObservationSavePayload.model_validate(saved).group_ids == [group_id]


# --- 请求边界：严格整数、枚举提示语与未提交/显式 null ---


NAVIGATION_MATRIX: tuple[tuple[dict, int, str | None], ...] = (
    ({"direction": "next", "expectedCursor": 0, "expectedRevision": 0}, 200, None),
    ({"direction": "next", "expectedCursor": True, "expectedRevision": 0}, 400, "缺少有效的 expectedCursor"),
    ({"direction": "next", "expectedCursor": "0", "expectedRevision": 0}, 400, "缺少有效的 expectedCursor"),
    ({"direction": "next", "expectedRevision": 0}, 400, "缺少有效的 expectedCursor"),
    ({"direction": "next", "expectedCursor": 0, "expectedRevision": True}, 400, "缺少有效的 expectedRevision"),
    ({"direction": "next", "expectedCursor": 0, "expectedRevision": "0"}, 400, "缺少有效的 expectedRevision"),
    ({"direction": "next", "expectedCursor": 0}, 400, "缺少有效的 expectedRevision"),
    ({"direction": "sideways", "expectedCursor": 0, "expectedRevision": 0}, 400, "未知浏览方向：sideways"),
    ({}, 400, "缺少有效的 expectedCursor"),
)


def test_navigation_command_keeps_strict_revisions(tmp_path):
    """严格 cursor／revision 不接受布尔值、字符串数字与缺失，提示语与状态码不变。"""
    with _classification_client(tmp_path) as (client, _, _):
        for payload, status, message in NAVIGATION_MATRIX:
            response = client.post("/api/classification/view/navigate", json=payload)
            assert response.status_code == status, (payload, response.text)
            if message is not None:
                assert response.json()["detail"]["message"] == message, payload


VIEW_CHANGES_MATRIX: tuple[tuple[dict, int, str | None], ...] = (
    ({}, 200, None),
    ({"unexpected": 1}, 200, None),
    ({"scope": "processed"}, 200, None),
    ({"viewMode": "list"}, 200, None),
    ({"result": "dismissed"}, 200, None),
    # 空结果值照迁移前等同主动清空，不是错误
    ({"result": ""}, 200, None),
    ({"scope": None}, 200, None),
    ({"scope": "bogus"}, 400, "'bogus' is not a valid CandidateScope"),
    ({"result": "bogus"}, 400, "'bogus' is not a valid CandidateState"),
    ({"viewMode": "grid"}, 400, "未知视图模式：grid"),
    ({"currentCandidateId": "missing"}, 404, "候选项不存在：missing"),
)


def test_view_changes_keep_legacy_judgements(tmp_path):
    """改筛选的取值判定仍是原来的 400／404 与原提示语，多余字段照旧忽略。"""
    with _classification_client(tmp_path) as (client, _, _):
        for payload, status, message in VIEW_CHANGES_MATRIX:
            response = client.put("/api/classification/view", json=payload)
            assert response.status_code == status, (payload, response.text)
            if message is not None:
                assert response.json()["detail"]["message"] == message, payload


def test_view_changes_keep_unset_and_explicit_null_apart(tmp_path):
    """未提交的字段不动，显式 null 才是主动清空（`exclude_unset` 而不是 `exclude_none`）。"""
    with _classification_client(tmp_path) as (client, ids, _):
        _open(client, ids[0])
        dated = client.put(
            "/api/classification/view", json={"importDate": "2026-09-11"}
        ).json()
        assert dated["importDate"] == "2026-09-11"

        # 未提交 importDate：保持原筛选
        untouched = client.put("/api/classification/view", json={"search": "000001"}).json()
        assert untouched["importDate"] == "2026-09-11"

        # 显式 null：主动清空
        cleared = client.put("/api/classification/view", json={"importDate": None}).json()
        assert cleared["importDate"] is None


def test_cleanup_command_and_result_are_declared(tmp_path):
    """清理的输入与回执也来自契约：可以不带请求体，`importDate` 为 null 表示不限日期。"""
    with _classification_client(tmp_path) as (client, ids, _):
        first = client.post("/api/classification/cleanup", json={"importDate": None})
        assert first.status_code == 200, first.text
        assert_wire_contract(CleanupResult, first.json(), CLEANUP_KEYS)
        assert first.json()["clearedCount"] == 3
        # 已经清理过：重复提交仍是同一个回执形状，没有可清理项就是 0
        for kwargs in ({}, {"json": {}}, {"json": {"importDate": None}}):
            response = client.post("/api/classification/cleanup", **kwargs)
            assert response.status_code == 200, response.text
            assert set(response.json()) == {"clearedCount"}
            assert response.json()["clearedCount"] == 0
        # 限定日期的正常用法仍被接受（界面把当前筛选日期原样回传）
        assert client.post(
            "/api/classification/cleanup", json={"importDate": "2026-09-11"}
        ).status_code == 200
        # 线格式仍是字符串：数字日期由结构校验拒绝（与 PUT /view 同一收紧）
        assert client.post(
            "/api/classification/cleanup", json={"importDate": 20260911}
        ).status_code == 422


def test_non_string_view_values_are_structural_errors(tmp_path):
    """本片显式收紧的输入：类型不对的筛选值、方向与非数组 groupIds 改由结构校验拒绝。

    迁移前它们分别是「直接存进状态」「按字符串逐字符拆成组标识」的宽松转换，
    这里逐条固定新边界，避免把收紧当成隐含副作用。
    """
    with _classification_client(tmp_path) as (client, ids, group_id):
        assert client.put("/api/classification/view", json={"importDate": 20260911}).status_code == 422
        assert client.put("/api/classification/view", json={"search": 123}).status_code == 422
        assert client.put("/api/classification/view", json={"currentCandidateId": 5}).status_code == 422
        assert client.put("/api/classification/view", json={"viewMode": 5}).status_code == 422
        for direction in (5, None):
            assert client.post(
                "/api/classification/view/navigate",
                json={"direction": direction, "expectedCursor": 0, "expectedRevision": 0},
            ).status_code == 422
        assert client.post(
            f"/api/observations/candidates/{ids[0]}/observe", json={"groupIds": "abc"}
        ).status_code == 422
        assert client.post(
            f"/api/observations/candidates/{ids[0]}/observe", json={"groupIds": [group_id, 7]}
        ).status_code == 422


def test_view_changes_model_matches_the_wire(tmp_path):
    """DTO 自己也要能吃进真实请求：未提交字段缺省、显式 null 保留。"""
    assert set(ViewChanges.model_validate({}).model_dump(exclude_unset=True)) == set()
    cleared = ViewChanges.model_validate({"importDate": None}).model_dump(
        by_alias=True, exclude_unset=True, mode="json"
    )
    assert cleared == {"importDate": None}
    converted = ViewChanges.model_validate({"result": ""}).model_dump(
        by_alias=True, exclude_unset=True, mode="json"
    )
    assert converted == {"result": None}


# --- 失败与幂等边界 ---


def test_failure_envelopes_are_unchanged(tmp_path):
    """业务失败仍是 `{"detail": {"message": ...}}`：400 状态不允许、404 候选项不存在。"""
    with _classification_client(tmp_path) as (client, ids, group_id):
        client.post(f"/api/classification/candidates/{ids[0]}/dismiss")
        blocked = client.post(f"/api/classification/candidates/{ids[0]}/later")
        assert blocked.status_code == 400
        assert blocked.json()["detail"]["message"] == "已处理的候选项不能稍后处理"

        missing = client.get("/api/classification/candidates/missing")
        assert missing.status_code == 404
        assert missing.json()["detail"]["message"].startswith("候选项不存在")

        empty_groups = client.post(
            f"/api/observations/candidates/{ids[1]}/observe", json={"groupIds": []}
        )
        assert empty_groups.status_code == 400
        assert empty_groups.json()["detail"]["message"].startswith("请至少选择一个观察组")
        # 显式 null 与缺失同义（都表示没选组），仍由服务判定成 400 而不是结构错误
        for empty in ({}, {"groupIds": None}):
            same = client.post(
                f"/api/observations/candidates/{ids[1]}/observe", json=empty
            )
            assert same.status_code == 400
            assert same.json()["detail"]["message"].startswith("请至少选择一个观察组")

        unknown_linkage = client.post(
            "/api/observations/candidates/missing/observe", json={"groupIds": [group_id]}
        )
        assert unknown_linkage.status_code == 404

    # 失败信封与声明的模型一致（不是框架默认的 422 形状）
    assert set(blocked.json()) == {"detail"} == set(
        FailurePayload.model_json_schema()["properties"]
    )
    assert isinstance(FailurePayload.model_validate(missing.json()).detail.message, str)


def test_non_object_bodies_are_structural_errors(tmp_path):
    """没有请求体、JSON null 与非对象 JSON 仍由框架拒绝：422 且定位到请求体。"""
    frames: tuple[dict[str, object], ...] = (
        {},
        {"content": b"null", "headers": {"Content-Type": "application/json"}},
        {"content": b"[1,2]", "headers": {"Content-Type": "application/json"}},
        {"content": b'"x"', "headers": {"Content-Type": "application/json"}},
    )
    with _classification_client(tmp_path) as (client, ids, group_id):
        for kwargs in frames:
            for call in (
                lambda: client.post("/api/classification/view/navigate", **kwargs),
                lambda: client.put("/api/classification/view", **kwargs),
                lambda: client.post(
                    f"/api/observations/candidates/{ids[0]}/observe", **kwargs
                ),
            ):
                response = call()
                assert response.status_code == 422, (kwargs, response.text)
                detail = response.json()["detail"]
                # 结构校验的信封保持：定位在 body，字段仍是 type/loc/msg/input
                assert detail[0]["loc"] == ["body"], (kwargs, detail)
                assert set(detail[0]) == {"type", "loc", "msg", "input"}, (kwargs, detail)
        # 空 groupIds 的请求体本身是合法对象：业务判定留给服务
        assert client.post(
            f"/api/observations/candidates/{ids[0]}/observe", json={"groupIds": []}
        ).status_code == 400


def test_navigation_replay_keeps_the_position(tmp_path):
    """迟到或重复的导航不改位置，只回报当前真实位置与同一个列表版本。"""
    with _classification_client(tmp_path) as (client, ids, _):
        opened = _open(client, ids[0])
        request = {
            "direction": "next",
            "expectedCursor": opened["cursor"],
            "expectedRevision": opened["navigationRevision"],
        }
        moved = client.post("/api/classification/view/navigate", json=request).json()
        replayed = client.post("/api/classification/view/navigate", json=request).json()
        again = client.post("/api/classification/view/navigate", json=request).json()

    assert moved["currentCandidateId"] != opened["currentCandidateId"]
    for duplicate in (replayed, again):
        assert duplicate["currentCandidateId"] == moved["currentCandidateId"]
        assert duplicate["changes"] == {"changed": [], "removed": [], "order": []}
        assert duplicate["listRevision"] == moved["listRevision"]
