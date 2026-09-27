"""从授权的只读历史库生成初始权威证券库快照。

只读取 dailyscreen-data 中当前生效的证券名单与最近股票名称，不做任何写入、
迁移或清理。输出 data/securities/initial_snapshot.json，随代码交付给 Lite 首版使用。

用法（`--source-db` 指向本机授权的 dailyscreen-data 历史库，本仓库不含该库）：
    python backend/tools/build_securities_snapshot.py \
        --source-db <dailyscreen-data>/production/dailyscreen.duckdb \
        --out data/securities/initial_snapshot.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import duckdb

BEIJING = ZoneInfo("Asia/Shanghai")

FACTS = "security_universe_facts"
VERSIONS = "security_universe_effective_versions"
BATCHES = "security_universe_batches"
SNAPSHOTS = "security_state_snapshots"

EXCHANGE_LABEL = {"SH": "上交所", "SZ": "深交所", "BJ": "北交所"}


def build(source_db: Path) -> dict:
    con = duckdb.connect(str(source_db), read_only=True)
    try:
        version_row = con.execute(
            f"select effective_trade_date, batch_id from {VERSIONS} order by effective_trade_date desc limit 1"
        ).fetchone()
        if version_row is None:
            raise RuntimeError("生效证券名单为空，无法生成初始快照")
        effective_date, batch_id = version_row

        batch_row = con.execute(
            f"select status, actual_sources_json, acquired_at from {BATCHES} where batch_id = ?",
            [batch_id],
        ).fetchone()
        if batch_row is None:
            raise RuntimeError(f"找不到生效批次 {batch_id}")
        status, sources_json, acquired_at = batch_row
        if status != "valid":
            raise RuntimeError(f"生效批次状态为 {status}，拒绝生成快照")

        rows = con.execute(
            f"""
            with f as (
                select security_id, exchange, board, listing_date
                from {FACTS} where batch_id = ?
            ), n as (
                select security_id,
                       arg_max(name, effective_trade_date) as name,
                       arg_max(is_st, effective_trade_date) as is_st
                from {SNAPSHOTS}
                group by security_id
            )
            select f.security_id, f.exchange, f.board, f.listing_date,
                   coalesce(n.name, ''), coalesce(n.is_st, false)
            from f left join n using (security_id)
            order by f.security_id
            """,
            [batch_id],
        ).fetchall()
    finally:
        con.close()

    securities = [
        {
            "security_id": sid,
            "code": sid.split(".")[0],
            "exchange": exchange,
            "board": board,
            "name": name,
            "listing_date": listing_date or None,
            "is_st": bool(is_st),
        }
        for sid, exchange, board, listing_date, name, is_st in rows
    ]

    codes = sorted(s["security_id"] for s in securities)
    fingerprint = hashlib.sha256("\n".join(codes).encode("utf-8")).hexdigest()
    sources = json.loads(sources_json) if sources_json else {}
    market_counts: dict[str, int] = {}
    for s in securities:
        market_counts[s["exchange"]] = market_counts.get(s["exchange"], 0) + 1

    return {
        "schema_version": 1,
        "generated_at": datetime.now(BEIJING).isoformat(),
        "source": {
            "database": "dailyscreen-data/production/dailyscreen.duckdb",
            "batch_id": batch_id,
            "effective_trade_date": effective_date,
            "acquired_at": acquired_at,
            "declared_sources": sources,
            "note": "只读抽取当前生效 A 股名单与最近股票名称，未修改来源库。",
        },
        "record_count": len(securities),
        "market_counts": {EXCHANGE_LABEL.get(k, k): v for k, v in sorted(market_counts.items())},
        "identity_fingerprint": fingerprint,
        "securities": securities,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="生成初始权威证券库快照")
    parser.add_argument(
        "--source-db",
        default="dailyscreen-data/production/dailyscreen.duckdb",
        help="只读来源 DuckDB 路径（本机授权的 dailyscreen-data，不在本仓库内）",
    )
    parser.add_argument(
        "--out",
        default=str(root / "data" / "securities" / "initial_snapshot.json"),
        help="输出快照路径",
    )
    args = parser.parse_args()

    snapshot = build(Path(args.source_db))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"写入 {out}")
    print(f"记录数 {snapshot['record_count']}，市场分布 {snapshot['market_counts']}")
    print(f"身份指纹 {snapshot['identity_fingerprint']}")
    print(f"生效日期 {snapshot['source']['effective_trade_date']}")


if __name__ == "__main__":
    main()
