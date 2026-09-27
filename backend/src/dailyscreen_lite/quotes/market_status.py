"""每日股票状态来源：AKShare 停复牌接口 + 交易日历，与可注入的夹具来源。

获取渠道与业务分离：本模块只把来源返回转成规范记录（停牌区间、交易日历、
覆盖范围声明），不写库、不判完整性。生产用 AKShare，测试与回放用夹具来源，
两者走同一边界，使业务看到相同的数据形状。

来源语义（实源探查见 .scratch/core-modules/evidence/20260920-market-status/）：
- `stock_tfp_em` 返回的是「当前仍未复牌」的清单，date 参数不等于「该日停牌集合」，
  因此判定某日是否全天停牌一律依据停牌区间，而不是「是否出现在该日列表里」。
- 「盘中停牌」当天仍有成交，不等同于全天无交易，不豁免目标交易日日线。
- 探查中未见任何北交所行：北交所声明为未覆盖，其状态记未知，不宣称全市场已验证。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from dailyscreen_lite.domain.models import Suspension, SuspensionKind


class MarketStatusError(RuntimeError):
    """状态来源获取失败。失败不得当作「今天没有停牌」。"""


@dataclass(frozen=True)
class ProviderStatusSnapshot:
    """来源返回的一次状态快照（尚未落库）。"""

    target_trade_date: date
    trade_dates: tuple[date, ...]
    suspensions: tuple[Suspension, ...]
    covered_markets: tuple[str, ...]
    uncovered_markets: tuple[str, ...]
    source: str
    calendar_source: str | None


class MarketStatusSource(Protocol):
    """每日状态来源协议。实现方必须如实声明覆盖范围。"""

    source_id: str

    def snapshot(self, *, for_date: date) -> ProviderStatusSnapshot:
        """返回一次状态快照；失败抛 MarketStatusError。"""


_MARKET_EXCHANGE = (
    ("上交所", "SH"),
    ("深交所", "SZ"),
    ("北交所", "BJ"),
)


def exchange_of_market(market: str) -> str:
    """把来源的「所属市场」文本映射为交易所代码；无法识别返回空串。"""
    for prefix, exchange in _MARKET_EXCHANGE:
        if market.startswith(prefix):
            return exchange
    return ""


def _to_date(raw) -> date | None:
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "nat", "none", "<na>"}:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        try:
            return date(int(digits[0:4]), int(digits[4:6]), int(digits[6:8]))
        except ValueError:
            return None
    return None


def _to_kind(raw) -> SuspensionKind:
    text = str(raw).strip()
    if text == "连续停牌":
        return SuspensionKind.CONTINUOUS
    if text == "盘中停牌":
        return SuspensionKind.INTRADAY
    return SuspensionKind.OTHER


def _norm_code(raw) -> str:
    text = str(raw).strip()
    digits = re.sub(r"\D", "", text)
    return digits.zfill(6) if digits else text


class AkshareMarketStatusSource:
    """AKShare 状态来源：停复牌取东方财富，交易日历取新浪交易日历。"""

    source_id = "akshare.stock_tfp_em"
    calendar_source = "akshare.tool_trade_date_hist_sina"
    # 实源探查只见到沪、深两市；北交所按未覆盖声明，不冒充全市场
    covered_markets = ("SH", "SZ")
    uncovered_markets = ("BJ",)
    _CALENDAR_DAYS = 900

    def __init__(self, *, include_calendar: bool = True):
        self._include_calendar = include_calendar

    def _ak(self):
        from .akshare_worker import bounded_akshare
        return bounded_akshare()

    def trade_dates(self) -> tuple[date, ...]:
        """交易日历；失败返回空元组（由调用方按不可信处理，不当作没有交易日）。"""
        ak = self._ak()
        try:
            frame = ak.tool_trade_date_hist_sina()
        except Exception as exc:  # noqa: BLE001 - 日历失败不阻止停牌获取
            raise MarketStatusError(f"交易日历获取失败：{type(exc).__name__}: {exc}") from exc
        days = {_to_date(value) for value in frame["trade_date"].tolist()}
        return tuple(sorted(day for day in days if day is not None))

    def snapshot(self, *, for_date: date) -> ProviderStatusSnapshot:
        ak = self._ak()
        # 日历失败只作降级：停牌区间仍可用于目标日的停牌判定
        try:
            trade_dates = self.trade_dates() if self._include_calendar else ()
            calendar_source: str | None = self.calendar_source if self._include_calendar else None
        except MarketStatusError:
            trade_dates = ()
            calendar_source = None
        try:
            frame = ak.stock_tfp_em(date=for_date.strftime("%Y%m%d"))
        except Exception as exc:
            raise MarketStatusError(
                f"停复牌状态获取失败：{type(exc).__name__}: {exc}"
            ) from exc
        suspensions: list[Suspension] = []
        if frame is not None and not getattr(frame, "empty", True):
            for _, row in frame.iterrows():
                start = _to_date(row.get("停牌时间"))
                if start is None:
                    # 没有停牌起日就无法判定某日是否停牌，跳过而不是猜测
                    continue
                code = _norm_code(row.get("代码"))
                market = str(row.get("所属市场") or "").strip()
                exchange = exchange_of_market(market)
                suspensions.append(
                    Suspension(
                        security_id=f"{code}.{exchange}" if exchange else code,
                        code=code,
                        name=str(row.get("名称") or "").strip(),
                        kind=_to_kind(row.get("停牌期限")),
                        start_date=start,
                        end_date=_to_date(row.get("停牌截止时间")),
                        expected_resume=_to_date(row.get("预计复牌时间")),
                        market=market,
                        reason=str(row.get("停牌原因") or "").strip() or None,
                    )
                )
        return ProviderStatusSnapshot(
            target_trade_date=for_date,
            trade_dates=trade_dates,
            suspensions=tuple(suspensions),
            covered_markets=self.covered_markets,
            uncovered_markets=self.uncovered_markets,
            source=self.source_id,
            calendar_source=calendar_source,
        )


class FixtureMarketStatusSource:
    """夹具状态来源：确定性复现停牌、覆盖缺失与来源失败。"""

    source_id = "fixture.market_status"

    def __init__(self, path: Path) -> None:
        self._path = path

    def _data(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise MarketStatusError(f"状态夹具不存在：{self._path}") from exc
        except json.JSONDecodeError as exc:
            raise MarketStatusError(f"状态夹具无法解析：{exc}") from exc

    def declared_source(self) -> str:
        return str(self._data().get("source") or self.source_id)

    def snapshot(self, *, for_date: date) -> ProviderStatusSnapshot:
        data = self._data()
        if data.get("error"):
            raise MarketStatusError(str(data["error"]))
        if data.get("calendar_error"):
            trade_dates: tuple[date, ...] = ()
            calendar_source: str | None = None
        else:
            trade_dates = tuple(
                day
                for day in (_to_date(v) for v in data.get("trade_dates") or [])
                if day is not None
            )
            calendar_source = "fixture.trade_dates" if trade_dates else None
        suspensions: list[Suspension] = []
        for row in data.get("suspensions") or []:
            start = _to_date(row.get("start"))
            if start is None:
                continue
            code = _norm_code(row.get("code"))
            exchange = str(row.get("exchange") or exchange_of_market(str(row.get("market") or "")))
            suspensions.append(
                Suspension(
                    security_id=f"{code}.{exchange}" if exchange else code,
                    code=code,
                    name=str(row.get("name") or ""),
                    kind=_to_kind(
                        {
                            "continuous": "连续停牌",
                            "intraday": "盘中停牌",
                        }.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
                    ),
                    start_date=start,
                    end_date=_to_date(row.get("end")),
                    expected_resume=_to_date(row.get("expected_resume")),
                    market=str(row.get("market") or ""),
                    reason=(str(row["reason"]) if row.get("reason") else None),
                )
            )
        return ProviderStatusSnapshot(
            target_trade_date=for_date,
            trade_dates=trade_dates,
            suspensions=tuple(suspensions),
            covered_markets=tuple(data.get("covered_markets") or ("SH", "SZ")),
            uncovered_markets=tuple(data.get("uncovered_markets") or ("BJ",)),
            source=self.declared_source(),
            calendar_source=calendar_source,
        )


class NoMarketStatusSource:
    """不提供状态的来源：用于不关心停牌判定的场景，一律记未知。"""

    source_id = "none"

    def snapshot(self, *, for_date: date) -> ProviderStatusSnapshot:
        raise MarketStatusError("未配置每日状态来源")


def covering_suspension(
    security_id: str, target_date: date, suspensions: tuple[Suspension, ...]
) -> Suspension | None:
    """覆盖目标交易日的连续停牌记录；盘中停牌与其它取值都不豁免。

    只按停牌区间判定，不看行情：空行情与过期行情都不能单独证明停牌。
    """
    for suspension in suspensions:
        if suspension.security_id != security_id:
            continue
        if suspension.kind is SuspensionKind.CONTINUOUS and suspension.covers(target_date):
            return suspension
    return None


def resolve_market_status(
    security_id: str,
    target_date: date,
    suspensions: tuple[Suspension, ...],
    covered_markets: tuple[str, ...],
    *,
    trusted: bool = True,
) -> MarketStatus:
    """按停牌区间与覆盖范围判定某只股票在目标交易日的状态。

    trusted 为 False（没有覆盖目标交易日的新快照，或没有可信交易日历）时一律记未知：
    停牌区间来自旧快照，用它豁免新的目标日等于「用昨天的状态推断今天」，
    缺失行情因此显示「未补齐，状态待确认」，而不是被当成正常停牌。
    """
    from dailyscreen_lite.domain.models import MarketStatus

    if not trusted:
        return MarketStatus.UNKNOWN
    if covering_suspension(security_id, target_date, suspensions) is not None:
        return MarketStatus.SUSPENDED
    exchange = security_id.partition(".")[2]
    if exchange not in covered_markets:
        return MarketStatus.UNKNOWN
    return MarketStatus.TRADING


def build_market_status_source(settings) -> MarketStatusSource:
    """按配置选择状态来源：夹具 / AKShare / 无。"""
    if settings.market_status_fixture is not None:
        return FixtureMarketStatusSource(settings.market_status_fixture)
    if settings.market_status_enabled:
        return AkshareMarketStatusSource(include_calendar=False)
    return NoMarketStatusSource()
