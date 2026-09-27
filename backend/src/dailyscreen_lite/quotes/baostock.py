"""BaoStock 0.9.3 日线；股→手，直接使用服务端前复权价格。"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date

from .daily import validate_bars
from .source import ProviderBar, QuoteSourceError


def query_worker(request: dict) -> dict:
    # SDK uses a process-global socket and may hang on EOF. Kill the isolated worker
    # on timeout instead of leaving a blocked thread or changing global socket defaults.
    try:
        result = subprocess.run(
            [sys.executable, "-m", "dailyscreen_lite.quotes.baostock_worker"],
            input=json.dumps(request), capture_output=True, text=True, encoding="utf-8",
            timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise QuoteSourceError(f"BaoStock 子进程失败：{result.stderr[-500:]}")
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise QuoteSourceError("BaoStock 请求超过 45 秒") from exc
    except (OSError, ValueError) as exc:
        raise QuoteSourceError(f"BaoStock 协议失败：{exc}") from exc


class BaostockQuotesSource:
    source_id = "baostock"
    daily_markets = ("SH", "SZ")

    def __init__(self, *, transport=None):
        self._transport = transport or query_worker

    def declared_adjust(self):
        return "qfq"

    def daily_bars(self, *, code, exchange, start, end, adjust):
        if exchange not in {"SH", "SZ"} or adjust != "qfq":
            raise QuoteSourceError("BaoStock 不支持此市场或复权口径")
        symbol = f"{exchange.lower()}.{code}"
        payload = self._transport({"code": symbol, "start": start.isoformat(), "end": end.isoformat()})
        try:
            if payload["code"] != "0":
                raise QuoteSourceError(f"BaoStock {payload['code']}: {payload.get('message', '')}")
            fields = payload["fields"]
            required = {"date", "code", "open", "high", "low", "close", "volume", "amount", "adjustflag", "tradestatus"}
            if set(fields) != required or len(fields) != len(required):
                raise QuoteSourceError("BaoStock 日线字段不完整")
            bars = []
            previous = None
            for values in payload["rows"]:
                row = dict(zip(fields, values, strict=True))
                day = date.fromisoformat(row["date"])
                if row["code"] != symbol or row["adjustflag"] != "2":
                    raise QuoteSourceError("BaoStock 证券或复权口径不符")
                if not start <= day <= end or (previous and day <= previous):
                    raise QuoteSourceError("BaoStock 日线日期异常")
                previous = day
                if row["tradestatus"] == "0":
                    continue  # Explicit placeholders never become bars or suspension intervals.
                if row["tradestatus"] != "1":
                    raise QuoteSourceError("BaoStock 交易状态未知")
                bars.append(ProviderBar(
                    day, *(float(row[k]) for k in ("open", "high", "low", "close")),
                    float(row["volume"]) / 100,
                    float(row["amount"]) if row["amount"] not in ("", None) else None,
                ))
            validate_bars(bars, start, end)
            return bars
        except (ValueError, KeyError, TypeError) as exc:
            raise QuoteSourceError(f"BaoStock 日线格式异常：{exc}") from exc
