"""导入服务集成测试：真实临时 SQLite + 真实证券库快照。

覆盖前导零/市场、部分与全部未识别、真实零结果、重复与同日聚合、
导入日期固定、来源附带信息不覆盖自有资料。
"""

from __future__ import annotations

from datetime import datetime
import io

import pytest
from openpyxl import Workbook

from conftest import build_offline, make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import (
    BatchStatus,
    CandidateScope,
    CandidateState,
    StockOutcome,
)


def test_partial_unknown_publishes_valid_and_skips_unknown(container):
    data = make_csv("代码,名称", "000001,平安银行", "999999,不存在", "600519,贵州茅台")
    batch = container.imports.submit_file("candidates.csv", data)

    assert batch.status is BatchStatus.PUBLISHED
    assert batch.parsed_count == 3
    assert batch.recognized_count == 2
    assert batch.skipped_count == 1
    assert batch.new_candidate_count == 2
    # 去重股票数含未识别跳过项，与"识别股票"可区分
    assert batch.unique_count == 3
    outcomes = {s.raw_code: s.outcome for s in batch.stocks}
    assert outcomes["999999"] is StockOutcome.SKIPPED

    items = container.classification.list_candidates()
    assert {v.candidate.security_id for v in items} == {"000001.SZ", "600519.SH"}


def test_xlsx_source_credit_does_not_enter_batch_or_skipped_count(container):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["股票代码", "股票简称"])
    sheet.append(["000001", "平安银行"])
    sheet.append(["数据来源于：i问财网站（iwencai.com）", None])
    stream = io.BytesIO()
    workbook.save(stream)

    batch = container.imports.submit_file("wencai.xlsx", stream.getvalue())

    assert batch.status is BatchStatus.PUBLISHED
    assert batch.declared_total == batch.parsed_count == batch.unique_count == 1
    assert batch.skipped_count == 0
    assert [stock.raw_code for stock in batch.stocks] == ["000001"]


def test_all_unknown_creates_no_item_and_reports_reason(container):
    data = make_csv("代码", "999999", "888888")
    batch = container.imports.submit_file("unknown.csv", data)

    assert batch.status is BatchStatus.ALL_UNKNOWN
    assert batch.error_code == "all_securities_unknown"
    assert batch.error_message == "未导入任何股票：全部未识别"
    assert batch.new_candidate_count == 0
    assert container.classification.list_candidates() == []


def test_empty_source_is_true_zero_result_not_all_unknown(container):
    batch = container.imports.submit_file("empty.csv", b"")
    assert batch.status is BatchStatus.EMPTY
    assert batch.error_code == "empty_source"
    assert batch.declared_total == 0
    # 空来源不能与"全部未识别"混淆
    assert batch.error_code != "all_securities_unknown"

    header_only = container.imports.submit_file("header.csv", "代码,名称\n".encode("utf-8"))
    assert header_only.status is BatchStatus.EMPTY


def test_leading_zero_and_market_identity_preserved(container):
    data = make_csv("代码", "000001", "000001.SZ", "600519.SH")
    batch = container.imports.submit_file("codes.csv", data)
    # 000001 与 000001.SZ 指向同一证券，第二条记为重复
    assert batch.recognized_count == 2
    assert batch.new_candidate_count == 2
    assert {v.candidate.security_id for v in container.classification.list_candidates()} == {
        "000001.SZ",
        "600519.SH",
    }
    duplicate = [s for s in batch.stocks if s.outcome is StockOutcome.DUPLICATE]
    assert duplicate and duplicate[0].normalized_code == "000001"


def test_wrong_market_does_not_match(container):
    data = make_csv("代码", "000001.SH")
    batch = container.imports.submit_file("wrong-market.csv", data)
    # 000001 属于深市，显式标注 SH 时不应误配
    assert batch.status is BatchStatus.ALL_UNKNOWN
    assert batch.skipped_count == 1


