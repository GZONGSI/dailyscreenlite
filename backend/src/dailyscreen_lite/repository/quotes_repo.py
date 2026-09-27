"""日线行情与更新记录的持久化读写。

行情按 (security_id, trade_date, adjust) 存一行；同源重叠价格一致时合并旧段，
否则将旧曲线整体留档，再写入新曲线，不混拼复权基准。
更新记录保存每次运行的步骤结果，供界面展示与重启后读回。
"""

from __future__ import annotations

import sqlite3
import json
import hashlib
from dataclasses import dataclass
from datetime import date, datetime

from dailyscreen_lite.domain.models import (
    SECURITIES_FAILED,
    SECURITIES_OK,
    DailyBar,
    QuoteSeries,
    UpdateItemResult,
    UpdateItemStatus,
    UpdateKind,
    UpdateRun,
    UpdateStatus,
)

_BAR_COLUMNS = """
    security_id, trade_date, adjust, open, high, low, close,
    volume_lots, amount_yuan, source, fetched_at
"""


def _to_bar(row: sqlite3.Row) -> DailyBar:
    return DailyBar(
        security_id=row["security_id"],
        trade_date=date.fromisoformat(row["trade_date"]),
        open=row["open"],
        high=row["high"],
        low=row["low"],
        close=row["close"],
        volume_lots=row["volume_lots"],
        amount_yuan=row["amount_yuan"],
    )


def replace_series(
    conn: sqlite3.Connection,
    *,
    security_id: str,
    adjust: str,
    source: str,
    fetched_at: str,
    bars: list[DailyBar],
) -> None:
    """原子更新当前曲线，保留兼容旧段或留档不兼容旧曲线。

    空/过期序列不覆盖现有数据；同源且所有重叠 OHLC 一致才允许合并。
    """
    if not bars:
        return
    old = conn.execute(
        f"select {_BAR_COLUMNS} from daily_quotes where security_id = ? and adjust = ? order by trade_date",
        (security_id, adjust),
    ).fetchall()
    if old and bars[-1].trade_date.isoformat() < old[-1]["trade_date"]:
        return
    incoming = {b.trade_date.isoformat(): b for b in bars}
    overlap = [r for r in old if r["trade_date"] in incoming]
    compatible = bool(overlap) and all(r["source"] == source for r in old) and all(
        all(r[field] == getattr(incoming[r["trade_date"]], field) for field in ("open", "high", "low", "close"))
        for r in overlap
    )
    if compatible:
        bars = sorted([_to_bar(r) for r in old if r["trade_date"] not in incoming] + bars, key=lambda b: b.trade_date)
    elif old:
        payload = json.dumps([dict(r) for r in old], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        conn.execute(
            "insert or ignore into quote_history_archive(security_id, adjust, digest, archived_at, payload) values (?, ?, ?, ?, ?)",
            (security_id, adjust, digest, fetched_at, payload),
        )
    conn.execute(
        "delete from daily_quotes where security_id = ? and adjust = ?",
        (security_id, adjust),
    )
    conn.executemany(
        f"insert into daily_quotes ({_BAR_COLUMNS}) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                bar.security_id,
                bar.trade_date.isoformat(),
                adjust,
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume_lots,
                bar.amount_yuan,
                source,
                fetched_at,
            )
            for bar in bars
        ],
    )


class IncompatibleIncrement(Exception):
    """A short response cannot safely be spliced into the active QFQ curve."""


