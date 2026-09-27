"""初始权威证券库快照加载。

快照由 backend/tools/build_securities_snapshot.py 从授权来源只读抽取，
记录实际来源与身份指纹；加载失败不阻止应用启动，只是无法识别股票。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dailyscreen_lite.domain.clock import BEIJING
from dailyscreen_lite.domain.models import Security


class SnapshotLoadError(RuntimeError):
    """快照缺失或结构不合法。"""


@dataclass(frozen=True)
class LoadedSnapshot:
    snapshot_id: str
    loaded_at: str
    record_count: int
    identity_fingerprint: str
    effective_date: str | None
    source: dict
    securities: list[Security]


def load_snapshot(path: Path) -> LoadedSnapshot:
    if not path.exists():
        raise SnapshotLoadError(f"证券库快照不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 结构损坏时明确报错
        raise SnapshotLoadError(f"证券库快照无法解析：{exc}") from exc

    rows = data.get("securities")
    if not isinstance(rows, list) or not rows:
        raise SnapshotLoadError("证券库快照没有证券记录")

    securities = [
        Security(
            security_id=row["security_id"],
            code=str(row["code"]),
            exchange=row["exchange"],
            board=row["board"],
            name=row.get("name", ""),
            listing_date=row.get("listing_date"),
            is_st=bool(row.get("is_st", False)),
        )
        for row in rows
    ]
    source = data.get("source", {})
    return LoadedSnapshot(
        snapshot_id=source.get("batch_id") or data.get("identity_fingerprint", "unknown"),
        loaded_at=datetime.now(BEIJING).isoformat(),
        record_count=int(data.get("record_count", len(securities))),
        identity_fingerprint=str(data.get("identity_fingerprint", "")),
        effective_date=source.get("effective_trade_date"),
        source=source,
        securities=securities,
    )
