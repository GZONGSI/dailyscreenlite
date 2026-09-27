"""候选归类浏览路径：真实 SQLite、HTTP 与重启恢复。"""

from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.container import build_container
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock
from dailyscreen_lite.domain.models import END_STEP


def _navigate(service, direction: str):
    state = service.get_view().state
    return service.navigate(
        direction,
        expected_cursor=state.cursor,
        expected_revision=state.navigation_revision,
    ).view


def test_navigation_path_and_end_card_survive_restart(tmp_path):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    app = create_app(settings, clock)
    with TestClient(app) as client:
        client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001", "300750", "600519"), "text/csv")},
        )
        ids = [item["candidateId"] for item in client.get("/api/classification/candidates").json()["candidates"]]
        first = client.put("/api/classification/view", json={"currentCandidateId": ids[0]}).json()
        assert first["path"] == [ids[0]]
        assert first["cursor"] == 0
        first_revision = first["navigationRevision"]
        missing_revision = client.post(
            "/api/classification/view/navigate",
            json={"direction": "next", "expectedCursor": 0},
        )
        assert missing_revision.status_code == 400
        assert client.get("/api/classification/view").json()["currentCandidateId"] == ids[0]

        client.post(f"/api/classification/candidates/{ids[0]}/dismiss")
        second = client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 0, "expectedRevision": first_revision + 1}).json()
        assert second["currentCandidateId"] == ids[1]
        assert second["path"] == ids[:2]
        assert second["scope"] == "unprocessed"
        assert client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 0, "expectedRevision": first_revision + 1}).json()["currentCandidateId"] == ids[1]

        back = client.post("/api/classification/view/navigate", json={"direction": "previous", "expectedCursor": 1, "expectedRevision": second["navigationRevision"]}).json()
        assert back["currentCandidateId"] == ids[0]
        assert back["currentCandidate"]["state"] == "dismissed"
        assert back["scope"] == "processed"
        # 游标又回到 0 后，迟到的旧请求仍不得重复推进。
        replay = client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 0, "expectedRevision": first_revision + 1}).json()
        assert replay["currentCandidateId"] == ids[0]
        forth = client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 0, "expectedRevision": back["navigationRevision"]}).json()
        assert forth["currentCandidateId"] == ids[1]
        third = client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 1, "expectedRevision": forth["navigationRevision"]}).json()
        assert third["currentCandidateId"] == ids[2]
        ended = client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 2, "expectedRevision": third["navigationRevision"]}).json()
        assert ended["ended"] is True
        # 结束卡也是路径里的一步（`~end`），因此可回看、可跨重启恢复
        assert ended["cursor"] == 3
        assert ended["currentCandidateId"] is None
        assert ended["path"] == [*ids, END_STEP]
        assert client.post("/api/classification/view/navigate", json={"direction": "next", "expectedCursor": 3, "expectedRevision": ended["navigationRevision"]}).json()["ended"] is True

    with TestClient(create_app(settings, clock)) as client:
        restored = client.get("/api/classification/view").json()
        assert restored["ended"] is True
        assert restored["path"] == [*ids, END_STEP]
        # 本轮已看过全部候选项且没有前进历史：结束卡没有可去的下一步
        assert restored["hasNext"] is False
        previous = client.post("/api/classification/view/navigate", json={"direction": "previous", "expectedCursor": 3, "expectedRevision": restored["navigationRevision"]}).json()
        assert previous["currentCandidateId"] == ids[2]
        returned = client.post("/api/classification/view/return").json()
        assert returned["path"] == [ids[1]]
        assert returned["currentCandidateId"] == ids[1]
        assert returned["ended"] is False


def test_filter_path_later_and_reclassify(tmp_path):
    settings = offline_settings(tmp_path)
    service = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0))).classification
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "300750", "600519"))
    ids = [v.candidate.candidate_id for v in service.list_candidates()]
    service.update_view({"currentCandidateId": ids[0]})
    service.later(ids[0])
    view = _navigate(service, "next")
    assert view.state.current_candidate_id == ids[1]
    assert _navigate(service, "previous").state.current_candidate_id == ids[0]
    assert service.get_view().state.scope.value == "unprocessed"
    service.dismiss(ids[0])
    service.reclassify(ids[0])
    assert service.get_view().state.path == tuple(ids[:2])
    assert service.get_view().state.scope.value == "unprocessed"
    # 回看后可继续沿原路径前进到第二只
    assert _navigate(service, "next").state.current_candidate_id == ids[1]
    # 主动改搜索开启新一轮：原当前卡不匹配新搜索，落到新范围首项
    edited = service.update_view({"search": "贵州"})
    assert edited.state.search == "贵州"
    assert edited.state.current_candidate_id == ids[2]
    assert edited.state.path == (ids[2],)
    assert _navigate(service, "next").state.ended is True
    assert service.get_view().round["remaining"] == 3


