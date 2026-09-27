"""纯文本与粘贴表格解析。

两种入口分得很清楚，不互相混同：
- 纯代码模式：每一行都是一个股票代码，出现非代码行即明确报错，不扫描正文数字；
- 表格模式：使用逗号/分号/制表符/竖线或空白分隔的多列文本，走共享代码列识别。
"""

from __future__ import annotations

import csv
import io

from dailyscreen_lite.domain.errors import ParseFailed
from dailyscreen_lite.domain.models import ParsedCandidate, ParseOptions, ParseResult
from dailyscreen_lite.imports.parsers.csv_parser import detect_delimiter
from dailyscreen_lite.imports.parsers.decoding import decode_bytes
from dailyscreen_lite.imports.parsers.tabular import (
    build_result,
    find_header_column,
    is_code_like,
    make_table,
    select_code_column,
)
from dailyscreen_lite.securities.resolver import normalize_code

_CSV_DELIMITERS = (",", ";", "\t", "|")


def _build_rows(lines: list[str], structure: dict[str, str]) -> list[list[str]]:
    joined = "\n".join(lines)
    if any(d in joined for d in _CSV_DELIMITERS):
        delimiter = detect_delimiter(joined)
        structure["delimiter"] = delimiter
        return [list(r) for r in csv.reader(io.StringIO(joined), delimiter=delimiter)]
    structure["delimiter"] = "whitespace"
    return [ln.split() for ln in lines]


def _parse_pure_codes(lines: list[str], structure: dict[str, str]) -> ParseResult:
    structure["input_mode"] = "pure_codes"
    candidates: list[ParsedCandidate] = []
    for idx, line in enumerate(lines, start=1):
        if not is_code_like(line):
            raise ParseFailed(
                f"第{idx}行不是可识别的股票代码，纯代码文本不扫描正文数字",
                detail=f"第{idx}行：{line[:40]}",
            )
        normalized = normalize_code(line)
        candidates.append(
            ParsedCandidate(
                raw_code=line,
                position=f"第{idx}行",
                normalized_code=normalized.code,
            )
        )
    return ParseResult(
        candidates=tuple(candidates),
        declared_total=len(lines),
        structure=structure,
    )


def parse_text(
    data: bytes | str,
    *,
    source_name: str = "",
    options: ParseOptions | None = None,
) -> ParseResult:
    """解析粘贴或键入的文本块。"""
    if isinstance(data, bytes):
        decoded = decode_bytes(data)
        text, encoding = decoded.text, decoded.encoding
    else:
        text, encoding = data, "text"
    structure = {"encoding": encoding, "source_name": source_name, "input_mode": "table"}
    lines = [ln for ln in (raw.strip() for raw in text.splitlines()) if ln]
    if not lines:
        return ParseResult(candidates=(), declared_total=0, structure=structure)

    rows = _build_rows(lines, structure)
    if all(len(row) == 1 for row in rows):
        # 单列：只有明确标注为代码列的表头才按表格处理，否则走纯代码模式
        if find_header_column(tuple(rows[0])) is None:
            return _parse_pure_codes(lines, structure)

    table = make_table(rows)
    if not table.rows:
        return ParseResult(candidates=(), declared_total=0, structure=structure)
    chosen = options.code_column if options else None
    code_col = select_code_column(table, chosen=chosen)
    return build_result(
        table,
        code_col=code_col,
        structure=structure,
        declared_total=len(table.rows),
    )
