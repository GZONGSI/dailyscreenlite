"""日线按股票切源；证券名单、日历和停复牌保持独立。"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import date

from dailyscreen_lite.domain.models import SourceAttempt

from .source import (
    DailyQuotesSource,
    ProviderBar,
    QuoteSourceError,
    covered_markets,
    declared_adjust_of,
    declared_source_of,
)


def source_name(source: DailyQuotesSource) -> str:
    """页面与记录里显示的来源名：来源自带名字优先，否则用 source_id。"""
    return declared_source_of(source) or source.source_id


def validate_bars(bars: list[ProviderBar], start: date, end: date) -> None:
    previous = None
    for bar in bars:
        numbers = (bar.open, bar.high, bar.low, bar.close, bar.volume_lots)
        if not all(math.isfinite(n) for n in numbers):
            raise QuoteSourceError("日线含非有限数值")
        if (
            min(bar.open, bar.high, bar.low, bar.close) <= 0
            or bar.volume_lots < 0
            or bar.low > min(bar.open, bar.close)
            or bar.high < max(bar.open, bar.close)
            or bar.low > bar.high
        ):
            raise QuoteSourceError("日线价格或成交量格式异常")
        if bar.amount_yuan is not None and (not math.isfinite(bar.amount_yuan) or bar.amount_yuan < 0):
            raise QuoteSourceError("成交额格式异常")
        if not start <= bar.trade_date <= end or (previous is not None and bar.trade_date <= previous):
            raise QuoteSourceError("日线日期越界、重复或乱序")
        previous = bar.trade_date


@dataclass(frozen=True)
class DailyResult:
    bars: list[ProviderBar]
    source: str | None
    message: str | None
    failed: bool
    attempted: tuple[str, ...] = ()
    errors: tuple[tuple[str, str], ...] = ()
    attempt_details: tuple[SourceAttempt, ...] = ()


def fetch_daily(
    sources: list[DailyQuotesSource], *, preferred: str | None,
    code: str, exchange: str, start: date, end: date, adjust: str,
    excluded_sources: set[str] | None = None,
) -> DailyResult:
    ordered = sorted(sources, key=lambda source: source_name(source) != preferred)
    messages: list[str] = []
    best: list[ProviderBar] = []
    selected = None
    failed = False
    attempted: list[str] = []
    errors: list[tuple[str, str]] = []
    details: list[SourceAttempt] = []
    for source in ordered:
        if exchange not in covered_markets(source):
            continue
        name = source_name(source)
        if name in (excluded_sources or ()):
            continue
        attempted.append(name)
        started = time.perf_counter()
        try:
            declared = declared_adjust_of(source)
            if declared is not None and declared != adjust:
                raise QuoteSourceError("来源复权口径与请求不符")
            bars = source.daily_bars(code=code, exchange=exchange, start=start, end=end, adjust=adjust)
            validate_bars(bars, start, end)
            details.append({
                "source": name, "elapsedMs": round((time.perf_counter() - started) * 1000),
                "outcome": "data" if bars else "empty",
                "latestDate": bars[-1].trade_date.isoformat() if bars else None,
            })
            if bars and bars[-1].trade_date == end:
                return DailyResult(bars, name, "；".join(messages) or None, False,
                                   tuple(attempted), tuple(errors), tuple(details))
            if bars:
                messages.append(f"{name}: 最新行情 {bars[-1].trade_date}，落后目标 {end}")
                if not best or bars[-1].trade_date > best[-1].trade_date:
                    best, selected = bars, name
            else:
                messages.append(f"{name}: 来源无该股票数据")
        except Exception as exc:
            details.append({
                "source": name, "elapsedMs": round((time.perf_counter() - started) * 1000),
                "outcome": "failed", "latestDate": None, "error": str(exc),
            })
            messages.append(f"{name}: {exc}")
            errors.append((name, str(exc)))
            failed = True
    return DailyResult(best, selected, "；".join(messages) or "没有支持此股票的日线来源",
                       failed, tuple(attempted), tuple(errors), tuple(details))
