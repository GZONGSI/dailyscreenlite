"""运行配置：数据目录、数据库路径与证券库快照路径。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def _first_existing(*candidates: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


@dataclass(frozen=True)
class Settings:
    """应用配置。测试可显式传入临时数据目录，避免污染运行数据。"""

    data_dir: Path
    securities_snapshot: Path
    wencai_token_bundle: Path | None = None
    # 问财接口基址；默认官方站点，可通过 DSLITE_WENCAI_BASE 指向镜像或本地桩
    wencai_base_url: str | None = None
    # 行情来源：两者都为 None 时按市场走生产链（沪深腾讯→新浪，北交所新浪→AKShare/东方财富），名单仍用 AKShare
    quotes_fixture: Path | None = None
    quotes_history_db: Path | None = None
    # 每日状态来源（停复牌 + 交易日历）：夹具优先，其次 AKShare；关闭时状态记未知
    market_status_fixture: Path | None = None
    market_status_enabled: bool = True
    # 每日 16:30 自动更新与启动补更的开关；生产默认开启
    update_schedule_enabled: bool = False
    # 固定北京时间（ISO 8601）；仅供开发与端到端测试固定"今天"，生产不设置
    fixed_now: str | None = None

    @property
    def runtime_dir(self) -> Path:
        return self.data_dir / "runtime"

    @property
    def database_path(self) -> Path:
        return self.runtime_dir / "dailyscreen_lite.sqlite3"

    @property
    def archive_dir(self) -> Path:
        return self.runtime_dir / "archives"

    @property
    def secrets_path(self) -> Path:
        """本地凭据文件（Cookie）；不进版本控制，也不随批次存档。"""
        return self.runtime_dir / "secrets.json"

    @property
    def frontend_dist(self) -> Path:
        return REPO_ROOT / "frontend" / "dist"

    def ensure_dirs(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)


def load_settings(data_dir: str | os.PathLike[str] | None = None) -> Settings:
    """加载配置。DSLITE_DATA_DIR 环境变量可覆盖数据目录。"""
    env_dir = data_dir or os.environ.get("DSLITE_DATA_DIR")
    resolved_data = Path(env_dir) if env_dir else REPO_ROOT / "data"
    snapshot = _first_existing(
        resolved_data / "securities" / "initial_snapshot.json",
        REPO_ROOT / "data" / "securities" / "initial_snapshot.json",
    )
    env_bundle = os.environ.get("DSLITE_WENCAI_BUNDLE")
    bundle = Path(env_bundle) if env_bundle else REPO_ROOT / "data" / "wencai" / "hexin-v.bundle.js"
    env_now = os.environ.get("DSLITE_NOW")
    env_fixture = os.environ.get("DSLITE_QUOTES_FIXTURE")
    env_history = os.environ.get("DSLITE_QUOTES_HISTORY_DB")
    env_status = os.environ.get("DSLITE_MARKET_STATUS_FIXTURE")
    schedule = os.environ.get("DSLITE_UPDATE_SCHEDULE", "on").strip().lower()
    status_enabled = os.environ.get("DSLITE_MARKET_STATUS", "on").strip().lower()
    return Settings(
        data_dir=resolved_data,
        securities_snapshot=snapshot,
        wencai_token_bundle=bundle,
        wencai_base_url=os.environ.get("DSLITE_WENCAI_BASE"),
        quotes_fixture=Path(env_fixture) if env_fixture else None,
        quotes_history_db=Path(env_history) if env_history else None,
        market_status_fixture=Path(env_status) if env_status else None,
        market_status_enabled=status_enabled not in {"off", "0", "false", "no"},
        update_schedule_enabled=schedule not in {"off", "0", "false", "no"},
        fixed_now=env_now or None,
    )
