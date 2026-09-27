"""Tencent's daily QFQ endpoint, without importing AKShare for every stock.

The endpoint returns up to 640 rows ending near the requested year, including
dates outside the requested interval. Filter after parsing and check overlaps
between year requests before trusting the assembled series.
"""

from __future__ import annotations

import json
import math
from datetime import date

import requests

from .source import ProviderBar, QuoteSourceError


TENCENT_DAILY_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_TIMEOUT = (3, 10)


def fetch_tencent_daily(
    session: requests.Session, *, code: str, exchange: str, start: date, end: date,
) -> list[ProviderBar]:
    """Return QFQ bars in lots and yuan, matching the previous AKShare adapter."""
    symbol = f"{exchange.lower()}{code}"
    by_date: dict[date, ProviderBar] = {}
    for year in range(start.year, end.year + 1):
        params = {
            "_var": f"kline_dayqfq{year}",
            "param": f"{symbol},day,{year}-01-01,{year + 1}-12-31,640,qfq",
            "r": "0.8205512681390605",
        }
        try:
            response = session.get(TENCENT_DAILY_URL, params=params, timeout=_TIMEOUT)
            response.raise_for_status()
            prefix, separator, body = response.text.partition("=")
            if not separator or prefix.strip() != params["_var"]:
                raise ValueError("JSONP 前缀不匹配")
            payload = json.loads(body.rstrip(";\n\r "))
            if not isinstance(payload, dict) or payload.get("code") != 0:
                raise ValueError("来源返回错误状态")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("来源缺少股票数据")
            node = data.get(symbol)
            if not isinstance(node, dict):
                raise ValueError("来源缺少股票数据节点")
            if "qfqday" in node:
                rows = node["qfqday"]
                if not rows and node.get("day"):
                    raise ValueError("前复权序列为空但原始序列非空")
            else:
                rows = node.get("day")  # 从未除权的股票只返回 day。
            if not isinstance(rows, list):
                raise ValueError("来源缺少日线序列")
            for row in rows:
                if not isinstance(row, list) or len(row) < 9:
                    raise ValueError("日线行字段不足")
                trade_date = date.fromisoformat(str(row[0]))
                if not start <= trade_date <= end:
                    continue
                prices = tuple(_number(value) for value in row[1:6])
                amount = _number(row[8]) * 10_000 if row[8] not in (None, "") else None
                # Match AKShare's Tencent normalization: these prefixes already
                # report shares; the others report lots.
                volume_in_shares = symbol.startswith(("sh688", "sz399", "sh000", "sz000"))
                volume = prices[4] / 100 if volume_in_shares else prices[4]
                bar = ProviderBar(
                    trade_date=trade_date, open=prices[0], close=prices[1],
                    high=prices[2], low=prices[3], volume_lots=volume,
                    amount_yuan=amount,
                )
                existing = by_date.get(trade_date)
                if existing is not None and existing != bar:
                    raise ValueError(f"分段重复日期 {trade_date} 的价格不一致")
                by_date[trade_date] = bar
        except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
            raise QuoteSourceError(f"腾讯 {symbol} {year} 年日线获取失败：{type(exc).__name__}: {exc}") from exc
    return [by_date[day] for day in sorted(by_date)]


def _number(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("日线数值为布尔值")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("日线数值非有限")
    return result