def merge_incremental_series(
    conn: sqlite3.Connection, *, security_id: str, adjust: str, source: str,
    fetched_at: str, bars: list[DailyBar],
) -> None:
    """Check overlap and upsert only fetched dates; leave old rows untouched on error."""
    if not bars:
        return
    incoming = {bar.trade_date.isoformat(): bar for bar in bars}
    old = conn.execute(
        f"select {_BAR_COLUMNS} from daily_quotes where security_id = ? and adjust = ? "
        "and trade_date >= ? order by trade_date",
        (security_id, adjust, bars[0].trade_date.isoformat()),
    ).fetchall()
    overlap = [row for row in old if row["trade_date"] in incoming]
    if not overlap or any(row["source"] != source for row in old):
        raise IncompatibleIncrement("来源不同或缺少可核对的重叠日期")
    if len(overlap) != len(old):
        raise IncompatibleIncrement("短区间结果缺少已有日期")
    if any(
        any(row[field] != getattr(incoming[row["trade_date"]], field)
            for field in ("open", "high", "low", "close"))
        for row in overlap
    ):
        raise IncompatibleIncrement("前复权重叠价格发生变化")
    conn.executemany(
        f"""insert into daily_quotes ({_BAR_COLUMNS})
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(security_id, trade_date, adjust) do update set
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, volume_lots=excluded.volume_lots,
                amount_yuan=excluded.amount_yuan, source=excluded.source,
                fetched_at=excluded.fetched_at""",
        [
            (bar.security_id, bar.trade_date.isoformat(), adjust, bar.open, bar.high,
             bar.low, bar.close, bar.volume_lots, bar.amount_yuan, source, fetched_at)
            for bar in bars
        ],
    )


def archived_series(conn: sqlite3.Connection, security_id: str, adjust: str) -> list[list[dict]]:
    """Read preserved incompatible histories without splicing them into the active curve."""
    return [json.loads(r[0]) for r in conn.execute(
        "select payload from quote_history_archive where security_id = ? and adjust = ? order by rowid",
        (security_id, adjust),
    )]


