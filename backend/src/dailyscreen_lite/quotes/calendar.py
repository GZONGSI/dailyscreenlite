"""交易日历与目标交易日：行情完整性与更新范围都以此为准。

目标交易日是「最新已收盘交易日」：交易日历含未来交易日，因此当天未到收盘时点
（16:30）时回落到上一交易日；非交易日回落到最近一个已过去交易日，不把周末与
节假日当成「缺少当天日线」。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Iterable

# 本产品尝试更新的时点：北京时间 16:30 之后当天才算已收盘交易日
CLOSE_AT = time(16, 30)

# 目标交易日来源：可信日历、仅工作日回落（不可信）
TARGET_CALENDAR = "calendar"
TARGET_FALLBACK = "weekday_fallback"


def latest_closed_trade_date(
    now: datetime, trade_dates: Iterable[date], close_at: time = CLOSE_AT
) -> date | None:
    """最新已收盘交易日；无可用日历时返回 None（调用方须标注不可信）。"""
    today = now.date()
    best: date | None = None
    for day in trade_dates:
        if day > today:
            continue
        if day == today and now.time() < close_at:
            continue
        if best is None or day > best:
            best = day
    return best


def previous_weekday(now: datetime, close_at: time = CLOSE_AT) -> date:
    """没有可信日历时仅按工作日回落，用于「至少能算出一个日期」。

    调用方必须同时标注日历不可信（calendars 缺失），缺失行情显示「状态待确认」，
    不能据此宣称数据不完整或已完整。
    """
    day = now.date() if now.time() >= close_at else now.date() - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def previous_trade_date(day: date, trade_dates: Iterable[date]) -> date | None:
    """day 之前最近的一个交易日；没有可信日历时返回 None。"""
    earlier = [entry for entry in trade_dates if entry < day]
    return max(earlier) if earlier else None


def resolve_target(now: datetime, trade_dates: Iterable[date], covered_months: set[str] | None = None) -> tuple[date, str, bool]:
    """目标交易日与来源：日历可信时按日历算，否则按工作日回落并标注不可信。

    返回值 (目标交易日, 来源, 日历是否可信)；调用方（完整性判定与更新流程）
    共用同一份规则，避免两处各写一遍导致漂移。
    """
    resolved = latest_closed_trade_date(now, trade_dates)
    if covered_months is not None:
        # 从候选交易日到今天必须连续覆盖，跨月缺口不可推断为休市。
        current = resolved.replace(day=1) if resolved else now.date().replace(day=1)
        while current <= now.date():
            if current.strftime("%Y-%m") not in covered_months:
                return max(previous_weekday(now), resolved or date.min), TARGET_FALLBACK, False
            current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    if resolved is not None:
        return resolved, TARGET_CALENDAR, True
    return previous_weekday(now), TARGET_FALLBACK, False
