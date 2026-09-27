"""工单 01：候选归类队列、当前股票恢复、稍后/暂不关注/清理与主动重新归类。

真实临时 SQLite + 真实证券库快照 + 固定时钟；从服务与 HTTP 边界验证
队列顺序、浏览与处理状态分离、跨重启的归类上下文读回。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import CandidateScope, CandidateState
from dailyscreen_lite.classification.service import (
    ClassificationActionNotAllowed,
    ClassificationUnavailable,
)
from dailyscreen_lite.settings import Settings


def ids(container, **kwargs) -> list[str]:
    return [v.candidate.security_id for v in container.classification.list_candidates(**kwargs)]


def candidate_ids(container, **kwargs) -> list[str]:
    return [v.candidate.candidate_id for v in container.classification.list_candidates(**kwargs)]


def make_settings(tmp_path: Path) -> Settings:
    return offline_settings(tmp_path)


# --- 队列顺序 ---


def test_queue_is_fifo_then_security_tiebreak(container):
    # 同批同接收时间：按证券身份稳定排序，而不是来源出现顺序
    container.imports.submit_file("a.csv", make_csv("代码", "600519", "000001", "300750"))
    assert ids(container) == ["000001.SZ", "300750.SZ", "600519.SH"]

    # 后续提交排到队尾，不插队
    container.imports.submit_file("b.csv", make_csv("代码", "000002"))
    assert ids(container) == ["000001.SZ", "300750.SZ", "600519.SH", "000002.SZ"]


def test_new_source_does_not_change_order_or_steal_current(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.update_view({"currentCandidateId": head})

    container.imports.submit_file("b.csv", make_csv("代码", "000001", "000002"))

    assert ids(container) == ["000001.SZ", "600519.SH", "000002.SZ"]
    # 新增来源不夺取当前选择
    assert container.classification.get_view().state.current_candidate_id == head


# --- 浏览与处理状态分离 ---


def test_viewing_marks_viewed_without_completing(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    head = container.classification.list_candidates()[0].candidate.candidate_id

    view = container.classification.update_view({"currentCandidateId": head})

    assert view.current is not None
    assert view.current.candidate.viewed_at is not None
    assert view.current.candidate.state is CandidateState.PENDING
    assert view.round["viewed"] == 1
    assert view.round["remaining"] == 2


def test_later_moves_to_tail_and_stays_current(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519", "300750"))
    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.update_view({"currentCandidateId": head})

    container.classification.later(head)

    items = container.classification.list_candidates()
    assert [v.candidate.security_id for v in items] == ["300750.SZ", "600519.SH", "000001.SZ"]
    assert items[-1].candidate.state is CandidateState.LATER
    # 稍后处理仍在未处理池，且动作后不改变当前项
    assert container.classification.get_view().state.current_candidate_id == head


def test_dismiss_stays_current_and_leaves_pool(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.update_view({"currentCandidateId": head})

    container.classification.dismiss(head)

    assert container.classification.get_view().state.current_candidate_id == head
    assert ids(container) == ["600519.SH"]
    processed = container.classification.list_candidates(scope=CandidateScope.PROCESSED)
    assert [v.candidate.security_id for v in processed] == ["000001.SZ"]
    assert processed[0].candidate.state is CandidateState.DISMISSED


def test_reclassify_reuses_candidate_and_moves_to_front(container):
    """主动重新归类：复用同一候选项、保留历史，并调到队首。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519", "300750"))
    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.dismiss(head)

    container.classification.reclassify(head)

    items = container.classification.list_candidates()
    assert [v.candidate.security_id for v in items] == [
        "000001.SZ",
        "300750.SZ",
        "600519.SH",
    ]
    assert items[0].candidate.state is CandidateState.PENDING
    # 复用同一对象、保留历次处理记录，不创建第二个候选项
    assert items[0].candidate.candidate_id == head
    assert any(e.action == "dismissed" for e in items[0].candidate.history)
    assert len(container.classification.list_candidates(scope=CandidateScope.PROCESSED)) == 0


