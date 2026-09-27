"""问财获取适配器：一次解析固定分页上下文，显式逐页并判定完整性。

关键规则（沿用原型审视结论）：
- 每次全量只解析一次查询，固定该次返回的分页上下文取后续页面，绝不每页重新查询；
- 分页遗漏、重复页、提前空页、数量不符、总数缺失均判定为不完整，不发布；
- 401/403 与明确要求登录是鉴权异常，不能解读为零结果；
- 只有明确成功且零总数、条件结构有效时才确认真实零结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil
from typing import Mapping, Protocol

from dailyscreen_lite.domain.errors import (
    WencaiIncomplete,
    WencaiUnexpected,
)
from dailyscreen_lite.domain.models import ParsedCandidate
from dailyscreen_lite.securities.resolver import normalize_code
from dailyscreen_lite.wencai.condition import condition_fingerprint, condition_labels
from dailyscreen_lite.wencai.url import decode_link

DEFAULT_PERPAGE = 100
MAX_PAGES = 50
_CODE_FIELDS = ("股票代码", "code", "证券代码")

COMPLETENESS_CONSISTENT = "internally_consistent"
COMPLETENESS_ZERO = "confirmed_zero_result"


@dataclass(frozen=True)
class QueryContext:
    """一次查询解析得到的分页上下文；后续页面全部复用它。

    rows 为 None 时表示"逐页向来源请求"（真实旧流程与固定响应实现）；
    为元组时表示来源已在解析阶段一次性取回全部行，page() 只做切片。
    """

    query: str
    condition: object | None
    declared_total: int | None
    page_context: Mapping[str, object]
    comp_id: str | None
    uuid: str | None
    rows: tuple[Mapping[str, object], ...] | None = None


class WencaiProtocol(Protocol):
    """问财获取边界：真实 HTTP 与测试固定响应实现同一协议。"""

    def parse(self, query: str) -> QueryContext:
        """只解析一次查询，返回分页上下文。鉴权/结构异常在此抛出。"""

    def page(self, context: QueryContext, *, page: int, perpage: int) -> list[dict]:
        """取指定页的原始行；返回空列表表示该页无数据。"""


@dataclass(frozen=True)
class AcquisitionResult:
    query: str
    original_url: str
    condition: object | None
    condition_labels: tuple[str, ...]
    condition_fingerprint: str | None
    declared_total: int | None
    candidates: tuple[ParsedCandidate, ...]
    completeness: str
    issues: tuple[str, ...] = ()
    evidence: dict = field(default_factory=dict)


def _extract_codes(rows: list[dict]) -> list[str]:
    codes: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = None
        for key in _CODE_FIELDS:
            if row.get(key) not in (None, ""):
                value = str(row[key]).strip()
                break
        if value:
            codes.append(value)
    return codes


def _fingerprint(codes: list[str]) -> str:
    import hashlib

    return hashlib.sha256("\n".join(sorted(codes)).encode("utf-8")).hexdigest()


def acquire(url: str, session: WencaiProtocol, *, perpage: int = DEFAULT_PERPAGE) -> AcquisitionResult:
    link = decode_link(url)
    context = session.parse(link.query)
    condition = context.condition
    labels = condition_labels(condition)
    cond_fp = condition_fingerprint(condition)

    evidence: dict = {
        "originalUrl": link.original_url,
        "query": link.query,
        "declaredTotal": context.declared_total,
        "conditionLabels": list(labels),
        "conditionFingerprint": cond_fp,
        "pages": [],
        "issues": [],
    }

    if context.declared_total is None:
        raise WencaiUnexpected(
            "问财响应未提供来源总数，无法确认获取完整性，不发布",
        )

    if context.declared_total == 0:
        # 真实零结果要求"明确成功 + 合法空列表 + 可确认条件"，三者同时成立：
        # 声明零总数却带回结果行属自相矛盾，不能确认为零结果。
        if context.rows:
            raise WencaiUnexpected(
                f"问财声明零结果却返回 {len(context.rows)} 行，不承认零结果",
            )
        if condition_fingerprint(condition) is None:
            raise WencaiUnexpected("问财声称零结果但未返回可确认的条件结构，不承认零结果")
        evidence["completeness"] = COMPLETENESS_ZERO
        return AcquisitionResult(
            query=link.query,
            original_url=link.original_url,
            condition=condition,
            condition_labels=labels,
            condition_fingerprint=cond_fp,
            declared_total=0,
            candidates=(),
            completeness=COMPLETENESS_ZERO,
            evidence=evidence,
        )

    if context.rows is None and not context.page_context:
        raise WencaiUnexpected("问财响应未提供可固定的分页上下文，无法逐页获取")

    # 来源已在解析阶段取回全部行（如当前流式接口）：一页即全部，按实际行数分页，
    # 否则会被 MAX_PAGES × perpage 截断，把完整结果误判为不完整。
    page_size = perpage
    if context.rows is not None:
        page_size = max(perpage, len(context.rows))
    expected_pages = ceil(context.declared_total / page_size)
    issues: list[str] = []
    seen_pages: dict[str, int] = {}
    seen_codes: list[str] = []
    actual_page_size: int | None = None
    page_no = 1

    while page_no <= min(expected_pages, MAX_PAGES):
        rows = session.page(context, page=page_no, perpage=page_size)
        codes = _extract_codes(rows)
        fingerprint = _fingerprint(codes) if codes else None
        evidence["pages"].append(
            {"page": page_no, "rows": len(rows), "codes": len(codes), "fingerprint": fingerprint}
        )

        if not codes:
            issues.append(f"第 {page_no} 页返回空列表（提前空页）")
            break
        if fingerprint in seen_pages:
            issues.append(f"第 {page_no} 页与第 {seen_pages[fingerprint]} 页代码集合相同（重复页）")
            break
        if actual_page_size is None:
            actual_page_size = len(codes)
        elif len(codes) != actual_page_size and page_no < expected_pages:
            issues.append(
                f"第 {page_no} 页返回 {len(codes)} 条，与首页 {actual_page_size} 条不一致（非末页）"
            )
        seen_pages[fingerprint] = page_no
        seen_codes.extend(codes)
        page_no += 1

    unique_codes: list[str] = []
    seen_code_set: set[str] = set()
    for code in seen_codes:
        if code not in seen_code_set:
            seen_code_set.add(code)
            unique_codes.append(code)
    if len(seen_codes) != len(unique_codes):
        issues.append(f"跨页重复代码 {len(seen_codes) - len(unique_codes)} 条")
    if len(unique_codes) != context.declared_total:
        issues.append(
            f"唯一证券数 {len(unique_codes)} 与来源总数 {context.declared_total} 不一致"
        )
    if len(evidence["pages"]) < expected_pages:
        issues.append(f"只取得 {len(evidence['pages'])} 页，应有 {expected_pages} 页")

    evidence["uniqueCount"] = len(unique_codes)
    evidence["issues"] = list(issues)

    if issues:
        raise WencaiIncomplete(
            "问财结果不完整或不一致，未发布任何股票",
            detail="；".join(issues),
        )

    evidence["completeness"] = COMPLETENESS_CONSISTENT
    candidates: list[ParsedCandidate] = []
    for code in unique_codes:
        normalized = normalize_code(code)
        candidates.append(
            ParsedCandidate(
                raw_code=code,
                position="问财结果",
                normalized_code=normalized.code if normalized.valid else code,
            )
        )
    return AcquisitionResult(
        query=link.query,
        original_url=link.original_url,
        condition=condition,
        condition_labels=labels,
        condition_fingerprint=cond_fp,
        declared_total=context.declared_total,
        candidates=tuple(candidates),
        completeness=COMPLETENESS_CONSISTENT,
        issues=(),
        evidence=evidence,
    )
