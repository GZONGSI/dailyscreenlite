"""个股笔记——以证券为身份、跨导入日期共用。

真实临时 SQLite + 真实证券库快照 + 固定时钟；覆盖新增、同一股票不同导入日期
看到同一笔记流、股票之间不串数据、编辑覆盖正文与保存时间、删除后不留修订历史、
写笔记不改变处理状态、未经导入的股票不能写笔记、重启读回，以及 HTTP 边界。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.app.routes.notes import (
    DeleteNoteResponse,
    NotePayload,
    NoteStreamPayload,
)
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import CandidateScope, CandidateState
from dailyscreen_lite.notes.service import NoteInvalid, NoteUnavailable
from dailyscreen_lite.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return offline_settings(tmp_path)


def import_one(container, *codes: str) -> list[str]:
    """导入并返回证券标识（笔记与观察关系都以证券为身份）。"""
    container.imports.submit_file("a.csv", make_csv("代码", *codes))
    return [v.candidate.security_id for v in container.classification.list_candidates()]


# --- 归属与身份 ---


def test_create_note_and_read_back(container):
    security_id = import_one(container, "000001")[0]

    note = container.notes.create_for_security(security_id, "  关注不良率变化  ")

    assert note.security_id == "000001.SZ"
    # 首尾空白不入库，但正文内容保留
    assert note.body == "关注不良率变化"
    stream = container.notes.stream_for_security(security_id)
    assert [n.note_id for n in stream.notes] == [note.note_id]
    # 卡片标注笔记条数（候选项接口仍按证券汇总）
    candidate_id = container.classification.list_candidates()[0].candidate.candidate_id
    assert container.classification.get_candidate(candidate_id).note_count == 1


def test_note_owner_is_security_not_date(tmp_path):
    """同一股票的笔记流跨导入日期共用；股票之间不串数据。"""
    settings = make_settings(tmp_path)
    day1 = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    day1.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    first_note = day1.notes.create_for_security("000001.SZ", "第一天的记录")
    day1.notes.create_for_security("600519.SH", "另一只股票的记录")

    # 次日同一股票再入选：合并到同一候选项，笔记流仍是同一份
    day2 = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    day2.imports.submit_file("b.csv", make_csv("代码", "000001"))
    pending = [
        v
        for v in day2.classification.list_candidates(scope=CandidateScope.UNPROCESSED)
        if v.candidate.security_id == "000001.SZ"
    ]
    assert len(pending) == 1

    stream = day2.notes.stream_for_security("000001.SZ")
    assert [n.note_id for n in stream.notes] == [first_note.note_id]
    # 股票之间不串：贵州茅台的笔记不出现在平安银行的笔记流
    assert all(n.security_id == "000001.SZ" for n in stream.notes)


def test_notes_are_not_scoped_by_batch_or_group(tmp_path):
    """笔记归属只看股票：换导入批次、加观察组都不改变笔记流。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    note = container.notes.create_for_security("000001.SZ", "与批次无关")

    group = container.observations.create_group("核心")
    candidate_id = container.classification.list_candidates()[0].candidate.candidate_id
    container.observations.observe_candidate(candidate_id, [group.group_id])
    # 同日再提交一个来源：仍归同一候选项（不新建项），笔记流不变
    container.imports.submit_file("b.csv", make_csv("代码", "000001"))
    assert container.notes.stream_for_security("000001.SZ").security_id == "000001.SZ"
    assert [n.note_id for n in container.notes.stream_for_security("000001.SZ").notes] == [
        note.note_id
    ]
    # 同日不会因新来源产生第二个候选项
    assert len(container.classification.list_candidates(scope=CandidateScope.PROCESSED)) == 1


def test_note_requires_imported_security(container):
    """笔记以导入识别过的股票为身份，不能成为绕过导入的写入入口。"""
    import_one(container, "000001")

    with pytest.raises(NoteUnavailable):
        container.notes.create_for_security("999999.SZ", "全市场没有这只股票")
    with pytest.raises(NoteUnavailable):
        container.notes.stream_for_security("999999.SZ")


# --- 编辑与删除 ---


def test_update_replaces_body_and_save_time(container):
    security_id = import_one(container, "000001")[0]
    created = container.notes.create_for_security(security_id, "初稿")

    # 用不同时钟重开，确认保存时间确实被覆盖为新的
    later = _rebuild_with_clock(container, datetime(2026, 9, 11, 23, 0, 0))
    updated = later.notes.update(created.note_id, "修改后的正文")

    assert updated.body == "修改后的正文"
    assert updated.note_id == created.note_id
    assert updated.updated_at > created.updated_at
    # 覆盖式更新：库中仍只有一条，不产生旧版本
    stream = later.notes.stream_for_security(security_id)
    assert [n.body for n in stream.notes] == ["修改后的正文"]