def series(
    conn: sqlite3.Connection,
    security_id: str,
    adjust: str = "qfq",
    *,
    limit: int | None = None,
    end: str | None = None,
) -> QuoteSeries | None:
    """读取行情序列。

    limit 取最近 N 条；end（含）用于向前翻页浏览更早历史。两者都只影响范围，
    返回始终按日期升序，便于直接画图。fetched_at 表达整条当前曲线的最近更新，
    不随分页变化；增量更新保留旧行采集时间不能被误判为另一条曲线版本。
    """
    clauses = ["security_id = ?", "adjust = ?"]
    params: list[object] = [security_id, adjust]
    if end:
        clauses.append("trade_date <= ?")
        params.append(end)
    where = " and ".join(clauses)
    # 与分页行在同一条 SELECT 中读取，避免更新插在两次读取之间造成版本与数据错配。
    version = """(select max(fetched_at) from daily_quotes
        where security_id = ? and adjust = ?) as series_fetched_at"""
    if limit is not None:
        rows = conn.execute(
            f"""
            select * from (
                select {_BAR_COLUMNS}, {version} from daily_quotes
                where {where}
                order by trade_date desc limit ?
            ) order by trade_date asc
            """,
            (security_id, adjust, *params, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            select {_BAR_COLUMNS}, {version} from daily_quotes
            where {where} order by trade_date asc
            """,
            (security_id, adjust, *params),
        ).fetchall()
    if not rows:
        return None
    return QuoteSeries(
        security_id=security_id,
        adjust=adjust,
        source=rows[-1]["source"],
        bars=tuple(_to_bar(r) for r in rows),
        fetched_at=datetime.fromisoformat(rows[-1]["series_fetched_at"]),
    )


def total_count(conn: sqlite3.Connection, security_id: str, adjust: str = "qfq") -> int:
    return conn.execute(
        "select count(*) from daily_quotes where security_id = ? and adjust = ?",
        (security_id, adjust),
    ).fetchone()[0]


def latest_date(conn: sqlite3.Connection, security_id: str, adjust: str = "qfq") -> str | None:
    row = conn.execute(
        "select max(trade_date) as d from daily_quotes where security_id = ? and adjust = ?",
        (security_id, adjust),
    ).fetchone()
    return row["d"] if row and row["d"] else None


def stored_securities(conn: sqlite3.Connection, adjust: str = "qfq") -> set[str]:
    rows = conn.execute(
        "select distinct security_id from daily_quotes where adjust = ?", (adjust,)
    ).fetchall()
    return {r["security_id"] for r in rows}


def target_security_ids(conn: sqlite3.Connection) -> list[str]:
    """持续更新范围：待归类股票与现存观察组成员的去重并集。

    已处理且已离组的历史股票不进持续范围（其行情原样保留，不被删除），
    再次入选或主动重新归类时才回到范围；只更新与用户相关的股票，不下载全市场历史。
    只读跨聚合查询，仍属持久化层。
    """
    rows = conn.execute(
        """
        select security_id from candidates where state in ('pending', 'later')
        union
        select security_id from observation_members
        order by security_id
        """
    ).fetchall()
    return [r["security_id"] for r in rows]


def latest_quote_dates(
    conn: sqlite3.Connection, security_ids: list[str], adjust: str = "qfq"
) -> dict[str, str]:
    """批量读取各股票已入库的最新行情日期，供完整性判定逐股比对目标交易日。"""
    if not security_ids:
        return {}
    placeholders = ",".join("?" for _ in security_ids)
    rows = conn.execute(
        f"""
        select security_id, max(trade_date) as latest
        from daily_quotes
        where adjust = ? and security_id in ({placeholders})
        group by security_id
        """,
        [adjust, *security_ids],
    ).fetchall()
    return {r["security_id"]: r["latest"] for r in rows if r["latest"]}


@dataclass(frozen=True)
class QuoteSnapshot:
    """队列列表用的轻量行情摘要：最新收盘价、对应行情日与日涨跌幅。"""

    security_id: str
    trade_date: str
    close: float
    change_pct: float | None


def latest_snapshots(
    conn: sqlite3.Connection, security_ids: list[str], adjust: str = "qfq"
) -> dict[str, QuoteSnapshot]:
    """批量读取各股票最近两个交易日的收盘价，用于列表展示涨跌幅。

    只取两行，避免为列表加载整段历史；不足两行时涨跌幅保持未知而不是补零。
    """
    if not security_ids:
        return {}
    placeholders = ",".join("?" for _ in security_ids)
    rows = conn.execute(
        f"""
        select security_id, trade_date, close from (
            select security_id, trade_date, close,
                   row_number() over (
                       partition by security_id order by trade_date desc
                   ) as rn
            from daily_quotes
            where adjust = ? and security_id in ({placeholders})
        ) where rn <= 2
        order by security_id asc, trade_date desc
        """,
        [adjust, *security_ids],
    ).fetchall()
    grouped: dict[str, list[tuple[str, float]]] = {}
    for row in rows:
        grouped.setdefault(row["security_id"], []).append(
            (row["trade_date"], row["close"])
        )
    snapshots: dict[str, QuoteSnapshot] = {}
    for security_id, series in grouped.items():
        latest_date, latest_close = series[0]
        previous_close = series[1][1] if len(series) > 1 else None
        change = (
            (latest_close - previous_close) / previous_close * 100
            if previous_close
            else None
        )
        snapshots[security_id] = QuoteSnapshot(
            security_id=security_id,
            trade_date=latest_date,
            close=latest_close,
            change_pct=change,
        )
    return snapshots


def _to_run(row: sqlite3.Row) -> UpdateRun:
    failed = row["failed_securities"]
    keys = row.keys()
    return UpdateRun(
        run_id=row["run_id"],
        kind=UpdateKind(row["kind"]),
        status=UpdateStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        finished_at=datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None,
        securities_status=row["securities_status"],
        securities_message=row["securities_message"],
        securities_count=row["securities_count"],
        quotes_ok=row["quotes_ok"],
        quotes_failed=row["quotes_failed"],
        quotes_skipped=row["quotes_skipped"] if "quotes_skipped" in keys else 0,
        quotes_pending=row["quotes_pending"] if "quotes_pending" in keys else 0,
        failed_securities=tuple(failed.split(",")) if failed else (),
        elapsed_ms=row["elapsed_ms"] if "elapsed_ms" in keys else None,
    )


def insert_run(conn: sqlite3.Connection, run: UpdateRun) -> None:
    conn.execute(
        """
        insert into update_runs
            (run_id, kind, status, started_at, finished_at, securities_status,
             securities_message, securities_count, quotes_ok, quotes_failed,
             quotes_skipped, quotes_pending, failed_securities, elapsed_ms)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run.run_id,
            run.kind.value,
            run.status.value,
            run.started_at.isoformat(),
            run.finished_at.isoformat() if run.finished_at else None,
            run.securities_status,
            run.securities_message,
            run.securities_count,
            run.quotes_ok,
            run.quotes_failed,
            run.quotes_skipped,
            run.quotes_pending,
            ",".join(run.failed_securities) if run.failed_securities else None,
            run.elapsed_ms,
        ),
    )