def test_old_classification_state_migration_keeps_filter_scope(tmp_path):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)
    container.classification.update_view({"scope": "processed", "search": "平安"})
    with container.db.transaction() as conn:
        for name in ("filter_scope", "path_json", "cursor", "ended", "navigation_revision"):
            conn.execute(f"alter table classification_state drop column {name}")
    migrated = build_container(settings, clock).classification.get_view().state
    assert migrated.scope.value == "processed"
    assert migrated.filter_scope.value == "processed"
    assert migrated.search == "平安"
    assert migrated.path == ()
    assert migrated.cursor == -1
    assert migrated.ended is False


def test_new_import_joins_path_tail_and_missing_history_is_skipped(tmp_path):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 10, 0, 0))
    container = build_container(settings, clock)
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "300750"))
    ids = [v.candidate.candidate_id for v in container.classification.list_candidates()]
    service = container.classification
    service.update_view({"currentCandidateId": ids[0]})
    _navigate(service, "next")
    _navigate(service, "next")
    assert service.get_view().state.ended is True

    container.imports.submit_file("b.csv", make_csv("代码", "600519"))
    assert service.get_view().state.ended is True
    # 新导入不抢结束卡；从结束卡按「下一个」落到新导入的候选（它成为结束卡之后
    # 的一步），回看仍能回到结束卡。
    assert service.browse().has_next is True
    fresh = _navigate(service, "next")
    assert fresh.state.current_candidate_id == "600519.SH"
    assert fresh.state.path == (*ids, END_STEP, "600519.SH")
    assert _navigate(service, "previous").state.ended is True

    # 历史项不再存在时，读回与后退都直接越过它，其他路径位置仍可用。
    with container.db.transaction() as conn:
        conn.execute("delete from candidates where candidate_id = ?", (ids[0],))
    previous = _navigate(service, "previous")
    assert previous.state.current_candidate_id == ids[1]
    assert previous.state.path == (ids[1], END_STEP, "600519.SH")


def test_filter_edit_after_reclassify_clears_processed_result(tmp_path):
    settings = offline_settings(tmp_path)
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "600519"))
    service = container.classification
    first, second = [v.candidate.candidate_id for v in service.list_candidates()]
    service.dismiss(first)
    service.update_view({"scope": "processed", "result": "dismissed", "currentCandidateId": first})
    service.reclassify(first)

    # 回看路径保留原已处理结果筛选，但用户改搜索时新路径不能继续带着“暂不关注”的
    # 已处理结果筛选；新范围仍含原当前卡（它已重新归类为待归类）就保留它。
    new_path = service.update_view({"search": "贵州"})
    # 筛选范围跟着当前展示的页签更新（改搜索时以用户眼前这一栏开始新一轮），
    # 保留下来的当前卡是待归类，因此展示页签与筛选范围都落在待归类。
    assert new_path.state.scope.value == "unprocessed"
    assert new_path.state.filter_scope.value == "unprocessed"
    assert new_path.state.result is None
    assert new_path.state.current_candidate_id == second
    assert new_path.state.path == (second,)


def test_filter_edit_after_history_backtrack_starts_from_visible_tab(tmp_path):
    settings = offline_settings(tmp_path)
    service = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0))).classification
    container = build_container(settings, FixedClock(datetime(2026, 9, 11, 10, 0, 0)))
    container.imports.submit_file("a.csv", make_csv("代码", "000001", "300750"))
    ids = [v.candidate.candidate_id for v in service.list_candidates()]
    service.update_view({"currentCandidateId": ids[0]})
    service.dismiss(ids[0])
    _navigate(service, "next")
    history = _navigate(service, "previous")
    assert history.state.scope.value == "processed"
    assert history.state.filter_scope.value == "unprocessed"

    edited = service.update_view({"search": "平安"})
    assert edited.state.scope.value == "processed"
    assert edited.state.filter_scope.value == "processed"
    # 当前卡仍属于新范围（这条“暂不关注”记录本身）：卡片与列表保持一致，
    # 而不是留下一个没有卡片的空视图
    assert edited.state.current_candidate_id == ids[0]
    assert edited.state.path == (ids[0],)
