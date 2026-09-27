"""问财链接解析与查询身份。

只支持已确认的桌面端结果链接：域名属于 iwencai.com 且携带可解码的问句。
原链接与解码后的查询都保留，不擅自改写条件。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

from dailyscreen_lite.domain.errors import UnsupportedLink

_QUERY_KEYS = ("w", "q", "question")
_ALLOWED_HOST_SUFFIX = "iwencai.com"


@dataclass(frozen=True)
class WencaiLink:
    original_url: str
    query: str
    querytype: str | None
    sign: str | None

    @property
    def query_fingerprint(self) -> str:
        """查询身份：只由原句决定，不同查询不会因集合相同而互相复用确认。"""
        return hashlib.sha256(self.query.encode("utf-8")).hexdigest()


def decode_link(url: str) -> WencaiLink:
    text = (url or "").strip()
    if not text:
        raise UnsupportedLink("链接为空")
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if host != _ALLOWED_HOST_SUFFIX and not host.endswith("." + _ALLOWED_HOST_SUFFIX):
        raise UnsupportedLink(
            "只支持问财（iwencai.com）结果链接",
            detail=f"链接域名：{host or '未知'}",
        )
    params = parse_qs(parsed.query)
    raw = None
    for key in _QUERY_KEYS:
        if params.get(key):
            raw = params[key][0]
            break
    if not raw:
        raise UnsupportedLink(
            "链接中没有可解码的问句（缺少 w/q/question 参数）",
            detail="请提交问财结果页的完整链接",
        )
    query = unquote(raw).strip()
    if not query:
        raise UnsupportedLink("链接中的问句为空")
    return WencaiLink(
        original_url=text,
        query=query,
        querytype=(params.get("querytype") or [None])[0],
        sign=(params.get("sign") or [None])[0],
    )
