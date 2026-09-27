"""问财解析条件的提取与口径指纹。

条件确认依据可取得的条件结构，而不是只比较证券集合：
- 普通滚动窗口（如"近 5 日"、"创 120 日新高"）的解析日期随数据日期推进，
  归一化这些"具体取值"后取指纹，使日常导入不重复确认；
- 显式历史日期（如"2020 年涨幅"）是用户口径的一部分，必须保留具体年份，
  改年份或区间即视为口径变化，重新确认。
"""

from __future__ import annotations

import hashlib
import json
import re

_DATE_TOKEN = re.compile(r"(\d{8}|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{4}\s*年)")
# 解析后附加在条件文本里的窗口标注（如「...创120日新高[20250101-20260911]」）。
# 纯窗口标注表示这是滚动解析结果，不是用户显式指定的历史日期。
_BRACKET = re.compile(r"[\[【][^\]】]*[\]】]")
# 随日期滚动而变化的"具体取值"字段：仅在滚动条件下忽略。
_DATE_VALUE_KEYS = {"dateText", "dateRange", "date_range", "date"}
# 纯粹的计算/来源标记，随时间或重算变化，不代表用户口径变化。
_VOLATILE_KEYS = {"source", "score", "ci"}


def _to_structure(condition: object) -> object:
    if isinstance(condition, str):
        text = condition.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return condition


def _text_has_explicit_date(text: str) -> bool:
    """去掉窗口标注后的文本是否含显式日期，如 2020年 / 2020-01-01。"""
    return bool(_DATE_TOKEN.search(_BRACKET.sub("", text)))


def _has_explicit_date(node: dict) -> bool:
    """节点中语义字段（uiText/question 等）是否含显式日期。

    只看语义字段：dateText 等本就是解析出的日期取值，不能据此把滚动条件误判为固定历史日期。
    """
    return any(
        key not in _DATE_VALUE_KEYS
        and key not in _VOLATILE_KEYS
        and isinstance(value, str)
        and _text_has_explicit_date(value)
        for key, value in node.items()
    )


def _normalize(node: object, *, rolling: bool) -> object:
    if isinstance(node, dict):
        # 节点自身含显式日期时按固定口径处理；否则沿用外层（滚动）判定
        node_rolling = rolling and not _has_explicit_date(node)
        result = {}
        for key, value in node.items():
            if key in _VOLATILE_KEYS:
                continue
            if key in _DATE_VALUE_KEYS and node_rolling:
                continue
            normalized = _normalize(value, rolling=node_rolling)
            if normalized not in (None, "", [], {}):
                result[key] = normalized
        return result
    if isinstance(node, list):
        return [_normalize(item, rolling=rolling) for item in node]
    if isinstance(node, str):
        if rolling and not _text_has_explicit_date(node):
            return _DATE_TOKEN.sub("<date>", node)
        return node
    return node


def condition_fingerprint(condition: object) -> str | None:
    """口径指纹：归一化日期后为空时返回 None（无法确认口径）。

    默认按滚动条件处理（保证普通滚动不重复确认）；一旦发现显式历史日期，
    该分支保留具体取值，使年份/区间变化能被识别为口径变化。
    """
    structure = _to_structure(condition)
    if structure is None:
        return None
    normalized = _normalize(structure, rolling=True)
    if normalized in (None, "", [], {}):
        return None
    payload = json.dumps(normalized, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def condition_labels(condition: object) -> tuple[str, ...]:
    """从条件结构提取人可读的解析条件，供首次确认展示。

    旧结构用 uiText；当前网页接口的条件为 model_sql，其 requirements 用 text。
    两者取其一，保证界面显示的是来源实际解析出的条件。
    """
    structure = _to_structure(condition)
    labels: list[str] = []

    def clean(text: str) -> str:
        # 来源会给条件加"选股票,"前缀；去掉它，只留条件本身
        return text.strip().removeprefix("选股票,").strip()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key in ("uiText", "text"):
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    labels.append(clean(value))
                    break
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(structure)
    # 去重且保持出现顺序
    seen: set[str] = set()
    unique: list[str] = []
    for label in labels:
        if label and label not in seen:
            seen.add(label)
            unique.append(label)
    return tuple(unique)