def test_delete_removes_note_without_revision_history(container):
    security_id = import_one(container, "000001")[0]
    created = container.notes.create_for_security(security_id, "会被删除")

    container.notes.delete(created.note_id)

    assert container.notes.stream_for_security(security_id).notes == ()
    candidate_id = container.classification.list_candidates()[0].candidate.candidate_id
    assert container.classification.get_candidate(candidate_id).note_count == 0
    with pytest.raises(NoteUnavailable):
        container.notes.update(created.note_id, "重编辑")
    with pytest.raises(NoteUnavailable):
        container.notes.delete(created.note_id)


def test_note_body_rules(container):
    security_id = import_one(container, "000001")[0]
    with pytest.raises(NoteInvalid):
        container.notes.create_for_security(security_id, "   ")
    with pytest.raises(NoteUnavailable):
        container.notes.update("note-missing", "正文")
    # 校验失败不写入空笔记，也不影响卡片计数
    assert container.notes.stream_for_security(security_id).notes == ()


# --- 与归类状态互不干扰 ---


def test_writing_note_does_not_complete_or_switch_item(container):
    """写笔记不完成候选项、不切换当前项（写笔记与决策相互独立）。"""
    import_one(container, "000001", "600519")
    items = [v.candidate.candidate_id for v in container.classification.list_candidates()]
    current = items[0]
    container.classification.update_view({"currentCandidateId": current})

    container.notes.create_for_security(
        container.classification.security_id_for_candidate(current), "随手记录"
    )

    view = container.classification.get_candidate(current)
    assert view.candidate.state is CandidateState.PENDING
    assert view.candidate.action_result is None
    assert container.classification.get_view().state.current_candidate_id == current
    assert view.note_count == 1


# --- 重启读回 ---


def test_notes_persist_across_restart(tmp_path):
    settings = make_settings(tmp_path)
    first = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    security_id = import_one(first, "000001")[0]
    keep = first.notes.create_for_security(security_id, "保留的笔记")
    drop = first.notes.create_for_security(security_id, "待删除")
    first.notes.update(keep.note_id, "编辑后的笔记")
    first.notes.delete(drop.note_id)

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))

    stream = reopened.notes.stream_for_security(security_id)
    assert [n.note_id for n in stream.notes] == [keep.note_id]
    assert stream.notes[0].body == "编辑后的笔记"


# --- HTTP 边界 ---


def test_note_endpoints_work_through_http(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        )
        candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]
        security_id = client.get(
            f"/api/classification/candidates/{candidate_id}"
        ).json()["securityId"]

        created = client.post(f"/api/notes/securities/{security_id}", json={"body": "接口新增"})
        assert created.status_code == 200
        note = created.json()
        assert note["securityId"] == security_id
        assert note["body"] == "接口新增"

        listed = client.get(f"/api/notes/securities/{security_id}").json()
        assert [n["noteId"] for n in listed["notes"]] == [note["noteId"]]
        # 卡片从候选项接口读到笔记条数
        card = client.get(f"/api/classification/candidates/{candidate_id}").json()
        assert card["noteCount"] == 1

        updated = client.patch(f"/api/notes/{note['noteId']}", json={"body": "接口编辑"})
        assert updated.json()["body"] == "接口编辑"

        removed = client.delete(f"/api/notes/{note['noteId']}")
        assert removed.status_code == 200
        assert client.get(f"/api/notes/securities/{security_id}").json()["notes"] == []


def test_note_http_rejects_empty_and_missing(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        )
        candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]
        security_id = client.get(
            f"/api/classification/candidates/{candidate_id}"
        ).json()["securityId"]

        empty = client.post(f"/api/notes/securities/{security_id}", json={"body": "  "})
        assert empty.status_code == 400
        unknown = client.post("/api/notes/securities/999999.SZ", json={"body": "正文"})
        assert unknown.status_code == 404
        missing_note = client.patch("/api/notes/nope", json={"body": "正文"})
        assert missing_note.status_code == 404


def _note_http_client(tmp_path: Path) -> tuple[TestClient, str]:
    """导入一只股票并返回 (客户端, 证券标识)，用于笔记 HTTP 契约用例。"""
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    client = TestClient(create_app(settings, clock))
    client.post(
        "/api/imports/csv",
        files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
    )
    candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]
    security_id = client.get(f"/api/classification/candidates/{candidate_id}").json()["securityId"]
    return client, security_id


# 迁移前的真实响应（探针实测）：对象内的 body 先按 `str(value or "")` 转成字符串，
# 再交给领域校验；DTO 只能保持这张表，不能顺便收紧。
NOTE_BODY_MATRIX: tuple[tuple[object, int, str | None], ...] = (
    ({"body": "正文"}, 200, "正文"),
    ({"body": "   "}, 400, None),
    ({}, 400, None),
    ({"body": None}, 400, None),
    ({"body": 123}, 200, "123"),
    ({"body": {"a": 1}}, 200, "{'a': 1}"),
    ({"body": ["x"]}, 200, "['x']"),
    ({"body": "正文", "extra": 1}, 200, "正文"),
)


