"""CSV/TSV 文本解析。

只用标准库 csv；编码、分隔符与代码列识别走共享表格核心，
输出带原始位置的候选，无需知道证券库或数据库。
"""

from __future__ import annotations

import csv
import io

from dailyscreen_lite.domain.models import ParseOptions, ParseResult
from dailyscreen_lite.imports.parsers.decoding import DecodedText, decode_bytes
from dailyscreen_lite.imports.parsers.tabular import (
    build_result,
    make_table,
    select_code_column,
    strip_source_credit_footer,
)

__all__ = ["DecodedText", "decode_bytes", "detect_delimiter", "parse_csv"]


def detect_delimiter(text: str) -> str:
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return ","


def parse_csv(
    data: bytes,
    *,
    source_name: str = "",
    options: ParseOptions | None = None,
) -> ParseResult:
    """解析 CSV/TSV 内容为候选股票列表。"""
    decoded = decode_bytes(data)
    text = decoded.text
    structure = {"encoding": decoded.encoding, "source_name": source_name}
    if not text.strip():
        return ParseResult(candidates=(), declared_total=0, structure=structure)

    delimiter = detect_delimiter(text)
    structure["delimiter"] = delimiter
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = strip_source_credit_footer([list(row) for row in reader])
    table = make_table(rows)
    if not table.rows:
        # 仅有表头（或全空）时视为真实零结果
        return ParseResult(candidates=(), declared_total=0, structure=structure)

    chosen = options.code_column if options else None
    code_col = select_code_column(table, chosen=chosen)
    return build_result(
        table,
        code_col=code_col,
        structure=structure,
        declared_total=len(table.rows),
    )
