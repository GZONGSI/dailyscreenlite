"""导入批次、批次明细与待确认候选的持久化读写。"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime

from dailyscreen_lite.domain.models import (
    BatchStatus,
    BatchStock,
    ImportBatch,
    ImportDate,
    ParsedCandidate,
    SelectionOption,
    SelectionPreview,
    SelectionRequest,
    SourceKind,
    StockOutcome,
)


def _selection_json(selection: SelectionRequest | None) -> str | None:
    if selection is None:
        return None
    return json.dumps(selection.to_dict(), ensure_ascii=False)


def _selection_from_json(raw: str | None) -> SelectionRequest | None:
    if not raw:
        return None
    data = json.loads(raw)
    return SelectionRequest(
        kind=data["kind"],
        prompt=data["prompt"],
        options=tuple(
            SelectionOption(value=o["value"], label=o["label"], detail=o.get("detail"))
            for o in data.get("options", [])
        ),
        preview=SelectionPreview(
            headers=tuple(data.get("preview", {}).get("headers", [])),
            rows=tuple(tuple(r) for r in data.get("preview", {}).get("rows", [])),
        ),
    )


def insert_batch(conn: sqlite3.Connection, batch: ImportBatch, created_at: str) -> None:
    """写入或更新批次。

    用 upsert 而不是 insert or replace：REPLACE 会先删行，外键级联会把该批次的
    每日入选与来源追溯一并删掉，重新识别就抹掉了去重记录。created_at 保留首次写入时间。
    """
    conn.execute(
        """
        insert into import_batches
            (batch_id, source_kind, source_name, source_ref, archive_path, received_at,
             import_date, status, declared_total, parsed_count, unique_count,
             recognized_count, skipped_count, new_candidate_count,
             merged_candidate_count, reopened_candidate_count,
             error_code, error_message, query_text, condition_labels,
             condition_fingerprint, identity_fingerprint, completeness, selection,
             reidentified_at, reidentified_imported, reidentified_removed, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(batch_id) do update set
            source_kind = excluded.source_kind,
            source_name = excluded.source_name,
            source_ref = excluded.source_ref,
            archive_path = excluded.archive_path,
            received_at = excluded.received_at,
            import_date = excluded.import_date,
            status = excluded.status,
            declared_total = excluded.declared_total,
            parsed_count = excluded.parsed_count,
            unique_count = excluded.unique_count,
            recognized_count = excluded.recognized_count,
            skipped_count = excluded.skipped_count,
            new_candidate_count = excluded.new_candidate_count,
            merged_candidate_count = excluded.merged_candidate_count,
            reopened_candidate_count = excluded.reopened_candidate_count,
            error_code = excluded.error_code,
            error_message = excluded.error_message,
            query_text = excluded.query_text,
            condition_labels = excluded.condition_labels,
            condition_fingerprint = excluded.condition_fingerprint,
            identity_fingerprint = excluded.identity_fingerprint,
            completeness = excluded.completeness,
            selection = excluded.selection,
            reidentified_at = excluded.reidentified_at,
            reidentified_imported = excluded.reidentified_imported,
            reidentified_removed = excluded.reidentified_removed
        """,
        (
            batch.batch_id,
            batch.source_kind.value,
            batch.source_name,
            batch.source_ref,
            batch.archive_path,
            batch.received_at.isoformat(),
            batch.import_date.iso,
            batch.status.value,
            batch.declared_total,
            batch.parsed_count,
            batch.unique_count,
            batch.recognized_count,
            batch.skipped_count,
            batch.new_candidate_count,
            batch.merged_candidate_count,
            batch.reopened_candidate_count,
            batch.error_code,
            batch.error_message,
            batch.query_text,
            json.dumps(list(batch.condition_labels), ensure_ascii=False)
            if batch.condition_labels
            else None,
            batch.condition_fingerprint,
            batch.identity_fingerprint,
            batch.completeness,
            _selection_json(batch.selection),
            batch.reidentified_at.isoformat() if batch.reidentified_at else None,
            batch.reidentified_imported,
            batch.reidentified_removed,
            created_at,
        ),
    )
    replace_batch_stocks(conn, batch)


def update_candidate_counts(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    new: int,
    merged: int,
    reopened: int,
) -> None:
    """回填批次对候选队列的影响数量。

    批次行必须先落库，每日入选与来源追溯才能引用它，因此数量在发布之后单独回填，
    而不是把整行重写一遍。
    """
    conn.execute(
        """
        update import_batches
        set new_candidate_count = ?, merged_candidate_count = ?, reopened_candidate_count = ?
        where batch_id = ?
        """,
        (new, merged, reopened, batch_id),
    )


def replace_batch_stocks(conn: sqlite3.Connection, batch: ImportBatch) -> None:
    """覆盖式写入批次明细，使重新识别/重新发布不产生重复行。"""
    conn.execute("delete from batch_stocks where batch_id = ?", (batch.batch_id,))
    conn.executemany(
        """
        insert into batch_stocks
            (batch_id, position, raw_code, normalized_code, outcome, security_id, reason,
             raw_extras, candidate_effect)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                batch.batch_id,
                s.position,
                s.raw_code,
                s.normalized_code,
                s.outcome.value,
                s.security_id,
                s.reason,
                json.dumps(s.raw_extras, ensure_ascii=False) if s.raw_extras else None,
                s.effect,
            )
            for s in batch.stocks
        ],
    )


