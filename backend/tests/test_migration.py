"""数据库初始化测试：新库建出候选项表，旧库只做加法补齐、不迁移旧业务数据。

本轮候选项重建（一只股票一个候选项 + 每日入选）不迁移旧研究项数据：
旧库中的 research_items / workbench_state 等遗留表原样保留但不再读写，
实际清理留到首次交付切换。这里只保证 initialize() 能就地补齐列、建出新表，
并且不删除既有导入事实与遗留数据。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dailyscreen_lite.repository import Database

CANDIDATE_TABLES = {
    "candidates",
    "candidate_selections",
    "candidate_sources",
    "candidate_history",
    "classification_state",
}


def _create_legacy_db(path: Path) -> None:
    """构造重建前一代的旧库：旧研究项身份 + 缺列 + 缺新表。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            create table import_batches (
                batch_id text primary key,
                source_kind text not null,
                source_name text not null,
                source_ref text,
                archive_path text,
                received_at text not null,
                import_date text not null,
                status text not null,
                declared_total integer,
                parsed_count integer not null default 0,
                unique_count integer not null default 0,
                recognized_count integer not null default 0,
                skipped_count integer not null default 0,
                new_item_count integer not null default 0,
                existing_item_count integer not null default 0,
                error_code text,
                error_message text,
                created_at text not null
            );
            create table batch_stocks (
                id integer primary key autoincrement,
                batch_id text not null,
                position text not null,
                raw_code text not null,
                normalized_code text not null,
                outcome text not null,
                security_id text,
                reason text
            );
            create table research_items (
                item_id text primary key,
                security_id text not null,
                import_date text not null,
                state text not null,
                first_seen_at text not null,
                last_action_at text,
                action_result text,
                unique (security_id, import_date)
            );
            create table workbench_state (
                id integer primary key check (id = 1),
                current_item_id text,
                view_mode text not null default 'card',
                scope text not null default 'unprocessed',
                import_date text,
                search text not null default '',
                result text,
                round_started_at text,
                updated_at text not null
            );
            insert into import_batches
                (batch_id, source_kind, source_name, received_at, import_date, status, created_at)
            values ('legacy', 'csv', 'old.csv', '2026-09-01T10:00:00+08:00', '2026-09-01', 'published', '2026-09-01T10:00:00+08:00');
            insert into batch_stocks
                (batch_id, position, raw_code, normalized_code, outcome, reason)
            values ('legacy', '第2行', '000001', '000001', 'imported', null);
            insert into research_items
                (item_id, security_id, import_date, state, first_seen_at)
            values ('000001.SZ@2026-09-01', '000001.SZ', '2026-09-01', 'pending', '2026-09-01T10:00:00+08:00');
            """
        )
        conn.commit()
    finally:
        conn.close()


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"] for row in conn.execute("select name from sqlite_master where type='table'")
    }


def test_initialize_creates_candidate_tables_on_fresh_db(tmp_path: Path):
    db = Database(tmp_path / "runtime" / "fresh.sqlite3")
    db.initialize()

    with db.read() as conn:
        assert CANDIDATE_TABLES <= _tables(conn)
        columns = {r["name"] for r in conn.execute("pragma table_info(candidates)")}
        assert {"candidate_id", "security_id", "state", "queue_order", "viewed_at"} <= columns
        selection_columns = {
            r["name"] for r in conn.execute("pragma table_info(candidate_selections)")
        }
        assert {"candidate_id", "import_date", "batch_id", "selected_at"} <= selection_columns


def test_initialize_adds_raw_extras_column_without_losing_rows(tmp_path: Path):
    db_path = tmp_path / "runtime" / "legacy.sqlite3"
    _create_legacy_db(db_path)

    db = Database(db_path)
    db.initialize()

    with db.read() as conn:
        columns = {row["name"] for row in conn.execute("pragma table_info(batch_stocks)")}
        assert "raw_extras" in columns
        row = conn.execute("select * from batch_stocks where batch_id = 'legacy'").fetchone()
        assert row["raw_code"] == "000001"
        assert row["raw_extras"] is None
        # 既有批次事实未被删除
        batch = conn.execute("select * from import_batches where batch_id = 'legacy'").fetchone()
        assert batch["status"] == "published"


def test_initialize_adds_candidate_count_columns_on_legacy_batches(tmp_path: Path):
    """旧库批次只有旧计数列：就地补上新增/合并/重新归类计数，旧行保留。"""
    db_path = tmp_path / "runtime" / "legacy.sqlite3"
    _create_legacy_db(db_path)

    db = Database(db_path)
    db.initialize()

    with db.read() as conn:
        columns = {row["name"] for row in conn.execute("pragma table_info(import_batches)")}
        assert {
            "new_candidate_count",
            "merged_candidate_count",
            "reopened_candidate_count",
        } <= columns
        batch = conn.execute("select * from import_batches where batch_id = 'legacy'").fetchone()
        assert batch["status"] == "published"
        assert batch["new_candidate_count"] == 0


def test_initialize_creates_new_tables_and_leaves_legacy_data_untouched(tmp_path: Path):
    """新表就地建出；旧业务数据不迁移也不清理（清理留到首次交付切换）。"""
    db_path = tmp_path / "runtime" / "legacy.sqlite3"
    _create_legacy_db(db_path)

    db = Database(db_path)
    db.initialize()

    with db.read() as conn:
        tables = _tables(conn)
        assert CANDIDATE_TABLES <= tables
        assert "notes" in tables
        assert {"observation_groups", "observation_members"} <= tables
        # 遗留研究项行保留，但不再作为当前候选项参与工作台
        assert conn.execute("select count(*) from research_items").fetchone()[0] == 1
        assert conn.execute("select count(*) from import_batches").fetchone()[0] == 1
        assert conn.execute("select count(*) from candidates").fetchone()[0] == 0
