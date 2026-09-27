"""工单 01 核心规则回归：搜索跳转、组合归类动作、来源与处理记录、行情摘要。

真实临时 SQLite + 真实证券库快照；从服务与 HTTP 边界验证导入到候选归类的完整流程。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.classification.service import (
    ClassificationActionNotAllowed,
    ClassificationUnavailable,
)
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import CandidateScope, CandidateState, DailyBar
from dailyscreen_lite.observations.service import ObservationInvalid
from dailyscreen_lite.repository import classification_repo, quotes_repo
from dailyscreen_lite.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return offline_settings(tmp_path)


def order(container, **kwargs) -> list[str]:
    return [
        v.candidate.security_id
        for v in container.classification.list_candidates(**kwargs)
    ]


# --- 搜索跳转 ---


def test_search_focus_opens_card_without_reordering_or_changing_filters(container):
    """搜索打开候选卡：只改变浏览位置与历史，不动队列顺序、不清除筛选。

    未处理池顺序只由稍后处理、主动重新归类与再次入选决定；搜索或列表点选暗中
    重排队列会让「先处理其他股票」的意图失效。
    """
    container.imports.submit_file(
        "a.csv", make_csv("代码", "000001", "600519", "300750")
    )
    assert order(container) == ["000001.SZ", "300750.SZ", "600519.SH"]

    container.classification.update_view(
        {"scope": "processed", "importDate": "2026-09-11", "search": "茅", "result": "dismissed"}
    )
    view = container.classification.focus("600519.SH")

    # 队列顺序与筛选条件保持原样
    assert order(container) == ["000001.SZ", "300750.SZ", "600519.SH"]
    assert view.state.import_date == "2026-09-11"
    assert view.state.search == "茅"
    assert view.state.filter_scope is CandidateScope.PROCESSED
    # 目标照常打开：左侧列表跟随它的真实范围，原筛选条件（页签／日期／搜索／结果）
    # 都保持原样；卡片正是当前展示的那一份结果，因此不会有自相矛盾的提示
    assert view.current is not None
    assert view.current.candidate.security_id == "600519.SH"
    assert view.current.candidate.viewed_at is not None
    assert view.state.scope is CandidateScope.UNPROCESSED
    assert view.state.result is CandidateState.DISMISSED
    assert [row.candidate_id for row in view.rows] == ["600519.SH"]
    assert view.in_filter is True


def test_search_returns_only_imported_recognized_securities(container):
    """全局搜索只覆盖导入成功识别的股票，不引入全市场名单。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    assert [v.candidate.security_id for v in container.classification.search("平安")] == [
        "000001.SZ"
    ]
    # 证券库里的其他股票没有被导入，因此搜索不到
    assert container.classification.search("贵州茅台") == []


def test_search_keeps_processed_state_until_explicit_reclassify(container):
    """仅有暂不关注/清理记录的股票：搜索展示处理状态，重新归类才恢复待处理。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    container.classification.dismiss("000001.SZ")

    found = container.classification.search("000001")
    assert [v.candidate.state for v in found] == [CandidateState.DISMISSED]
    # 搜索本身不创建待归类对象
    assert order(container) == []

    container.classification.reclassify("000001.SZ")
    assert order(container) == ["000001.SZ"]
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.PENDING
    )


def test_dismiss_rejects_processed_candidate(container):
    """已处理项不能直接暂不关注：一次点击不得改写已观察/已清理的记录。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    group = container.observations.create_group("组")
    container.observations.observe_candidate("000001.SZ", [group.group_id])

    with pytest.raises(ClassificationActionNotAllowed):
        container.classification.dismiss("000001.SZ")
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.OBSERVED
    )

    # 主动重新归类之后才允许再次归类
    container.classification.reclassify("000001.SZ")
    container.classification.dismiss("000001.SZ")
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.DISMISSED
    )


def test_focus_requires_existing_candidate(container):
    """打开候选卡只要求候选项存在；处理状态由卡片如实展示。

    已处理候选可以打开查看（已处理页签与历史回看共用），是否重新归类仍由用户显式
    动作决定，打开卡片本身不改变处理状态。
    """
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    container.classification.dismiss("000001.SZ")
    with pytest.raises(ClassificationUnavailable):
        container.classification.focus("999999.SZ")

    opened = container.classification.focus("000001.SZ")
    assert opened.current is not None
    assert opened.current.candidate.state is CandidateState.DISMISSED
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.DISMISSED
    )