def test_reclassify_unprocessed_candidate_keeps_single_object(container):
    """未处理对象直接复用并调到队首，不重复创建。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    tail = container.classification.list_candidates()[1].candidate.candidate_id

    container.classification.reclassify(tail)

    items = container.classification.list_candidates()
    assert [v.candidate.candidate_id for v in items] == [
        tail,
        container.classification.list_candidates()[1].candidate.candidate_id,
    ]
    assert len(items) == 2


def test_actions_reject_wrong_state(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    candidate_id = container.classification.list_candidates()[0].candidate.candidate_id

    container.classification.dismiss(candidate_id)
    with pytest.raises(ClassificationActionNotAllowed):
        container.classification.later(candidate_id)  # 已处理项不能稍后处理
    with pytest.raises(ClassificationUnavailable):
        container.classification.focus("999999.SZ")  # 不存在的候选项
    # 已处理的候选项可以打开查看（已处理页签与历史回看共用），只是不进入待归类
    opened = container.classification.focus(candidate_id)
    assert opened.current.candidate.state is CandidateState.DISMISSED
    assert container.classification.list_candidates() == []


# --- 主动清理 ---


def test_cleanup_clears_pool_but_keeps_import_facts(container):
    batch = container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))

    cleared = container.classification.cleanup()

    assert cleared == 2
    assert container.classification.list_candidates() == []
    processed = container.classification.list_candidates(scope=CandidateScope.PROCESSED)
    assert {v.candidate.state for v in processed} == {CandidateState.CLEARED}
    # 不删除导入事实：批次与来源关联仍在
    assert container.imports.get_batch(batch.batch_id) is not None
    assert all(v.sources for v in processed)


def test_cleanup_scoped_to_import_date(tmp_path):
    settings = make_settings(tmp_path)
    day1 = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    day1.imports.submit_file("a.csv", make_csv("代码", "000001"))
    day2 = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    day2.imports.submit_file("b.csv", make_csv("代码", "600519"))

    assert day2.classification.cleanup(import_date="2026-09-11") == 1
    assert ids(day2, import_date="2026-09-12") == ["600519.SH"]
    assert day2.classification.list_candidates(import_date="2026-09-11") == []


# --- 筛选与查找 ---


def test_search_and_result_filters(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519", "300750"))

    assert ids(container, search="平安") == ["000001.SZ"]
    assert ids(container, search="600519") == ["600519.SH"]
    assert ids(container, search=".SZ") == ["000001.SZ", "300750.SZ"]

    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.dismiss(head)
    assert ids(container, scope=CandidateScope.PROCESSED, search="平安") == ["000001.SZ"]
    assert ids(container, scope=CandidateScope.PROCESSED, search="茅台") == []


def test_result_filter_does_not_leak_across_scopes(container):
    """结果筛选只在所属范围内生效：已处理的"暂不关注"不能出现在未处理池。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.dismiss(head)

    # 未处理范围 + 暂不关注结果：已离池，应为空而不是把已处理项拉回来
    assert ids(container, scope=CandidateScope.UNPROCESSED, result=CandidateState.DISMISSED) == []
    # 已处理范围 + 暂不关注结果：命中
    assert ids(container, scope=CandidateScope.PROCESSED, result=CandidateState.DISMISSED) == [
        "000001.SZ"
    ]


def test_processed_lookup_by_code_name_and_date(tmp_path):
    """已处理项可按股票代码/名称、导入日期与结果查找并恢复（A16）。"""
    settings = make_settings(tmp_path)
    day1 = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    day1.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    day2 = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    day2.imports.submit_file("b.csv", make_csv("代码", "300750"))

    # 处理 9-11 的平安银行
    head = next(
        v.candidate.candidate_id
        for v in day2.classification.list_candidates(import_date="2026-09-11")
        if v.candidate.security_id == "000001.SZ"
    )
    day2.classification.dismiss(head)

    # 按名称查找已处理项
    assert ids(day2, scope=CandidateScope.PROCESSED, search="平安") == ["000001.SZ"]
    # 按代码查找
    assert ids(day2, scope=CandidateScope.PROCESSED, search="000001.SZ") == ["000001.SZ"]
    # 按导入日期过滤：9-11 命中，9-12 无已处理项
    assert ids(day2, scope=CandidateScope.PROCESSED, import_date="2026-09-11") == ["000001.SZ"]
    assert ids(day2, scope=CandidateScope.PROCESSED, import_date="2026-09-12") == []
    # 按结果过滤
    assert ids(day2, scope=CandidateScope.PROCESSED, result=CandidateState.DISMISSED) == [
        "000001.SZ"
    ]
    # 主动重新归类后回到待归类队列（并发起新的一次归类）
    day2.classification.reclassify(head)
    assert ids(day2, scope=CandidateScope.UNPROCESSED, search="平安") == ["000001.SZ"]


