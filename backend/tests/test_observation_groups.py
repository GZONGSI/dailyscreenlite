"""观察组：组管理（删除而非归档）、观察列表、共用详情与归类联动。

真实临时 SQLite + 真实证券库快照 + 固定时钟；覆盖默认组删除后不重建、
删除组保留其他关系与来源/处理历史、观察列表按证券去重与切组、
观察浏览上下文的独立保存与失效回落、共用个股详情跨日期与入口共享、
归类动作与观察关系的单向联动与事务一致，以及 HTTP 边界。
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
from dailyscreen_lite.domain.models import (
    DEFAULT_OBSERVATION_GROUP_ID,
    CandidateState,
)
from dailyscreen_lite.observations.service import ObservationInvalid, ObservationUnavailable
from dailyscreen_lite.settings import Settings


def make_settings(tmp_path: Path) -> Settings:
    return offline_settings(tmp_path)


def import_one(container, *codes: str) -> list[str]:
    """导入并返回候选项标识（队列顺序）。"""
    container.imports.submit_file("a.csv", make_csv("代码", *codes))
    return [v.candidate.candidate_id for v in container.classification.list_candidates()]


def observe(container, candidate_id: str, group_ids: list[str]) -> None:
    container.observations.observe_candidate(candidate_id, group_ids)


# --- 默认组与组管理 ---


def test_default_group_exists_on_first_start(container):
    groups = container.observations.list_groups()
    assert [g.group_id for g in groups] == [DEFAULT_OBSERVATION_GROUP_ID]
    assert groups[0].is_default
    assert groups[0].name == "默认观察组"


def test_create_rename_delete_persist_across_restart(tmp_path):
    settings = make_settings(tmp_path)
    first = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    created = first.observations.create_group("长期跟踪")
    first.observations.rename_group(created.group_id, "核心跟踪")
    dropped = first.observations.create_group("旧分组")
    first.observations.delete_group(dropped.group_id)

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    groups = {g.group_id: g for g in reopened.observations.list_groups()}

    assert groups[DEFAULT_OBSERVATION_GROUP_ID].name == "默认观察组"
    assert groups[created.group_id].name == "核心跟踪"
    # 删除的组不再存在，也不以归档形式保留
    assert dropped.group_id not in groups


def test_deleted_default_group_is_not_recreated_on_restart(tmp_path):
    """默认组可删；重启不得重建用户已删除的默认组，且允许删至无组。"""
    settings = make_settings(tmp_path)
    first = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    first.observations.delete_group(DEFAULT_OBSERVATION_GROUP_ID)
    assert first.observations.list_groups() == []

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    assert reopened.observations.list_groups() == []

    # 分类时可以重新建组，名字（含默认名）重新可用
    again = reopened.observations.create_group("默认观察组")
    assert again.group_id != DEFAULT_OBSERVATION_GROUP_ID
    assert again.is_default is False


def test_group_name_rules(container):
    with pytest.raises(ObservationInvalid):
        container.observations.create_group("   ")
    created = container.observations.create_group("核心")
    with pytest.raises(ObservationInvalid):
        container.observations.create_group("核心")
    with pytest.raises(ObservationUnavailable):
        container.observations.rename_group("group-missing", "新名")
    with pytest.raises(ObservationUnavailable):
        container.observations.delete_group("group-missing")

    # 删除后名字重新可用
    container.observations.delete_group(created.group_id)
    reused = container.observations.create_group("核心")
    assert reused.name == "核心"


def test_delete_group_keeps_notes_sources_history_and_other_groups(tmp_path):
    """删除组只影响该组关系：不影响其他组，也不删除笔记、来源与处理历史。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(container, "000001")[0]
    keep = container.observations.create_group("保留组")
    drop = container.observations.create_group("删除组")
    observe(container, candidate_id, [keep.group_id, drop.group_id])
    container.notes.create_for_security("000001.SZ", "组删了笔记还在")

    container.observations.delete_group(drop.group_id)

    assert container.observations.memberships("000001.SZ") == (keep.group_id,)
    assert [g.group_id for g in container.observations.list_groups()] == [
        DEFAULT_OBSERVATION_GROUP_ID,
        keep.group_id,
    ]
    # 处理历史与来源、笔记都保留，因此历史记录在原组被删后仍可读（最新的在前）
    view = container.classification.get_candidate(candidate_id)
    assert view.candidate.state is CandidateState.OBSERVED
    assert [entry.action for entry in view.candidate.history] == ["observed", "selected"]
    assert len(view.sources) == 1
    assert view.note_count == 1

    # 重启后仍是同样事实
    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    assert reopened.observations.memberships("000001.SZ") == (keep.group_id,)
    assert reopened.classification.get_candidate(candidate_id).note_count == 1