# --- 组合归类动作与观察关系 ---


def test_observe_keeps_existing_groups_and_adds_selected(container):
    """加入或保留观察：只增加所选关系，保留原有关系。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    old = container.observations.create_group("原有")
    new = container.observations.create_group("新增")
    # 先按归类流程加入原有组，再主动重新归类（已观察股票再次归类）加入新增组
    container.observations.observe_candidate("000001.SZ", [old.group_id])
    container.classification.reclassify("000001.SZ")

    result = container.observations.observe_candidate("000001.SZ", [new.group_id])

    assert set(container.observations.memberships("000001.SZ")) == {
        old.group_id,
        new.group_id,
    }
    assert set(result.group_ids) == {old.group_id, new.group_id}
    assert result.candidate is not None
    assert result.candidate.candidate.state is CandidateState.OBSERVED


def test_remove_from_groups_removes_only_selected_and_dismisses(container):
    """移出观察组：只移除所选关系，同时记为暂不关注。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    keep = container.observations.create_group("保留")
    drop = container.observations.create_group("移出")
    container.observations.observe_candidate("000001.SZ", [keep.group_id, drop.group_id])

    container.observations.remove_candidate_from_groups("000001.SZ", [drop.group_id])

    assert container.observations.memberships("000001.SZ") == (keep.group_id,)
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.DISMISSED
    )


def test_remove_from_groups_needs_a_selection(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    group = container.observations.create_group("组")
    container.observations.observe_candidate("000001.SZ", [group.group_id])

    with pytest.raises(ObservationInvalid):
        container.observations.remove_candidate_from_groups("000001.SZ", [])

    # 取消/失败都不改变状态与关系
    assert container.observations.memberships("000001.SZ") == (group.group_id,)
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.OBSERVED
    )


def test_remove_from_groups_rolls_back_relation_and_state(tmp_path, monkeypatch):
    """关系变更与处理结果同事务：任一步失败整体回滚，不半成功。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    group = container.observations.create_group("组")
    container.observations.observe_candidate("000001.SZ", [group.group_id])

    original = classification_repo.update_state

    def boom(conn, *args, **kwargs):
        original(conn, *args, **kwargs)
        raise RuntimeError("注入的保存失败")

    monkeypatch.setattr(classification_repo, "update_state", boom)

    with pytest.raises(RuntimeError):
        container.observations.remove_candidate_from_groups("000001.SZ", [group.group_id])

    assert container.observations.memberships("000001.SZ") == (group.group_id,)
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.OBSERVED
    )


def test_observation_removal_does_not_touch_candidate_state(container):
    """观察组页面自身的关系调整不联动候选处理状态。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    group = container.observations.create_group("组")
    container.observations.observe_candidate("000001.SZ", [group.group_id])

    container.observations.save_membership("000001.SZ", [])

    assert container.observations.memberships("000001.SZ") == ()
    assert container.classification.get_candidate("000001.SZ").candidate.state is (
        CandidateState.OBSERVED
    )


# --- 来源与处理记录 ---


def test_records_show_every_selection_source_and_action(container):
    """卡片展开的「来源与处理记录」：各次入选日期、全部来源与历次动作。"""
    first = container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    second = container.imports.submit_file("b.csv", make_csv("代码", "000001"))
    container.classification.later("000001.SZ")

    view = container.classification.get_candidate("000001.SZ")

    # 同日两个来源都留痕，但每日入选只有一条
    assert [d.iso for d in view.candidate.import_dates] == ["2026-09-11"]
    assert {s.batch_id for s in view.sources} == {first.batch_id, second.batch_id}
    assert {s.import_date for s in view.sources} == {"2026-09-11"}
    assert [s.batch_id for s in view.candidate.selections] == [first.batch_id]
    actions = [entry.action for entry in view.candidate.history]
    assert actions[0] == "later"
    assert "selected" in actions


def test_history_survives_reopen_and_reclassify(container):
    container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    container.classification.dismiss("000001.SZ")
    container.classification.reclassify("000001.SZ")
    container.classification.later("000001.SZ")

    actions = [
        entry.action
        for entry in container.classification.get_candidate("000001.SZ").candidate.history
    ]
    assert set(actions) >= {"selected", "dismissed", "reclassified", "later"}


