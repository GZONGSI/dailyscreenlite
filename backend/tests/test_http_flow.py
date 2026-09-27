"""HTTP + 持久化集成测试：真实 FastAPI 应用 + 临时真实数据库 + 重启读回。

通过公共 HTTP 边界进入，验证导入 → 股票卡 → 暂不关注 → 关闭重开容器后读回一致。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_csv, offline_settings
from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.domain.clock import FixedClock


@pytest.fixture
def settings_and_clock(tmp_path: Path):
    settings = offline_settings(tmp_path)
    clock = FixedClock(datetime(2026, 9, 11, 9, 5, 0))
    return settings, clock


def test_health_reports_securities_library(settings_and_clock):
    settings, clock = settings_and_clock
    with TestClient(create_app(settings, clock)) as client:
        body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["securitiesLoaded"] is True
    assert body["securitiesCount"] == 5551
    assert body["importDate"] == "2026-09-11"


def test_upload_csv_publishes_card_and_rejects_unsupported(settings_and_clock):
    settings, clock = settings_and_clock
    with TestClient(create_app(settings, clock)) as client:
        response = client.post(
            "/api/imports/csv",
            files={"file": ("candidates.csv", make_csv("代码,名称", "000001,平安银行", "999999,未知"), "text/csv")},
        )
        assert response.status_code == 200
        batch = response.json()
        assert batch["status"] == "published"
        assert batch["recognizedCount"] == 1
        assert batch["skippedCount"] == 1
        assert batch["importDate"] == "2026-09-11"

        items = client.get("/api/classification/candidates").json()
        assert len(items["candidates"]) == 1
        card = items["candidates"][0]
        assert card["security"]["name"] == "平安银行"
        # 行情经 /api/quotes 读取；未取到时明确缺失，不伪造零值
        quote = client.get("/api/quotes/000001.SZ").json()
        assert quote["available"] is False
        assert quote["reason"] == "行情暂未取得"
        assert quote["bars"] == []

        bad = client.post(
            "/api/imports/csv",
            files={"file": ("data.xlsx", b"whatever", "application/vnd.ms-excel")},
        )
        assert bad.status_code == 400
        assert bad.json()["detail"]["code"] == "unsupported_format"


def test_dismiss_stays_on_item_then_restart_reads_back(settings_and_clock):
    settings, clock = settings_and_clock
    app = create_app(settings, clock)
    with TestClient(app) as client:
        batch = client.post(
            "/api/imports/csv",
            files={"file": ("a.csv", make_csv("代码", "000001"), "text/csv")},
        ).json()
        candidate_id = client.get("/api/classification/candidates").json()["candidates"][0]["candidateId"]

        dismissed = client.post(f"/api/classification/candidates/{candidate_id}/dismiss").json()
        assert dismissed["state"] == "dismissed"
        # 处理动作不自动跳转：现在未处理池为空
        assert client.get("/api/classification/candidates").json()["candidates"] == []
        processed = client.get("/api/classification/candidates", params={"scope": "processed"}).json()
        assert [i["candidateId"] for i in processed["candidates"]] == [candidate_id]

    # 重启：新容器 / 新应用实例，同一数据库文件
    app2 = create_app(settings, clock)
    with TestClient(app2) as client2:
        processed = client2.get("/api/classification/candidates", params={"scope": "processed"}).json()
        assert [i["candidateId"] for i in processed["candidates"]] == [candidate_id]
        assert processed["candidates"][0]["state"] == "dismissed"

        batch_readback = client2.get(f"/api/imports/{batch['batchId']}").json()
        assert batch_readback["status"] == "published"
        assert batch_readback["recognizedCount"] == 1
        assert batch_readback["importDate"] == "2026-09-11"


def test_duplicate_submission_via_http_does_not_duplicate(settings_and_clock):
    settings, clock = settings_and_clock
    with TestClient(create_app(settings, clock)) as client:
        first = client.post(
            "/api/imports/csv", files={"file": ("a.csv", make_csv("代码", "600519"), "text/csv")}
        ).json()
        second = client.post(
            "/api/imports/csv", files={"file": ("b.csv", make_csv("代码", "600519"), "text/csv")}
        ).json()
        assert first["newCandidateCount"] == 1
        # 同一天重复提交：只追加来源，既不新建也不再次触发归类
        assert second["newCandidateCount"] == 0 and second["mergedCandidateCount"] == 0
        candidates = client.get("/api/classification/candidates").json()["candidates"]
        assert len(candidates) == 1
        assert len(candidates[0]["sourceBatchIds"]) == 2
        assert [d for d in candidates[0]["importDates"]] == ["2026-09-11"]


def test_batch_list_and_detail_are_http_readable(settings_and_clock):
    """页面重启后靠这两个接口读回导入事实：列表用于回退，详情用于统计与跳过明细。"""
    settings, clock = settings_and_clock
    with TestClient(create_app(settings, clock)) as client:
        batch = client.post(
            "/api/imports/csv",
            files={
                "file": (
                    "c.csv",
                    make_csv("代码,名称", "000001,平安银行", "999999,未知"),
                    "text/csv",
                )
            },
        ).json()

        listed = client.get("/api/imports").json()["batches"]
        assert [b["batchId"] for b in listed] == [batch["batchId"]]
        # 列表不含明细
        assert "stocks" not in listed[0]

        detail = client.get(f"/api/imports/{batch['batchId']}").json()
        assert detail["recognizedCount"] == 1
        assert detail["skippedCount"] == 1
        assert detail["uniqueCount"] == 2
        skipped = [s for s in detail["stocks"] if s["outcome"] == "skipped"]
        assert skipped[0]["rawCode"] == "999999"
        assert [s["position"] for s in detail["stocks"]] == ["第2行", "第3行"]


def test_unknown_scope_is_rejected(settings_and_clock):
    settings, clock = settings_and_clock
    with TestClient(create_app(settings, clock)) as client:
        response = client.get("/api/classification/candidates", params={"scope": "nonsense"})
    assert response.status_code == 422
