"""个股笔记服务：快捷新增、编辑与删除。

业务要点：
- 笔记按证券标识归属，同一股票在任何导入日期与入口看到同一笔记流；
- 只保存当前正文与最近保存时间：编辑覆盖正文，删除即删除，不留修订历史；
- 写笔记只动笔记，不完成归类、不切换当前项、不标记已查看；
- 正文必填：空白输入就近报错，而不是写入空笔记；
- 只有经过导入与证券识别的股票有笔记，与观察关系共用同一条校验。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.models import Note
from dailyscreen_lite.repository import Database, notes_repo
from dailyscreen_lite.classification.service import ClassificationService


class NoteUnavailable(LookupError):
    """笔记不存在，或该证券未经导入识别。"""


class NoteInvalid(ValueError):
    """笔记正文不符合规则（空内容）。"""


def _normalize_body(raw: str) -> str:
    """去掉首尾空白后校验；笔记是轻量文字，不写入纯空白内容。"""
    body = (raw or "").strip()
    if not body:
        raise NoteInvalid("笔记内容不能为空")
    return body


@dataclass(frozen=True)
class NoteStream:
    """某股票（以证券为身份）的笔记流。"""

    security_id: str
    notes: tuple[Note, ...]


class NoteService:
    def __init__(
        self, db: Database, clock: Clock, classification: ClassificationService
    ) -> None:
        self._db = db
        self._clock = clock
        self._classification = classification

    def stream_for_security(self, security_id: str) -> NoteStream:
        """该股票跨导入日期与入口共用的全部笔记。"""
        self._require_imported(security_id)
        with self._db.read() as conn:
            notes = notes_repo.list_for_security(conn, security_id)
        return NoteStream(security_id=security_id, notes=tuple(notes))

    def create_for_security(self, security_id: str, body: str) -> Note:
        """新增笔记：归属该证券，不改变处理状态、不切换当前股票。"""
        self._require_imported(security_id)
        clean = _normalize_body(body)
        note = Note(
            note_id=f"note-{uuid.uuid4().hex[:12]}",
            security_id=security_id,
            body=clean,
            updated_at=self._clock.now(),
        )
        with self._db.transaction() as conn:
            notes_repo.insert(conn, note)
        return note

    def update(self, note_id: str, body: str) -> Note:
        """覆盖当前正文与保存时间；不保留被覆盖的旧版本。"""
        clean = _normalize_body(body)
        with self._db.transaction() as conn:
            existing = notes_repo.get(conn, note_id)
            if existing is None:
                raise NoteUnavailable(f"笔记不存在：{note_id}")
            notes_repo.update_body(conn, note_id, clean, self._clock.now().isoformat())
            updated = notes_repo.get(conn, note_id)
        assert updated is not None
        return updated

    def delete(self, note_id: str) -> None:
        """删除笔记：界面与库中都不再展示，也不留下修订历史。"""
        with self._db.transaction() as conn:
            if notes_repo.get(conn, note_id) is None:
                raise NoteUnavailable(f"笔记不存在：{note_id}")
            notes_repo.delete(conn, note_id)

    # --- 内部 ---

    def _require_imported(self, security_id: str) -> None:
        """笔记以导入识别过的股票为身份，不成为绕过导入的写入入口。"""
        if not security_id or not self._classification.is_imported(security_id):
            raise NoteUnavailable(f"股票未经导入与识别，不能记录笔记：{security_id}")