def test_import_date_fixed_at_receipt_across_midnight():
    """23:59 接收、次日才完成也归原导入日期。固定时钟保证确定性。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = offline_settings(Path(tmp))
        clock = FixedClock(datetime(2026, 9, 11, 23, 59, 0))
        container = build_offline(settings, clock)
        batch = container.imports.submit_file("c.csv", make_csv("代码", "000001"))
        assert batch.import_date.iso == "2026-09-11"

        later = container.classification.list_candidates()
        assert later[0].candidate.latest_import_date.iso == "2026-09-11"


def test_source_historical_date_and_fake_name_price_do_not_override(container):
    """来源附带的历史日期/假名称/假价格只进批次明细分档，不改变归属或自有资料。"""
    data = make_csv(
        "代码,名称,最新价,日期",
        "000001,假名称,999.99,1999-01-01",
    )
    batch = container.imports.submit_file("fake.csv", data)
    assert batch.import_date.iso == "2026-09-11"

    view = container.classification.list_candidates()[0]
    assert view.security.name == "平安银行"  # 来自权威证券库，未被来源假名称覆盖

    # 来源附带字段作为最小只读证据随批次明细存档，且可从数据库读回
    assert batch.stocks[0].raw_extras == {
        "名称": "假名称",
        "最新价": "999.99",
        "日期": "1999-01-01",
    }
    stored = container.imports.get_batch(batch.batch_id)
    assert stored is not None
    assert stored.stocks[0].raw_extras["最新价"] == "999.99"
    # 工作台卡片不采用来源附带行情
    assert view.candidate.action_result is None


def test_batch_is_readable_after_reopen(container, settings, fixed_clock):
    """批次与明细经真实数据库重启读回后仍一致。"""

    batch = container.imports.submit_file(
        "readback.csv", make_csv("代码,名称", "000001,平安银行", "999999,未知")
    )
    reopened = build_container(settings, fixed_clock)
    stored = reopened.imports.get_batch(batch.batch_id)
    assert stored is not None
    assert stored.recognized_count == 1
    assert stored.skipped_count == 1
    assert stored.unique_count == 2
    assert [s.raw_code for s in stored.stocks] == ["000001", "999999"]
    assert reopened.imports.list_batches()[0].batch_id == batch.batch_id


def test_repeat_submission_same_day_appends_source_without_new_selection(container):
    """同一天重复包含同一股票：只追加来源追溯，不再次触发归类。"""
    data = make_csv("代码", "000001")
    first = container.imports.submit_file("a.csv", data)
    second = container.imports.submit_file("b.csv", data)

    # 当天第二次入选已被每日入选去重吸收：既不是新建也不是合并
    assert first.new_candidate_count == 1 and first.merged_candidate_count == 0
    assert second.new_candidate_count == 0 and second.merged_candidate_count == 0

    candidates = container.classification.list_candidates()
    assert len(candidates) == 1
    view = candidates[0]
    assert {s.batch_id for s in view.sources} == {first.batch_id, second.batch_id}
    # 每日入选只有一条：同日其他来源不消耗第二次入选机会
    assert [s.batch_id for s in view.candidate.selections] == [first.batch_id]
    assert [d.iso for d in view.candidate.import_dates] == ["2026-09-11"]


def test_new_source_same_day_does_not_reset_processed_state(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    candidate_id = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.dismiss(candidate_id)
    assert container.classification.list_candidates() == []

    container.imports.submit_file("b.csv", make_csv("代码", "000001"))
    processed = container.classification.list_candidates(scope=CandidateScope.PROCESSED)
    assert [v.candidate.candidate_id for v in processed] == [candidate_id]
    assert processed[0].candidate.state is CandidateState.DISMISSED
    # 同日来源仍要留下追溯
    assert len(processed[0].sources) == 2


def test_next_day_selection_merges_into_unprocessed_candidate():
    """跨日入选且一直未处理：合并到同一候选项，保留各日入选日期。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = offline_settings(Path(tmp))
        day1 = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
        c1 = build_offline(settings, day1)
        first = c1.imports.submit_file("a.csv", make_csv("代码", "000001"))

        day2 = FixedClock(datetime(2026, 9, 12, 10, 0, 0))
        c2 = build_offline(settings, day2)
        second = c2.imports.submit_file("b.csv", make_csv("代码", "000001"))

        candidates = c2.classification.list_candidates()
        assert len(candidates) == 1
        assert [d.iso for d in candidates[0].candidate.import_dates] == [
            "2026-09-11",
            "2026-09-12",
        ]
        # 未处理期间合并计入「合并」而不是新建，且两次来源都能读回
        assert second.new_candidate_count == 0
        assert second.merged_candidate_count == 1
        assert {s.batch_id for s in candidates[0].sources} == {
            first.batch_id,
            second.batch_id,
        }


