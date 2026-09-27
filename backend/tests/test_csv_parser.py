"""解析器单元测试：前导零、市场标识、表头、编码、歧义与失败分类。"""

from __future__ import annotations

import pytest

from dailyscreen_lite.domain.errors import AmbiguousColumns, DecodeFailed, ParseFailed
from dailyscreen_lite.imports.parsers.csv_parser import decode_bytes, parse_csv
from dailyscreen_lite.securities.resolver import normalize_code


def test_preserves_leading_zero_plain_codes():
    result = parse_csv(b"code\n000001\n600000\n")
    assert [c.normalized_code for c in result.candidates] == ["000001", "600000"]
    assert result.candidates[0].position == "第2行"


def test_accepts_market_suffix_and_prefix():
    result = parse_csv("代码\n000001.SZ\nSH600519\n".encode("utf-8"))
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519"]


def test_header_aliases_select_code_column():
    content = "序号,股票代码,名称,最新价\n1,000001,平安银行,11.5\n2,600519,贵州茅台,1500\n"
    result = parse_csv(content.encode("utf-8"))
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519"]
    assert result.structure["code_column"] == "股票代码"
    assert result.candidates[0].raw_extras["名称"] == "平安银行"


def test_stock_name_column_beside_code_column_is_not_selected():
    """表头"股票"常指名称列；代码列应据内容比例选中，而非按名称列头。"""
    content = "股票,代码\n平安银行,000001\n贵州茅台,600519\n"
    result = parse_csv(content.encode("utf-8"))
    assert result.structure["code_column"] == "代码"
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519"]
    assert result.candidates[0].raw_extras["股票"] == "平安银行"


def test_single_column_titled_stock_with_codes_falls_back_to_content():
    """只有一列且列头为"股票"时，按内容比例兜底识别为代码列。"""
    result = parse_csv("股票\n000001\n600519\n".encode("utf-8"))
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519"]


def test_no_header_bare_code_text():
    result = parse_csv(b"000001\n600519\n300750\n")
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519", "300750"]


def test_price_column_not_treated_as_code_column():
    """价格列即使含有形似代码的数字，也不应成为唯一代码列。"""
    content = "name,amount\nalpha,11.50\nbeta,13.20\ngamma,9.90\n"
    with pytest.raises(ParseFailed):
        parse_csv(content.encode("utf-8"))


def test_ambiguous_two_code_columns_requires_choice():
    content = "a,b\n000001,600000\n000002,600519\n"
    with pytest.raises(AmbiguousColumns):
        parse_csv(content.encode("utf-8"))


def test_gbk_chinese_header_decodes():
    content = "股票代码,名称\n000001,平安银行\n"
    result = parse_csv(content.encode("gbk"))
    assert result.structure["encoding"] == "gbk"
    assert result.candidates[0].raw_extras["名称"] == "平安银行"


def test_utf8_bom_decodes():
    content = "\ufeff代码\n000001\n"
    result = parse_csv(content.encode("utf-8"))
    assert result.structure["encoding"] == "utf-8-sig"
    assert result.candidates[0].normalized_code == "000001"


def test_undecodable_bytes_raise_decode_failed():
    with pytest.raises(DecodeFailed):
        decode_bytes(b"\xff\xfe\x00\x00\xff")


def test_source_total_is_row_count_and_may_exceed_candidates():
    """来源总数为数据行数；代码单元格为空的行计入总数但不产生候选。"""
    content = "代码,名称\n000001,平安银行\n,无代码\n600519,贵州茅台\n"
    result = parse_csv(content.encode("utf-8"))
    assert result.declared_total == 3
    assert [c.normalized_code for c in result.candidates] == ["000001", "600519"]
    # 候选位置按原始行号，跳过的空行不被重编号
    assert result.candidates[1].position == "第4行"


def test_empty_content_is_true_zero_result():
    result = parse_csv(b"")
    assert result.candidates == ()
    assert result.declared_total == 0


def test_semicolon_delimiter_supported():
    content = "代码;名称\n000001;平安银行\n"
    result = parse_csv(content.encode("utf-8"))
    assert result.candidates[0].normalized_code == "000001"


@pytest.mark.parametrize(
    "raw,code,market,valid",
    [
        ("000001", "000001", None, True),
        ("000001.SZ", "000001", "SZ", True),
        ("sz000001", "000001", "SZ", True),
        ("600519.SH", "600519", "SH", True),
        (" 000001 ", "000001", None, True),
        ("abc", "abc", None, False),
        ("12345", "12345", None, False),
        ("", "", None, False),
    ],
)
def test_normalize_code(raw, code, market, valid):
    result = normalize_code(raw)
    assert (result.code, result.market, result.valid) == (code, market, valid)
