"""按访问步骤持久化归类浏览路径（浏览内核第二张工单）。

覆盖：手动打开候选截断前进分支并形成新步骤、再次访问同一候选形成新步骤、
搜索与列表点选不改队列顺序与筛选、筛选外候选可打开并标记、已处理页签不触发
自动遍历、结束卡作为历史步骤（可回看、可跨重启恢复、有前进历史时「下一个」可用）、
新导入不抢当前位置、失效步骤跳过、导航响应丢失后重试不跨过第二只。
真实临时 SQLite 与真实证券库快照，不注入伪仓储。
"""

from __future__ import annotations

import json
from datetime import datetime

from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import END_STEP

CODES = ("000001", "600519", "300750")


def _container(tmp_path, codes: str = " ".join(CODES)):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)
    container.imports.submit_file("a.csv", make_csv("代码", *codes.split()))
    return container


def _ids(container) -> list[str]:
    return [v.candidate.candidate_id for v in container.classification.list_candidates()]


def _navigate(service, direction: str):
    state = service.get_view().state
    return service.navigate(
        direction,
        expected_cursor=state.cursor,
        expected_revision=state.navigation_revision,
    ).view.state


# --- 访问历史本身 ---


def test_manual_jump_replaces_forward_branch_and_records_new_step(tmp_path):
    """A→B→C 退回 B 后打开 D：历史变为 A→B→D，上一项回到本次跳转前的位置。"""
    container = _container(tmp_path, "000001 600519 300750 000002")
    service = container.classification
    a, b, c, d = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    _navigate(service, "next")
    assert service.get_view().state.path == (a, b, c)

    back = _navigate(service, "previous")
    assert back.current_candidate_id == b
    assert back.path == (a, b, c)

    # 历史中途跳转替换前进分支：C 被 D 取代
    opened = service.focus(d)
    assert opened.state.path == (a, b, d)
    assert opened.state.cursor == 2
    assert opened.current.candidate.candidate_id == d
    assert opened.in_filter is True

    # 上一个回到本次跳转前的位置（B），下一个再回到 D
    assert _navigate(service, "previous").current_candidate_id == b
    assert _navigate(service, "next").current_candidate_id == d


def test_revisiting_a_candidate_forms_a_new_step(tmp_path):
    """再次打开曾看过的候选也形成新的一步：上一项回到本次跳转前的位置。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    _navigate(service, "next")
    assert service.get_view().state.path == (a, b, c)

    # 退回 B 再打开更早访问过的 A：A 追加为新的一步，B 之后的分支被替换
    _navigate(service, "previous")
    revisited = service.focus(a)
    assert revisited.state.path == (a, b, a)
    assert revisited.state.cursor == 2
    # 上一项回到本次跳转前的位置（B），再上一项是 A 上一次的访问
    assert _navigate(service, "previous").current_candidate_id == b
    assert _navigate(service, "previous").current_candidate_id == a


def test_history_review_shows_current_processing_state(tmp_path):
    """回看历史中的股票显示其当下状态：浏览不撤销归类。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, _ = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    service.dismiss(b)

    back = _navigate(service, "previous")
    assert back.path == (a, b)
    assert back.current_candidate_id == a
    assert service.get_candidate(a).candidate.state.value == "pending"

    forward = _navigate(service, "next")
    # B 已处理仍可回看，显示真实状态与「已处理」页签
    assert forward.current_candidate_id == b
    assert forward.scope.value == "processed"
    assert service.get_candidate(b).candidate.state.value == "dismissed"


# --- 不改队列与筛选 ---