# --- 多组关系与幂等 ---


def test_stock_can_belong_to_multiple_groups_and_preselect(container):
    candidate_id = import_one(container, "000001")[0]
    a = container.observations.create_group("组A")
    b = container.observations.create_group("组B")

    result = container.observations.observe_candidate(candidate_id, [a.group_id, b.group_id])

    assert set(result.group_ids) == {a.group_id, b.group_id}
    assert container.observations.memberships("000001.SZ") == tuple(
        sorted([a.group_id, b.group_id])
    )


def test_duplicate_save_does_not_duplicate_members(container):
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")

    observe(container, candidate_id, [group.group_id])
    # 重复保存同一关系：不产生第二行，也不因已处理而失败
    kept = container.observations.save_membership("000001.SZ", [group.group_id])

    assert kept == (group.group_id,)
    groups = {g.group_id: g for g in container.observations.list_groups()}
    assert groups[group.group_id].member_count == 1


def test_save_membership_moves_between_groups_without_touching_state(container):
    """转组：整组替换后只保留新组，候选处理状态不变。"""
    candidate_id = import_one(container, "000001")[0]
    wrong = container.observations.create_group("加错了")
    right = container.observations.create_group("正确的")
    observe(container, candidate_id, [wrong.group_id])
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.OBSERVED

    kept = container.observations.save_membership("000001.SZ", [right.group_id])

    assert kept == (right.group_id,)
    assert container.observations.memberships("000001.SZ") == (right.group_id,)
    # 转组只改关系，不反转处理状态
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.OBSERVED


def test_save_membership_can_clear_all_relations(container):
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")
    observe(container, candidate_id, [group.group_id])

    assert container.observations.save_membership("000001.SZ", []) == ()
    assert container.observations.memberships("000001.SZ") == ()
    # 退出全部组不改变候选处理状态
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.OBSERVED


def test_membership_rejected_for_unimported_security(container):
    """观察关系不能绕过导入归类新增全市场证券。"""
    import_one(container, "000001")
    group = container.observations.create_group("核心")

    with pytest.raises(ObservationUnavailable):
        container.observations.save_membership("999999.SZ", [group.group_id])
    assert container.observations.memberships("999999.SZ") == ()


def test_membership_requires_completed_classification(container):
    """已导入但尚未归类的股票不能从观察页面加组；归类之后才可调整关系。"""
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")

    with pytest.raises(ObservationInvalid):
        container.observations.save_membership("000001.SZ", [group.group_id])

    # 走归类流程加入观察后，观察页面就可以转组
    observe(container, candidate_id, [DEFAULT_OBSERVATION_GROUP_ID])
    other = container.observations.create_group("另一个")
    assert container.observations.save_membership("000001.SZ", [other.group_id]) == (
        other.group_id,
    )
    # 已观察股票有新入选（回到未处理）时仍可调整关系，不反向改变处理状态
    container.classification.reclassify(candidate_id)
    assert container.observations.save_membership("000001.SZ", []) == ()
    assert container.classification.get_candidate(candidate_id).candidate.state is (
        CandidateState.PENDING
    )


# --- 观察列表 ---


def test_observed_stocks_dedup_by_security_with_all_groups(container):
    items = import_one(container, "000001", "600519")
    a = container.observations.create_group("组A")
    b = container.observations.create_group("组B")
    observe(container, items[0], [a.group_id])
    observe(container, items[1], [a.group_id, b.group_id])

    stocks = {s.security_id: s for s in container.observations.stocks()}
    assert set(stocks) == {"000001.SZ", "600519.SH"}
    assert stocks["600519.SH"].group_ids == tuple(sorted([a.group_id, b.group_id]))
    assert stocks["000001.SZ"].group_ids == (a.group_id,)