def finish_run(conn: sqlite3.Connection, run: UpdateRun) -> None:
    conn.execute(
        """
        update update_runs
        set status = ?, finished_at = ?, securities_status = ?, securities_message = ?,
            securities_count = ?, quotes_ok = ?, quotes_failed = ?, quotes_skipped = ?,
            quotes_pending = ?, failed_securities = ?, elapsed_ms = ?
        where run_id = ?
        """,
        (
            run.status.value,
            run.finished_at.isoformat() if run.finished_at else None,
            run.securities_status,
            run.securities_message,
            run.securities_count,
            run.quotes_ok,
            run.quotes_failed,
            run.quotes_skipped,
            run.quotes_pending,
            ",".join(run.failed_securities) if run.failed_securities else None,
            run.elapsed_ms,
            run.run_id,
        ),
    )


def _latest_run(
    conn: sqlite3.Connection, clause: str, params: tuple[object, ...] = ()
) -> UpdateRun | None:
    """按更新时间取最近一条记录；调用方只给过滤条件，次序约定只写一处。

    rowid 作为并列时间的次序：固定时钟下同一时刻会写入多条记录。
    """
    row = conn.execute(
        f"select * from update_runs where {clause} "
        "order by started_at desc, rowid desc limit 1",
        params,
    ).fetchone()
    return _to_run(row) if row else None


def latest_run(conn: sqlite3.Connection) -> UpdateRun | None:
    return _latest_run(conn, "1 = 1")


def last_completed_run(
    conn: sqlite3.Connection, exclude_kinds: tuple[UpdateKind, ...] = ()
) -> UpdateRun | None:
    """最近一次已结束（非 RUNNING）的整体更新，供调度判断是否需要补更。

    定向补取（IMPORT）只覆盖少量新股票，不代表证券库与行情已整体更新，
    因此不计入调度判断，避免它抑制当日 16:30 或启动补更。
    """
    clauses = ["status != ?"]
    params: list[object] = [UpdateStatus.RUNNING.value]
    if exclude_kinds:
        placeholders = ",".join("?" for _ in exclude_kinds)
        clauses.append(f"kind not in ({placeholders})")
        params.extend(k.value for k in exclude_kinds)
    return _latest_run(conn, " and ".join(clauses), tuple(params))


def latest_securities_run(conn: sqlite3.Connection) -> UpdateRun | None:
    """最近一次真正执行过证券库步骤的更新（成功或失败）。

    定向补取、单股更新与重试都不碰证券库（securities_status 记 skipped），
    它们之后的更新时间顺序不能把上一次证券库失败提示抹成「本次未更新」。
    """
    return _latest_run(
        conn,
        "securities_status in (?, ?, ?)",
        (SECURITIES_OK, SECURITIES_FAILED, "partial"),
    )


def recent_runs(conn: sqlite3.Connection, limit: int = 10) -> list[UpdateRun]:
    rows = conn.execute(
        "select * from update_runs order by started_at desc, rowid desc limit ?",
        (limit,),
    ).fetchall()
    return [_to_run(r) for r in rows]