def test_focus_keeps_pool_order_filters_and_marks_outside_target(tmp_path):
    """搜索与列表点选不改变未处理池顺序或当前筛选；筛选外候选可打开并明确提示。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.focus(a)
    # 用户主动改搜索：开启新一轮，落到新范围首项（当前卡已不在范围内）
    filtered = service.update_view({"search": "宁德"})
    assert filtered.state.path == (b,)
    assert [row.candidate_id for row in filtered.rows] == [b]

    opened = service.focus(c)

    # 队列顺序不变（未处理池只由稍后处理、重新归类与再次入选决定）
    assert _ids(container) == [a, b, c]
    # 筛选条件不变：列表仍是「宁德」的结果，只是当前卡不在其中
    assert opened.state.search == "宁德"
    assert opened.state.filter_scope.value == "unprocessed"
    assert [row.candidate_id for row in opened.rows] == [b]
    assert opened.current.candidate.candidate_id == c
    assert opened.in_filter is False

    # 上一个回到本次跳转前的位置（B），筛选仍然保留
    previous = _navigate(service, "previous")
    assert previous.current_candidate_id == b
    assert service.browse().state.search == "宁德"

    # 命令响应为省掉整份列表改做成员判断（`with_rows=False`）：结论必须与完整浏览
    # 结果同一口径——筛选外的候选在两条路径上都是「不在当前结果里」。
    current = service.get_view().state
    command = service.navigate(
        "next",
        expected_cursor=current.cursor,
        expected_revision=current.navigation_revision,
    )
    assert command.view.current.candidate.candidate_id == c
    assert command.view.in_filter is False
    assert command.view.in_filter == service.browse().in_filter


def test_manual_open_outside_range_shows_card_in_the_list_it_belongs_to(tmp_path):
    """在待归类范围里打开已处理候选：页签落到它真实所在的一栏，不留错位卡片。

    卡片与左侧列表始终属于同一份结果；筛选条件与筛选范围都不因一次打开而改变，
    因此返回上一项后仍在原来的工作范围，而不会出现「空列表 + 卡片说自己在别处」。
    """
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.dismiss(a)

    # 用户仍在待归类范围（当前卡是刚刚打开的那一只）
    service.update_view({"scope": "unprocessed", "currentCandidateId": b})
    before = service.browse()
    assert before.state.filter_scope.value == "unprocessed"
    assert before.state.scope.value == "unprocessed"
    assert [row.candidate_id for row in before.rows] == [b, c]

    opened = service.focus(a)
    # 卡片与列表同步落到「已处理」，不出现一张不属于所见列表的卡片
    assert opened.current.candidate.candidate_id == a
    assert opened.state.scope.value == "processed"
    assert opened.in_filter is True
    assert [row.candidate_id for row in opened.rows] == [a]
    # 用户选定的筛选范围没有被他的一次打开改掉：后续仍在待归类里找新候选
    assert opened.state.filter_scope.value == "unprocessed"
    assert opened.has_next is True

    # 返回上一项仍在原来的工作范围：当前卡是待归类，页签随之回到待归类
    previous = _navigate(service, "previous")
    assert previous.current_candidate_id == b
    assert previous.scope.value == "unprocessed"
    restored = service.browse()
    assert restored.in_filter is True
    assert [row.candidate_id for row in restored.rows] == [b, c]


def test_processed_range_never_falls_back_to_the_unprocessed_pool(tmp_path):
    """空的已处理范围只呈现空状态：当前位置绝不落到待归类池的首项。"""
    container = _container(tmp_path)
    service = container.classification
    service.update_view({"scope": "processed"})
    empty = service.browse()
    assert empty.state.current_candidate_id is None
    assert empty.state.path == ()
    assert empty.rows == ()
    # 待归类池里的三只不会被当作「已处理范围的首项」打开
    assert [row.candidate_id for row in empty.pending] == _ids(container)

    # 空状态下按「下一个」只是收尾（进入结束卡），仍然不会打开任何待归类候选
    ended = _navigate(service, "next")
    assert ended.ended is True
    assert ended.path == (END_STEP,)
    assert ended.current_candidate_id is None
    assert service.browse().rows == ()


def test_pool_order_sources_are_unchanged(tmp_path):
    """稍后处理移尾、主动重新归类移首、未处理期间跨日再次入选保留原位。"""
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519", "300750"))
    service = container.classification
    a, b, c = _ids(container)

    service.focus(c)
    assert _ids(container) == [a, b, c]  # 打开不改顺序
    service.later(a)
    assert _ids(container) == [b, c, a]  # 稍后处理移尾
    service.reclassify(a)
    assert _ids(container) == [a, b, c]  # 主动重新归类移首

    # 未处理期间跨日再次入选：合并到同一候选项并保留原队列位置
    container.imports.submit_file("b.csv", make_csv("代码", "600519"))
    assert _ids(container) == [a, b, c]
    assert service.get_candidate(b).candidate.import_dates[-1].iso == "2026-09-11"


# --- 页签、状态与自动遍历 ---


def test_processed_tab_opens_candidate_without_auto_iterating(tmp_path):
    """已处理页签可点开候选，但历史末端不自动遍历已处理项。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.dismiss(a)
    service.dismiss(b)

    opened = service.update_view({"scope": "processed"})
    assert opened.state.filter_scope.value == "processed"
    assert opened.state.current_candidate_id is not None
    assert opened.current.candidate.state.value == "dismissed"

    # 历史末端按「下一个」在已处理范围里没有待归类候选：进入结束卡，不逐项遍历
    ended = _navigate(service, "next")
    assert ended.ended is True
    assert ended.path[-1] == END_STEP
    assert ended.current_candidate_id is None
    assert _navigate(service, "next").ended is True

    # 待归类范围内仍有真实剩余数量（当前范围看完 ≠ 全部处理完）
    browsed = service.browse()
    assert browsed.state.ended is True
    assert browsed.round["remaining"] == 1
    assert browsed.summary["unprocessed"] == 1
    assert browsed.summary["processed"] == 2

    # 主动改页签重置本轮并从新范围落位
    back = service.update_view({"scope": "unprocessed"})
    assert back.state.path == (c,)
    assert back.state.filter_scope.value == "unprocessed"


