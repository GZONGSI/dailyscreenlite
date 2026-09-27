"""XLSX 解析（openpyxl 只读）。

重点处理 Excel 的"显示格式 vs 原始数值"：单元格类型为数字时用其显示格式
（如 000000）格式化，避免把 000001 读成 1；公式单元格若没有缓存值则明确报错，
不把陈旧或缺失的缓存当作权威代码。多工作表或代码列不唯一时交给用户选择。
"""

from __future__ import annotations

import io
import re
import zipfile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from dailyscreen_lite.domain.errors import AmbiguousSheets, ParseFailed
from dailyscreen_lite.domain.models import ParseOptions, ParseResult, SelectionOption
from dailyscreen_lite.imports.parsers.tabular import (
    build_result,
    make_table,
    select_code_column,
    strip_source_credit_footer,
)

_ZERO_FORMAT = re.compile(r"^0+$")


def _format_numeric(value: float | int, number_format: str | None) -> str:
    """按 Excel 显示格式还原文本，仅处理前导零格式，其余按整数展示。

    只处理"纯 0 占位"格式（如 000000），不去猜测千分位、货币或科学计数；
    不确定的形式保持原样交给后续识别，避免伪造代码。
    """
    fmt = (number_format or "").strip()
    if _ZERO_FORMAT.match(fmt) and float(value).is_integer():
        return str(int(value)).zfill(len(fmt))
    if float(value).is_integer():
        return str(int(value))
    return repr(value)


def _cell_text(cell) -> str:
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _format_numeric(value, cell.number_format)
    return str(value)


def _sheet_rows(sheet) -> list[list[str]]:
    return [[_cell_text(cell) for cell in row] for row in sheet.iter_rows()]


def _read_sheets(stream: io.BytesIO, *, data_only: bool) -> dict[str, list[list[str]]]:
    stream.seek(0)
    workbook = load_workbook(stream, read_only=True, data_only=data_only)
    try:
        return {name: _sheet_rows(workbook[name]) for name in workbook.sheetnames}
    finally:
        workbook.close()


def _formula_columns_without_cache(
    raw_rows: list[list[str]], cached_rows: list[list[str]]
) -> set[int]:
    """返回"公式但无缓存值"的列号集合（0 起）。

    data_only=True 时无缓存公式读到 None，若不显式检查会被当成空单元格静默丢弃。
    按列返回，使调用方只对代码列这类必须可靠读取的列阻断，不因价格等附带列误伤。
    """
    columns: set[int] = set()
    for r_idx, row in enumerate(raw_rows):
        for c_idx, value in enumerate(row):
            if not (isinstance(value, str) and value.startswith("=")):
                continue
            cached_value = (
                cached_rows[r_idx][c_idx]
                if r_idx < len(cached_rows) and c_idx < len(cached_rows[r_idx])
                else None
            )
            if not cached_value:
                columns.add(c_idx)
    return columns


def _first_missing_formula(
    raw_rows: list[list[str]], cached_rows: list[list[str]], columns: set[int]
) -> str | None:
    """在指定列中找第一个无缓存公式单元格坐标，用于错误信息。"""
    for r_idx, row in enumerate(raw_rows, start=1):
        for c_idx, value in enumerate(row, start=1):
            if c_idx - 1 not in columns:
                continue
            if isinstance(value, str) and value.startswith("="):
                col = chr(64 + c_idx) if c_idx <= 26 else str(c_idx)
                return f"{col}{r_idx}"
    return None


def _has_content(rows: list[list[str]]) -> bool:
    return any(any(cell for cell in row) for row in rows)


def parse_xlsx(
    data: bytes,
    *,
    source_name: str = "",
    options: ParseOptions | None = None,
) -> ParseResult:
    try:
        raw_sheets = _read_sheets(io.BytesIO(data), data_only=False)
        cached_sheets = _read_sheets(io.BytesIO(data), data_only=True)
    except (InvalidFileException, zipfile.BadZipFile, KeyError, OSError, ValueError) as exc:
        raise ParseFailed("无法读取 XLSX 文件，请确认文件未损坏") from exc

    names = list(raw_sheets.keys())
    # 是否有内容以原始读取为准：只有公式而无缓存的表不能被当成"空表/真实零结果"
    non_empty = [
        (name, cached_sheets.get(name, []))
        for name, rows in raw_sheets.items()
        if _has_content(rows)
    ]
    structure = {"source_name": source_name, "sheet_count": str(len(names))}
    if not non_empty:
        return ParseResult(candidates=(), declared_total=0, structure=structure)

    selected = options.sheet if options else None
    if selected is None:
        if len(non_empty) > 1:
            raise AmbiguousSheets(
                "工作簿包含多个非空工作表，需要选择",
                kind="sheet",
                options=[SelectionOption(value=name, label=name) for name, _ in non_empty],
                detail=f"非空工作表：{', '.join(name for name, _ in non_empty)}",
            )
        selected = non_empty[0][0]

    if selected not in raw_sheets:
        raise ParseFailed(f"选择的工作表不存在：{selected}")

    selected_raw = raw_sheets.get(selected, [])
    selected_cached = cached_sheets.get(selected, [])
    missing_columns = _formula_columns_without_cache(selected_raw, selected_cached)

    structure["sheet"] = selected
    table = make_table(strip_source_credit_footer(selected_cached))
    if not table.rows:
        # 无读得到的数据行：若原始表确有内容（如整表只有无缓存公式），不能当作零结果
        if _has_content(selected_raw):
            cell = _first_missing_formula(
                selected_raw, selected_cached, missing_columns
            ) or "有公式单元格"
            raise ParseFailed(
                f"工作表「{selected}」内容无法可靠读取（{cell} 为公式且无缓存值），"
                "请改为数值后重新提交（不运行公式）"
            )
        return ParseResult(candidates=(), declared_total=0, structure=structure)

    chosen = options.code_column if options else None
    code_col = select_code_column(table, chosen=chosen)
    # 只有代码列必须可靠读取；价格等附带列缺公式缓存只作最小存档，不阻断导入
    if code_col in missing_columns:
        cell = _first_missing_formula(selected_raw, selected_cached, {code_col})
        raise ParseFailed(
            f"工作表「{selected}」代码列单元格 {cell} 是公式且没有可信缓存值；"
            "请改为数值后重新提交（不运行公式）"
        )
    return build_result(
        table,
        code_col=code_col,
        structure=structure,
        sheet=selected if len(names) > 1 else None,
        declared_total=len(table.rows),
    )
