"""候选归类浏览的隔离数据基准（个人使用上限）。

用途：在**隔离数据目录**里构造与生产同形的数据（全部已识别证券同时待归类，
并叠加多年每日入选、来源与处理历史以及较长浏览路径），测量统一浏览读取与
普通切卡、筛选、路径末端选择的耗时与响应量，并同时量一组**旧接口组合**的代价
（`GET /view` + 两次 `GET /candidates`，即改造前工作区打开一次要发的请求），
作为同一次运行内的对照列，不做跨代码版本的对比。

另外测一项**真实归类动作**（经 HTTP `POST`，不直接调服务层）：「暂不关注」把候选项
移出未处理队列、「稍后处理」把它移到队尾，两者都会改变队列顺序，因此记录耗时、
响应体字节数，以及响应里 `listOrder` 的长度，与动作前后的未处理池只数对照，
用来回答「普通移出队列是否把剩余全体的顺序一并下发」。动作会改变未处理池，
所以放在所有读取测量之后；数据目录是本次运行新建的临时隔离目录（`--data-dir`
播种模式同理），重复运行不写真实 `data/runtime`。

只读交付资源、不访问外网、不修改仓库内数据库：

    backend\\.venv\\Scripts\\python.exe backend\\tools\\benchmark_classification_browse.py
    backend\\.venv\\Scripts\\python.exe backend\\tools\\benchmark_classification_browse.py --securities 5551 --days 750

`--path` 是**实际写入浏览路径的步数**（上限为证券数）：报告的 `path_steps` 记的就是
它，避免参数写 200 步而库里存了全体 5,551 步这种报告与事实不符。

**静态代码风险与实测结论分开陈述**：本脚本给出的是实测数字；"曾经存在两份列表
读取、逐候选装配、末端全列表查找"属于改造前的代码形态，不是本脚本的结论。
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dailyscreen_lite.app.container import build_container  # noqa: E402
from dailyscreen_lite.app.main import create_app  # noqa: E402
from dailyscreen_lite.domain.clock import FixedClock  # noqa: E402
from dailyscreen_lite.domain.models import (  # noqa: E402
    BatchStatus,
    Candidate,
    CandidateState,
    ImportBatch,
    ImportDate,
    SourceKind,
)
from dailyscreen_lite.repository import (  # noqa: E402
    classification_repo,
    imports_repo,
)
from dailyscreen_lite.settings import Settings  # noqa: E402

REPO_ROOT = BACKEND_ROOT.parent
SNAPSHOT = REPO_ROOT / "data" / "securities" / "initial_snapshot.json"
LIVE_SOURCE_ENV = (
    "DSLITE_QUOTES_FIXTURE",
    "DSLITE_MARKET_STATUS",
    "DSLITE_UPDATE_SCHEDULE",
)


@dataclass
class Timing:
    """一次操作的耗时样本与响应字节数。"""

    label: str
    seconds: list[float] = field(default_factory=list)
    byte_samples: list[int] = field(default_factory=list)

    def report(self) -> dict:
        return {
            "次数": len(self.seconds),
            "p50_ms": round(statistics.median(self.seconds) * 1000, 2),
            "p95_ms": round(_percentile(self.seconds, 0.95) * 1000, 2),
            "max_ms": round(max(self.seconds) * 1000, 2),
            **(
                {
                    "响应字节_p50": int(statistics.median(self.byte_samples)),
                    "响应字节_max": max(self.byte_samples),
                }
                if self.byte_samples
                else {}
            ),
        }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[index]


def build_settings(data_dir: Path) -> Settings:
    """隔离数据目录 + 离线来源：基准不访问外网，也不启动定时更新。"""
    (data_dir / "securities").mkdir(parents=True, exist_ok=True)
    fixture = data_dir / "empty_quotes.json"
    fixture.write_text("{}", encoding="utf-8")
    return Settings(
        data_dir=data_dir,
        securities_snapshot=SNAPSHOT,
        quotes_fixture=fixture,
        update_schedule_enabled=False,
    )


def load_security_ids(limit: int | None) -> list[str]:
    """交付证券库快照里的证券标识；`limit` 为 None 时取全部（个人使用上限）。"""
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    ids: list[str] = []
    for record in snapshot.get("securities") or []:
        code = record.get("code")
        exchange = record.get("exchange")
        if not code or not exchange:
            continue
        ids.append(f"{code}.{exchange}")
        if limit is not None and len(ids) >= limit:
            break
    return ids


def seed(
    container,
    security_ids: list[str],
    *,
    days: int,
    history_rounds: int,
    path_steps: int | None = None,
) -> dict:
    """构造满量数据：全部待归类 + 多年每日入选/来源 + 处理历史。

    同时把浏览上下文落到满量数据的起点（较长的浏览路径与一只当前卡），使浏览器侧
    基准打开工作区就能看到真实规模的队列与卡片，而不是空状态。

    浏览路径**只写 `min(path_steps, len(security_ids))` 步**：报告参数写 `path: 200`
    时，库里 `path_json` 就必须真的是 200 步，不能悄悄写成全体 5,551 步。`path_steps`
    缺省（None）时沿用旧行为写全体。清单第一个证券作为当前卡；`--data-dir` 播种模式
    写出的这一份数据只给基准用，不进入真实 `data/runtime`。
    """
    now = container.clock.now()
    start = now - timedelta(days=days)
    requested_steps = len(security_ids) if path_steps is None else max(0, int(path_steps))
    written_steps = min(requested_steps, len(security_ids))
    with container.db.transaction() as conn:
        # 每个自然日一个来源批次：多年每日入选都要有可追溯的来源
        day_batches: list[tuple[str, str]] = []
        for day_index in range(days):
            import_day = (start + timedelta(days=day_index)).date().isoformat()
            batch_id = f"bench-{import_day}"
            imports_repo.insert_batch(
                conn,
                ImportBatch(
                    batch_id=batch_id,
                    source_kind=SourceKind.TEXT,
                    source_name=f"基准来源 {import_day}",
                    source_ref=None,
                    archive_path=None,
                    received_at=start + timedelta(days=day_index),
                    import_date=ImportDate(date.fromisoformat(import_day)),
                    status=BatchStatus.PUBLISHED,
                    declared_total=len(security_ids),
                    parsed_count=len(security_ids),
                    unique_count=len(security_ids),
                    recognized_count=len(security_ids),
                    skipped_count=0,
                    new_candidate_count=len(security_ids),
                    merged_candidate_count=0,
                    reopened_candidate_count=0,
                    error_code=None,
                    error_message=None,
                ),
                (start + timedelta(days=day_index)).isoformat(),
            )
            day_batches.append((import_day, batch_id))

        for index, security_id in enumerate(security_ids):
            classification_repo.insert_candidate(
                conn,
                Candidate(
                    candidate_id=security_id,
                    security_id=security_id,
                    state=CandidateState.PENDING,
                    first_seen_at=start,
                    last_action_at=None,
                    action_result=None,
                ),
                queue_order=index,
            )
            for import_day, batch_id in day_batches:
                classification_repo.record_selection(
                    conn, security_id, import_day, batch_id, now.isoformat()
                )
                classification_repo.link_source(
                    conn, security_id, batch_id, import_day, now.isoformat()
                )
            for round_index in range(history_rounds):
                classification_repo.record_history(
                    conn,
                    security_id,
                    action="dismissed" if round_index % 2 else "reclassified",
                    from_state=CandidateState.PENDING,
                    to_state=CandidateState.DISMISSED,
                    acted_at=start + timedelta(hours=round_index),
                )
        classification_repo.bump_list_revision(conn)
        # 浏览上下文：满量数据里的第一只作为当前卡，`--path` 步作为它的回看历史。
        # 步数按 `--path` 如实落库（最多全体），报告与事实因此一致。
        state = classification_repo.get_state(conn)
        state.path = tuple(security_ids[:written_steps])
        state.cursor = 0
        state.current_candidate_id = security_ids[0] if security_ids else None
        classification_repo.save_state(conn, state, now.isoformat())
    return {
        "候选数": len(security_ids),
        "每日入选条数": len(security_ids) * days,
        "每日入选天数": days,
        "每候选处理历史": history_rounds,
        # `--path` 请求的步数与实际写入 `path_json` 的步数分开记录：满量数据只有
        # 5,551 只证券，参数块里的 path_steps 必须是真写进去的步数。
        "path_steps": written_steps,
        "path_steps_requested": requested_steps,
        "path_steps_capped": written_steps != requested_steps,
    }


def time_call(store: Timing, call) -> object:
    started = time.perf_counter()
    result = call()
    store.seconds.append(time.perf_counter() - started)
    return result


def pending_count(client) -> int:
    """未处理池只数：取 `GET /api/classification/view` 的 `pending` 行数。

    浏览结果的 `pending` 与归类动作的 `listOrder` 都按未处理状态（`PENDING`/`LATER`）
    取全池；基准里日期与搜索筛选都是空，因此两者同口径，可以直接对照。
    """
    payload = client.get("/api/classification/view").json()
    rows = payload.get("pending")
    if isinstance(rows, list):
        return len(rows)
    return int((payload.get("summary") or {}).get("pending", 0))


def measure_action(client, *, endpoint: str, note: str, candidates: list[str]) -> dict:
    """经 HTTP 执行真实归类动作，记录耗时、响应字节数与响应里 `listOrder` 的长度。

    评审关注「普通移出队列是否把剩余全体的顺序一并下发」：这里把 `listOrder` 长度
    与动作前后的未处理池只数一起报出来，只陈述事实，不做断言（改造后 `listOrder`
    可能变空或变成增量，两种形态都能从这组字段读出来）。
    """
    timing = Timing(endpoint)
    statuses: list[int] = []
    has_order: list[bool] = []
    order_lengths: list[int] = []
    pool_before: list[int] = []
    pool_after: list[int] = []
    for candidate_id in candidates:
        pool_before.append(pending_count(client))
        started = time.perf_counter()
        response = client.post(f"/api/classification/candidates/{candidate_id}/{endpoint}")
        timing.seconds.append(time.perf_counter() - started)
        timing.byte_samples.append(len(response.content))
        statuses.append(response.status_code)
        payload = response.json()
        order = (payload.get("listOrder") or []) if isinstance(payload, dict) else []
        has_order.append(isinstance(payload, dict) and "listOrder" in payload)
        order_lengths.append(len(order))
        pool_after.append(pending_count(client))
    return {
        "说明": note,
        "端点": f"POST /api/classification/candidates/{{candidateId}}/{endpoint}",
        "候选": list(candidates),
        **timing.report(),
        "响应状态": statuses,
        "响应含listOrder": has_order,
        "listOrder长度": order_lengths,
        "listOrder长度_p50": (
            int(statistics.median(order_lengths)) if order_lengths else 0
        ),
        "未处理池_动作前只数": pool_before,
        "未处理池_动作后只数": pool_after,
        "listOrder等于动作后未处理池只数": [
            length == after for length, after in zip(order_lengths, pool_after)
        ],
        "listOrder等于动作前未处理池只数": [
            length == before for length, before in zip(order_lengths, pool_before)
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--securities",
        default="600",
        help="参与待归类的证券数，或 all（交付快照里的全部证券）",
    )
    parser.add_argument("--days", type=int, default=250, help="叠加的每日入选天数")
    parser.add_argument("--history", type=int, default=20, help="每个候选项的处理历史条数")
    parser.add_argument(
        "--path",
        type=int,
        default=200,
        help="浏览路径长度（回看历史）；实际写入为 min(path, 证券数)，见报告 path_steps",
    )
    parser.add_argument("--navigations", type=int, default=40, help="切卡采样次数")
    parser.add_argument("--samples", type=int, default=10, help="首开与筛选采样次数")
    parser.add_argument("--legacy-samples", type=int, default=3, help="旧接口组合采样次数")
    parser.add_argument(
        "--action-samples",
        type=int,
        default=3,
        help="归类动作（暂不关注／稍后处理）的采样次数，每次作用在不同的候选项上",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="把隔离数据写到该目录并只做播种，供浏览器侧基准复用（不清理）",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="把报告写入该文件（UTF-8）；缺省打印到标准输出",
    )
    args = parser.parse_args()

    for name in LIVE_SOURCE_ENV:
        os.environ.pop(name, None)

    limit = None if str(args.securities).strip().lower() == "all" else int(args.securities)

    if args.data_dir is not None:
        # 播种模式：只写隔离数据并报告规模，供浏览器侧基准在同一份数据上量首屏与滚动。
        data_dir = Path(args.data_dir)
        data_dir.mkdir(parents=True, exist_ok=True)
        settings = build_settings(data_dir)
        clock = FixedClock(datetime(2026, 9, 25, 16, 0, 0))
        container = build_container(settings, clock)
        container.imports._published_hook = None
        data = seed(
            container,
            load_security_ids(limit),
            days=args.days,
            history_rounds=args.history,
            path_steps=args.path,
        )
        print(json.dumps({"dataDir": str(data_dir), **data}, ensure_ascii=False))
        return 0

    with tempfile.TemporaryDirectory(prefix="dslite-bench-") as tmp:
        settings = build_settings(Path(tmp))
        clock = FixedClock(datetime(2026, 9, 25, 16, 0, 0))
        container = build_container(settings, clock)
        container.imports._published_hook = None  # 基准不触发后台补取

        security_ids = load_security_ids(limit)
        data = seed(
            container,
            security_ids,
            days=args.days,
            history_rounds=args.history,
            path_steps=args.path,
        )
        service = container.classification
        report: dict[str, object] = {"隔离数据": data, "时序": {}}

        # 服务层：统一浏览读取、筛选、切卡、末端选择
        first_open = Timing("首开：统一浏览结果")
        for _ in range(args.samples):
            time_call(first_open, service.browse)
        report["时序"]["首开"] = first_open.report()

        filter_day = (clock.now() - timedelta(days=30)).date().isoformat()
        for change in (
            {"scope": "processed"},
            {"scope": "unprocessed", "importDate": filter_day},
            {"importDate": None, "viewMode": "list"},
        ):
            timing = Timing(f"筛选：{json.dumps(change, ensure_ascii=False)}")
            for _ in range(args.samples):
                time_call(timing, lambda c=change: service.update_view(dict(c)))
            report["时序"][timing.label] = timing.report()

        with container.db.transaction() as conn:
            state = classification_repo.get_state(conn)
            state.path = tuple(security_ids[: args.path])
            state.cursor = 0
            state.current_candidate_id = security_ids[0]
            state.ended = False
            classification_repo.save_state(conn, state, clock.now().isoformat())

        navigation = Timing("普通切卡：上一个／下一个")
        for _ in range(args.navigations):
            state = service.get_view().state
            started = time.perf_counter()
            result = service.navigate(
                "next",
                expected_cursor=state.cursor,
                expected_revision=state.navigation_revision,
            )
            navigation.seconds.append(time.perf_counter() - started)
            # 只量这次响应里真正要回传的部分：卡片与列表变化（不含整份列表）
            navigation.byte_samples.append(
                len(
                    json.dumps(
                        {
                            "currentCandidate": result.view.state.current_candidate_id,
                            "changes": [row.candidate_id for row in result.delta.changed],
                        }
                    )
                )
            )
            if state.cursor + 1 >= len(security_ids[: args.path]):
                with container.db.transaction() as conn:
                    fresh = classification_repo.get_state(conn)
                    fresh.cursor = 0
                    fresh.current_candidate_id = security_ids[0]
                    classification_repo.save_state(conn, fresh, clock.now().isoformat())
        report["时序"]["普通切卡"] = navigation.report()

        picker = Timing("路径末端：寻找下一只未查看")
        for _ in range(args.samples):
            with container.db.transaction() as conn:
                state = classification_repo.get_state(conn)
                state.path = tuple(security_ids[: args.path])
                state.cursor = len(state.path)
                state.current_candidate_id = None
                state.ended = False
                classification_repo.save_state(conn, state, clock.now().isoformat())
            revision = service.get_view().state.navigation_revision
            time_call(
                picker,
                lambda: service.navigate(
                    "next",
                    expected_cursor=len(security_ids[: args.path]),
                    expected_revision=revision,
                ),
            )
        report["时序"]["路径末端选择"] = picker.report()

        # HTTP 边界：新接口（一次请求一份结果）与旧接口组合（一次操作多次请求）
        app = create_app(settings, clock)
        with TestClient(app) as client:
            candidate_id = security_ids[0]
            client.put("/api/classification/view", json={"currentCandidateId": candidate_id})

            http_open = Timing("HTTP 首开：统一浏览结果")
            for _ in range(args.samples):
                started = time.perf_counter()
                response = client.get("/api/classification/view")
                http_open.seconds.append(time.perf_counter() - started)
                http_open.byte_samples.append(len(response.content))
            report["时序"]["HTTP 首开"] = http_open.report()

            legacy_open = Timing("HTTP 旧接口组合：打开工作区")
            for _ in range(args.legacy_samples):
                started = time.perf_counter()
                state = client.get("/api/classification/view").json()
                total = len(
                    client.get(
                        "/api/classification/candidates",
                        params={"scope": state["scope"]},
                    ).content
                )
                # 未处理池：改造前无论当前页签是哪一个都要再读一次
                total += len(
                    client.get(
                        "/api/classification/candidates",
                        params={"scope": "unprocessed"},
                    ).content
                )
                legacy_open.seconds.append(time.perf_counter() - started)
                legacy_open.byte_samples.append(total)
            report["时序"]["HTTP 旧接口组合"] = legacy_open.report()

            view = client.get("/api/classification/view").json()
            http_nav = Timing("HTTP 普通切卡")
            for _ in range(args.navigations):
                payload = {
                    "direction": "next",
                    "expectedCursor": view["cursor"],
                    "expectedRevision": view["navigationRevision"],
                }
                started = time.perf_counter()
                response = client.post("/api/classification/view/navigate", json=payload)
                http_nav.seconds.append(time.perf_counter() - started)
                http_nav.byte_samples.append(len(response.content))
                # 只量切卡命令本身：读数请求在循环外单独准备下一次的游标，
                # 这样这一列与旧接口组合列量的都是「一次用户操作」的代价。
                view = client.get("/api/classification/view").json()
            report["时序"]["HTTP 普通切卡"] = http_nav.report()

            legacy_nav = Timing("HTTP 旧接口组合：普通切卡")
            for _ in range(args.legacy_samples):
                view = client.get("/api/classification/view").json()
                started = time.perf_counter()
                client.post(
                    "/api/classification/view/navigate",
                    json={
                        "direction": "next",
                        "expectedCursor": view["cursor"],
                        "expectedRevision": view["navigationRevision"],
                    },
                )
                total = len(
                    client.get(
                        "/api/classification/candidates",
                        params={"scope": view["scope"]},
                    ).content
                )
                total += len(
                    client.get(
                        "/api/classification/candidates",
                        params={"scope": "unprocessed"},
                    ).content
                )
                legacy_nav.seconds.append(time.perf_counter() - started)
                legacy_nav.byte_samples.append(total)
            report["时序"]["HTTP 旧接口组合切卡"] = legacy_nav.report()

            # 列表行投影与当前卡详细资料各自的体积（放在归类动作之前：此时池仍是满量）
            browse = service.browse()
            report["响应大小"] = {
                "列表行数": len(browse.rows),
                "列表行_字节": len(
                    json.dumps(
                        [
                            {
                                "candidateId": row.candidate_id,
                                "name": row.security.name if row.security else None,
                                "state": row.state.value,
                                "latestImportDate": row.latest_import_date,
                                "sourceCount": row.source_count,
                                "observed": row.observed,
                            }
                            for row in browse.rows
                        ],
                        ensure_ascii=False,
                    )
                ),
                "当前卡来源条数": len(browse.current.sources) if browse.current else 0,
            }

            # 归类动作：满量数据下经 HTTP 各执行一次真实动作。动作会改变未处理池，
            # 因此放在所有读取测量之后。数据目录是本次运行新建的临时隔离目录
            # （`--data-dir` 播种模式写的也是隔离目录），重复运行不写真实 data/runtime。
            action_samples = max(1, args.action_samples)
            tail_candidates = list(reversed(security_ids[-action_samples:]))
            tail_set = set(tail_candidates)
            head_candidates = [
                candidate_id
                for candidate_id in security_ids[1 : 1 + action_samples]
                if candidate_id not in tail_set
            ]
            actions: dict[str, object] = {
                "口径": (
                    "满量数据下经 HTTP 执行的真实归类动作；数据目录是本次运行新建的"
                    "临时隔离目录，不写真实 data/runtime。listOrder 由服务端按未处理"
                    "状态取全池（不带日期/搜索筛选），未处理池只数取 GET /view 的"
                    "pending，基准里两者同口径，因此可以直接比较长度。"
                )
            }
            if tail_candidates:
                actions["暂不关注（普通移出队列）"] = measure_action(
                    client,
                    endpoint="dismiss",
                    note="候选离开未处理池：队列成员变化，队列顺序随之改变",
                    candidates=tail_candidates,
                )
            if head_candidates:
                actions["稍后处理（移尾）"] = measure_action(
                    client,
                    endpoint="later",
                    note="候选留在未处理池但被移到队尾：队列顺序改变",
                    candidates=head_candidates,
                )
            report["归类动作"] = actions

        payload = json.dumps(report, ensure_ascii=False, indent=2)
        if args.out is not None:
            # 由 Python 直接写文件：避免 shell 重定向把中文按本地代码页转坏
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(payload + "\n", encoding="utf-8")
            print(f"报告已写入 {args.out}")
        else:
            print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