def test_filter_change_and_return_to_queue_reset_the_round(tmp_path):
    """主动改页签／日期／搜索／结果与返回队列都重置本轮（浏览路径重新开始）。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    _navigate(service, "next")
    assert service.get_view().state.path == (a, b, c)
    assert service.browse().round["viewed"] == 3

    # 主动改日期：开启新一轮，本轮统计重新开始；原当前卡仍在新范围里就保留它
    edited = service.update_view({"importDate": "2026-09-11"})
    assert edited.state.path == (c,)
    assert edited.round["viewed"] == 1
    # 新路径末端：其余候选尚未查看，再按「下一个」能找到第一只
    assert _navigate(service, "next").current_candidate_id == a

    # 主动改结果筛选：新范围里没有可打开的候选，落到空状态
    reset = service.update_view({"scope": "processed", "result": "dismissed"})
    assert reset.state.path == ()
    assert reset.state.current_candidate_id is None
    assert reset.round["viewed"] == 0

    # 返回队列：结束旧路径并从当前筛选范围重新落位；本轮统计重新开始
    service.update_view({"scope": "unprocessed"})
    restarted = service.return_to_queue()
    assert restarted.state.round_started_at is not None
    assert restarted.state.path == (a,)
    assert restarted.round["viewed"] == 1
    assert restarted.round["remaining"] == 3


# --- 结束卡作为历史步骤 ---


def test_end_card_is_a_history_step_and_survives_restart(tmp_path):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    service = container.classification
    a, b = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    ended = _navigate(service, "next")
    assert ended.ended is True
    assert ended.path == (a, b, END_STEP)

    with TestClient(create_app(settings, clock)) as client:
        restored = client.get("/api/classification/view").json()
        assert restored["ended"] is True
        assert restored["path"] == [a, b, END_STEP]
        assert restored["currentCandidateId"] is None
        # 本轮已看过全部候选项且没有前进历史：结束卡没有可去的下一步
        assert restored["hasNext"] is False
        # 上一个回结束卡之前的那一步
        assert (
            client.post(
                "/api/classification/view/navigate",
                json={
                    "direction": "previous",
                    "expectedCursor": restored["cursor"],
                    "expectedRevision": restored["navigationRevision"],
                },
            ).json()["currentCandidateId"]
            == b
        )


def test_end_card_with_forward_history_allows_next(tmp_path):
    """结束卡是历史里的一步：前进与回看都停在它上面；控件按可达位置可用。"""
    container = _container(tmp_path, "000001 000002 000006 000007")
    service = container.classification
    a, b, c, d = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    _navigate(service, "next")
    _navigate(service, "next")
    ended = _navigate(service, "next")
    assert ended.ended is True
    assert ended.path == (a, b, c, d, END_STEP)
    # 末端没有去处：结束卡不显示无效前进
    assert service.browse().has_next is False

    # 新导入不抢结束卡；从结束卡按「下一个」才找到它，成为结束卡之后的一步
    container.imports.submit_file("b.csv", make_csv("代码", "000008"))
    e = "000008.SZ"
    assert service.browse().has_next is True
    forward_to_new = _navigate(service, "next")
    assert forward_to_new.current_candidate_id == e
    assert forward_to_new.path == (a, b, c, d, END_STEP, e)

    # 上一个回结束卡（它是历史里的一步），此时它拥有前进历史 E，因此「下一个」可用
    back_to_end = _navigate(service, "previous")
    assert back_to_end.ended is True
    assert back_to_end.cursor == 4
    assert service.browse().has_next is True
    assert _navigate(service, "next").current_candidate_id == e

    # 从结束卡回看后再前进回到 E 的那一步（不是重复追加，也不越过结束卡）
    manual = service.focus(e)
    assert manual.state.path == (a, b, c, d, END_STEP, e)
    assert manual.state.cursor == 5
    assert _navigate(service, "previous").ended is True
    assert _navigate(service, "next").current_candidate_id == e

    # 结束卡在路径中间时同样停步：它之前的步骤按「下一个」先回到结束卡，再前进到候选
    mid = service.browse().state
    assert mid.path[mid.cursor] == e
    assert _navigate(service, "previous").ended is True
    offset = _navigate(service, "previous")
    assert offset.current_candidate_id == d
    assert offset.path == (a, b, c, d, END_STEP, e)
    assert _navigate(service, "next").ended is True
    assert _navigate(service, "next").current_candidate_id == e


def test_reviewing_back_from_a_step_behind_the_end_card_reaches_it(tmp_path):
    """「A→B→结束卡→D」：从 B 按下一个先到结束卡、再按一次才到 D，从 D 按上一个回结束卡。"""
    container = _container(tmp_path, "000001 000002")
    service = container.classification
    a, b = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    assert _navigate(service, "next").ended is True
    assert service.browse().state.path == (a, b, END_STEP)

    # 从结束卡手动打开一只：结束卡保留为可回看的一步，目标成为它之后的前进历史
    container.imports.submit_file("b.csv", make_csv("代码", "000006"))
    d = "000006.SZ"
    opened = service.focus(d)
    assert opened.state.path == (a, b, END_STEP, d)
    assert opened.state.cursor == 3

    # 从 D 回看：结束卡是历史里的一步，于是停在它上面
    assert _navigate(service, "previous").ended is True
    # 再回看一步到 B；从 B 前进同样先回到结束卡，再按一次才到 D（两个方向都不跨过它）
    assert _navigate(service, "previous").current_candidate_id == b
    assert _navigate(service, "next").ended is True
    forward = _navigate(service, "next")
    assert forward.current_candidate_id == d
    assert forward.path == (a, b, END_STEP, d)


def test_ending_again_after_a_jump_keeps_the_visited_step(tmp_path):
    """再次收尾是一次导航，不删除已经访问过的历史。

    「A→B→结束卡→D」在 D 按「下一个」时没有未查看的候选，于是追加一步结束卡；
    D 的访问步骤必须保留（旧实现会截断到第一个结束卡，把 D 删掉）。
    """
    container = _container(tmp_path, "000001 000002")
    service = container.classification
    a, b = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    assert _navigate(service, "next").ended is True
    assert service.browse().state.path == (a, b, END_STEP)

    container.imports.submit_file("b.csv", make_csv("代码", "000006"))
    d = "000006.SZ"
    assert service.focus(d).state.path == (a, b, END_STEP, d)

    ended = _navigate(service, "next")
    assert ended.ended is True
    assert ended.path == (a, b, END_STEP, d, END_STEP)
    assert ended.cursor == 4

    # 回看能回到 D（正是旧实现删掉的那一步），再回看是前一个结束卡
    assert _navigate(service, "previous").current_candidate_id == d
    assert _navigate(service, "previous").ended is True
    # 前进仍沿同一份历史逐步回到 D
    assert _navigate(service, "next").current_candidate_id == d

    # 反复收尾不会无限追加：停在结束卡上再按「下一个」仍是同一步
    assert _navigate(service, "next").ended is True
    again = _navigate(service, "next")
    assert again.ended is True
    assert again.cursor == 4
    assert again.path == (a, b, END_STEP, d, END_STEP)


def test_new_import_does_not_steal_current_or_end_card(tmp_path):
    """新导入不抢当前卡或结束卡；结束卡之后新候选仍可继续。"""
    container = _container(tmp_path, "000001 000002")
    service = container.classification
    a, b = _ids(container)
    service.focus(a)
    assert service.get_view().state.current_candidate_id == a

    container.imports.submit_file("b.csv", make_csv("代码", "000006"))
    # 新导入不抢当前卡
    assert service.get_view().state.current_candidate_id == a
    assert service.browse().state.current_candidate_id == a

    # 走完已有的两只与新导入的那只：进入结束卡
    _navigate(service, "next")
    _navigate(service, "next")
    ended = _navigate(service, "next")
    assert ended.ended is True
    assert ended.path == (a, b, "000006.SZ", END_STEP)
    assert service.browse().state.current_candidate_id is None

    container.imports.submit_file("c.csv", make_csv("代码", "000007"))
    fresh = service.browse()
    # 新导入不抢结束卡：位置与路径原样保留
    assert fresh.state.ended is True
    assert fresh.state.current_candidate_id is None
    assert fresh.state.path == (a, b, "000006.SZ", END_STEP)
    # 新股票参与后续「下一个」：它成为结束卡之后的一步，位置上从结束卡继续
    assert fresh.has_next is True
    advanced = _navigate(service, "next")
    assert advanced.current_candidate_id == "000007.SZ"
    assert advanced.path == (a, b, "000006.SZ", END_STEP, "000007.SZ")

# --- 失效步骤与清理 ---


def test_legacy_end_marker_is_read_back_as_a_path_step(tmp_path):
    """旧库把结束卡存成「游标越过路径 + ended 标记」：读回时归一为路径里的一步。

    这是「兼容现有本地数据」的实际要求：不迁移数据，也不能把已经看完的位置读成
    一张候选卡。
    """
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    with container.db.transaction() as conn:
        conn.execute(
            "update classification_state set path_json = ?, cursor = ?, ended = 1, "
            "current_candidate_id = null where id = 1",
            (json.dumps([a, b]), 2),
        )

    restored = service.browse().state
    assert restored.path == (a, b, END_STEP)
    assert restored.cursor == 2
    assert restored.ended is True
    assert restored.current_candidate_id is None

    # 空路径 + ended 的旧库同样归一为「停在结束卡」
    with container.db.transaction() as conn:
        conn.execute(
            "update classification_state set path_json = '[]', cursor = 0, ended = 1, "
            "current_candidate_id = null where id = 1"
        )
    empty = service.browse().state
    assert empty.path == (END_STEP,)
    assert empty.cursor == 0
    assert empty.ended is True
    assert empty.current_candidate_id is None


def test_missing_history_step_is_skipped_on_read(tmp_path):
    """真正不存在的历史候选被跳过，其他路径位置仍可用。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    _navigate(service, "next")
    assert service.browse().state.path == (a, b, c)

    with container.db.transaction() as conn:
        conn.execute("delete from candidates where candidate_id = ?", (b,))

    # 读回时修补：失效步骤被移除，当前位置跟随它之前的有效步骤
    repaired = service.browse().state
    assert repaired.path == (a, c)
    assert repaired.cursor == 1
    assert repaired.current_candidate_id == c
    # 上一个直接越过失效项，回到 A
    assert _navigate(service, "previous").current_candidate_id == a


