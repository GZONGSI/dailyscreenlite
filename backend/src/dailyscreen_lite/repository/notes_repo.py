"""个股笔记的持久化读写。

笔记以证券标识归属、跨导入日期共用：同一股票在任何导入日期与入口看到同一笔记流。
只保留当前正文与最近保存时间，更新即覆盖，删除即删除，不保留修订历史。
security_id 故意不设外键：证券库每次启动整体替换，笔记必须不受名单变化影响。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from dailyscreen_lite.domain.models import Note

_COLUMNS = "note_id, security_id, body, updated_at"

# 最近保存的排在最前；同刻用标识稳定排序，避免列表顺序漂移
_ORDER = "order by updated_at desc, note_id desc"


def _to_note(row: sqlite3.Row) -> Note:
    return Note(
        note_id=row["note_id"],
        security_id=row["security_id"],
        body=row["body"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def list_for_security(conn: sqlite3.Connection, security_id: str) -> list[Note]:
    rows = conn.execute(
        f"select {_COLUMNS} from notes where security_id = ? {_ORDER}",
        (security_id,),
    ).fetchall()
    return [_to_note(r) for r in rows]


def get(conn: sqlite3.Connection, note_id: str) -> Note | None:
    row = conn.execute(
        f"select {_COLUMNS} from notes where note_id = ?", (note_id,)
    ).fetchone()
    return _to_note(row) if row else None


def insert(conn: sqlite3.Connection, note: Note) -> None:
    conn.execute(
        f"insert into notes ({_COLUMNS}) values (?, ?, ?, ?)",
        (note.note_id, note.security_id, note.body, note.updated_at.isoformat()),
    )


def update_body(conn: sqlite3.Connection, note_id: str, body: str, updated_at: str) -> None:
    conn.execute(
        "update notes set body = ?, updated_at = ? where note_id = ?",
        (body, updated_at, note_id),
    )


def delete(conn: sqlite3.Connection, note_id: str) -> None:
    conn.execute("delete from notes where note_id = ?", (note_id,))


def count_for_security(conn: sqlite3.Connection, security_id: str) -> int:
    return conn.execute(
        "select count(*) from notes where security_id = ?", (security_id,)
    ).fetchone()[0]
