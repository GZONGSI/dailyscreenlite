"""个股笔记路由：按证券读取笔记流、新增、编辑与删除。

笔记与共用个股详情同以证券为身份，跨导入日期与入口共用；前端不自报候选项，
写笔记只动笔记，不改变处理状态，也不成为绕过导入的写入入口。

这里的请求／响应模型就是笔记的传输契约（线格式基类见 `app/contract.py`）：前端类型由它生成
（`backend/tools/export_openapi.py` → `frontend/src/api/generated/`），字段名与时间串口径
只在服务端维护一处。正文仍沿用迁移前的宽松转换，业务失败仍由 `_run` 映射成
404／400 的 `{"detail": {"message": ...}}`，不改成框架默认的 422 形状。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from dailyscreen_lite.app.contract import FailurePayload, WireContract
from dailyscreen_lite.notes.service import NoteInvalid, NoteUnavailable

router = APIRouter(prefix="/api/notes", tags=["notes"])


class NoteWrite(WireContract):
    """写笔记的请求体：只认识正文，多余字段忽略。

    正文刻意不声明成 `str`：迁移前是 `str(payload.get("body") or "")`，缺失、null、
    空串与非字符串都先落到字符串再交给领域校验，所以畸形输入的错误是 400／404 而不是
    结构校验 422。若将来要收紧契约，应作为一次单列的行为变化，而不是 DTO 迁移的副作用。
    """

    body: Any = None

    def loose_body(self) -> str:
        """迁移前的宽松转换：与 `str(payload.get("body") or "")` 等价。"""
        return str(self.body or "")


class NotePayload(WireContract):
    """个股笔记：只含正文与最近保存时间，不暴露修订历史。"""

    note_id: str
    security_id: str
    body: str
    # 保持现有 ISO 字符串口径：不在迁移里顺便改时区或格式
    updated_at: str


class NoteStreamPayload(WireContract):
    """某股票（以证券为身份）跨导入日期与入口共用的完整笔记流。"""

    security_id: str
    notes: list[NotePayload]


class DeleteNoteResponse(WireContract):
    """删除成功只回执被删的笔记标识。"""

    deleted: str


_WRITE_FAILURES = {
    400: {"model": FailurePayload, "description": "笔记内容不合规"},
    404: {"model": FailurePayload, "description": "股票或笔记不存在"},
}
_READ_FAILURES = {404: {"model": FailurePayload, "description": "股票或笔记不存在"}}


def _notes(request: Request):
    return request.app.state.container.notes


def _run(action):
    """统一错误映射：不存在 → 404，内容不合规 → 400。"""
    try:
        return action()
    except NoteUnavailable as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    except NoteInvalid as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


def _note_payload(note) -> NotePayload:
    """领域笔记 → 传输模型：别名与时间串由模型承担，映射只做字段对应。"""
    return NotePayload(
        note_id=note.note_id,
        security_id=note.security_id,
        body=note.body,
        updated_at=note.updated_at.isoformat(),
    )


@router.get(
    "/securities/{security_id}",
    response_model=NoteStreamPayload,
    responses=_READ_FAILURES,
)
def stream_for_security(security_id: str, request: Request) -> NoteStreamPayload:
    """该股票跨导入日期与入口共用的完整笔记流。"""
    stream = _run(lambda: _notes(request).stream_for_security(security_id))
    return NoteStreamPayload(
        security_id=stream.security_id,
        notes=[_note_payload(note) for note in stream.notes],
    )


@router.post(
    "/securities/{security_id}",
    response_model=NotePayload,
    responses=_WRITE_FAILURES,
)
def create_for_security(
    security_id: str, request: Request, payload: NoteWrite
) -> NotePayload:
    note = _run(lambda: _notes(request).create_for_security(security_id, payload.loose_body()))
    return _note_payload(note)


@router.patch("/{note_id}", response_model=NotePayload, responses=_WRITE_FAILURES)
def update(note_id: str, request: Request, payload: NoteWrite) -> NotePayload:
    """覆盖正文与保存时间，不保留旧版本。"""
    return _note_payload(_run(lambda: _notes(request).update(note_id, payload.loose_body())))


@router.delete("/{note_id}", response_model=DeleteNoteResponse, responses=_READ_FAILURES)
def delete(note_id: str, request: Request) -> DeleteNoteResponse:
    _run(lambda: _notes(request).delete(note_id))
    return DeleteNoteResponse(deleted=note_id)
