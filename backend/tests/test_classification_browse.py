"""统一归类浏览结果与轻量候选列表（浏览内核第一张工单）。

覆盖：一次读取形成一致的当前卡＋完整轻量列表＋数量；列表装配不做逐项详细读取；
普通切卡不重传整份列表；列表版本在任何改变列表的写入中同事务推进；
路径末端寻找下一只由数据库完成。
真实临时 SQLite 与真实证券库快照，不注入伪仓储。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.repository import classification_repo
from dailyscreen_lite.repository.database import Database


class CountingDatabase(Database):
    """记录一次操作里真正发出的 SQL，用于验证列表装配没有逐候选详细读取。"""

    def __init__(self, path) -> None:
        super().__init__(path)
        self.queries: list[str] = []

    def connect(self) -> sqlite3.Connection:
        conn = super().connect()
        conn.set_trace_callback(self.queries.append)
        return conn


def _container(tmp_path, codes: str):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)
    container.imports.submit_file("a.csv", make_csv("代码", *codes.split()))
    return container


def test_browse_returns_current_card_list_and_counts_together(tmp_path):
    container = _container(tmp_path, "000001 600519 300750")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})

    result = service.browse()

    # 当前卡与左侧列表在同一份结果里，指向同一时刻的真实状态
    assert result.current is not None
    assert result.current.candidate.candidate_id == head
    assert [row.candidate_id for row in result.rows] == [
        v.candidate.candidate_id for v in service.list_candidates()
    ]
    assert [row.candidate_id for row in result.pending] == [
        v.candidate.candidate_id for v in service.list_candidates()
    ]
    assert result.dates == ("2026-09-11",)
    assert result.summary["unprocessed"] == 3
    assert result.round["remaining"] == 3
    assert result.state.list_revision >= 1
    with container.db.read() as conn:
        assert classification_repo.list_revision(conn) == result.state.list_revision
    # 列表行带用户可见字段，不带逐项详细资料
    row = result.rows[0]
    assert row.security is not None and row.security.name
    assert row.latest_import_date == "2026-09-11"
    assert row.source_count == 1
    assert row.observed is False


SAMPLE_CODES = (
    "000001 000002 000004 000005 000006 000007 000008 000009 000010 "
    "000011 000012 000014 000016 000017 000018 000019 000020 000021 000023 000025"
)


def test_browse_assembles_list_without_per_candidate_detail_reads(tmp_path):
    container = _container(tmp_path, SAMPLE_CODES)
    spied = CountingDatabase(container.db.path)
    container.classification._db = spied  # 同一真实库，只加一层 SQL 计数

    spied.queries.clear()
    result = container.classification.browse()

    assert len(result.rows) >= 15
    assert len(result.pending) == len(result.rows)
    # 逐候选详细读取会让这些查询随行数增长；列表装配只允许固定次数的批量读取。
    # （列表投影把「已观察」压进子查询，因此不出现逐行的关系表读取。）
    per_row = [
        sql
        for sql in spied.queries
        if re.search(r"from\s+(notes|candidate_history)\b", sql)
    ]
    assert per_row == [], per_row


def test_navigate_returns_only_card_and_delta_not_whole_list(tmp_path):
    container = _container(tmp_path, "000001 600519 300750")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})
    state = service.get_view().state

    result = service.navigate(
        "next",
        expected_cursor=state.cursor,
        expected_revision=state.navigation_revision,
    )

    # 切卡响应只有新卡片与浏览状态，没有整份列表
    assert not hasattr(result, "rows")
    assert result.view.state.current_candidate_id != head
    # 标记已查看改变了列表行（查看时间），因此响应带上该行：
    # 行变化不推进列表版本，前端据此更新列表而不重读整份列表。
    assert result.delta.changed
    changed = result.delta.changed[0]
    assert changed.candidate_id == result.view.state.current_candidate_id
    assert changed.viewed_at is not None
    assert result.delta.revision == result.view.state.list_revision == state.list_revision


def test_list_revision_advances_with_every_list_write(tmp_path):
    container = _container(tmp_path, "000001 600519")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})
    baseline = service.browse().state.list_revision

    # 归类动作、队列重排与再次入选都必须推进版本（同事务），前端据此判断列表过期
    outcome = service.dismiss(head)
    assert outcome.revision > baseline
    assert service.browse().state.list_revision == outcome.revision

    reclassified = service.reclassify(head)
    assert reclassified.revision > outcome.revision

    container.imports.submit_file("b.csv", make_csv("代码", "000002"))
    after_import = service.browse().state.list_revision
    assert after_import > reclassified.revision

    # 纯浏览与已查看时间变化不推进版本：它们不改变列表成员与顺序
    view = service.update_view({"viewMode": "list"})
    assert view.state.list_revision == after_import
    before_nav = service.browse().state.list_revision
    command = service.navigate(
        "next",
        expected_cursor=view.state.cursor,
        expected_revision=view.state.navigation_revision,
    )
    assert command.delta.revision == before_nav


def test_browse_rows_reflect_state_order_and_filters(tmp_path):
    container = _container(tmp_path, "000001 600519 300750")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})
    service.later(head)

    opened = service.browse()
    # 稍后处理移到队尾：轻量列表与服务端队列顺序一致
    assert [row.security.name for row in opened.rows] == ["宁德时代", "贵州茅台", "平安银行"]
    assert opened.rows[-1].state.value == "later"

    service.dismiss(head)
    after = service.browse()
    # 已处理项离开待归类列表：页签随之落到它真实所在的范围（已处理），
    # 左侧列表与卡片属于同一份结果；未处理池只剩另外两只。
    assert after.state.scope.value == "processed"
    assert [row.candidate_id for row in after.rows] == [head]
    assert [row.candidate_id for row in after.pending] == [
        row.candidate_id for row in opened.rows if row.candidate_id != head
    ]


def test_next_candidate_is_picked_in_the_database(tmp_path):
    container = _container(tmp_path, "000001 600519 300750")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})
    state = service.get_view().state

    spied = CountingDatabase(container.db.path)
    service._db = spied
    spied.queries.clear()
    result = service.navigate(
        "next",
        expected_cursor=state.cursor,
        expected_revision=state.navigation_revision,
    )

    assert result.view.state.current_candidate_id == "300750.SZ"
    # 末端寻找下一只：一条选择查询（游标移动后仍带 limit 1），
    # 而不是把整份列表取到应用层再逐项扫描。
    selectors = [
        sql
        for sql in spied.queries
        if "from candidates c" in sql and "limit 1" in sql.lower()
    ]
    assert selectors, spied.queries
    # 换卡只读取目标那一张卡的详细资料，不为整份列表逐项装配
    history_reads = [
        sql for sql in spied.queries if re.search(r"from\s+candidate_history\b", sql)
    ]
    assert len(history_reads) == 1


def test_write_response_carries_list_delta_and_version(tmp_path):
    """归类写入返回服务端算好的列表变化与版本：前端不必为一次动作重读整份结果。"""
    container = _container(tmp_path, "000001 600519")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})

    # 稍后处理：仍在待归类列表里，不产生删除；队列因此重排，因此下发写入后的真实行序
    later = service.later(head)
    assert later.removed == ()
    assert later.revision == service.browse().state.list_revision
    assert later.order == tuple(
        row.candidate_id for row in service.browse().rows
    )
    assert later.order[-1] == head

    # 暂不关注：离开待归类列表，服务端指出被移除的那一行；行序里不再含这一只
    dismissed = service.dismiss(head)
    assert dismissed.removed == (head,)
    assert dismissed.revision > later.revision
    assert head not in dismissed.order

    # 重新归类：回到待归类列表，同样不产生删除
    back = service.reclassify(head)
    assert back.removed == ()

    # 观察动作走同一协议（观察完成本次归类，未处理项离开待归类列表）
    container.classification.update_view({"currentCandidateId": head})
    observed = container.observations.observe_candidate(
        head, [container.observations.list_groups()[0].group_id]
    )
    assert observed.removed == (head,)
    assert observed.list_revision == container.classification.browse().state.list_revision

    # HTTP 响应把行序与列表版本一起下发（每个归类动作都带 listOrder 字段）：
    # 这里先把观察动作送进待归类池的那一只重新归类，再对它执行「稍后处理」。
    with TestClient(
        create_app(offline_settings(tmp_path), FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    ) as client:
        head = client.get("/api/classification/candidates").json()["candidates"][0][
            "candidateId"
        ]
        client.post(f"/api/classification/candidates/{head}/reclassify")
        client.post("/api/imports/csv", files={"file": ("b.csv", make_csv("代码", "300750"), "text/csv")})
        ids = [
            row["candidateId"]
            for row in client.get("/api/classification/candidates").json()["candidates"]
        ]
        assert len(ids) >= 2, ids
        view_before = client.get(
            "/api/classification/view", params={"scope": "unprocessed"}
        ).json()
        payload = client.post(f"/api/classification/candidates/{ids[0]}/later").json()
        assert payload["listRevisionBefore"] == view_before["listRevision"]
        assert payload["listRevision"] == view_before["listRevision"] + 1
        assert payload["listOrder"][-1] == ids[0]
        assert payload["listOrder"] == [
            row["candidateId"]
            for row in client.get(
                "/api/classification/view", params={"scope": "unprocessed"}
            ).json()["rows"]
        ]

        # 普通归类（移出待归类池）不下发整份行序：这次变化由 listRemoved 表达，
        # 前端只删掉那一行，不需要整列重排；写入前后的版本仍然照发。
        plain_before = client.get(
            "/api/classification/view", params={"scope": "unprocessed"}
        ).json()
        plain = client.post(f"/api/classification/candidates/{ids[1]}/dismiss").json()
        assert plain["listRevisionBefore"] == plain_before["listRevision"]
        assert plain["listRevision"] == plain_before["listRevision"] + 1
        assert plain["listRemoved"] == [ids[1]]
        assert plain["listOrder"] == []


def test_ordinary_classification_reports_versions_without_the_whole_order(tmp_path):
    """普通归类不下发整份行序：只有真的移动位置的动作用得着写入前后的行序。

    写入前的列表版本随每次动作下发，供前端判断自己手上的列表是否已经落后于
    另一个入口的写入；剩余行的相对顺序没变时不需要 `order`（前端只删掉那一行）。
    """
    container = _container(tmp_path, "000001 600519 300750")
    service = container.classification
    head = service.list_candidates()[0].candidate.candidate_id
    service.update_view({"currentCandidateId": head})

    before = service.browse().state.list_revision
    dismissed = service.dismiss(head)
    assert dismissed.revision_before == before
    assert dismissed.revision == before + 1
    assert dismissed.removed == (head,)
    assert dismissed.order == ()

    # 「稍后处理」移尾：写前版本照旧给出，移动了位置才给出写入后的真实行序
    remaining = [row.candidate.candidate_id for row in service.list_candidates()]
    assert len(remaining) >= 2, remaining
    before_later = service.browse().state.list_revision
    later = service.later(remaining[0])
    assert later.revision_before == before_later
    assert later.removed == ()
    assert later.order
    assert later.order[-1] == remaining[0]
    assert later.order[0] == remaining[1]


def test_list_revision_migration_adds_column_to_old_db(tmp_path):
    container = _container(tmp_path, "000001")
    with container.db.transaction() as conn:
        conn.execute("alter table classification_state drop column list_revision")

    reopened = build_container(
        offline_settings(tmp_path), FixedClock(datetime(2026, 9, 11, 11, 0, 0))
    )
    assert reopened.classification.browse().state.list_revision == 0
