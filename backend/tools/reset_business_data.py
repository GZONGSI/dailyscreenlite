"""旧业务数据清理（首次交付切换用）。

用途：把运行库里的旧导入、原始来源存档、候选、观察组、笔记与行情清空，保留
本地 Cookie（`secrets.json`）、连接配置与交付资源，证券库留空由应用启动时按
交付快照重新加载。

安全约束（AGENTS.md：执行前必须定位实际运行数据路径并验证清理目标边界）：
- 默认只做演练，打印将要删除的表与行数、将要删除的存档文件，不修改任何数据；
- 真正执行需要显式 `--apply`，且执行前先把数据库与存档目录整体备份到
  `runtime/backups/<时间戳>/`（数据库走 SQLite 备份接口，WAL 中未合并的数据一并备份）；
- 只操作 `runtime/` 下的数据库与 `runtime/archives/`，拒绝把工作区、数据目录
  本身、仓库根目录或任何不在运行目录内的路径当作清理目标；
- `secrets.json` 与交付资源（`data/securities`、`data/wencai`）永不删除。

库上的读写都经 `repository/maintenance`，本脚本不自己拼 SQL、不自己复制数据库文件。

用法：
    python backend/tools/reset_business_data.py                 # 演练（默认）
    python backend/tools/reset_business_data.py --apply         # 执行并先备份
    python backend/tools/reset_business_data.py --data-dir PATH # 指定数据目录
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from dailyscreen_lite.repository import Database, maintenance  # noqa: E402
from dailyscreen_lite.settings import REPO_ROOT, load_settings  # noqa: E402

# 需要清空的业务表（清空行，不删表结构）。证券库留给应用按交付快照重新加载。
BUSINESS_TABLES = (
    "batch_stocks",
    "batch_candidates",
    "candidate_sources",
    "candidate_selections",
    "candidate_history",
    "candidates",
    "import_batches",
    "wencai_confirmations",
    "classification_state",
    "observation_members",
    "observation_groups",
    "observation_state",
    "notes",
    "quote_history_archive",
    "daily_quotes",
    "market_suspensions",
    "market_status_snapshots",
    "trade_dates",
    "update_stock_results",
    "update_runs",
    "app_seeds",
    "securities",
    "securities_snapshot_meta",
    # 旧版本遗留表：本轮不再读写，一并清空
    "research_items",
    "research_item_sources",
    "workbench_state",
)


def resolve_targets(data_dir: Path) -> tuple[Path, Path, Path]:
    """返回 (data_dir, runtime_dir, database_path)，并校验清理目标边界。"""
    resolved = data_dir.resolve()
    runtime = (resolved / "runtime").resolve()
    database = runtime / "dailyscreen_lite.sqlite3"

    # 目标必须严格位于运行目录内：拒绝仓库根、数据目录本身、盘符根与家目录
    if resolved in {REPO_ROOT.resolve(), REPO_ROOT.resolve().parent}:
        raise SystemExit(f"拒绝清理仓库目录：{resolved}")
    if resolved.anchor == str(resolved):
        raise SystemExit(f"拒绝清理盘符根目录：{resolved}")
    if resolved == Path.home().resolve():
        raise SystemExit(f"拒绝清理用户主目录：{resolved}")
    if not database.exists():
        raise SystemExit(
            f"运行库不存在，清理目标可疑：{database}\n"
            "请用 --data-dir 指定实际数据目录（其下应有 runtime/dailyscreen_lite.sqlite3）。"
        )
    return resolved, runtime, database


def archive_files(runtime: Path) -> list[Path]:
    archives = runtime / "archives"
    if not archives.exists():
        return []
    return sorted(path for path in archives.rglob("*") if path.is_file())


def backup(runtime: Path, database: Path) -> Path:
    """执行前备份：数据库走 SQLite 备份接口，存档目录整体复制。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = runtime / "backups" / stamp
    target.mkdir(parents=True, exist_ok=True)
    maintenance.backup_database(database, target / database.name)
    archives = runtime / "archives"
    if archives.exists():
        shutil.copytree(archives, target / "archives")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="清空旧业务数据（默认演练）")
    parser.add_argument("--data-dir", default=None, help="数据目录；默认取 DSLITE_DATA_DIR 或 data/")
    parser.add_argument("--apply", action="store_true", help="真正执行清理（默认只演练）")
    args = parser.parse_args()

    data_dir, runtime, database = resolve_targets(load_settings(args.data_dir).data_dir)
    files = archive_files(runtime)
    db = Database(database)

    counts = maintenance.row_counts(db, BUSINESS_TABLES)
    print(f"数据目录：{data_dir}")
    print(f"运行目录：{runtime}")
    print(f"数据库  ：{database}")
    print(f"将清空 {sum(counts.values())} 行：")
    for table, count in counts.items():
        print(f"  - {table}: {count}")
    print(f"将删除来源存档 {len(files)} 个文件（runtime/archives/）：")
    for path in files:
        print(f"  - {path.relative_to(runtime)}")
    print("保留：secrets.json（Cookie 与连接配置）、交付资源 data/securities 与 data/wencai；")
    print("     证券库清空后由应用启动时按交付快照重新加载。")

    if not args.apply:
        print("\n演练结束：未修改任何数据。确认无误后加 --apply 执行。")
        return 0

    target = backup(runtime, database)
    print(f"\n已备份到：{target}")
    maintenance.purge_tables(db, BUSINESS_TABLES)
    maintenance.vacuum(db)
    for path in files:
        path.unlink()
    archives = runtime / "archives"
    if archives.exists():
        for junk in sorted(archives.iterdir(), reverse=True):
            if junk.is_dir() and not any(junk.iterdir()):
                junk.rmdir()
    print("清理完成：业务数据已清空，来源存档已删除；重启应用即按交付快照加载证券库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
