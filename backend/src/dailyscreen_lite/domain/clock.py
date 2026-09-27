"""北京时间时钟。

导入日期在服务端接收提交时固定，使用北京时间；来源自带的日期不参与。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

BEIJING = ZoneInfo("Asia/Shanghai")


class Clock(Protocol):
    """可注入时钟，便于测试固定跨午夜与 16:30 等时点。"""

    def now(self) -> datetime:
        """返回带北京时区的当前时间。"""

    def today(self) -> date:
        """返回北京时间当前日期。"""


class BeijingClock:
    """生产时钟：真实北京时间。"""

    def now(self) -> datetime:
        return datetime.now(BEIJING)

    def today(self) -> date:
        return self.now().date()


class FixedClock:
    """测试时钟：固定到某个北京时间瞬间。"""

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=BEIJING)
        self._instant = instant.astimezone(BEIJING)

    def now(self) -> datetime:
        return self._instant

    def today(self) -> date:
        return self._instant.date()

    def set(self, instant: datetime) -> None:
        """测试用：把固定时刻移到新的北京时间瞬间（跨日、发布延迟等场景）。"""
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=BEIJING)
        self._instant = instant.astimezone(BEIJING)