def test_deleted_current_step_falls_back_to_range_head(tmp_path):
    """历史候选真正不存在时被跳过；当前目标不存在则按模块规则回到范围内可用位置。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    # 历史中间一项失效：读回时移除该步骤，其他位置仍可用
    service.focus(a)
    _navigate(service, "next")
    _navigate(service, "next")

    with container.db.transaction() as conn:
        conn.execute("delete from candidates where candidate_id = ?", (b,))
    repaired = service.browse()
    assert repaired.state.path == (a, c)
    assert repaired.state.current_candidate_id == c

    # 当前目标本身失效：按「当前对象已不存在」的规则落到范围内首项
    with container.db.transaction() as conn:
        conn.execute("delete from candidates where candidate_id = ?", (c,))
    fallen = service.browse().state
    assert fallen.current_candidate_id == a
    assert fallen.path == (a,)


def test_cleaned_candidate_stays_reviewable_with_latest_state(tmp_path):
    """主动清理但仍存在的候选可沿历史回看其最新状态。"""
    container = _container(tmp_path, "000001 600519")
    service = container.classification
    a, b = _ids(container)
    service.focus(a)
    _navigate(service, "next")
    assert service.browse().state.path == (a, b)

    service.cleanup()

    # 清理把待归类项移出未处理范围：当前筛选（待归类）里已没有可落位的卡片
    assert service.browse().state.current_candidate_id is None
    assert service.get_candidate(a).candidate.state.value == "cleared"

    # 沿历史回看：候选项仍存在，因此历史步骤保留并显示它当下的最新状态
    back = _navigate(service, "previous")
    assert back.current_candidate_id == a
    assert back.path == (a, b)
    assert back.scope.value == "processed"
    assert service.get_candidate(a).candidate.state.value == "cleared"
    assert _navigate(service, "next").current_candidate_id == b


# --- 保存与导航分离、迟到命令 ---


def test_lost_navigation_response_retry_does_not_skip_a_candidate(tmp_path):
    """导航响应丢失后重试不跨过第二只：同一游标与修订号的重复请求只落到同一位置。"""
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    app = create_app(settings, clock)
    with TestClient(app) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001", "600519", "300750"), "text/csv")},
        )
        items = client.get("/api/classification/candidates").json()["candidates"]
        ids = [item["candidateId"] for item in items]
        opened = client.put(
            "/api/classification/view", json={"currentCandidateId": ids[0]}
        ).json()
        cursor = opened["cursor"]
        revision = opened["navigationRevision"]
        client.post(f"/api/classification/candidates/{ids[0]}/dismiss")
        # 动作与导航是两个确认步骤：动作只报事实，导航命令带着当时的游标与修订号
        after_save = client.get("/api/classification/view").json()
        assert after_save["currentCandidateId"] == ids[0]
        assert after_save["cursor"] == cursor
        cursor = after_save["cursor"]
        revision = after_save["navigationRevision"]

        first = client.post(
            "/api/classification/view/navigate",
            json={"direction": "next", "expectedCursor": cursor, "expectedRevision": revision},
        ).json()
        assert first["currentCandidateId"] == ids[1]

        # 响应丢失：浏览器用同一游标与修订号重试，仍落在同一只，不跨过第二只
        retry = client.post(
            "/api/classification/view/navigate",
            json={"direction": "next", "expectedCursor": cursor, "expectedRevision": revision},
        ).json()
        assert retry["currentCandidateId"] == ids[1]
        assert retry["cursor"] == first["cursor"]

        # 迟到且游标已过期的旧命令不能覆盖更新的用户选择
        chosen = client.post(f"/api/classification/candidates/{ids[2]}/focus").json()
        assert chosen["currentCandidateId"] == ids[2]
        late = client.post(
            "/api/classification/view/navigate",
            json={"direction": "next", "expectedCursor": cursor, "expectedRevision": revision},
        ).json()
        assert late["currentCandidateId"] == ids[2]


def test_manual_jump_cancels_pending_automatic_navigation(tmp_path):
    """手动打开 D 会取消待重试的自动导航：旧命令只读回 D，不重复前进。"""
    container = _container(tmp_path)
    service = container.classification
    a, b, c = _ids(container)
    service.focus(a)

    # 归类保存成功（独立确认），随后本页的自动导航尚未发出
    outcome = service.dismiss(a)
    assert outcome.removed == (a,)
    stale = service.get_view().state

    # 用户手动打开 B（已处理项也可打开，只是不进入待归类）：取消待重试的自动导航
    manual = service.focus(b)
    assert manual.state.current_candidate_id == b
    manual_cursor = manual.state.cursor
    manual_revision = manual.state.navigation_revision

    # 迟到的自动导航带着保存时的旧游标与修订号：只回报当前位置，不前进
    late = service.navigate(
        "next", expected_cursor=stale.cursor, expected_revision=stale.navigation_revision
    )
    assert late.view.state.current_candidate_id == b
    assert late.view.state.cursor == manual_cursor
    # 用当前修订号重试仍沿着新路径前进到下一步，不重复写入归类结果
    again = service.navigate(
        "next",
        expected_cursor=late.view.state.cursor,
        expected_revision=late.view.state.navigation_revision,
    )
    assert again.view.state.current_candidate_id == c
    # 归类结果只写入一次（迟到的旧命令不重复写入）
    dismissals = [
        entry for entry in service.get_candidate(a).candidate.history
        if entry.action == "dismissed"
    ]
    assert len(dismissals) == 1