def test_processed_candidate_reopens_on_next_day_selection():
    """处理完成后另一个导入日期的首次有效入选重新待归类，保留历史记录。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = offline_settings(Path(tmp))
        day1 = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
        c1 = build_offline(settings, day1)
        c1.imports.submit_file("a.csv", make_csv("代码", "000001"))
        candidate_id = c1.classification.list_candidates()[0].candidate.candidate_id
        c1.classification.dismiss(candidate_id)

        day2 = FixedClock(datetime(2026, 9, 12, 10, 0, 0))
        c2 = build_offline(settings, day2)
        batch = c2.imports.submit_file("b.csv", make_csv("代码", "000001"))

        assert batch.reopened_candidate_count == 1
        assert batch.new_candidate_count == 0
        reopened = c2.classification.list_candidates()
        assert [v.candidate.candidate_id for v in reopened] == [candidate_id]
        assert reopened[0].candidate.state is CandidateState.PENDING
        # 历次处理记录保留：暂不关注这次记录仍在卡片历史里
        assert [entry.action for entry in reopened[0].candidate.history][:1] == ["reopened"]
        assert any(entry.action == "dismissed" for entry in reopened[0].candidate.history)


def test_selection_published_after_processing_reopens_candidate():
    """先提交、处理后才成功发布的新入选：按成功发布时间重新进入待归类。

    未发布（待选择/待确认）批次不提前占用当天的入选机会，也不得绕过去重。
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = offline_settings(Path(tmp))
        clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
        app = build_offline(settings, clock)
        app.imports.submit_file("a.csv", make_csv("代码", "000001"))
        candidate_id = app.classification.list_candidates()[0].candidate.candidate_id

        # 次日先提交一个有歧义的来源：未发布，因此还没有 09-12 的入选记录
        clock.set(datetime(2026, 9, 12, 9, 0, 0))
        held = app.imports.submit_file(
            "ambiguous.csv", make_csv("代码,证券代码", "000001,600519")
        )
        assert held.status is BatchStatus.AWAITING_SELECTION

        # 用户在确认之前先处理掉这只股票
        clock.set(datetime(2026, 9, 12, 9, 30, 0))
        app.classification.dismiss(candidate_id)

        # 之后才确认列并发布：导入日期仍是 09-12，但发布时间在处理之后
        clock.set(datetime(2026, 9, 12, 10, 0, 0))
        published = app.imports.resolve_selection(held.batch_id, code_column=0)
        assert published.status is BatchStatus.PUBLISHED
        assert published.reopened_candidate_count == 1
        assert published.new_candidate_count == 0

        pending = app.classification.list_candidates()
        assert [v.candidate.candidate_id for v in pending] == [candidate_id]
        assert pending[0].candidate.state is CandidateState.PENDING
        assert [d.iso for d in pending[0].candidate.import_dates] == [
            "2026-09-11",
            "2026-09-12",
        ]


def test_late_same_day_publish_does_not_reopen_after_processing():
    """同一天已入选过：稍后发布的其他来源只追加来源，不绕过去重重新归类。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        settings = offline_settings(Path(tmp))
        clock = FixedClock(datetime(2026, 9, 11, 9, 0, 0))
        app = build_offline(settings, clock)
        app.imports.submit_file("a.csv", make_csv("代码", "000001"))
        candidate_id = app.classification.list_candidates()[0].candidate.candidate_id

        clock.set(datetime(2026, 9, 11, 9, 30, 0))
        held = app.imports.submit_file(
            "ambiguous.csv", make_csv("代码,证券代码", "000001,600519", "600519,000001")
        )
        clock.set(datetime(2026, 9, 11, 10, 0, 0))
        app.classification.dismiss(candidate_id)

        clock.set(datetime(2026, 9, 11, 15, 0, 0))
        published = app.imports.resolve_selection(held.batch_id, code_column=0)
        assert published.reopened_candidate_count == 0
        assert published.merged_candidate_count == 0
        # 已入选的 000001 只追加来源；600519 是当天的首次入选，正常进入待归类
        assert published.new_candidate_count == 1

        processed = app.classification.list_candidates(scope=CandidateScope.PROCESSED)
        assert [v.candidate.candidate_id for v in processed] == [candidate_id]
        assert len(processed[0].sources) == 2
        assert {v.candidate.security_id for v in app.classification.list_candidates()} == {
            "600519.SH"
        }


def test_dismiss_keeps_import_facts_and_other_sources(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    items = {v.candidate.security_id: v.candidate.candidate_id for v in container.classification.list_candidates()}
    container.classification.dismiss(items["000001.SZ"])

    processed = container.classification.list_candidates(scope=CandidateScope.PROCESSED)
    assert len(processed) == 1
    # 不删除导入事实：批次仍在
    from dailyscreen_lite.repository import imports_repo

    with container.db.read() as conn:
        assert imports_repo.list_batches(conn)


def test_rejected_parse_leaves_no_partial_publish(container):
    from dailyscreen_lite.domain.errors import ParseFailed

    data = make_csv("name,amount", "alpha,11.50", "beta,13.20")
    batch = container.imports.submit_file("bad.csv", data)
    assert batch.status is BatchStatus.REJECTED
    assert container.classification.list_candidates() == []
