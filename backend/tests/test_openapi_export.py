"""OpenAPI 导出工具：隔离、确定性、只含 /api，生成物与当前 DTO 一致。

对应实施边界（框架报告 B4）：不能用默认 `create_app().openapi()` 直接导出——它会
建目录、建库并初始化种子，所以导出必须落在临时目录里，且不进入 lifespan。
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "tools"))

import export_openapi as tool  # noqa: E402


def test_isolated_settings_do_not_point_at_runtime_data(tmp_path):
    """导出用的配置只能指向临时工作目录：不读运行库、不装问财、不开调度与状态来源。"""
    workspace = tmp_path / "ws"
    settings = tool.isolated_settings(workspace)

    assert settings.data_dir == workspace / "data"
    assert settings.data_dir != tool.REPO_ROOT / "data"
    assert settings.securities_snapshot == tool.DELIVERED_SNAPSHOT
    assert settings.wencai_token_bundle is None
    assert settings.market_status_enabled is False
    assert settings.update_schedule_enabled is False
    assert settings.quotes_fixture is not None
    assert settings.quotes_fixture.read_text(encoding="utf-8") == "{}"


def test_export_is_deterministic_and_only_api(tmp_path):
    """重复导出逐字一致；只含 /api 路径；运行库确实建在临时目录里。

    跨运行的一致性由 `test_committed_schema_matches_current_models` 保证：那份 schema
    是上一次运行生成的，能与本次导出逐字相同，说明输出里没有时间戳之类的运行期内容。
    """
    first = tool.render(tool.build_schema(tmp_path / "one"))
    second = tool.render(tool.build_schema(tmp_path / "two"))

    assert first == second
    # 隔离：容器的运行库落在传入的工作目录，不碰仓库 data/
    assert (tmp_path / "one" / "data" / "runtime" / "dailyscreen_lite.sqlite3").exists()

    schema = tool.build_schema(tmp_path / "three")
    assert schema["paths"], "导出不应为空"
    assert all(path.startswith("/api") for path in schema["paths"]), schema["paths"].keys()


def test_notes_endpoints_expose_the_contract(tmp_path):
    """四个笔记端点在 schema 里带着 DTO：请求体、200 响应与 400/404 失败形状。"""
    schema = tool.build_schema(tmp_path / "ws")

    def component(name: str) -> str:
        return f"#/components/schemas/{name}"

    stream_path = schema["paths"]["/api/notes/securities/{security_id}"]
    assert stream_path["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": component("NoteStreamPayload")
    }
    assert stream_path["get"]["responses"]["404"]["content"]["application/json"]["schema"] == {
        "$ref": component("FailurePayload")
    }
    assert stream_path["post"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": component("NoteWrite")
    }
    assert stream_path["post"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": component("NotePayload")
    }
    assert stream_path["post"]["responses"]["400"]["content"]["application/json"]["schema"] == {
        "$ref": component("FailurePayload")
    }

    note_path = schema["paths"]["/api/notes/{note_id}"]
    assert note_path["patch"]["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": component("NoteWrite")
    }
    assert note_path["patch"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": component("NotePayload")
    }
    assert note_path["delete"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": component("DeleteNoteResponse")
    }

    # 字段名与可空性以服务端模型为准：camelCase、正文任意、时间串是字符串
    schemas = schema["components"]["schemas"]
    assert sorted(schemas["NotePayload"]["properties"]) == [
        "body",
        "noteId",
        "securityId",
        "updatedAt",
    ]
    assert schemas["NotePayload"]["required"] == ["noteId", "securityId", "body", "updatedAt"]
    assert schemas["NoteWrite"]["properties"]["body"] == {"title": "Body"}
    assert schemas["FailurePayload"]["properties"]["detail"] == {
        "$ref": component("FailureDetail")
    }


def test_classification_endpoints_expose_the_contract(tmp_path):
    """归类响应图与请求体都在 schema 里：详情、浏览、导航增量、动作与联动各是完整模型。

    迁移前这些响应是手写字典、请求是裸 dict；这里固定「每个端点用哪个模型」以及
    动作专属字段（列表版本／移除／行序）确实被声明，不会被 response_model 过滤。
    """
    schema = tool.build_schema(tmp_path / "ws")

    def component(name: str) -> str:
        return f"#/components/schemas/{name}"

    def response(path: str, method: str, status: str) -> dict:
        return schema["paths"][path][method]["responses"][status]["content"]["application/json"][
            "schema"
        ]

    def body(path: str, method: str) -> dict:
        return schema["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]

    assert response("/api/classification/candidates/{candidate_id}", "get", "200") == {
        "$ref": component("CandidatePayload")
    }
    assert response("/api/classification/candidates/{candidate_id}", "get", "404") == {
        "$ref": component("FailurePayload")
    }
    assert response("/api/classification/view", "get", "200") == {
        "$ref": component("BrowsePayload")
    }
    assert body("/api/classification/view", "put") == {"$ref": component("ViewChanges")}
    assert response("/api/classification/view", "put", "200") == {
        "$ref": component("BrowsePayload")
    }
    assert response("/api/classification/view", "put", "400") == {
        "$ref": component("FailurePayload")
    }
    assert body("/api/classification/view/navigate", "post") == {
        "$ref": component("NavigationCommand")
    }
    assert response("/api/classification/view/navigate", "post", "200") == {
        "$ref": component("CommandPayload")
    }
    assert response("/api/classification/candidates/{candidate_id}/later", "post", "200") == {
        "$ref": component("CandidateActionPayload")
    }
    assert response("/api/classification/candidates/{candidate_id}/later", "post", "400") == {
        "$ref": component("FailurePayload")
    }
    assert body("/api/observations/candidates/{candidate_id}/observe", "post") == {
        "$ref": component("CandidateGroupsCommand")
    }
    assert response("/api/observations/candidates/{candidate_id}/observe", "post", "200") == {
        "$ref": component("ObservationSavePayload")
    }
    assert body("/api/classification/cleanup", "post")["anyOf"] == [
        {"$ref": component("CleanupCommand")},
        {"type": "null"},
    ]
    assert response("/api/classification/cleanup", "post", "200") == {
        "$ref": component("CleanupResult")
    }

    schemas = schema["components"]["schemas"]
    detail = schemas["CandidatePayload"]["properties"]
    action = schemas["CandidateActionPayload"]["properties"]
    written = {"listRevision", "listRevisionBefore", "listRemoved", "navigationRevision"}
    # 普通详情不带动作字段；动作结果才带，行序只在归类动作里
    assert written.isdisjoint(detail) and "listOrder" not in detail
    assert written | {"listOrder"} <= set(action)
    assert written <= set(schemas["ObservationCandidatePayload"]["properties"])
    assert "listOrder" not in schemas["ObservationCandidatePayload"]["properties"]

    # 必需／可省略与可空如实表达；枚举来自领域模型
    assert schemas["CandidateState"]["enum"] == [
        "pending",
        "later",
        "dismissed",
        "observed",
        "cleared",
    ]
    assert schemas["CandidateScope"]["enum"] == ["unprocessed", "processed"]
    assert schemas["ViewChanges"].get("required") is None
    assert schemas["ViewChanges"]["properties"]["result"]["anyOf"] == [
        {"$ref": component("CandidateState")},
        {"type": "null"},
    ]
    assert schemas["NavigationCommand"]["required"] == [
        "expectedCursor",
        "expectedRevision",
    ]
    for field in ("expectedCursor", "expectedRevision"):
        assert schemas["NavigationCommand"]["properties"][field]["type"] == "integer"


def test_observation_endpoints_expose_the_contract(tmp_path):
    """观察工作表的组管理、浏览上下文、列表、详情与成员关系都在 schema 里。"""
    schema = tool.build_schema(tmp_path / "ws")

    def component(name: str) -> str:
        return f"#/components/schemas/{name}"

    def response(path: str, method: str, status: str) -> dict:
        return schema["paths"][path][method]["responses"][status]["content"]["application/json"][
            "schema"
        ]

    def body(path: str, method: str) -> dict:
        return schema["paths"][path][method]["requestBody"]["content"]["application/json"]["schema"]

    assert response("/api/observations/groups", "get", "200") == {
        "$ref": component("GroupListPayload")
    }
    assert body("/api/observations/groups", "post") == {"$ref": component("GroupNameCommand")}
    assert response("/api/observations/groups", "post", "200") == {
        "$ref": component("ObservationGroupPayload")
    }
    assert response("/api/observations/groups", "post", "400") == {
        "$ref": component("FailurePayload")
    }
    assert body("/api/observations/groups/{group_id}", "patch") == {
        "$ref": component("GroupNameCommand")
    }
    assert response("/api/observations/groups/{group_id}", "delete", "200") == {
        "$ref": component("DeleteGroupResponse")
    }
    assert response("/api/observations/view", "get", "200") == {
        "$ref": component("ObservationViewPayload")
    }
    assert body("/api/observations/view", "put") == {
        "$ref": component("ObservationViewChanges")
    }
    assert response("/api/observations/view", "put", "400") == {
        "$ref": component("FailurePayload")
    }
    assert response("/api/observations/securities", "get", "200") == {
        "$ref": component("ObservationStockListPayload")
    }
    assert response("/api/observations/securities/{security_id}", "get", "200") == {
        "$ref": component("StockDetailPayload")
    }
    assert response("/api/observations/securities/{security_id}", "get", "404") == {
        "$ref": component("FailurePayload")
    }
    assert response("/api/observations/securities/{security_id}/focus", "post", "200") == {
        "$ref": component("ObservationViewPayload")
    }
    assert response("/api/observations/memberships", "get", "200") == {
        "$ref": component("MembershipPayload")
    }
    assert body("/api/observations/memberships", "put") == {
        "$ref": component("MembershipCommand")
    }

    schemas = schema["components"]["schemas"]
    view = schemas["ObservationViewPayload"]["properties"]
    assert sorted(view) == ["groups", "state", "stocks"]
    assert sorted(schemas["ObservedStockPayload"]["properties"]) == [
        "groupIds",
        "joinedAt",
        "security",
        "securityId",
    ]
    assert sorted(schemas["ObservationStatePayload"]["properties"]) == [
        "currentSecurityId",
        "groupId",
        "sort",
    ]
    # 详情复用归类的候选详情模型：普通读取不带动作专属字段
    assert schemas["StockDetailPayload"]["properties"]["candidate"]["anyOf"] == [
        {"$ref": component("CandidatePayload")},
        {"type": "null"},
    ]
    # 可省略与可空如实表达：请求体的三个字段都可省，组列表可空
    assert schemas["ObservationViewChanges"].get("required") is None
    assert schemas["MembershipCommand"]["properties"]["groupIds"]["anyOf"] == [
        {"items": {"type": "string"}, "type": "array"},
        {"type": "null"},
    ]
    # 失败信封只有一处声明：三个功能复用同一个模型
    assert schemas["FailurePayload"]["properties"]["detail"] == {
        "$ref": component("FailureDetail")
    }


def test_committed_schema_matches_current_models(tmp_path):
    """重新生成无差异：仓库里的 schema 必须等于当前 DTO 导出的结果。"""
    assert tool.DEFAULT_OUT.exists(), "生成的 schema 必须随代码交付"
    fresh = tool.render(tool.build_schema(tmp_path / "ws"))
    assert tool.matches_current(tool.DEFAULT_OUT, fresh), (
        "仓库里的 schema 与当前 DTO 不一致，请重跑 backend/tools/export_openapi.py"
    )


def test_check_reports_missing_stale_and_current(tmp_path, capsys):
    """`--check` 是「重新生成无差异」的显式入口：缺失与过期都返回非零。"""
    out = tmp_path / "openapi.json"

    assert tool.main(["--check", "--out", str(out)]) == 1
    assert "缺少生成物" in capsys.readouterr().out

    out.write_text("{}\n", encoding="utf-8")
    assert tool.main(["--check", "--out", str(out)]) == 1
    assert "已过期" in capsys.readouterr().out

    assert tool.main(["--out", str(out)]) == 0
    assert tool.main(["--check", "--out", str(out)]) == 0
    assert "一致" in capsys.readouterr().out