def store_candidates(
    conn: sqlite3.Connection, batch_id: str, candidates: tuple[ParsedCandidate, ...]
) -> None:
    """保存待确认批次的候选快照，确认后据此发布。"""
    conn.execute("delete from batch_candidates where batch_id = ?", (batch_id,))
    conn.executemany(
        """
        insert into batch_candidates (batch_id, position, raw_code, normalized_code, raw_extras)
        values (?, ?, ?, ?, ?)
        """,
        [
            (
                batch_id,
                c.position,
                c.raw_code,
                c.normalized_code,
                json.dumps(c.raw_extras, ensure_ascii=False) if c.raw_extras else None,
            )
            for c in candidates
        ],
    )


def load_candidates(conn: sqlite3.Connection, batch_id: str) -> tuple[ParsedCandidate, ...]:
    rows = conn.execute(
        "select * from batch_candidates where batch_id = ? order by id", (batch_id,)
    ).fetchall()
    return tuple(
        ParsedCandidate(
            raw_code=r["raw_code"],
            position=r["position"],
            normalized_code=r["normalized_code"],
            raw_extras=json.loads(r["raw_extras"]) if r["raw_extras"] else {},
        )
        for r in rows
    )


def _to_batch(row: sqlite3.Row, stocks: tuple[BatchStock, ...]) -> ImportBatch:
    labels = json.loads(row["condition_labels"]) if row["condition_labels"] else []
    return ImportBatch(
        batch_id=row["batch_id"],
        source_kind=SourceKind(row["source_kind"]),
        source_name=row["source_name"],
        source_ref=row["source_ref"],
        archive_path=row["archive_path"],
        received_at=datetime.fromisoformat(row["received_at"]),
        import_date=ImportDate(date.fromisoformat(row["import_date"])),
        status=BatchStatus(row["status"]),
        declared_total=row["declared_total"],
        parsed_count=row["parsed_count"],
        unique_count=row["unique_count"],
        recognized_count=row["recognized_count"],
        skipped_count=row["skipped_count"],
        new_candidate_count=row["new_candidate_count"],
        merged_candidate_count=row["merged_candidate_count"],
        reopened_candidate_count=row["reopened_candidate_count"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        stocks=stocks,
        query_text=row["query_text"],
        condition_labels=tuple(labels),
        condition_fingerprint=row["condition_fingerprint"],
        identity_fingerprint=row["identity_fingerprint"],
        completeness=row["completeness"],
        selection=_selection_from_json(row["selection"]),
        reidentified_at=(
            datetime.fromisoformat(row["reidentified_at"]) if row["reidentified_at"] else None
        ),
        reidentified_imported=row["reidentified_imported"],
        reidentified_removed=row["reidentified_removed"],
    )


def _load_stocks(conn: sqlite3.Connection, batch_id: str) -> tuple[BatchStock, ...]:
    rows = conn.execute(
        "select * from batch_stocks where batch_id = ? order by id", (batch_id,)
    ).fetchall()
    return tuple(
        BatchStock(
            position=r["position"],
            raw_code=r["raw_code"],
            normalized_code=r["normalized_code"],
            outcome=StockOutcome(r["outcome"]),
            security_id=r["security_id"],
            reason=r["reason"],
            raw_extras=json.loads(r["raw_extras"]) if r["raw_extras"] else {},
            effect=r["candidate_effect"] if "candidate_effect" in r.keys() else None,
        )
        for r in rows
    )


def get_batch(conn: sqlite3.Connection, batch_id: str) -> ImportBatch | None:
    row = conn.execute(
        "select * from import_batches where batch_id = ?", (batch_id,)
    ).fetchone()
    if row is None:
        return None
    return _to_batch(row, _load_stocks(conn, batch_id))


def list_batches(
    conn: sqlite3.Connection, import_date: str | None = None
) -> list[ImportBatch]:
    if import_date:
        rows = conn.execute(
            "select * from import_batches where import_date = ? order by received_at desc",
            (import_date,),
        ).fetchall()
    else:
        rows = conn.execute(
            "select * from import_batches order by received_at desc"
        ).fetchall()
    return [_to_batch(r, ()) for r in rows]


def confirm_query(
    conn: sqlite3.Connection,
    *,
    query_fingerprint: str,
    query_text: str,
    condition_fingerprint: str | None,
    confirmed_at: str,
) -> None:
    """记录已确认查询身份；口径变化时以最新确认覆盖。"""
    conn.execute(
        """
        insert into wencai_confirmations
            (query_fingerprint, query_text, condition_fingerprint, confirmed_at)
        values (?, ?, ?, ?)
        on conflict(query_fingerprint) do update set
            query_text = excluded.query_text,
            condition_fingerprint = excluded.condition_fingerprint,
            confirmed_at = excluded.confirmed_at
        """,
        (query_fingerprint, query_text, condition_fingerprint, confirmed_at),
    )


def confirmed_condition(conn: sqlite3.Connection, query_fingerprint: str) -> str | None:
    row = conn.execute(
        "select condition_fingerprint from wencai_confirmations where query_fingerprint = ?",
        (query_fingerprint,),
    ).fetchone()
    return row["condition_fingerprint"] if row else None
