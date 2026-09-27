"""表格解析核心：代码列识别与候选构造。

CSV/TSV、粘贴表格文本与 XLSX 共用同一套业务规则：先判断表头，
再在"列内容中代码占比"足够的列中选代码列；多列同时符合时返回需要选择，
而不是猜一个。价格、序号等即使命中证券库也不因数值形状被当作代码列。
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from dailyscreen_lite.domain.errors import AmbiguousColumns, ParseFailed
from dailyscreen_lite.domain.models import (
    ParsedCandidate,
    ParseResult,
    SelectionOption,
    SelectionPreview,
)
from dailyscreen_lite.securities.resolver import normalize_code

# 表头中明确表示"股票代码"的别名，命中即可直接确定代码列。
# 不收录单独的"股票"：该列名常指向名称列，宁可让代码列按内容比例兜底判断。
CODE_HEADER_ALIASES = {
    "代码",
    "股票代码",
    "证券代码",
    "证券代号",
    "code",
    "symbol",
    "ticker",
    "security code",
    "securitycode",
    "stockcode",
    "stock code",
    "ts_code",
    "tscode",
    "wind代码",
}

MIN_CODE_RATIO = 0.6
PREVIEW_ROWS = 5

# 明确表示"非股票代码"语义的列名。这类列即使内容恰好是 6 位数字并命中证券库，
# 也不能被当作代码列自动导入；只允许用户在歧义选择中显式指定。
NON_CODE_HEADER_ALIASES = {
    "序号",
    "编号",
    "排序",
    "数量",
    "成交额",
    "成交金额",
    "成交额(元)",
    "成交额(万)",
    "成交量",
    "金额",
    "价格",
    "最新价",
    "收盘价",
    "开盘价",
    "最高价",
    "最低价",
    "涨跌幅",
    "涨幅",
    "跌幅",
    "换手率",
    "总市值",
    "流通市值",
    "市盈率",
    "市净率",
    "日期",
    "时间",
    "no",
    "no.",
    "index",
    "amount",
    "price",
    "close",
    "open",
    "high",
    "low",
    "volume",
    "turnover",
    "pe",
    "pb",
    "date",
}


@dataclass(frozen=True)
class Table:
    """规整后的表格：可选表头与数据行（全部按字符串保存）。"""

    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    has_header: bool
    first_data_line: int

    @property
    def width(self) -> int:
        return max((len(r) for r in self.rows), default=len(self.header))

    def column_label(self, col: int) -> str:
        if self.has_header and col < len(self.header) and self.header[col]:
            return self.header[col]
        return f"第{col + 1}列"


def clean_cell(value: object) -> str:
    return ("" if value is None else str(value)).strip().strip('"').strip("'")


_SOURCE_CREDIT = re.compile(r"^数据来源(?:于)?\s*[:：]\s*\S", re.IGNORECASE)


def strip_source_credit_footer(raw_rows: list[list[str]]) -> list[list[str]]:
    """移除文件末尾独占一行的来源声明，不吞掉中间的异常数据行。"""
    end = len(raw_rows)
    while end and not any(clean_cell(cell) for cell in raw_rows[end - 1]):
        end -= 1
    while end:
        cells = [clean_cell(cell) for cell in raw_rows[end - 1] if clean_cell(cell)]
        if len(cells) != 1 or not _SOURCE_CREDIT.match(cells[0]):
            break
        end -= 1
    return raw_rows[:end]


def is_code_like(value: str) -> bool:
    return normalize_code(value).valid


def find_header_columns(row: tuple[str, ...]) -> list[int]:
    """返回所有列名明确表示"股票代码"的列号，保留出现顺序。"""
    normalized = [clean_cell(c).lower() for c in row]
    aliases = {a.replace(" ", "") for a in CODE_HEADER_ALIASES}
    return [idx for idx, cell in enumerate(normalized) if cell.replace(" ", "") in aliases]


def find_header_column(row: tuple[str, ...]) -> int | None:
    columns = find_header_columns(row)
    return columns[0] if columns else None


def row_is_header(row: tuple[str, ...]) -> tuple[bool, int | None]:
    """判断首行是否为表头；返回 (是否表头, 命中别名的列号)。"""
    alias_col = find_header_column(row)
    if alias_col is not None:
        return True, alias_col
    normalized = [clean_cell(c) for c in row]
    has_code = any(is_code_like(c) for c in normalized)
    has_text = any(c and not is_code_like(c) for c in normalized)
    if not has_code and has_text:
        return True, None
    return False, None


def make_table(raw_rows: list[list[str]], *, treat_first_as_header: bool = True) -> Table:
    """把原始行规整为 Table：丢弃全空行，并按需识别表头。"""
    rows = [tuple(clean_cell(c) for c in r) for r in raw_rows if any(clean_cell(c) for c in r)]
    if not rows:
        return Table(header=(), rows=(), has_header=False, first_data_line=1)
    has_header = False
    header: tuple[str, ...] = ()
    if treat_first_as_header:
        has_header, _ = row_is_header(rows[0])
    if has_header:
        header = rows[0]
        data = rows[1:]
        first_line = 2
    else:
        data = rows
        first_line = 1
    return Table(header=header, rows=tuple(data), has_header=has_header, first_data_line=first_line)


def is_non_code_label(label: str) -> bool:
    text = clean_cell(label).lower().replace(" ", "")
    return text in {a.replace(" ", "") for a in NON_CODE_HEADER_ALIASES}


def code_ratios(table: Table) -> list[tuple[int, float, int]]:
    ratios: list[tuple[int, float, int]] = []
    for col in range(table.width):
        total = 0
        codes = 0
        for row in table.rows:
            if col < len(row):
                cell = row[col]
                if cell:
                    total += 1
                    if is_code_like(cell):
                        codes += 1
        ratio = codes / total if total else 0.0
        if codes > 0 and ratio >= MIN_CODE_RATIO:
            ratios.append((col, ratio, codes))
    return ratios


def _column_preview(table: Table, candidates: list[int]) -> SelectionPreview:
    header = table.header if table.has_header else tuple(f"第{i + 1}列" for i in range(table.width))
    return SelectionPreview(
        headers=tuple(header),
        rows=tuple(tuple(r) for r in table.rows[:PREVIEW_ROWS]),
    )


def select_code_column(table: Table, *, chosen: int | None = None) -> int:
    """确定代码列；明确不了时抛出 AmbiguousColumns 或 ParseFailed。

    chosen 为用户在歧义选择中已确认的列号（0 起）；给定时直接采用。
    """
    if chosen is not None:
        if 0 <= chosen < max(table.width, 1):
            return chosen
        raise ParseFailed(f"选择的代码列超出范围：第{chosen + 1}列")

    header_cols = find_header_columns(table.header) if table.has_header else []
    if len(header_cols) > 1:
        # 多个列名都明确是"股票代码"（如「代码」+「证券代码」）：不能擅自取第一个
        options = [
            SelectionOption(
                value=str(col),
                label=table.column_label(col),
                detail="列名明确指示股票代码",
            )
            for col in header_cols
        ]
        raise AmbiguousColumns(
            "存在多个明确的股票代码列，需要选择",
            kind="column",
            options=options,
            preview=_column_preview(table, header_cols),
            detail=f"明确代码列序号：{', '.join(str(c) for c in header_cols)}",
        )
    if header_cols:
        return header_cols[0]

    ratios = code_ratios(table)
    if not ratios:
        if table.width == 1:
            return 0
        raise ParseFailed("无法在来源中确定股票代码列")

    # 列名表示非代码语义（成交额/序号/价格等）时不自动采用，交由用户显式选择
    eligible = [
        (col, ratio, codes)
        for col, ratio, codes in ratios
        if not (table.has_header and is_non_code_label(table.header[col] if col < len(table.header) else ""))
    ]
    if not eligible:
        options = [
            SelectionOption(
                value=str(col),
                label=table.column_label(col),
                detail="列名指示非代码语义；如确为股票代码请手动选择",
            )
            for col, _, _ in ratios
        ]
        raise AmbiguousColumns(
            "列名与内容不一致，需要选择股票代码列",
            kind="column",
            options=options,
            preview=_column_preview(table, [c for c, _, _ in ratios]),
            detail=f"内容形似代码但列名非代码语义：{', '.join(str(c) for c, _, _ in ratios)}",
        )

    if len(eligible) > 1:
        options = [
            SelectionOption(
                value=str(col),
                label=table.column_label(col),
                detail=f"{codes} 个形似代码的值，占比 {ratio:.0%}",
            )
            for col, ratio, codes in eligible
        ]
        raise AmbiguousColumns(
            "存在多个候选股票代码列，需要选择",
            kind="column",
            options=options,
            preview=_column_preview(table, [c for c, _, _ in eligible]),
            detail=f"候选列序号：{', '.join(str(c) for c, _, _ in eligible)}",
        )
    return eligible[0][0]


def build_candidates(
    table: Table, code_col: int, *, sheet: str | None = None
) -> tuple[ParsedCandidate, ...]:
    """按确定好的代码列构造候选，保留原始位置与来源附带字段。"""
    candidates: list[ParsedCandidate] = []
    for offset, row in enumerate(table.rows):
        line = table.first_data_line + offset
        raw_value = row[code_col] if code_col < len(row) else ""
        if not raw_value:
            continue
        extras: dict[str, str] = {}
        if table.has_header:
            for idx, cell in enumerate(row):
                if idx == code_col or idx >= len(table.header):
                    continue
                label = table.header[idx] or f"列{idx + 1}"
                extras[label] = cell
        normalized = normalize_code(raw_value)
        position = f"{sheet}!第{line}行" if sheet else f"第{line}行"
        candidates.append(
            ParsedCandidate(
                raw_code=raw_value,
                position=position,
                normalized_code=normalized.code if normalized.valid else raw_value,
                raw_extras=extras,
            )
        )
    return tuple(candidates)


def build_result(
    table: Table,
    *,
    code_col: int,
    structure: dict[str, str],
    sheet: str | None = None,
    declared_total: int | None = None,
) -> ParseResult:
    structure = dict(structure)
    structure["code_column"] = table.column_label(code_col)
    structure["column_index"] = str(code_col)
    structure["has_header"] = "true" if table.has_header else "false"
    if sheet:
        structure["sheet"] = sheet
    return ParseResult(
        candidates=build_candidates(table, code_col, sheet=sheet),
        declared_total=len(table.rows) if declared_total is None else declared_total,
        structure=structure,
    )
