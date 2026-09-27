"""行情来源接入：AKShare 适配器与可注入的夹具来源。

获取渠道与业务分离：本模块只负责把供应商返回转成规范记录（日期、价格、单位为
手/元），不写库、不做候选项判断。名单使用 AKShare，日线独立装配；测试与回放使用夹具来源，
两者走同一边界，使页面与业务看到相同的数据形状。

来源身份如实记录（A24）：AKShare 取东方财富日线与交易所名单，来源附带
（历史库）数据记为其自身来源，不统称为交易所权威数据。

来源能力在这里显式声明，调用方不再靠 getattr 猜来源支持什么：
- `DailyQuotesSource`：任何来源都有的最小能力（日线、覆盖市场、可选的自带口径与来源名）；
- `MarketScopedSource`：额外支持分市场名单与退市核对，生产与夹具来源实现它，
  只做回放或单点的来源不实现，调用方用 `market_scoped()` 取得或得到 None。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from dailyscreen_lite.domain.clock import BEIJING


class QuoteSourceError(RuntimeError):
    """来源获取失败（网络、协议、结构异常）。失败不得当作真实零结果。"""


@dataclass(frozen=True)
class ProviderSecurity:
    """来源返回的一条证券身份（尚未落库）。"""

    code: str
    exchange: str
    board: str
    name: str
    listing_date: str | None
    is_st: bool

    @property
    def security_id(self) -> str:
        return f"{self.code}.{self.exchange}"


@dataclass(frozen=True)
class ProviderBar:
    """来源返回的一条日线；价格单位为元，成交量单位为手。"""

    trade_date: date
    open: float
    high: float
    low: float
    close: float
    volume_lots: float
    amount_yuan: float | None


class DailyQuotesSource(Protocol):
    """独立日线能力；来源失败与真实空结果必须区分。

    `daily_markets`、`declared_adjust`、`declared_source` 有默认实现，
    只声明"能不能覆盖某市场／自带什么口径"，来源可以不覆盖。
    """

    source_id: str

    def daily_bars(
        self, *, code: str, exchange: str, start: date, end: date, adjust: str
    ) -> list[ProviderBar]:
        """返回某股票区间日线；无数据返回空列表，失败抛 QuoteSourceError。"""

    def declared_adjust(self) -> str | None:
        """来源自带口径（如回放样本）；None 表示用调用方指定的口径。"""
        return None

    def declared_source(self) -> str | None:
        """来源在页面／记录里显示的名字；None 表示用 `source_id`。"""
        return None


#: 来源能取日线的市场；来源可用同名实例属性覆盖（如腾讯只做沪深）。
DEFAULT_DAILY_MARKETS: tuple[str, ...] = ("SH", "SZ", "BJ")


def covered_markets(source: DailyQuotesSource) -> tuple[str, ...]:
    """来源实际能取日线的市场；没声明就按全部 A 股。"""
    declared = getattr(source, "daily_markets", DEFAULT_DAILY_MARKETS)
    return tuple(declared)


def declared_source_of(source: DailyQuotesSource) -> str | None:
    """页面与记录里显示的来源名；没声明就交给调用方回落到 `source_id`。

    协议声明 + 测试替身兼容都集中在这里，调用方不再各自 getattr 探测。
    """
    declared = getattr(source, "declared_source", None)
    return declared() if declared is not None else None


def declared_adjust_of(source: DailyQuotesSource) -> str | None:
    """来源自带口径；没声明就返回 None，表示用调用方指定的口径。"""
    declared = getattr(source, "declared_adjust", None)
    return declared() if declared is not None else None


@runtime_checkable
class MarketListSource(Protocol):
    """能按市场取名单的来源；只有这类来源参与分市场名单更新。"""

    def security_list_for_market(self, exchange: str) -> list[ProviderSecurity] | None:
        """取单市场名单；None 表示该来源不支持分市场（调用方回落到整体名单）。"""


@runtime_checkable
class DelistingVerifier(Protocol):
    """能核对退市的来源；没有它时名单缺项一律保旧。"""

    def confirmed_delistings(self, exchange: str, on_date: date) -> set[str]:
        """返回已确认退市、可安全移出名单的代码；空集表示无法核对。"""


@dataclass(frozen=True)
class MarketScopedSource:
    """分市场名单更新的两个能力：取名单必选，核对退市可选。

    两者分别取得：来源只实现其中一个时，另一个按「不支持分市场／无法核对」处理，
    与逐项探测的语义一致（测试里注入的替身往往只实现其中一个）。
    """

    fetch_market: MarketListSource
    verify_delistings: DelistingVerifier | None = None

    def confirmed_delistings(self, exchange: str, on_date: date) -> set[str]:
        """无核对入口时返回空集：缺项不能被当作退市。"""
        if self.verify_delistings is None:
            return set()
        return self.verify_delistings.confirmed_delistings(exchange, on_date)


def market_scoped(source: object) -> MarketScopedSource | None:
    """来源能否按市场取名单；不能时返回 None（调用方回落到整体名单），不抛异常。

    这里按属性取而不是 isinstance：注入的替身不必显式继承协议，只实现用到的那项能力。
    """
    fetch_market = getattr(source, "security_list_for_market", None)
    if not callable(fetch_market):
        return None
    verify = getattr(source, "confirmed_delistings", None)
    return MarketScopedSource(
        fetch_market=fetch_market,
        verify_delistings=source if callable(verify) else None,
    )


class QuotesSource(DailyQuotesSource, Protocol):
    """既有名单/回放入口；日线选择不再要求来源提供名单。"""

    def security_list(self) -> list[ProviderSecurity]:
        """返回沪深北全部 A 股；失败抛 QuoteSourceError，不返回残缺列表。"""


# --- AKShare（生产） ---

_SH_BOARDS = (("主板A股", "main"), ("科创板", "star_market"))


def _norm_code(raw) -> str:
    text = str(raw).strip()
    digits = re.sub(r"\D", "", text)
    return digits.zfill(6) if digits else text


def _norm_date(raw) -> str | None:
    text = str(raw).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return f"{digits[0:4]}-{digits[4:6]}-{digits[6:8]}"
    return None


def _optional_amount(value) -> float | None:
    import math
    if value is None or str(value).strip().lower() in {"", "nan", "none"}:
        return None
    result = float(value)
    return result if not math.isnan(result) else None


class AkshareQuotesSource:
    """AKShare 适配器：名单取交易所官方接口，日线取东方财富（默认前复权）。

    延迟导入 akshare：导入会拉起 pandas 等重依赖，未使用行情时不拖慢启动与测试。
    """

    source_id = "akshare"

    def __init__(self, *, timeout: float = 30.0, daily_markets=("SH", "SZ", "BJ")) -> None:
        self._timeout = timeout
        self.daily_markets = daily_markets

    def declared_adjust(self) -> str | None:
        # 日线按调用方指定口径获取（生产默认前复权）
        return None

    def _ak(self):
        from .akshare_worker import bounded_akshare
        return bounded_akshare()

    def security_list(self) -> list[ProviderSecurity]:
        return [s for market in ("SH", "SZ", "BJ") for s in self.security_list_for_market(market)]

    def security_list_for_market(self, exchange: str) -> list[ProviderSecurity]:
        ak = self._ak()
        name = {"SH": "stock_info_sh_name_code", "SZ": "stock_info_sz_name_code", "BJ": "stock_info_bj_name_code"}[exchange]
        clear = getattr(getattr(ak, name), "cache_clear", None)
        if clear:
            clear()  # AKShare 默认 lru_cache，否则本机不重启就永远拿旧名单
        securities: list[ProviderSecurity] = []
        try:
            for symbol, board in (_SH_BOARDS if exchange == "SH" else ()):
                frame = ak.stock_info_sh_name_code(symbol=symbol)
                rows = list(frame.iterrows())
                # 单个交易所/板块为空也是获取不完整：不得用残缺名单覆盖完整旧库
                if not rows:
                    raise QuoteSourceError(f"交易所名单为空：上交所 {symbol}")
                for _, row in rows:
                    securities.append(
                        ProviderSecurity(
                            code=_norm_code(row.get("证券代码")),
                            exchange="SH",
                            board=board,
                            name=str(row.get("证券简称") or "").strip(),
                            listing_date=_norm_date(row.get("上市日期")),
                            is_st="ST" in str(row.get("证券简称") or "").upper(),
                        )
                    )
            sz_rows = list(ak.stock_info_sz_name_code(symbol="A股列表").iterrows()) if exchange == "SZ" else []
            if exchange == "SZ" and not sz_rows:
                raise QuoteSourceError("交易所名单为空：深交所 A股列表")
            for _, row in sz_rows:
                board = "chinext" if "创业" in str(row.get("板块")) else "main"
                securities.append(
                    ProviderSecurity(
                        code=_norm_code(row.get("A股代码")),
                        exchange="SZ",
                        board=board,
                        name=str(row.get("A股简称") or "").strip(),
                        listing_date=_norm_date(row.get("A股上市日期")),
                        is_st="ST" in str(row.get("A股简称") or "").upper(),
                    )
                )
            bj_rows = list(ak.stock_info_bj_name_code().iterrows()) if exchange == "BJ" else []
            if exchange == "BJ" and not bj_rows:
                raise QuoteSourceError("交易所名单为空：北交所")
            for _, row in bj_rows:
                securities.append(
                    ProviderSecurity(
                        code=_norm_code(row.get("证券代码")),
                        exchange="BJ",
                        board="beijing",
                        name=str(row.get("证券简称") or "").strip(),
                        listing_date=_norm_date(row.get("上市日期")),
                        is_st="ST" in str(row.get("证券简称") or "").upper(),
                    )
                )
        except QuoteSourceError:
            raise
        except Exception as exc:
            # 任一交易所获取不完整即整体失败，绝不用残缺名单覆盖已有证券库
            raise QuoteSourceError(f"交易所名单获取失败：{type(exc).__name__}: {exc}") from exc
        if not securities:
            raise QuoteSourceError("交易所名单为空，拒绝用空名单覆盖证券库")
        return securities

    def confirmed_delistings(self, exchange: str, on_date: date) -> set[str]:
        """只在名单缩减时核对交易所终止上市资料，不用缺项推断退市。"""
        if exchange == "BJ":
            return set()  # 暂无准入的北交所退市核对路径，继续保旧
        ak = self._ak()
        if exchange == "SH":
            frame = ak.stock_info_sh_delist(symbol="全部")
            code_field, day_field = "公司代码", "暂停上市日期"  # AKShare 将 DELIST_DATE 命名为此字段
        else:
            frame = ak.stock_info_sz_delist(symbol="终止上市公司")
            code_field, day_field = "证券代码", "终止上市日期"
        confirmed = set()
        for _, row in frame.iterrows():
            code, day = _norm_code(row.get(code_field)), _norm_date(row.get(day_field))
            if day and date.fromisoformat(day) <= on_date and len(code) == 6 and code.isdigit():
                confirmed.add(f"{code}.{exchange}")
        return confirmed

    def daily_bars(
        self, *, code: str, exchange: str, start: date, end: date, adjust: str
    ) -> list[ProviderBar]:
        ak = self._ak()
        try:
            frame = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust=adjust,
                timeout=self._timeout,
            )
            if frame is None or getattr(frame, "empty", True):
                return []
            bars: dict[date, ProviderBar] = {}
            # 逐行转换也在同一异常边界内：非数字价格等格式异常必须成为来源失败
            # （记入失败并保留旧数据），不能逃逸成「更新永久进行中」。
            for _, row in frame.iterrows():
                day = _norm_date(row.get("日期"))
                if day is None:
                    raise QuoteSourceError("日线日期缺失")
                bars[date.fromisoformat(day)] = ProviderBar(
                    trade_date=date.fromisoformat(day),
                    open=float(row["开盘"]),
                    high=float(row["最高"]),
                    low=float(row["最低"]),
                    close=float(row["收盘"]),
                    volume_lots=float(row["成交量"]),
                    amount_yuan=_optional_amount(row.get("成交额")),
                )
        except QuoteSourceError:
            raise
        except Exception as exc:
            raise QuoteSourceError(f"{code} 日线获取失败：{type(exc).__name__}: {exc}") from exc
        return [bars[d] for d in sorted(bars)]


def _sina_daily_bars(frame, *, volume_divisor: int) -> list[ProviderBar]:
    if frame is None or getattr(frame, "empty", True):
        return []
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        raise QuoteSourceError("日线字段不完整")
    bars: dict[date, ProviderBar] = {}
    for _, row in frame.iterrows():
        day = _norm_date(row["date"])
        if day is None:
            raise QuoteSourceError("日线日期缺失")
        trade_date = date.fromisoformat(day)
        bars[trade_date] = ProviderBar(
            trade_date=trade_date,
            open=float(row["open"]), high=float(row["high"]),
            low=float(row["low"]), close=float(row["close"]),
            volume_lots=float(row["volume"]) / volume_divisor,
            amount_yuan=_optional_amount(row.get("amount")),
        )
    return [bars[day] for day in sorted(bars)]


class TencentHttpQuotesSource:
    """腾讯前复权日线；复用 HTTP 会话，避免逐股启动 AKShare。"""

    source_id = "tencent"
    daily_markets = ("SH", "SZ")

    def __init__(self, session=None) -> None:
        import requests

        self._session = session or requests.Session()

    def declared_adjust(self) -> str:
        return "qfq"

    def declared_source(self) -> str | None:
        return None  # 页面显示 source_id

    def daily_bars(self, *, code: str, exchange: str, start: date, end: date, adjust: str) -> list[ProviderBar]:
        if exchange not in self.daily_markets or adjust != "qfq":
            raise QuoteSourceError("腾讯日线不支持此市场或复权口径")
        from .tencent_http import fetch_tencent_daily

        try:
            return fetch_tencent_daily(
                self._session, code=code, exchange=exchange, start=start, end=end,
            )
        except QuoteSourceError:
            raise
        except Exception as exc:
            raise QuoteSourceError(f"腾讯 {code} 日线获取失败：{type(exc).__name__}: {exc}") from exc


class AkshareSinaQuotesSource:
    """新浪前复权日线；返回的 volume 是股，需换算为手。"""

    source_id = "sina"
    daily_markets = ("SH", "SZ", "BJ")

    def declared_adjust(self) -> str:
        return "qfq"

    def declared_source(self) -> str | None:
        return None  # 页面显示 source_id

    def daily_bars(self, *, code: str, exchange: str, start: date, end: date, adjust: str) -> list[ProviderBar]:
        if exchange not in self.daily_markets or adjust != "qfq":
            raise QuoteSourceError("新浪日线不支持此市场或复权口径")
        from .akshare_worker import bounded_akshare

        try:
            frame = bounded_akshare().stock_zh_a_daily(
                symbol=f"{exchange.lower()}{code}",
                start_date=start.strftime("%Y%m%d"),
                end_date=end.strftime("%Y%m%d"),
                adjust="qfq",
            )
            return _sina_daily_bars(frame, volume_divisor=100)
        except QuoteSourceError:
            raise
        except Exception as exc:
            raise QuoteSourceError(f"新浪 {code} 日线获取失败：{type(exc).__name__}: {exc}") from exc


# --- 夹具来源（测试与历史样本回放） ---


class FixtureQuotesSource:
    """从 JSON 夹具读取行情，用于确定性测试与授权历史样本回放。

    夹具命令式地描述来源行为（名单、日线、按股票注入失败），使「失败保留旧数据」
    与「来源附带假行情不替代自有行情」等分支可在不访问外网的条件下确定复现。
    """

    source_id = "fixture"

    def __init__(self, path: Path, provider_index: int | None = None) -> None:
        self._path = path
        self._provider_index = provider_index

    def _data(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            return data if self._provider_index is None else data["providers"][self._provider_index]
        except FileNotFoundError as exc:
            raise QuoteSourceError(f"行情夹具不存在：{self._path}") from exc
        except json.JSONDecodeError as exc:
            raise QuoteSourceError(f"行情夹具无法解析：{exc}") from exc

    def declared_adjust(self) -> str | None:
        """夹具可声明自身口径（如历史回放样本为不复权），避免页面误标前复权。"""
        adjust = self._data().get("adjust")
        return str(adjust) if adjust else None

    def declared_source(self) -> str | None:
        source = self._data().get("source")
        return str(source) if source else None

    def security_list_for_market(self, exchange: str) -> list[ProviderSecurity] | None:
        data = self._data()
        if "security_markets" not in data:
            return None  # 兼容既有离线整体快照，生产始终按市场获取
        market = data["security_markets"].get(exchange, {})
        if market.get("error"):
            raise QuoteSourceError(str(market["error"]))
        return [ProviderSecurity(
            code=str(r["code"]), exchange=str(r["exchange"]),
            board=str(r.get("board", "main")), name=str(r.get("name", "")),
            listing_date=r.get("listing_date"), is_st=bool(r.get("is_st", False)),
        ) for r in market.get("securities", [])]

    def confirmed_delistings(self, exchange: str, on_date: date) -> set[str]:
        market = self._data().get("security_markets", {}).get(exchange, {})
        return {f"{r['code']}.{exchange}" for r in market.get("delistings", [])
                if date.fromisoformat(r['date']) <= on_date}

    def security_list(self) -> list[ProviderSecurity]:
        data = self._data()
        if data.get("securities_error"):
            raise QuoteSourceError(str(data["securities_error"]))
        rows = data.get("securities")
        if not rows:
            # 空名单表示「本次不动证券库」，不是把库清空
            return []
        return [
            ProviderSecurity(
                code=_norm_code(r["code"]),
                exchange=str(r["exchange"]),
                board=str(r.get("board") or "main"),
                name=str(r.get("name") or ""),
                listing_date=r.get("listing_date"),
                is_st=bool(r.get("is_st", False)),
            )
            for r in rows
        ]

    def daily_bars(
        self, *, code: str, exchange: str, start: date, end: date, adjust: str
    ) -> list[ProviderBar]:
        data = self._data()
        security_id = f"{code}.{exchange}"
        if security_id in (data.get("fail_bars") or []):
            raise QuoteSourceError(f"{security_id} 日线获取失败（夹具注入）")
        rows = (data.get("bars") or {}).get(security_id) or []
        bars = [
            ProviderBar(
                trade_date=date.fromisoformat(str(r["date"])),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume_lots=float(r["volume_lots"]),
                amount_yuan=float(r["amount_yuan"]) if r.get("amount_yuan") is not None else None,
            )
            for r in rows
        ]
        return [b for b in bars if start <= b.trade_date <= end]


# --- 历史库只读回放（A25） ---


class HistoryDbQuotesSource:
    """从授权的只读历史库抽取日线，作为「来源」接入同一行情边界。

    仅读取，不写入、不迁移。历史库为通达信日线（价格分、成交股数），
    在此按已知口径换算为元与手，与 AKShare 记录一致，并如实记录来源身份。
    """

    source_id = "tdx.hsjday"

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def declared_adjust(self) -> str | None:
        # 历史库为通达信原始日线，非东方财富前复权；如实标注，不冒充前复权
        return "raw"

    def declared_source(self) -> str | None:
        return None  # 页面显示 source_id

    def _connect(self):
        try:
            import duckdb  # noqa: PLC0415 - 仅回放时需要
        except Exception as exc:  # pragma: no cover
            raise QuoteSourceError(f"回放需要 duckdb：{type(exc).__name__}") from exc
        return duckdb.connect(str(self._db_path), read_only=True)

    def security_list(self) -> list[ProviderSecurity]:
        # 历史库不作证券名单来源；名单仍由交付快照或 AKShare 提供
        return []

    def daily_bars(
        self, *, code: str, exchange: str, start: date, end: date, adjust: str
    ) -> list[ProviderBar]:
        security_id = f"{code}.{exchange}"
        try:
            con = self._connect()
        except QuoteSourceError:
            raise
        except Exception as exc:
            raise QuoteSourceError(f"历史库不可读：{type(exc).__name__}") from exc
        try:
            rows = con.execute(
                """
                select trade_date, open, high, low, close, volume_shares, amount_yuan
                from canonical_market_daily
                where security_id = ? and trade_date >= ? and trade_date <= ?
                order by trade_date asc
                """,
                [security_id, start.isoformat(), end.isoformat()],
            ).fetchall()
        except Exception as exc:
            raise QuoteSourceError(f"历史库查询失败：{type(exc).__name__}: {exc}") from exc
        finally:
            con.close()
        bars: list[ProviderBar] = []
        for trade_date, open_, high, low, close, volume_shares, amount_yuan in rows:
            # canonical_market_daily 的价格与成交额已是元（conversion_rules 记录 fen->yuan），
            # 成交量仍是股，换算为手（1 手 = 100 股）。此处不再对价格做换算。
            bars.append(
                ProviderBar(
                    trade_date=date.fromisoformat(str(trade_date)),
                    open=float(open_),
                    high=float(high),
                    low=float(low),
                    close=float(close),
                    volume_lots=round(float(volume_shares) / 100.0, 2),
                    amount_yuan=float(amount_yuan),
                )
            )
        return bars


def build_source(settings) -> QuotesSource:
    """按配置选择行情来源：历史库回放 / 夹具 / AKShare。"""
    if settings.quotes_history_db is not None:
        return HistoryDbQuotesSource(settings.quotes_history_db)
    if settings.quotes_fixture is not None:
        return FixtureQuotesSource(settings.quotes_fixture)
    return AkshareQuotesSource()


def build_daily_sources(settings, source: QuotesSource) -> list[DailyQuotesSource]:
    if settings.quotes_fixture is not None and settings.quotes_history_db is None:
        data = json.loads(settings.quotes_fixture.read_text(encoding="utf-8"))
        if "providers" in data:
            return [FixtureQuotesSource(settings.quotes_fixture, i) for i in range(len(data["providers"]))]
    if settings.quotes_fixture is None and settings.quotes_history_db is None and isinstance(source, AkshareQuotesSource):
        # 沪深优先腾讯、再试新浪；北交所优先新浪，再试东方财富。
        return [TencentHttpQuotesSource(), AkshareSinaQuotesSource(), AkshareQuotesSource(daily_markets=("BJ",))]
    return [source]
