"""深交所完整自然月日历；失败不推断休市，缓存与停牌来源独立。"""
from __future__ import annotations

import calendar
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol


class CalendarError(RuntimeError):
    pass


@dataclass(frozen=True)
class CalendarMonth:
    month: str
    trade_dates: tuple[date, ...]
    source: str


class CalendarSource(Protocol):
    source_id: str

    def month(self, year: int, month: int) -> CalendarMonth: ...


def validate_month(payload: dict, year: int, month: int, source: str) -> CalendarMonth:
    expected = {date(year, month, day) for day in range(1, calendar.monthrange(year, month)[1] + 1)}
    try:
        rows = payload["data"]
        if not isinstance(rows, list) or len(rows) != len(expected):
            raise ValueError("未发布或缺日")
        seen, opened = set(), []
        for row in rows:
            day = date.fromisoformat(row["jyrq"])
            flag = row["jybz"]
            if day not in expected or day in seen or type(flag) not in (str, int) or str(flag) not in ("0", "1"):
                raise ValueError("日期重复、错月或未知开市标志")
            seen.add(day)
            if str(flag) == "1":
                opened.append(day)
        return CalendarMonth(f"{year:04}-{month:02}", tuple(sorted(opened)), source)
    except (KeyError, TypeError, ValueError) as exc:
        raise CalendarError(f"{source} 日历不完整：{exc}") from exc


def fetch_month(year: int, month: int) -> dict:
    import requests

    response = requests.get(
        "https://www.szse.cn/api/report/exchange/onepersistenthour/monthList",
        params={"month": f"{year}-{month}"}, timeout=(5, 12),
        headers={"Referer": "https://www.szse.cn/", "User-Agent": "DailyScreen-Lite/1.0"},
    )
    response.raise_for_status()
    return response.json()


class SzseCalendarSource:
    source_id = "szse.monthList"

    def __init__(self, transport=None):
        self._transport = transport or fetch_month

    def month(self, year: int, month: int) -> CalendarMonth:
        try:
            return validate_month(self._transport(year, month), year, month, self.source_id)
        except Exception as exc:
            raise CalendarError(f"深交所日历获取失败：{exc}") from exc


class FixtureCalendarSource:
    source_id = "fixture.szse.monthList"

    def __init__(self, path: Path):
        self._path = path

    def month(self, year: int, month: int) -> CalendarMonth:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            payload = data["calendar_months"][f"{year:04}-{month:02}"]
            return validate_month(payload, year, month, self.source_id)
        except Exception as exc:
            raise CalendarError(f"月日历获取失败：{exc}") from exc


def build_calendar_source(settings) -> CalendarSource | None:
    if settings.market_status_fixture is not None:
        try:
            data = json.loads(settings.market_status_fixture.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return FixtureCalendarSource(settings.market_status_fixture)
        return FixtureCalendarSource(settings.market_status_fixture) if "calendar_months" in data else None
    return SzseCalendarSource() if settings.market_status_enabled else None
