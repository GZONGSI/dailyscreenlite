"""权威证券库的持久化读写。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from dailyscreen_lite.domain.models import Security


@dataclass(frozen=True)
class SnapshotMeta:
    snapshot_id: str
    loaded_at: str
    record_count: int
    identity_fingerprint: str
    effective_date: str | None
    source_json: str


def replace_snapshot(
    conn: sqlite3.Connection,
    meta: SnapshotMeta,
    securities: list[Security],
    *,
    exchange: str | None = None,
) -> None:
    """以新快照整体替换证券库。已有候选项不因名单变化删除。"""
    if exchange is None:
        conn.execute("delete from securities")
    else:
        conn.execute("delete from securities where exchange = ?", (exchange,))
    conn.execute(
        """
        insert or replace into securities_snapshot_meta
            (snapshot_id, loaded_at, record_count, identity_fingerprint, effective_date, source_json)
        values (?, ?, ?, ?, ?, ?)
        """,
        (
            meta.snapshot_id,
            meta.loaded_at,
            meta.record_count,
            meta.identity_fingerprint,
            meta.effective_date,
            meta.source_json,
        ),
    )
    conn.executemany(
        """
        insert into securities
            (security_id, code, exchange, board, name, listing_date, is_st, snapshot_id)
        values (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                s.security_id,
                s.code,
                s.exchange,
                s.board,
                s.name,
                s.listing_date,
                1 if s.is_st else 0,
                meta.snapshot_id,
            )
            for s in securities
        ],
    )


def _to_security(row: sqlite3.Row) -> Security:
    return Security(
        security_id=row["security_id"],
        code=row["code"],
        exchange=row["exchange"],
        board=row["board"],
        name=row["name"],
        listing_date=row["listing_date"],
        is_st=bool(row["is_st"]),
    )


def lookup(conn: sqlite3.Connection, code: str) -> Security | None:
    """按规范化代码精确匹配证券身份。"""
    row = conn.execute(
        "select * from securities where code = ? limit 1", (code,)
    ).fetchone()
    return _to_security(row) if row else None


def get(conn: sqlite3.Connection, security_id: str) -> Security | None:
    """按组合身份精确读取证券。"""
    row = conn.execute(
        "select * from securities where security_id = ?", (security_id,)
    ).fetchone()
    return _to_security(row) if row else None


def lookup_many(conn: sqlite3.Connection, codes: list[str]) -> dict[str, Security]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"select * from securities where code in ({placeholders})", codes
    ).fetchall()
    return {row["code"]: _to_security(row) for row in rows}


def get_many(
    conn: sqlite3.Connection, security_ids: list[str]
) -> dict[str, Security]:
    """按证券身份批量取整条身份；观察列表一次取全，避免逐行查库。"""
    if not security_ids:
        return {}
    placeholders = ",".join("?" for _ in security_ids)
    rows = conn.execute(
        f"select * from securities where security_id in ({placeholders})",
        security_ids,
    ).fetchall()
    return {row["security_id"]: _to_security(row) for row in rows}


def names_for(
    conn: sqlite3.Connection, security_ids: list[str]
) -> dict[str, str]:
    """按证券身份批量取名称；导入结果表用它显示股票名称。"""
    if not security_ids:
        return {}
    placeholders = ",".join("?" for _ in security_ids)
    rows = conn.execute(
        f"select security_id, name from securities where security_id in ({placeholders})",
        security_ids,
    ).fetchall()
    return {row["security_id"]: row["name"] for row in rows}


def count(conn: sqlite3.Connection) -> int:
    return conn.execute("select count(*) from securities").fetchone()[0]


def current_meta(conn: sqlite3.Connection) -> SnapshotMeta | None:
    row = conn.execute(
        "select * from securities_snapshot_meta order by loaded_at desc, rowid desc limit 1"
    ).fetchone()
    if row is None:
        return None
    return SnapshotMeta(
        snapshot_id=row["snapshot_id"],
        loaded_at=row["loaded_at"],
        record_count=row["record_count"],
        identity_fingerprint=row["identity_fingerprint"],
        effective_date=row["effective_date"],
        source_json=row["source_json"],
    )


def market_ids(conn: sqlite3.Connection, exchange: str) -> set[str]:
    return {r[0] for r in conn.execute("select security_id from securities where exchange = ?", (exchange,))}


def record_market_update(conn, exchange, *, status, message, source, attempted_at):
    conn.execute("""
        insert into securities_market_updates values (?, ?, ?, ?, ?, ?)
        on conflict(exchange) do update set
            status=excluded.status, message=excluded.message, source=excluded.source,
            attempted_at=excluded.attempted_at,
            succeeded_at=coalesce(excluded.succeeded_at, securities_market_updates.succeeded_at)
    """, (exchange, status, message, source, attempted_at, attempted_at if status == "ok" else None))


def market_updates(conn):
    return [dict(r) for r in conn.execute("select * from securities_market_updates order by exchange")]