def test_observed_stocks_single_group_view(container):
    items = import_one(container, "000001", "600519")
    a = container.observations.create_group("组A")
    b = container.observations.create_group("组B")
    observe(container, items[0], [a.group_id])
    observe(container, items[1], [a.group_id, b.group_id])

    only_b = container.observations.stocks(b.group_id)
    assert [s.security_id for s in only_b] == ["600519.SH"]
    # 切单组只改变筛选：所属组列仍列出该股票的全部现存组
    assert only_b[0].group_ids == tuple(sorted([a.group_id, b.group_id]))
    assert container.observations.stocks(a.group_id)[0].security_id in {
        "000001.SZ",
        "600519.SH",
    }


def test_stocks_rejects_unknown_group(container):
    """指定不存在的组明确报错，不把空列表当成「这个组没有股票」。"""
    import_one(container, "000001")
    with pytest.raises(ObservationUnavailable):
        container.observations.stocks("group-missing")


def test_observed_stocks_empty_when_no_groups(container):
    """允许所有组为空：列表为空而不是报错，缺行情也不影响浏览。"""
    import_one(container, "000001")
    assert container.observations.stocks() == []

    container.observations.delete_group(DEFAULT_OBSERVATION_GROUP_ID)
    assert container.observations.stocks() == []


# --- 观察浏览上下文 ---


