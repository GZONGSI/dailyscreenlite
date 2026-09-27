"""首次交付清理工具：备份走 SQLite 接口（含 WAL）、演练不动数据、执行只清业务表。

对应复核意见：仅复制主文件会漏掉 WAL 中已提交但未合并的行；库操作必须在
repository 层完成，工具只负责路径边界校验、演练输出与存档文件删除。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest
from conftest import build_offline, make_csv, offline_settings

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "tools"))

import reset_business_data as tool  # noqa: E402

from dailyscreen_lite.domain.clock import FixedClock  # noqa: E402
from dailyscreen_lite.repository import Database, maintenance  # noqa: E402
from dailyscreen_lite.settings import REPO_ROOT, Settings  # noqa: E402


def _rows(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f'select count(*) from "{table}"').fetchone()[0]
    finally:
        conn.close()


def _run(data_dir: Path, monkeypatch: pytest.MonkeyPatch, *extra: str) -> str:
    monkeypatch.setattr(
        sys, "argv", ["reset_business_data", "--data-dir", str(data_dir), *extra]
    )
    return tool.main()


def _seed(tmp_path: Path) -> Settings:
    """用真实导入流程种下业务数据与来源存档，再放一个 Cookie 文件。

    走离线夹具并停用后台补取：本用例只关心「库里有什么」，不该顺带打供应商。
    """
    settings = offline_settings(tmp_path)
    container = build_offline(settings, FixedClock(datetime(2026, 9, 18, 17, 0, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    settings.secrets_path.write_text('{"cookie": "keep-me"}', encoding="utf-8")
    return settings


def test_backup_keeps_rows_that_are_still_only_in_wal(tmp_path):
    """备份必须包含 WAL 中尚未合并进主文件的已提交数据。

    写入后保持连接打开（关闭时 SQLite 才会归并 WAL），此时主文件里还没有这些行：
    直接复制文件会得到一个打不开这些表的空库，必须走 SQLite 备份接口。
    """
    path = tmp_path / "runtime" / "dailyscreen_lite.sqlite3"
    Database(path).initialize()
    writer = sqlite3.connect(path, isolation_level=None)
    writer.execute("pragma journal_mode=wal")
    writer.execute("pragma wal_autocheckpoint=0")
    writer.execute("insert into notes values ('n1', '000001.SZ', '正文', '2026-09-18T10:00:00')")
    writer.commit()

    target = tool.backup(tmp_path / "runtime", path)
    assert target.exists()
    assert _rows(target / path.name, "notes") == 1
    writer.close()

    # 备份文件本身是可直接打开的完整库
    assert _rows(target / path.name, "securities") == 0


def test_dry_run_reports_scope_and_changes_nothing(tmp_path, monkeypatch, capsys):
    settings = _seed(tmp_path)
    assert _run(settings.data_dir, monkeypatch) == 0
    out = capsys.readouterr().out
    assert "演练结束" in out
    assert "- import_batches: 1" in out

    # 主库与存档都原样不动
    assert _rows(settings.database_path, "import_batches") == 1
    assert list(settings.archive_dir.iterdir())


def test_apply_purges_business_tables_and_keeps_cookie_and_delivery_files(
    tmp_path, monkeypatch, capsys
):
    settings = _seed(tmp_path)
    archives = list(settings.archive_dir.rglob("*"))
    assert archives

    assert _run(settings.data_dir, monkeypatch, "--apply") == 0
    assert "清理完成" in capsys.readouterr().out

    db = Database(settings.database_path)
    counts = maintenance.row_counts(
        db,
        (
            "candidates",
            "candidate_selections",
            "import_batches",
            "batch_stocks",
            "observation_groups",
            "securities",
            "notes",
            "daily_quotes",
            "update_runs",
        ),
    )
    assert set(counts.values()) == {0}

    # Cookie 与交付资源保留；证券库清空后由应用启动时按交付快照重新加载
    assert settings.secrets_path.exists()
    assert settings.secrets_path.read_text(encoding="utf-8") == '{"cookie": "keep-me"}'
    assert (REPO_ROOT / "data" / "securities" / "initial_snapshot.json").exists()
    assert (REPO_ROOT / "data" / "wencai").exists()

    # 来源存档删除、留下的空目录清掉；备份保留了清空前的行
    assert not [p for p in settings.archive_dir.rglob("*") if p.is_file()]
    backups = sorted((settings.runtime_dir / "backups").iterdir())
    assert _rows(backups[-1] / settings.database_path.name, "import_batches") == 1


@pytest.mark.parametrize("unsafe", ["repo", "repo_parent", "home", "anchor"])
def test_refuses_unsafe_cleanup_targets(tmp_path, unsafe):
    candidate = {
        "repo": REPO_ROOT,
        "repo_parent": REPO_ROOT.parent,
        "home": Path.home(),
        "anchor": Path(tmp_path.anchor),
    }[unsafe]
    with pytest.raises(SystemExit):
        tool.resolve_targets(candidate)


def test_refuses_data_dir_without_runtime_database(tmp_path):
    # 工作区这种没有 runtime/dailyscreen_lite.sqlite3 的目录一律拒绝
    with pytest.raises(SystemExit):
        tool.resolve_targets(tmp_path)