def _check_body_matrix(call) -> None:
    """把 NOTE_BODY_MATRIX 逐条打在给定的写入调用上（新增与编辑用同一张表）。"""
    for payload, status, body in NOTE_BODY_MATRIX:
        response = call(payload)
        assert response.status_code == status, (payload, response.text)
        if body is None:
            assert response.json()["detail"]["message"] == "笔记内容不能为空"
        else:
            assert response.json()["body"] == body


def test_note_write_body_contract(tmp_path):
    """新增与编辑的请求体映射：宽松转换、空内容 400、未知笔记 404 都保持原样。"""
    client, security_id = _note_http_client(tmp_path)
    url = f"/api/notes/securities/{security_id}"
    _check_body_matrix(lambda payload: client.post(url, json=payload))

    target = client.post(url, json={"body": "编辑目标"}).json()
    _check_body_matrix(lambda payload: client.patch(f"/api/notes/{target['noteId']}", json=payload))

    # 未知笔记同样先过正文校验：空正文 400，有正文 404
    assert client.patch("/api/notes/note-missing", json={"body": "正文"}).status_code == 404
    assert client.patch("/api/notes/note-missing", json={"body": "  "}).status_code == 400


def test_note_write_body_frame_errors(tmp_path):
    """没有请求体、JSON null 与非对象 JSON 都由框架拒绝：422 且定位到请求体。"""
    client, security_id = _note_http_client(tmp_path)
    frames: tuple[dict[str, object], ...] = (
        {},
        {"content": b"null", "headers": {"Content-Type": "application/json"}},
        {"content": b"[1,2]", "headers": {"Content-Type": "application/json"}},
        {"content": b'"x"', "headers": {"Content-Type": "application/json"}},
    )
    for kwargs in frames:
        for call in (
            lambda: client.post(f"/api/notes/securities/{security_id}", **kwargs),
            lambda: client.patch("/api/notes/note-missing", **kwargs),
        ):
            response = call()
            assert response.status_code == 422, (kwargs, response.text)
            detail = response.json()["detail"]
            # 结构校验的信封保持：定位在 body，字段仍是 type/loc/msg/input
            assert detail[0]["loc"] == ["body"], (kwargs, detail)
            assert set(detail[0]) == {"type", "loc", "msg", "input"}, (kwargs, detail)


def test_note_response_shapes(tmp_path):
    """三个成功响应的字段与时间串口径：DTO 替换手写字典后必须逐字保持。"""
    client, security_id = _note_http_client(tmp_path)
    created = client.post(f"/api/notes/securities/{security_id}", json={"body": " 正文 "})
    note = created.json()
    assert sorted(note) == ["body", "noteId", "securityId", "updatedAt"]
    assert note["body"] == "正文"
    assert note["securityId"] == security_id
    # updatedAt 保持现有 ISO 字符串口径（固定时钟：2026-09-11T09:05:00+08:00）
    assert note["updatedAt"] == "2026-09-11T09:05:00+08:00"

    listed = client.get(f"/api/notes/securities/{security_id}").json()
    assert sorted(listed) == ["notes", "securityId"]
    assert listed["securityId"] == security_id
    assert [item["noteId"] for item in listed["notes"]] == [note["noteId"]]

    removed = client.delete(f"/api/notes/{note['noteId']}")
    assert removed.status_code == 200
    assert removed.json() == {"deleted": note["noteId"]}

    # 真实响应必须能吃进声明的响应模型：schema 与线上形状不脱节
    assert NotePayload.model_validate(note).note_id == note["noteId"]
    assert NoteStreamPayload.model_validate(listed).security_id == security_id
    assert DeleteNoteResponse.model_validate(removed.json()).deleted == note["noteId"]


def test_note_error_shapes(tmp_path):
    """业务失败形状保持：404/400 都是 {"detail": {"message": ...}}。"""
    client, security_id = _note_http_client(tmp_path)
    unknown_security = client.get("/api/notes/securities/999999.SZ")
    assert unknown_security.status_code == 404
    assert set(unknown_security.json()) == {"detail"}
    assert isinstance(unknown_security.json()["detail"]["message"], str)

    empty_body = client.post(f"/api/notes/securities/{security_id}", json={"body": ""})
    assert empty_body.status_code == 400
    assert empty_body.json()["detail"]["message"] == "笔记内容不能为空"

    missing_note = client.delete("/api/notes/note-missing")
    assert missing_note.status_code == 404
    assert missing_note.json()["detail"]["message"].startswith("笔记不存在")


def _rebuild_with_clock(container, instant: datetime):
    """在同一声明数据目录上用新时钟重开容器，验证保存时间被真实覆盖。"""
    settings = offline_settings(container.settings.data_dir, data_dir=container.settings.data_dir)
    return build_container(settings, FixedClock(instant))