def test_view_state_persists_independently_of_classification(tmp_path):
    """观察组模块独立保存当前组、排序与当前股票，且不与候选归类互相覆盖。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")
    observe(container, candidate_id, [group.group_id])
    container.classification.update_view({"currentCandidateId": candidate_id})

    container.observations.update_view(
        {"groupId": group.group_id, "sort": "change", "currentSecurityId": "000001.SZ"}
    )

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    state = reopened.observations.get_view().state
    assert state.group_id == group.group_id
    assert state.sort == "change"
    assert state.current_security_id == "000001.SZ"
    # 归类模块自己的当前股票没有被观察模块的写入改动
    assert reopened.classification.get_view().state.current_candidate_id == candidate_id


def test_view_clears_current_when_target_leaves_observation(container):
    items = import_one(container, "000001", "600519")
    container.observations.update_view({"currentSecurityId": items[0]})
    # 未入组的目标当场就不保留，避免详情停在看不见的股票上
    assert container.observations.get_view().state.current_security_id is None


def test_view_drops_current_and_falls_back_when_group_deleted(container):
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")
    observe(container, candidate_id, [group.group_id])
    container.observations.update_view(
        {"groupId": group.group_id, "currentSecurityId": "000001.SZ"}
    )

    container.observations.delete_group(group.group_id)

    view = container.observations.get_view()
    assert view.state.group_id is None
    assert view.state.current_security_id is None
    assert view.stocks == ()


def test_view_rejects_unknown_sort(container):
    with pytest.raises(ObservationInvalid):
        container.observations.update_view({"sort": "unknown"})


# --- 共用个股详情 ---


def test_stock_detail_is_security_centred_and_shared_across_dates(tmp_path):
    """详情以股票为身份：跨导入日期共享行情归属、笔记、观察关系、来源与处理记录。"""
    settings = make_settings(tmp_path)
    day1 = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(day1, "000001")[0]
    group = day1.observations.create_group("核心")
    day1.observations.observe_candidate(candidate_id, [group.group_id])
    day1.notes.create_for_security("000001.SZ", "跨日期共用")

    day2 = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    day2.imports.submit_file("b.csv", make_csv("代码", "000001"))
    detail = day2.observations.stock_detail("000001.SZ")

    assert detail.security is not None and detail.security.code == "000001"
    assert detail.group_ids == (group.group_id,)
    assert detail.candidate is not None
    assert detail.candidate.candidate.candidate_id == candidate_id
    assert detail.candidate.observed is True
    assert detail.candidate.note_count == 1
    # 新导入日期带来新的来源，处理记录保留（最新的在前）
    assert {s.import_date for s in detail.candidate.sources} == {"2026-09-11", "2026-09-12"}
    assert [entry.action for entry in detail.candidate.candidate.history] == [
        "reopened",
        "observed",
        "selected",
    ]


def test_stock_detail_unknown_security_raises(container):
    with pytest.raises(ObservationUnavailable):
        container.observations.stock_detail("999999.SZ")


def test_focus_requires_observed_stock(container):
    candidate_id = import_one(container, "000001", "600519")[0]
    group = container.observations.create_group("核心")
    observe(container, candidate_id, [group.group_id])

    with pytest.raises(ObservationUnavailable):
        container.observations.focus("600519.SH")

    view = container.observations.focus("000001.SZ")
    assert view.state.current_security_id == "000001.SZ"
    assert view.state.group_id is None


# --- 与候选处理状态的联动 ---


def test_observe_completes_candidate_and_stays_current(container):
    candidate_id = import_one(container, "000001", "600519")[0]
    container.classification.update_view({"currentCandidateId": candidate_id})
    default = DEFAULT_OBSERVATION_GROUP_ID

    result = container.observations.observe_candidate(candidate_id, [default])

    assert result.candidate is not None
    assert result.candidate.candidate.state is CandidateState.OBSERVED
    # 动作完成后停留当前卡：当前项仍是该股票
    assert container.classification.get_view().state.current_candidate_id == candidate_id
    assert container.classification.get_candidate(candidate_id).candidate.action_result == "observed"


def test_observe_requires_at_least_one_group(container):
    candidate_id = import_one(container, "000001")[0]
    with pytest.raises(ObservationInvalid):
        container.observations.observe_candidate(candidate_id, [])
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.PENDING


def test_observe_rejects_unknown_group(container):
    candidate_id = import_one(container, "000001")[0]
    with pytest.raises(ObservationInvalid):
        container.observations.observe_candidate(candidate_id, ["group-missing"])
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.PENDING


def test_failed_save_rolls_back_membership_and_state(tmp_path, monkeypatch):
    """注入保存失败：成员关系与处理状态一起回滚，不半成功。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")
    container.classification.update_view({"currentCandidateId": candidate_id})

    from dailyscreen_lite.repository import classification_repo

    original = classification_repo.update_state

    def boom(conn, *args, **kwargs):
        # 先借助真实实现把状态写进去，再抛错：验证事务回滚而不是顺序巧合
        original(conn, *args, **kwargs)
        raise RuntimeError("注入的保存失败")

    monkeypatch.setattr(classification_repo, "update_state", boom)

    with pytest.raises(RuntimeError):
        container.observations.observe_candidate(candidate_id, [group.group_id])

    # 成员关系未写入、候选项仍未处理，用户可留在原卡重试
    assert container.observations.memberships("000001.SZ") == ()
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.PENDING
    assert container.classification.get_view().state.current_candidate_id == candidate_id