# --- 行情摘要（队列列表用） ---


def test_quote_summary_returns_latest_close_and_change(container):
    bars = [
        DailyBar("000001.SZ", date(2026, 9, 10), 10.0, 10.2, 9.9, 10.0, 100.0, 1000.0),
        DailyBar("000001.SZ", date(2026, 9, 11), 10.0, 11.2, 10.0, 11.0, 120.0, 1200.0),
    ]
    with container.db.transaction() as conn:
        quotes_repo.replace_series(
            conn,
            security_id="000001.SZ",
            adjust="qfq",
            source="fixture",
            fetched_at="2026-09-11T16:30:00+08:00",
            bars=bars,
        )

    snapshots = container.quotes.snapshots(["000001.SZ", "600519.SH"])

    assert snapshots["000001.SZ"].trade_date == "2026-09-11"
    assert snapshots["000001.SZ"].close == pytest.approx(11.0)
    assert snapshots["000001.SZ"].change_pct == pytest.approx(10.0)
    # 没有行情的股票不返回条目，不用零值伪造
    assert "600519.SH" not in snapshots


def test_quote_summary_endpoint_through_http(tmp_path):
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings, FixedClock(datetime(2026, 9, 11, 9, 5, 0)))) as client:
        empty = client.get("/api/quotes/summary", params={"securityIds": ""}).json()
        assert empty == {"quotes": []}
        batch = client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        ).json()
        assert batch["newCandidateCount"] == 1
        payload = client.get(
            "/api/quotes/summary", params={"securityIds": "000001.SZ"}
        ).json()
        assert payload["quotes"] == []


# --- 空库与筛选 ---


def test_empty_database_reports_empty_queue_and_no_dates(container):
    assert container.classification.list_candidates() == []
    assert container.classification.dates() == []
    assert container.classification.summary()["unprocessed"] == 0


def test_cleanup_writes_history_so_it_stays_traceable(container):
    """主动清理也是用户动作：清理 → 重新归类之后仍能追溯那次清理。"""
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    container.classification.cleanup()

    for candidate_id in ("000001.SZ", "600519.SH"):
        history = container.classification.get_candidate(candidate_id).candidate.history
        assert history[0].action == "cleared"
        assert history[0].from_state is CandidateState.PENDING
        assert history[0].to_state is CandidateState.CLEARED

    container.classification.reclassify("000001.SZ")
    actions = [
        entry.action
        for entry in container.classification.get_candidate("000001.SZ").candidate.history
    ]
    assert actions == ["reclassified", "cleared", "selected"]


def test_cleanup_scoped_by_import_date_writes_history(tmp_path):
    settings = make_settings(tmp_path)
    day1 = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    day1.imports.submit_file("a.csv", make_csv("代码", "000001"))
    day2 = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    day2.imports.submit_file("b.csv", make_csv("代码", "600519"))

    assert day2.classification.cleanup(import_date="2026-09-11") == 1

    cleared = day2.classification.get_candidate("000001.SZ")
    untouched = day2.classification.get_candidate("600519.SH")
    assert [e.action for e in cleared.candidate.history][0] == "cleared"
    assert [e.action for e in untouched.candidate.history] == ["selected"]
    assert untouched.candidate.state is CandidateState.PENDING


def test_batch_detail_marks_each_row_effect_and_name(container):
    """导入结果表逐行标注影响，名称来自权威证券库。"""
    first = container.imports.submit_file("a.csv", make_csv("代码", "000001"))
    assert [(s.raw_code, s.effect) for s in first.stocks] == [("000001", "new")]

    second = container.imports.submit_file("b.csv", make_csv("代码", "000001"))
    assert [s.effect for s in second.stocks] == ["source_only"]

    detail = container.imports.get_batch(second.batch_id)
    assert detail is not None
    names = container.imports.batch_security_names(detail)
    assert names == {"000001.SZ": "平安银行"}


def test_reopened_row_is_marked_in_batch_detail(tmp_path):
    settings = make_settings(tmp_path)
    day1 = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    first = build_container(settings, day1)
    first.imports.submit_file("a.csv", make_csv("代码", "000001"))
    first.classification.dismiss("000001.SZ")

    day2 = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    second = day2.imports.submit_file("b.csv", make_csv("代码", "000001"))
    assert [s.effect for s in second.stocks] == ["reopened"]
    assert second.reopened_candidate_count == 1