def claim_automatic_round(conn, *, run_date: str, slot: int, run_id: str, started_at: str) -> bool:
    """与 running 记录同事务；只认最新时点，错过的早期轮次不追跑。"""
    last = conn.execute("select max(slot) from automatic_rounds where run_date = ?", (run_date,)).fetchone()[0]
    if last is not None and last >= slot:
        return False
    conn.execute("insert into automatic_rounds values (?, ?, ?, ?)", (run_date, slot, run_id, started_at))
    return True


def automatic_rounds(conn, run_date: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "select * from automatic_rounds where run_date = ? order by slot", (run_date,))]


def insert_item_results(
    conn: sqlite3.Connection, run_id: str, items: list[UpdateItemResult]
) -> None:
    """写入一次更新的逐股结果；同一股票同一运行只留一条。"""
    if not items:
        return
    conn.executemany(
        """
        insert or replace into update_stock_results
            (run_id, security_id, status, message, trade_date, fetch_mode,
             request_start, elapsed_ms, attempts_json)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (run_id, item.security_id, item.status.value, item.message, item.trade_date,
             item.fetch_mode, item.request_start, item.elapsed_ms,
             json.dumps(item.attempts, ensure_ascii=False))
            for item in items
        ],
    )


def count_running(conn: sqlite3.Connection) -> int:
    """仍在「进行中」的更新记录数；启动收尾前先问它，免去无谓的时钟读取。"""
    return conn.execute(
        "select count(*) from update_runs where status = ?", (UpdateStatus.RUNNING.value,)
    ).fetchone()[0]


def fail_stale_runs(conn: sqlite3.Connection, *, finished_at: str, message: str) -> int:
    """把上次进程中断留下的 RUNNING 记录收成失败终态。

    进程被硬中断时 RUNNING 行不会被收尾，界面会一直显示「更新中」；
    启动时按失败收尾，使「不出现永久运行中」不依赖用户手动干预。
    """
    stale = count_running(conn)
    if not stale:
        return 0
    conn.execute(
        """
        update update_runs
        set status = ?, finished_at = ?, securities_message = ?
        where status = ?
        """,
        (UpdateStatus.FAILED.value, finished_at, message, UpdateStatus.RUNNING.value),
    )
    return stale


def _to_item(row: sqlite3.Row) -> UpdateItemResult:
    return UpdateItemResult(
        security_id=row["security_id"],
        status=UpdateItemStatus(row["status"]),
        message=row["message"],
        trade_date=row["trade_date"],
        fetch_mode=row["fetch_mode"],
        request_start=row["request_start"],
        elapsed_ms=row["elapsed_ms"],
        attempts=tuple(json.loads(row["attempts_json"] or "[]")),
    )


def item_results(conn: sqlite3.Connection, run_id: str) -> list[UpdateItemResult]:
    rows = conn.execute(
        "select * from update_stock_results where run_id = ? order by security_id asc",
        (run_id,),
    ).fetchall()
    return [_to_item(r) for r in rows]


def latest_failed_items(conn: sqlite3.Connection) -> list[UpdateItemResult]:
    """最近一次已结束更新的失败逐股结果，供失败原因与重试入口。"""
    run = last_completed_run(conn)
    if run is None:
        return []
    rows = conn.execute(
        """
        select * from update_stock_results
        where run_id = ? and status = ?
        order by security_id asc
        """,
        (run.run_id, UpdateItemStatus.FAILED.value),
    ).fetchall()
    return [_to_item(r) for r in rows]


def latest_item_messages(conn: sqlite3.Connection) -> dict[str, str | None]:
    """逐股保留最后一次结束的结果，包括切源成功之前的失败原因。"""
    rows = conn.execute("""
        select security_id, message from (
            select security_id, message,
                   row_number() over (partition by security_id order by rowid desc) as rank
            from update_stock_results
        ) where rank = 1
    """).fetchall()
    return {row["security_id"]: row["message"] for row in rows}


def latest_sources(conn: sqlite3.Connection, adjust: str) -> dict[str, str]:
    return {r["security_id"]: r["source"] for r in conn.execute(
        "select security_id, source, max(trade_date) from daily_quotes where adjust = ? group by security_id", (adjust,)
    )}
