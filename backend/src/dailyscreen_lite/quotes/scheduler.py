"""更新调度：北京时间 16:30–22:30 每小时有限补取、白天启动补更，与手动触发复用同一更新流程。

不引入常驻系统服务：应用进程内一个轻量线程按分钟检查是否到达尝试时点。错过更新
时下次启动补到最新，不逐次重放历史任务；已完成当日更新不因重复启动而重复执行。
调度判断写成纯函数，便于用固定时钟确定复现。

16:30 只是本产品尝试更新的时点，不保证供应商此时数据已就绪；界面显示上游实际日期。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

from dailyscreen_lite.domain.clock import BEIJING, Clock
from dailyscreen_lite.domain.models import UpdateKind
from dailyscreen_lite.quotes.update import UpdateBusy, UpdateService

# 正常按分钟检查，忙时短暂避让，仍受北京时间截止约束。
_BUSY_INTERVAL_SECONDS = 5.0
_CHECK_INTERVAL_SECONDS = 60.0


def automatic_slot(now: datetime, *, startup: bool) -> int | None:
    """0 为当日白天启动补更；1–7 为晚间时点。22:30 分钟内允许轮询触发。"""
    local = now.astimezone(BEIJING)
    minutes = local.hour * 60 + local.minute
    if minutes > 22 * 60 + 30:
        return None
    if minutes < 16 * 60 + 30:
        return 0 if startup else None
    return (minutes - (16 * 60 + 30)) // 60 + 1

class UpdateScheduler:
    """应用内轻量调度：只恢复未完成数据，轮次由更新服务持久化。"""

    def __init__(
        self,
        service: UpdateService,
        clock: Clock,
        *,
        enabled: bool = True,
        interval: float = _CHECK_INTERVAL_SECONDS,
    ) -> None:
        self._service = service
        self._clock = clock
        self._enabled = enabled
        self._busy = False
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_done = False

    def tick(self, *, kind: UpdateKind | None = None) -> bool:
        """执行一次调度判断；需要且可运行时启动更新，返回是否触发。

        kind 为 None 时按「首次补更」用 STARTUP，之后用 SCHEDULED。
        """
        self._busy = False
        if not self._enabled:
            return False
        slot = automatic_slot(self._clock.now(), startup=not self._startup_done)
        if slot is None:
            self._startup_done = True
            return False
        resolved = kind or (UpdateKind.STARTUP if not self._startup_done else UpdateKind.SCHEDULED)
        try:
            record = self._service.run(resolved, automatic_slot=slot)
        except UpdateBusy:
            self._busy = True
            # 手动与定时同时触发：不并发执行相同更新
            return False
        self._startup_done = True
        return record is not None

    def start(self) -> None:
        """启动后台线程：先做一次启动补更判断，再按间隔检查每日时点。"""
        if not self._enabled or self._thread is not None:
            return
        self._stop.clear()

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    self.tick()
                except Exception:
                    logging.getLogger(__name__).exception("自动补取异常，稍后重新检查")
                delay = min(self._interval, _BUSY_INTERVAL_SECONDS) if self._busy else self._interval
                if self._stop.wait(delay):
                    break
        self._thread = threading.Thread(target=loop, name="dslite-update-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5)
            self._thread = None


def beijing_now() -> datetime:
    """当前北京时间，供无时钟注入的场景使用。"""
    return datetime.now(BEIJING)
