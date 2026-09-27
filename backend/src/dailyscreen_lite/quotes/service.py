"""日线行情读取：把库中序列整理成界面可用的视图（含缺失语义）。"""

from __future__ import annotations

from dataclasses import dataclass

from dailyscreen_lite.domain.models import DailyBar
from dailyscreen_lite.repository import Database, quotes_repo

# 首屏默认展示约 250 个交易日；可向前浏览更早历史
DEFAULT_LIMIT = 250
ADJUST_QFQ = "qfq"
MISSING_REASON = "行情暂未取得"

# 复权口径的中文标签；页面据此如实标注，不把样本一律称作前复权
ADJUST_LABEL = {
    "qfq": "前复权",
    "hfq": "后复权",
    "raw": "不复权",
    "": "不复权",
}


@dataclass(frozen=True)
class QuoteView:
    """某股票行情视图。available=False 时明确缺失原因，不用零值伪造。"""

    security_id: str
    available: bool
    reason: str | None
    adjust: str
    adjust_label: str
    source: str | None
    latest_date: str | None
    earliest_date: str | None
    total: int
    fetched_at: str | None
    bars: tuple[DailyBar, ...]


class QuoteService:
    def __init__(self, db: Database, *, adjust: str = ADJUST_QFQ) -> None:
        self._db = db
        self._adjust = adjust

    def view(
        self, security_id: str, *, limit: int = DEFAULT_LIMIT, end: str | None = None
    ) -> QuoteView:
        """行情视图；end（含）用于向前浏览更早历史，仍只返回 up to limit 条。"""
        with self._db.read() as conn:
            total = quotes_repo.total_count(conn, security_id, self._adjust)
            series = quotes_repo.series(conn, security_id, self._adjust, limit=limit, end=end)
        if series is None or not series.bars:
            return QuoteView(
                security_id=security_id,
                available=False,
                reason=MISSING_REASON,
                adjust=self._adjust,
                adjust_label=ADJUST_LABEL.get(self._adjust, self._adjust),
                source=None,
                latest_date=None,
                earliest_date=None,
                total=0,
                fetched_at=None,
                bars=(),
            )
        return QuoteView(
            security_id=security_id,
            available=True,
            reason=None,
            adjust=self._adjust,
            adjust_label=ADJUST_LABEL.get(self._adjust, self._adjust),
            source=series.source,
            latest_date=series.bars[-1].trade_date.isoformat(),
            earliest_date=series.bars[0].trade_date.isoformat(),
            total=total,
            fetched_at=series.fetched_at.isoformat(),
            bars=series.bars,
        )

    def latest_date(self, security_id: str) -> str | None:
        with self._db.read() as conn:
            series = quotes_repo.series(conn, security_id, self._adjust, limit=1)
        return series.bars[-1].trade_date.isoformat() if series and series.bars else None

    def snapshots(self, security_ids: list[str]) -> dict[str, quotes_repo.QuoteSnapshot]:
        """批量轻量行情摘要：队列列表展示最新收盘价、行情日与日涨跌幅。"""
        with self._db.read() as conn:
            return quotes_repo.latest_snapshots(conn, security_ids, self._adjust)
