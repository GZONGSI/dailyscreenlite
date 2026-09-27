"""Read-only live comparison of direct Tencent and the installed AKShare adapter.

Run from the repository root, for example:
    backend/.venv/Scripts/python.exe backend/tools/verify_tencent_daily.py 2026-09-14 2026-09-23
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dailyscreen_lite.quotes.akshare_worker import bounded_akshare  # noqa: E402
from dailyscreen_lite.quotes.source import TencentHttpQuotesSource  # noqa: E402


def check_stock(security_id: str, start: date, end: date, source: TencentHttpQuotesSource) -> dict:
    code, exchange = security_id.split(".")
    begun = time.perf_counter()
    direct = source.daily_bars(code=code, exchange=exchange, start=start, end=end, adjust="qfq")
    direct_ms = round((time.perf_counter() - begun) * 1000)
    begun = time.perf_counter()
    frame = bounded_akshare().stock_zh_a_hist_tx(
        symbol=f"{exchange.lower()}{code}", start_date=start.strftime("%Y%m%d"),
        end_date=end.strftime("%Y%m%d"), adjust="qfq", timeout=10,
    )
    adapter_ms = round((time.perf_counter() - begun) * 1000)
    expected = [
        (date.fromisoformat(str(row["date"])[:10]), float(row["open"]),
         float(row["high"]), float(row["low"]), float(row["close"]),
         float(row["volume"]) / 100, float(row["amount"]))
        for _, row in frame.iterrows()
    ]
    actual = [
        (bar.trade_date, bar.open, bar.high, bar.low, bar.close,
         bar.volume_lots, bar.amount_yuan)
        for bar in direct
    ]
    matched = bool(actual) and len(expected) == len(actual) and all(
        old[0] == new[0] and all(
            old_value is not None and new_value is not None and
            math.isclose(old_value, new_value, rel_tol=1e-7, abs_tol=1e-5)
            for old_value, new_value in zip(old[1:], new[1:])
        )
        for old, new in zip(expected, actual)
    )
    return {
        "securityId": security_id,
        "directMs": direct_ms, "adapterMs": adapter_ms,
        "directRows": len(actual), "adapterRows": len(expected),
        "latestDate": direct[-1].trade_date.isoformat() if direct else None,
        "matched": matched,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("start", type=date.fromisoformat)
    parser.add_argument("end", type=date.fromisoformat)
    args = parser.parse_args()
    source = TencentHttpQuotesSource()
    results = []
    for security_id in ("600519.SH", "688981.SH", "000001.SZ"):
        result = check_stock(security_id, args.start, args.end, source)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        results.append(result)
    return 0 if all(result["matched"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
