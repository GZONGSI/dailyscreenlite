"""导入发布时的每日入选规则。

一个来源批次发布成功后，对每个新识别的证券应用以下规则（同一事务内）：
- 没有候选项：新建待归类候选项，记录当天入选；
- 已有候选项且当天未入选：记录当天入选；未处理则合并（保留原队列位置与状态），
  已处理则重新进入待归类（保留历次处理记录，移到队尾）；
- 已存在候选项且当天已入选：只追加来源，不再次自动触发归类。

入选与来源都以成功发布为准，因此未确认、失败或不完整批次不会提前消耗当天机会，
延迟发布也先经同一去重规则再按成功发布时点判断是否需要新的归类。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from dailyscreen_lite.domain.models import (
    UNPROCESSED_STATES,
    Candidate,
    ImportDate,
    Security,
)
from dailyscreen_lite.domain.models import CandidateState
from dailyscreen_lite.repository import classification_repo


@dataclass(frozen=True)
class PublishCounts:
    """一次发布的候选项影响数量：新建、合并、重新归类。"""

    new: int = 0
    merged: int = 0
    reopened: int = 0

    def plus(self, other: "PublishCounts") -> "PublishCounts":
        return PublishCounts(
            new=self.new + other.new,
            merged=self.merged + other.merged,
            reopened=self.reopened + other.reopened,
        )


@dataclass(frozen=True)
class PublishResult:
    """一次发布的候选项影响：数量汇总与每只股票的具体效果。

    effects 以证券身份为键，取 new / merged / reopened / source_only，
    供导入结果表逐行标注「新增 / 合并 / 重新归类 / 仅追加来源」。
    """

    counts: PublishCounts
    effects: dict[str, str]


def publish_selections(
    conn: sqlite3.Connection,
    *,
    securities: list[Security],
    batch_id: str,
    import_date: ImportDate,
    published_at: datetime,
) -> PublishResult:
    """把一次成功发布的识别结果落到候选项与每日入选上。"""
    counts = PublishCounts()
    effects: dict[str, str] = {}
    stamp = published_at.isoformat()
    # 同批新建的候选项共用同一队列序号：同一次发布内按证券身份稳定排序，
    # 不按来源出现顺序插队。
    batch_queue_order = classification_repo.next_queue_order(conn)
    for security in securities:
        candidate = classification_repo.find_by_security(conn, security.security_id)
        if candidate is None:
            # 候选项身份即证券身份：一只股票最多一个候选项，重新归类复用同一行
            candidate_id = security.security_id
            classification_repo.insert_candidate(
                conn,
                Candidate(
                    candidate_id=candidate_id,
                    security_id=security.security_id,
                    state=CandidateState.PENDING,
                    first_seen_at=published_at,
                    last_action_at=None,
                    action_result=None,
                ),
                queue_order=batch_queue_order,
            )
            classification_repo.record_selection(
                conn, candidate_id, import_date.iso, batch_id, stamp
            )
            classification_repo.link_source(
                conn, candidate_id, batch_id, import_date.iso, stamp
            )
            classification_repo.record_history(
                conn,
                candidate_id,
                action="selected",
                from_state=None,
                to_state=CandidateState.PENDING,
                acted_at=published_at,
            )
            counts = counts.plus(PublishCounts(new=1))
            effects[security.security_id] = "new"
            continue

        # 来源先追加：同日重复包含该股票的其他来源仍要有完整追溯
        classification_repo.link_source(
            conn, candidate.candidate_id, batch_id, import_date.iso, stamp
        )
        if not classification_repo.record_selection(
            conn, candidate.candidate_id, import_date.iso, batch_id, stamp
        ):
            # 该股票今天已经有效入选过：只追加来源，不再次触发归类
            effects[security.security_id] = "source_only"
            continue
        if candidate.state in UNPROCESSED_STATES:
            # 未处理期间跨日入选合并：保留原队列位置与处理状态
            counts = counts.plus(PublishCounts(merged=1))
            effects[security.security_id] = "merged"
            continue
        # 处理后另一个导入日期的首次有效入选：重新进入待归类，移到队尾
        classification_repo.update_state(
            conn, candidate.candidate_id, CandidateState.PENDING, "reopened", published_at
        )
        classification_repo.record_history(
            conn,
            candidate.candidate_id,
            action="reopened",
            from_state=candidate.state,
            to_state=CandidateState.PENDING,
            acted_at=published_at,
        )
        classification_repo.move_to_tail(conn, candidate.candidate_id)
        counts = counts.plus(PublishCounts(reopened=1))
        effects[security.security_id] = "reopened"
    return PublishResult(counts=counts, effects=effects)
