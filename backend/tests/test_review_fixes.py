"""复核修复回归：混合提交日期、损坏 XLSX、明确多代码列、历史日期口径、
XLSX 公式缓存双向、重识别保留汇总。"""

from __future__ import annotations

import io
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.errors import AmbiguousColumns, ParseFailed
from dailyscreen_lite.domain.models import BatchStatus, ParseOptions
from dailyscreen_lite.imports.parsers.text_parser import parse_text
from dailyscreen_lite.imports.parsers.xlsx_parser import parse_xlsx
from dailyscreen_lite.settings import Settings

BEIJING = ZoneInfo("Asia/Shanghai")


def _settings(tmp_path: Path) -> Settings:
    return offline_settings(tmp_path)


class AdvancingClock:
    """每次读取推进固定步长，用于模拟处理耗时跨午夜。"""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self._t = start
        self._step = step

    def now(self) -> datetime:
        value = self._t
        self._t = self._t + self._step
        return value

    def today(self):
        return self.now().date()


def _book(rows: list[list[object]], *, title: str = "Sheet") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --- S1：混合提交共用接收时刻 ---


def test_mixed_submission_keeps_one_import_date_across_midnight(tmp_path):
    settings = _settings(tmp_path)
    # 23:59:58 开始，每次读时钟推进 1 秒：逐来源读时钟会跨到次日
    clock = AdvancingClock(
        datetime(2026, 9, 11, 23, 59, 58, tzinfo=BEIJING), timedelta(seconds=1)
    )
    container = build_container(settings, clock)

    batches = container.imports.submit_sources(
        [("a.csv", make_csv("代码", "000001")), ("b.csv", make_csv("代码", "600519"))],
        [],
    )

    assert [b.import_date.iso for b in batches] == ["2026-09-11", "2026-09-11"]
    items = container.classification.list_candidates()
    # 同一次提交共用同一导入日期：两只股票各有一条 09-11 的入选记录
    assert {d.iso for v in items for d in v.candidate.import_dates} == {"2026-09-11"}


def test_mixed_submission_covers_files_and_multiple_text_blocks(tmp_path):
    settings = _settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)

    batches = container.imports.submit_sources(
        [("a.csv", make_csv("代码", "000001"))],
        ["600519", "300750"],
    )
    assert len(batches) == 3
    assert [b.import_date.iso for b in batches] == ["2026-09-11"] * 3


# --- Spec1：损坏 XLSX 不阻断其他来源 ---


def test_corrupt_xlsx_does_not_500_and_other_source_succeeds(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings, FixedClock(datetime(2026, 9, 11, 9, 0)))
    with TestClient(app) as client:
        response = client.post(
            "/api/imports",
            files=[
                ("files", ("broken.xlsx", b"not a zip", "application/octet-stream")),
                ("files", ("ok.csv", make_csv("代码", "000001"), "text/csv")),
            ],
        )
        assert response.status_code == 200
        batches = response.json()["batches"]
        assert len(batches) == 2
        assert batches[0]["status"] == "rejected"
        assert "损坏" in batches[0]["errorMessage"]
        assert batches[1]["status"] == "published"
        items = client.get("/api/classification/candidates").json()["candidates"]
        assert {i["security"]["code"] for i in items} == {"000001"}


def test_corrupt_xlsx_directly_is_parse_failed_not_exception(tmp_path):
    settings = _settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 9, 0)))
    batch = container.imports.submit_file("broken.xlsx", b"not a zip")
    assert batch.status is BatchStatus.REJECTED
    assert batch.error_code == "parse_failed"


# --- Spec2：多个明确代码列必须选择 ---


def test_two_explicit_code_columns_require_selection():
    with pytest.raises(AmbiguousColumns) as excinfo:
        parse_text("代码\t证券代码\n000001\t600519\n600519\t000001\n")
    assert {o.label for o in excinfo.value.options} == {"代码", "证券代码"}


def test_single_explicit_code_column_still_auto_selected():
    result = parse_text("代码\t名称\n000001\t平安银行\n")
    assert [c.raw_code for c in result.candidates] == ["000001"]


def test_explicit_code_column_choice_resolves_multi_header():
    result = parse_text(
        "代码\t证券代码\n000001\t600519\n", options=ParseOptions(code_column=1)
    )
    assert [c.raw_code for c in result.candidates] == ["600519"]


# --- Spec3：显式历史日期变化触发重新确认 ---


def _wencai_condition(year: str, *, rolling_window: str) -> dict:
    return {
        "uiText": f"{year}年涨幅超过20%",
        "dateType": "range",
        "dateText": f"{year}0101-{year}1231",
        "window": rolling_window,
    }


def test_explicit_historical_year_change_changes_fingerprint():
    from dailyscreen_lite.wencai.condition import condition_fingerprint

    a = condition_fingerprint([_wencai_condition("2020", rolling_window="近5日")])
    b = condition_fingerprint([_wencai_condition("2021", rolling_window="近5日")])
    assert a is not None and a != b


def test_rolling_window_dates_are_ignored():
    from dailyscreen_lite.wencai.condition import condition_fingerprint

    a = condition_fingerprint(
        [{"uiText": "最高价创120日新高[20250101-20260911]", "dateText": "20250101-20260911"}]
    )
    b = condition_fingerprint(
        [{"uiText": "最高价创120日新高[20250102-20260912]", "dateText": "20250102-20260912"}]
    )
    assert a is not None and a == b


# --- Spec7：XLSX 公式缓存双向处理 ---


def test_xlsx_only_formula_sheet_is_not_true_zero():
    with pytest.raises(ParseFailed) as excinfo:
        parse_xlsx(_book([["=1+1"]]))
    assert "公式" in str(excinfo.value)


def test_xlsx_uncached_formula_in_price_column_does_not_block_code_import():
    result = parse_xlsx(_book([["代码", "价格"], ["000001", "=1+1"]]))
    assert [c.raw_code for c in result.candidates] == ["000001"]


def test_xlsx_uncached_formula_in_code_column_still_blocks():
    with pytest.raises(ParseFailed) as excinfo:
        parse_xlsx(_book([["代码"], ["=1+0"]]))
    assert "公式" in str(excinfo.value)


# --- Spec8：重识别保留原批次汇总 ---


def test_reidentify_preserves_original_summary(tmp_path):
    settings = _settings(tmp_path)
    app = create_app(settings, FixedClock(datetime(2026, 9, 11, 9, 0)))
    with TestClient(app) as client:
        batch = client.post(
            "/api/imports/csv",
            files={"file": ("r.csv", make_csv("代码", "000001", "999999"), "text/csv")},
        ).json()
        assert batch["newCandidateCount"] == 1
        assert batch["parsedCount"] == 2
        assert batch["uniqueCount"] == 2

        after = client.post(f"/api/imports/{batch['batchId']}/reidentify").json()
        # 原批次汇总保留：解析条数、去重股票数、首次建项数不因重识别抹零
        assert after["parsedCount"] == 2
        assert after["uniqueCount"] == 2
        assert after["newCandidateCount"] == 1
        assert after["recognizedCount"] == 1
        assert after["skippedCount"] == 0
        assert after["reidentifiedRemoved"] == 1
