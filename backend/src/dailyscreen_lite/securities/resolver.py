"""股票代码规范化与权威证券库匹配。

规则要点：
- 代码全程按字符串处理，保留前导零；不做"盲目补零"。
- 支持带市场标识的形式（600000.SH、SH600000、sh.600000）。
- 只接受 6 位数字代码；其余形式不猜测，按未识别处理。
- 代码与市场的组合身份参与匹配：显式市场与库中不一致时不误配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dailyscreen_lite.domain.models import Security

_MARKETS = {"SH", "SZ", "BJ"}
_SUFFIX_RE = re.compile(r"^(?P<code>\d{6})[.\-]?(?P<market>SH|SZ|BJ)$")
_PREFIX_RE = re.compile(r"^(?P<market>SH|SZ|BJ)[.\-]?(?P<code>\d{6})$")
_PURE_RE = re.compile(r"^\d{6}$")


@dataclass(frozen=True)
class NormalizedCode:
    """规范化结果。market 为显式市场标识，未标注时为 None。"""

    code: str
    market: str | None
    valid: bool


def normalize_code(raw: str) -> NormalizedCode:
    text = (raw or "").strip().strip('"').strip("'")
    if not text:
        return NormalizedCode(code="", market=None, valid=False)
    upper = text.upper()
    match = _SUFFIX_RE.match(upper)
    if match:
        return NormalizedCode(code=match.group("code"), market=match.group("market"), valid=True)
    match = _PREFIX_RE.match(upper)
    if match:
        return NormalizedCode(code=match.group("code"), market=match.group("market"), valid=True)
    if _PURE_RE.match(text):
        return NormalizedCode(code=text, market=None, valid=True)
    return NormalizedCode(code=text, market=None, valid=False)


def resolve_normalized(
    normalized_codes: list[NormalizedCode], lookup: dict[str, Security]
) -> list[tuple[str, Security | None]]:
    """按输入顺序返回 (规范化代码, 证券或 None)。

    lookup 为 code -> Security 映射。显式市场与库中市场不一致时视为未识别。
    入参已是规范化结果，避免同一输入被重复解析。
    """
    resolved: list[tuple[str, Security | None]] = []
    for normalized in normalized_codes:
        security: Security | None = None
        if normalized.valid:
            candidate = lookup.get(normalized.code)
            if candidate is not None and (
                normalized.market is None or candidate.exchange == normalized.market
            ):
                security = candidate
        resolved.append((normalized.code, security))
    return resolved
