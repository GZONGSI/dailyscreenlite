"""每日状态快照与交易日历的持久化读写。

快照只保存停牌区间与覆盖声明（轻量），不逐只落「正常」；失败快照同样落一行，
但完整性判定只读最近一次成功的快照，因此「状态失败保留旧快照及其日期」。
交易日历单独缓存，状态来源失败时仍能算出目标交易日。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from dailyscreen_lite.domain.models import (
    MarketStatusSnapshot,
    Suspension,
    SuspensionKind,
)

_KEEP_SNAPSHOTS = 30


def _to_suspension(row: sqlite3.Row) -> Suspension:
    return Suspension(
        security_id=row["security_id"],
        code=row["code"],
        name=row["name"],
        kind=SuspensionKind(row["kind"]),
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
        expected_resume=date.fromisoformat(row["expected_resume"])
        if row["expected_resume"]
        else None,
        market=row["market"],
        reason=row["reason"],
    )


def _to_snapshot(row: sqlite3.Row, suspensions: tuple[Suspension, ...]) -> MarketStatusSnapshot:
    return MarketStatusSnapshot(
        snapshot_id=row["snapshot_id"],
        target_trade_date=date.fromisoformat(row["target_trade_date"]),
        collected_at=datetime.fromisoformat(row["collected_at"]),
        source=row["source"],
        calendar_source=row["calendar_source"],
        covered_markets=tuple(json.loads(row["covered_markets"])),
        uncovered_markets=tuple(json.loads(row["uncovered_markets"])),
        suspensions=suspensions,
        status=row["status"],
        message=row["message"],
    )


def insert_snapshot(conn: sqlite3.Connection, snapshot: MarketStatusSnapshot) -> None:
    """写入一次快照：状态为 failed 时不写停牌区间，只留失败事实。"""
    conn.execute(
        """
        insert or replace into market_status_snapshots
            (snapshot_id, target_trade_date, collected_at, source, calendar_source,
             covered_markets, uncovered_markets, suspension_count, status, message)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.snapshot_id,
            snapshot.target_trade_date.isoformat(),
            snapshot.collected_at.isoformat(),
            snapshot.source,
            snapshot.calendar_source,
            json.dumps(list(snapshot.covered_markets), ensure_ascii=False),
            json.dumps(list(snapshot.uncovered_markets), ensure_ascii=False),
            len(snapshot.suspensions),
            snapshot.status,
            snapshot.message,
        ),
    )
    if not snapshot.suspensions:
        return
    conn.executemany(
        """
        insert or replace into market_suspensions
            (snapshot_id, security_id, code, name, kind, start_date, end_date,
             expected_resume, market, reason)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                snapshot.snapshot_id,
                s.security_id,
                s.code,
                s.name,
                s.kind.value,
                s.start_date.isoformat(),
                s.end_date.isoformat() if s.end_date else None,
                s.expected_resume.isoformat() if s.expected_resume else None,
                s.market,
                s.reason,
            )
            for s in snapshot.suspensions
        ],
    )


def _snapshot_row(conn: sqlite3.Connection, *, only_ok: bool) -> sqlite3.Row | None:
    clause = "where status = 'ok'" if only_ok else ""
    # rowid 作为并列时间的次序：固定时钟下同一时刻会写入多条快照，
    # 只按 collected_at 排序会拿到不确定的「最近一次」。
    return conn.execute(
        f"""
        select * from market_status_snapshots {clause}
        order by collected_at desc, rowid desc limit 1
        """
    ).fetchone()


def latest_snapshot(conn: sqlite3.Connection, *, only_ok: bool = True) -> MarketStatusSnapshot | None:
    """最近一次快照；only_ok 时跳过失败快照，用于完整性判定。"""
    row = _snapshot_row(conn, only_ok=only_ok)
    if row is None:
        return None
    suspensions = conn.execute(
        "select * from market_suspensions where snapshot_id = ?", (row["snapshot_id"],)
    ).fetchall()
    return _to_snapshot(row, tuple(_to_suspension(r) for r in suspensions))


def prune_snapshots(conn: sqlite3.Connection, keep: int = _KEEP_SNAPSHOTS) -> None:
    """只保留最近若干次快照，避免每日累积。"""
    conn.execute(
        """
        delete from market_status_snapshots
        where snapshot_id not in (
            select snapshot_id from market_status_snapshots
            order by collected_at desc, rowid desc limit ?
        )
        and snapshot_id not in (
            select snapshot_id from market_status_snapshots where status = 'ok'
            order by collected_at desc, rowid desc limit 1
        )
        """,
        (keep,),
    )


def replace_trade_dates(
    conn: sqlite3.Connection, days: tuple[date, ...], *, source: str, updated_at: str
) -> None:
    """整段替换交易日历缓存；空日历不动缓存，避免一次失败清空日历。"""
    if not days:
        return
    conn.execute("delete from trade_dates")
    conn.executemany(
        "insert or replace into trade_dates (trade_date, source, updated_at) values (?, ?, ?)",
        [(day.isoformat(), source, updated_at) for day in days],
    )


def trade_dates(conn: sqlite3.Connection) -> tuple[date, ...]:
    rows = conn.execute("select trade_date from trade_dates order by trade_date asc").fetchall()
    return tuple(date.fromisoformat(r["trade_date"]) for r in rows)


def replace_calendar_month(conn, month, *, source, days, updated_at):
    conn.execute("delete from trade_dates where substr(trade_date, 1, 7) = ?", (month,))
    conn.executemany("insert into trade_dates values (?, ?, ?)",
                     [(d.isoformat(), source, updated_at) for d in days])
    conn.execute("insert or replace into calendar_months values (?, ?, ?)", (month, source, updated_at))


def calendar_months(conn):
    return [dict(r) for r in conn.execute("select * from calendar_months order by month")]


def record_calendar_update(conn, *, status, message, attempted_at):
    conn.execute("insert or replace into calendar_update values (1, ?, ?, ?)", (status, message, attempted_at))


def calendar_update(conn):
    row = conn.execute("select * from calendar_update where id = 1").fetchone()
    return dict(row) if row else None


def calendar_coverage(conn):
    """旧生产缓存无完整月证据即不可信；稀疏日历兼容仅限显式离线夹具。"""
    if calendar_update(conn) is None:
        sources = {r[0] for r in conn.execute("select distinct source from trade_dates")}
        return None if sources and all(s.startswith("fixture.") for s in sources) else set()
    return {r['month'] for r in calendar_months(conn)}