def test_date_filter_cross_week_items_are_retained(tmp_path):
    """跨周未处理项持续保留，不按自然周清空。"""
    settings = make_settings(tmp_path)
    week1 = build_container(settings, FixedClock(datetime(2026, 9, 7, 10, 0, 0)))
    week1.imports.submit_file("a.csv", make_csv("代码", "000001"))
    week2 = build_container(settings, FixedClock(datetime(2026, 9, 21, 10, 0, 0)))
    week2.imports.submit_file("b.csv", make_csv("代码", "600519"))

    assert week2.classification.dates() == ["2026-09-21", "2026-09-07"]
    assert ids(week2) == ["000001.SZ", "600519.SH"]
    assert ids(week2, import_date="2026-09-07") == ["000001.SZ"]


# --- 工作台上下文持久化与收尾统计 ---


def test_workbench_context_persists_across_restart(tmp_path):
    settings = make_settings(tmp_path)
    first = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    first.imports.submit_file("a.csv", make_csv("代码", "000001"))
    candidate_id = first.classification.list_candidates()[0].candidate.candidate_id
    first.classification.update_view(
        {
            "currentCandidateId": candidate_id,
            "viewMode": "list",
            "scope": "processed",
            "importDate": "2026-09-11",
            "search": "000001",
        }
    )

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 11, 11, 0, 0)))
    view = reopened.classification.get_view()

    assert view.state.current_candidate_id == candidate_id
    assert view.current is not None and view.current.candidate.candidate_id == candidate_id
    assert view.state.view_mode == "list"
    # 显示页签跟随当前股票真实状态，原筛选范围另行保留。
    assert view.state.scope is CandidateScope.UNPROCESSED
    assert view.state.filter_scope is CandidateScope.PROCESSED
    assert view.state.import_date == "2026-09-11"
    assert view.state.search == "000001"


def test_round_stats_count_viewed_and_processed(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519", "300750"))
    order = candidate_ids(container)
    container.classification.update_view({"currentCandidateId": order[0]})
    container.classification.update_view({"currentCandidateId": order[1]})
    container.classification.dismiss(order[0])

    view = container.classification.get_view()

    assert view.round["viewed"] == 2
    assert view.round["processed"] == 1
    assert view.round["remaining"] == 2


def test_return_to_queue_resets_context(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    head = container.classification.list_candidates()[0].candidate.candidate_id
    container.classification.update_view({"currentCandidateId": head})

    view = container.classification.return_to_queue()

    # 返回队列结束本轮：路径与本轮统计一起重置，再从当前范围重新落位
    assert view.state.round_started_at is not None
    assert view.state.path == (head,)
    assert view.round["viewed"] == 1
    assert view.round["remaining"] == 1


def test_invalid_workbench_values_are_rejected(container):
    with pytest.raises(ValueError):
        container.classification.update_view({"viewMode": "gallery"})
    with pytest.raises(ValueError):
        container.classification.update_view({"scope": "everything"})


# --- HTTP 边界 ---


def test_classification_endpoints_work_through_http(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001", "600519"), "text/csv")},
        )
        items = client.get("/api/classification/candidates").json()["candidates"]
        assert [i["securityId"] for i in items] == ["000001.SZ", "600519.SH"]

        head = items[0]["candidateId"]
        view = client.put("/api/classification/view", json={"currentCandidateId": head}).json()
        assert view["currentCandidateId"] == head
        assert view["currentCandidate"]["viewedAt"] is not None
        assert view["round"]["viewed"] == 1

        later = client.post(f"/api/classification/candidates/{head}/later").json()
        assert later["state"] == "later"
        dismissed = client.post(f"/api/classification/candidates/{head}/dismiss").json()
        assert dismissed["state"] == "dismissed"
        back = client.post(f"/api/classification/candidates/{head}/reclassify").json()
        assert back["state"] == "pending"

        assert client.post("/api/classification/cleanup", json={}).json()["clearedCount"] == 2
        processed = client.get("/api/classification/candidates", params={"scope": "processed"}).json()
        assert {i["state"] for i in processed["candidates"]} == {"cleared"}

        # 返回队列：结束本轮并从当前筛选范围重新落位
        returned = client.post("/api/classification/view/return").json()
        assert returned["rows"] == []
        assert returned["currentCandidateId"] is None
