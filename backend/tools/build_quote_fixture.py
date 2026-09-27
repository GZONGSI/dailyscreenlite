"""把授权历史库的真实日线样本导出为行情夹具，用于确定性回放测试（A25）。

只读抽取 dailyscreen-data 的 canonical_market_daily，不改来源库、不迁移。该表价格与
成交额已是元、成交量为股，导出时成交量换算为手（1 手 = 100 股），与 AKShare 记录
同形；夹具在加载时按同一边界读入，因此回放检验的是「历史样本经正常行情接入边界」
而非绕过被测流程。

用法（`--source-db` 指向本机授权的 dailyscreen-data 历史库，本仓库不含该库）：
    python backend/tools/build_quote_fixture.py \
        --source-db <dailyscreen-data>/production/dailyscreen.duckdb \
        --codes 000001.SZ,600519.SH --start 2019-01-01 --end 2026-09-03 \
        --out backend/tests/fixtures/quote_replay_sample.json
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


def build(source_db: Path, codes: list[str], start: str, end: str) -> dict:
    con = duckdb.connect(str(source_db), read_only=True)
    try:
        bars: dict[str, list[dict]] = {}
        fingerprints: dict[str, str] = {}
        for security_id in codes:
            rows = con.execute(
                """
                select trade_date, open, high, low, close, volume_shares, amount_yuan
                from canonical_market_daily
                where security_id = ? and trade_date >= ? and trade_date <= ?
                order by trade_date asc
                """,
                [security_id, start, end],
            ).fetchall()
            digest = hashlib.sha256()
            entries = []
            for trade_date, open_, high, low, close, volume_shares, amount_yuan in rows:
                # canonical_market_daily 价格与成交额已是元（conversion_rules: fen->yuan）；
                # 成交量仍是股，换算为手（1 手 = 100 股）。不再对价格做二次换算。
                entry = {
                    "date": str(trade_date),
                    "open": float(open_),
                    "high": float(high),
                    "low": float(low),
                    "close": float(close),
                    "volume_lots": round(float(volume_shares) / 100.0, 2),
                    "amount_yuan": float(amount_yuan),
                }
                entries.append(entry)
                digest.update(f"{entry['date']}:{entry['close']}".encode("utf-8"))
            bars[security_id] = entries
            fingerprints[security_id] = digest.hexdigest()
    finally:
        con.close()

    return {
        "schema_version": 1,
        "generated_at": datetime.now(BEIJING).isoformat(),
        # 该样本为通达信原始日线，非东方财富前复权；夹具来源据此如实标注口径
        "adjust": "raw",
        "source": "tdx.hsjday",
        "source_meta": {
            "database": "dailyscreen-data/production/dailyscreen.duckdb",
            "table": "canonical_market_daily",
            "actual_source_id": "tongdaxin.hsjday",
            "adapter_id": "dailyscreen.tdx-day",
            "note": "只读抽取；价格与成交额已是元，成交量由股换算为手。",
            "range": {"start": start, "end": end},
            "sample_fingerprints": fingerprints,
        },
        "bars": bars,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description="从授权历史库导出行情回放夹具")
    parser.add_argument(
        "--source-db",
        default="dailyscreen-data/production/dailyscreen.duckdb",
        help="只读来源 DuckDB 路径（本机授权的 dailyscreen-data，不在本仓库内）",
    )
    parser.add_argument("--codes", default="000001.SZ,600519.SH")
    parser.add_argument("--start", default="2019-01-01")
    parser.add_argument("--end", default="2026-09-03")
    parser.add_argument(
        "--out",
        default=str(root / "backend" / "tests" / "fixtures" / "quote_replay_sample.json"),
    )
    args = parser.parse_args()

    codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    payload = build(Path(args.source_db), codes, args.start, args.end)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"写入 {out}")
    fingerprints = payload["source_meta"]["sample_fingerprints"]
    for code, rows in payload["bars"].items():
        print(f"  {code}: {len(rows)} 行，指纹 {fingerprints[code][:12]}")


if __name__ == "__main__":
    main()
