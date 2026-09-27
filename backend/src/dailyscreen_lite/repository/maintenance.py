"""交付切换与运维用的库维护：备份、清空与压缩。只有本层出现 SQL。

清理脚本只负责路径边界校验、演练输出与存档文件删除；库上的读写一律经这里，
避免工具绕过持久化层自己拼 SQL。连接与事务都交给 `Database`，与各领域服务一致。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

from dailyscreen_lite.repository.database import Database


def _table_names(db: Database) -> set[str]:
    with db.read() as conn:
        rows = conn.execute(
            "select name from sqlite_master where type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def existing_tables(db: Database, tables: Sequence[str]) -> tuple[str, ...]:
    """给定表中实际存在的部分：旧库可能缺少某些遗留表，缺失不算错误。"""
    present = _table_names(db)
    return tuple(table for table in tables if table in present)


def row_counts(db: Database, tables: Sequence[str]) -> dict[str, int]:
    """按表统计行数，供演练输出将要清空的规模。"""
    with db.read() as conn:
        return {
            table: conn.execute(f'select count(*) from "{table}"').fetchone()[0]
            for table in existing_tables(db, tables)
        }


def purge_tables(db: Database, tables: Sequence[str]) -> None:
    """清空这些表的行（保留表结构）并重置自增序号；整段成功或整段回滚。"""
    targets = existing_tables(db, tables)
    sequence = "sqlite_sequence" in _table_names(db)
    with db.transaction() as conn:
        for table in targets:
            conn.execute(f'delete from "{table}"')
        if sequence:
            conn.execute("delete from sqlite_sequence")


def vacuum(db: Database) -> None:
    """压缩主文件；VACUUM 不能在事务里执行，因此单独取一个写连接。"""
    conn = db.connect()
    try:
        conn.execute("vacuum")  # noqa: S608 - 固定语句，无外部输入
    finally:
        conn.close()


def backup_database(source: Path, target: Path) -> None:
    """用 SQLite 备份接口复制运行库。

    逐页复制而不是复制文件：已提交但仍在 WAL 中、尚未合并进主文件的数据同样会
    被复制进去；直接复制主文件会漏掉这部分（文件看着存在，打开却没有这些行）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(source)
    try:
        dest_conn = sqlite3.connect(target)
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