def test_failed_save_keeps_previously_saved_relations(tmp_path, monkeypatch):
    """保存失败不半成功：既有关系不被清掉，也不会只留下一半新关系。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(container, "000001")[0]
    old = container.observations.create_group("原有")
    new = container.observations.create_group("新增")
    observe(container, candidate_id, [old.group_id])
    # 已观察股票有新入选时回到未处理，仍可调整关系
    container.classification.reclassify(candidate_id)

    from dailyscreen_lite.repository import classification_repo

    original = classification_repo.update_state

    def boom(conn, *args, **kwargs):
        original(conn, *args, **kwargs)
        raise RuntimeError("注入的保存失败")

    monkeypatch.setattr(classification_repo, "update_state", boom)

    with pytest.raises(RuntimeError):
        container.observations.observe_candidate(candidate_id, [new.group_id])

    # 回滚到保存前的状态：旧的仍在、新的没进来
    assert container.observations.memberships("000001.SZ") == (old.group_id,)
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.PENDING


def test_dismiss_and_restore_do_not_reverse_group_relation(container):
    candidate_id = import_one(container, "000001")[0]
    group = container.observations.create_group("核心")
    observe(container, candidate_id, [group.group_id])

    container.classification.reclassify(candidate_id)
    assert container.classification.get_candidate(candidate_id).candidate.state is CandidateState.PENDING
    assert container.observations.memberships("000001.SZ") == (group.group_id,)

    container.classification.dismiss(candidate_id)
    assert container.observations.memberships("000001.SZ") == (group.group_id,)


def test_observed_stock_new_import_date_requires_reclassification(tmp_path):
    settings = make_settings(tmp_path)
    first = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(first, "000001")[0]
    group = first.observations.create_group("核心")
    first.observations.observe_candidate(candidate_id, [group.group_id])

    # 次日的新导入日期：已观察股票复用同一候选项并重新进入待归类
    tomorrow = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    tomorrow.imports.submit_file("b.csv", make_csv("代码", "000001"))
    items = tomorrow.classification.list_candidates()

    assert len(items) == 1
    assert items[0].candidate.state is CandidateState.PENDING
    assert items[0].candidate.candidate_id == candidate_id
    # 观察关系不因新导入或重新归类而丢失
    assert tomorrow.observations.memberships("000001.SZ") == (group.group_id,)


def test_observed_flag_reflects_group_membership_not_state(tmp_path):
    """卡片按组归属标注已观察：新日期导入的未处理项也应标注，未入组的则不标注。"""
    settings = make_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 12, 10, 0, 0)))
    candidate_id = import_one(container, "000001", "600519")[0]
    group = container.observations.create_group("核心")
    container.observations.observe_candidate(candidate_id, [group.group_id])

    other = container.classification.get_candidate(
        next(
            v.candidate.candidate_id
            for v in container.classification.list_candidates()
            if v.candidate.candidate_id != candidate_id
        )
    )
    assert other.observed is False

    # 已观察股票次日再导入：新候选项仍未处理，但已标注已观察
    tomorrow = build_container(settings, FixedClock(datetime(2026, 9, 13, 10, 0, 0)))
    tomorrow.imports.submit_file("b.csv", make_csv("代码", "000001"))
    view = tomorrow.classification.get_candidate(candidate_id)
    assert view.candidate.state is CandidateState.PENDING
    assert view.observed is True


def test_observe_rejects_already_processed_item(container):
    candidate_id = import_one(container, "000001")[0]
    container.classification.dismiss(candidate_id)

    from dailyscreen_lite.classification.service import ClassificationActionNotAllowed

    with pytest.raises(ClassificationActionNotAllowed):
        container.observations.observe_candidate(candidate_id, [DEFAULT_OBSERVATION_GROUP_ID])


def test_remove_candidate_from_groups_is_atomic(container):
    """移出观察组：只移除选中关系，同时记为暂不关注。"""
    candidate_id = import_one(container, "000001")[0]
    keep = container.observations.create_group("保留组")
    drop = container.observations.create_group("移出组")
    observe(container, candidate_id, [keep.group_id, drop.group_id])

    result = container.observations.remove_candidate_from_groups(candidate_id, [drop.group_id])

    assert result.group_ids == (keep.group_id,)
    assert result.candidate is not None
    assert result.candidate.candidate.state is CandidateState.DISMISSED


# --- 重启读回 ---


def test_membership_persists_across_restart(tmp_path):
    settings = make_settings(tmp_path)
    first = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    candidate_id = import_one(first, "000001")[0]
    group = first.observations.create_group("核心")
    first.observations.observe_candidate(candidate_id, [group.group_id])

    reopened = build_container(settings, FixedClock(datetime(2026, 9, 11, 11, 0, 0)))

    assert reopened.observations.memberships("000001.SZ") == (group.group_id,)
    assert reopened.classification.get_candidate(candidate_id).candidate.state is CandidateState.OBSERVED
    groups = {g.group_id: g for g in reopened.observations.list_groups()}
    assert groups[group.group_id].member_count == 1


# --- HTTP 边界 ---


def test_join_date_preserved_on_keep_and_updated_only_on_reentry_through_http(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10))
    with TestClient(create_app(settings, clock)) as client:
        client.post("/api/imports/csv", files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")})
        candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]
        a = client.post("/api/observations/groups", json={"name": "组A"}).json()["groupId"]
        b = client.post("/api/observations/groups", json={"name": "组B"}).json()["groupId"]
        assert client.post(f"/api/observations/candidates/{candidate_id}/observe", json={"groupIds": [a]}).status_code == 200

        def joined(group_id):
            return client.get("/api/observations/securities", params={"groupId": group_id}).json()["stocks"][0]["joinedAt"][:10]

        clock.set(datetime(2026, 9, 12, 10))
        assert client.put("/api/observations/memberships", json={"securityId": "000001.SZ", "groupIds": [a]}).status_code == 200
        assert joined(a) == "2026-09-11"
        clock.set(datetime(2026, 9, 13, 10))
        client.put("/api/observations/memberships", json={"securityId": "000001.SZ", "groupIds": [a, b]})
        assert joined(a) == "2026-09-11"
        assert joined(b) == "2026-09-13"
        clock.set(datetime(2026, 9, 14, 10))
        client.put("/api/observations/memberships", json={"securityId": "000001.SZ", "groupIds": [b]})
        clock.set(datetime(2026, 9, 15, 10))
        client.put("/api/observations/memberships", json={"securityId": "000001.SZ", "groupIds": [a, b]})
        assert joined(a) == "2026-09-15"
        assert joined(b) == "2026-09-13"
        assert client.get(f"/api/classification/candidates/{candidate_id}").json()["state"] == "observed"

    with TestClient(create_app(settings, clock)) as reopened:
        for group_id, expected in [(a, "2026-09-15"), (b, "2026-09-13")]:
            stocks = reopened.get("/api/observations/securities", params={"groupId": group_id}).json()["stocks"]
            assert stocks[0]["joinedAt"][:10] == expected


def test_all_groups_join_date_uses_latest_existing_membership_through_http(tmp_path):
    clock = FixedClock(datetime(2026, 9, 11, 10))
    with TestClient(create_app(make_settings(tmp_path), clock)) as client:
        client.post("/api/imports/csv", files={"file": ("a.csv", make_csv("代码", "000001", "600519"), "text/csv")})
        items = {row["securityId"]: row["candidateId"] for row in client.get("/api/classification/candidates").json()["candidates"]}
        a = client.post("/api/observations/groups", json={"name": "组A"}).json()["groupId"]
        b = client.post("/api/observations/groups", json={"name": "组B"}).json()["groupId"]
        assert client.post(f"/api/observations/candidates/{items['000001.SZ']}/observe", json={"groupIds": [a]}).status_code == 200
        clock.set(datetime(2026, 9, 12, 10))
        assert client.post(f"/api/observations/candidates/{items['600519.SH']}/observe", json={"groupIds": [a]}).status_code == 200
        clock.set(datetime(2026, 9, 13, 10))
        # 加入或保留观察只新增组B，组A的原关系保留，两个日期不同。
        assert client.post(f"/api/classification/candidates/{items['000001.SZ']}/reclassify").status_code == 200
        assert client.post(f"/api/observations/candidates/{items['000001.SZ']}/observe", json={"groupIds": [b]}).status_code == 200
        stocks = client.get("/api/observations/view").json()["stocks"]
        assert [row["securityId"] for row in stocks] == ["000001.SZ", "600519.SH"]
        assert stocks[0]["joinedAt"][:10] == "2026-09-13"
        single = client.get("/api/observations/securities", params={"groupId": a}).json()["stocks"]
        assert next(row for row in single if row["securityId"] == "000001.SZ")["joinedAt"][:10] == "2026-09-11"


def test_observation_endpoints_work_through_http(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        )
        candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]
        client.put("/api/classification/view", json={"currentCandidateId": candidate_id})

        groups = client.get("/api/observations/groups").json()["groups"]
        default_id = groups[0]["groupId"]
        assert groups[0]["isDefault"] is True
        assert "archived" not in groups[0]

        created = client.post("/api/observations/groups", json={"name": "核心"}).json()
        assert created["name"] == "核心"

        saved = client.post(
            f"/api/observations/candidates/{candidate_id}/observe",
            json={"groupIds": [default_id, created["groupId"]]},
        ).json()
        assert set(saved["groupIds"]) == {default_id, created["groupId"]}
        assert saved["candidate"]["state"] == "observed"
        # 联动动作响应与候选归类的动作响应同一形状：带写入后的列表版本与导航修订号，
        # 前端据此直接发出自动推进，不为此重读完整浏览结果。
        assert saved["candidate"]["listRevision"] == saved["listRevision"]
        assert saved["candidate"]["navigationRevision"] == saved["navigationRevision"]
        # 写入前的列表版本也随响应下发：前端据此判断手上的列表是否已经落后
        assert saved["candidate"]["listRevisionBefore"] == saved["listRevisionBefore"]
        assert saved["listRevisionBefore"] < saved["listRevision"]
        current = client.get("/api/classification/view").json()
        assert saved["navigationRevision"] == current["navigationRevision"]
        assert current["currentCandidateId"] == candidate_id

        memberships = client.get(
            "/api/observations/memberships", params={"securityId": "000001.SZ"}
        ).json()
        assert set(memberships["groupIds"]) == {default_id, created["groupId"]}

        # 观察列表与共用详情
        stocks = client.get("/api/observations/securities").json()["stocks"]
        assert [s["securityId"] for s in stocks] == ["000001.SZ"]
        assert set(stocks[0]["groupIds"]) == {default_id, created["groupId"]}
        detail = client.get("/api/observations/securities/000001.SZ").json()
        assert detail["candidate"]["candidateId"] == candidate_id
        assert detail["security"]["code"] == "000001"

        # 浏览上下文
        put = client.put(
            "/api/observations/view",
            json={"groupId": "all", "sort": "name", "currentSecurityId": "000001.SZ"},
        ).json()
        assert put["state"] == {
            "groupId": None,
            "sort": "name",
            "currentSecurityId": "000001.SZ",
        }

        # 转组：整组替换后只保留新组，不改变已观察状态
        corrected = client.put(
            "/api/observations/memberships",
            json={"securityId": "000001.SZ", "groupIds": [created["groupId"]]},
        ).json()
        assert corrected["groupIds"] == [created["groupId"]]
        assert client.get(f"/api/classification/candidates/{candidate_id}").json()["state"] == "observed"

        # 删除组：该组消失，候选与笔记不受影响
        removed = client.delete(f"/api/observations/groups/{created['groupId']}")
        assert removed.status_code == 200
        remaining = client.get("/api/observations/groups").json()["groups"]
        assert created["groupId"] not in {g["groupId"] for g in remaining}
        assert client.get("/api/observations/memberships", params={"securityId": "000001.SZ"}).json()[
            "groupIds"
        ] == []

        renamed = client.patch(
            f"/api/observations/groups/{default_id}", json={"name": "我的关注"}
        ).json()
        assert renamed["name"] == "我的关注"


def test_http_observe_rejects_empty_and_unknown_group(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        )
        candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]

        empty = client.post(f"/api/observations/candidates/{candidate_id}/observe", json={"groupIds": []})
        assert empty.status_code == 400

        unknown_group = client.post(
            f"/api/observations/candidates/{candidate_id}/observe",
            json={"groupIds": ["group-missing"]},
        )
        assert unknown_group.status_code == 400

        missing = client.post(
            "/api/observations/candidates/does-not-exist/observe",
            json={"groupIds": [DEFAULT_OBSERVATION_GROUP_ID]},
        )
        assert missing.status_code == 404

        # 三次失败后候选项仍未处理
        detail = client.get(f"/api/classification/candidates/{candidate_id}").json()
        assert detail["state"] == "pending"


def test_http_membership_rejects_unimported_security(tmp_path):
    settings = make_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    with TestClient(create_app(settings, clock)) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        )
        groups = client.get("/api/observations/groups").json()["groups"]
        rejected = client.put(
            "/api/observations/memberships",
            json={"securityId": "999999.SZ", "groupIds": [groups[0]["groupId"]]},
        )
        assert rejected.status_code == 404
