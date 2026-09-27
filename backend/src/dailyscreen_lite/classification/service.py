"""候选归类：队列顺序、每日入选合并与重开、处理动作与浏览上下文。

规则要点（对应已确认规格）：
- 一只股票最多一个候选项；按「证券身份＋北京时间导入日期」记录每日入选，
  同日其他来源只追加来源，不再次自动触发归类；
- 未处理期间跨日入选合并到同一候选项，保留原队列位置与全部入选日期；
- 处理完成后另一个导入日期的首次有效入选使同一候选项重新待归类；
- 处理动作先保存，浏览命令再沿持久化路径前进；浏览与处理状态分离；
- 主动重新归类保留历次处理记录，已有未处理对象则复用并调到队首；
- 处理与观察单向联动：归类动作可改变观察关系，观察关系不反向改变处理状态。

浏览路径按访问步骤持久化（`ClassificationState.path`＋游标）：
- 手动打开候选（左栏点选、全局搜索、返回队列）从游标处截断前进分支再追加目标，
  因此历史中途跳转替换掉原前进分支；同一候选可多次出现（再次打开是新的一步）；
- 沿历史前后移动优先于寻找新候选；到路径末端才按当前筛选从未处理池找尚未查看的
  候选项，没有目标就追加结束卡步骤（结束卡也是可回看、可跨重启恢复的一步）；
- 归类动作与导航仍是两个确认步骤：动作只报事实，导航命令再沿路径前进。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from dailyscreen_lite.classification import path
from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.models import (
    END_STEP,
    UNPROCESSED_STATES,
    VIEW_MODES,
    Candidate,
    CandidateRow,
    CandidateScope,
    CandidateState,
    ClassificationState,
    Security,
)
from dailyscreen_lite.repository import (
    Database,
    classification_repo,
    notes_repo,
    observations_repo,
    securities_repo,
)


class ClassificationUnavailable(LookupError):
    """候选项不存在。"""


class ClassificationActionNotAllowed(ValueError):
    """当前处理状态不允许该动作。"""


@dataclass(frozen=True)
class SourceRef:
    """一次来源追溯：批次与其导入日期。"""

    batch_id: str
    import_date: str


@dataclass(frozen=True)
class CandidateView:
    candidate: Candidate
    security: Security | None
    # 来源批次（含同日只追加证据的批次），按时间升序
    sources: tuple[SourceRef, ...]
    # 是否已属于任一观察组：与处理状态无关
    observed: bool = False
    # 所属观察组标识；卡片据此明显展示观察关系
    group_ids: tuple[str, ...] = ()
    # 该股票（跨导入日期共用）的笔记条数，供卡片标注笔记入口
    note_count: int = 0


@dataclass(frozen=True)
class ClassificationView:
    state: ClassificationState
    current: CandidateView | None
    round: dict[str, int]
    summary: dict[str, int]
    # 当前卡是否属于左侧当前筛选结果：否（且在筛选结果之外）时界面明确提示，
    # 但筛选条件本身不因一次打开而改变，返回上一项仍继续原工作范围。
    in_filter: bool = True
    # 是否还有可前进的去处：路径前方有步骤，或当前筛选里还有尚未查看的待归类候选。
    # 界面据此决定「下一个」可用状态，结束卡因此能在拥有前进历史时继续前进。
    has_next: bool = False


@dataclass(frozen=True)
class BrowseResult:
    """一次一致的浏览读取：当前卡、左侧完整轻量列表、未处理池与数量。

    这些内容在同一次数据库读取视图里形成，因此打开工作区或改筛选时，当前卡、
    列表与数量指向同一时刻的真实状态。列表是轻量行投影（不装配逐项详细资料），
    当前卡才是完整候选项视图。路径读取时顺带跳过已失效的步骤，使浏览不卡在
    已不存在的候选上。
    """

    state: ClassificationState
    current: CandidateView | None
    rows: tuple[CandidateRow, ...]
    pending: tuple[CandidateRow, ...]
    dates: tuple[str, ...]
    round: dict[str, int]
    summary: dict[str, int]
    in_filter: bool = True
    has_next: bool = False


@dataclass(frozen=True)
class ListDelta:
    """一次写入造成的列表变化：变更行、离开当前列表的标识与写入后的列表版本。

    前端只应用服务端给出的变化，不自行推导筛选规则；版本不一致时重读完整结果。
    `order` 只在这次写入改变了队列顺序时下发（「稍后处理」把候选移到队尾）：
    它是写入后的真实行序，前端据此重排而不重读整份列表。
    """

    revision: int
    changed: tuple[CandidateRow, ...] = ()
    removed: tuple[str, ...] = ()
    order: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommandResult:
    """改变浏览位置的命令结果：新浏览状态与列表变化（不含整份列表）。"""

    view: ClassificationView
    delta: ListDelta


@dataclass(frozen=True)
class ActionOutcome:
    """归类动作结果：已保存的候选项视图、本次删除的列表行与写入前后的版本。

    三个版本随响应下发，前端因此不必为一次归类重读整份浏览结果：`revision_before`
    是写入前的列表版本（前端据此判断自己手上的列表在这次写入之前是否已经过期，
    例如另一个入口刚导入过候选——那时这次动作的增量补不齐，必须重读完整结果）、
    `revision` 是写入后的列表版本，`navigation_revision` 是写入后的导航修订号
    （保存成功后的自动推进带着它发出，服务端据此识别迟到命令）。`order` 只在这
    次动作真的移动了队列位置时下发（「稍后处理」移尾），前端据此重排已有行。
    """

    view: CandidateView
    revision: int
    revision_before: int = 0
    removed: tuple[str, ...] = ()
    navigation_revision: int = 0
    order: tuple[str, ...] = ()


def _in_filter(current: CandidateView | None, rows: list[CandidateRow]) -> bool:
    """当前卡是否属于左侧当前结果；没有当前卡时不适用（视为在范围内）。"""
    if current is None:
        return True
    return any(row.candidate_id == current.candidate.candidate_id for row in rows)


class ClassificationService:
    PROCESSED_STATES = CandidateScope.PROCESSED.states

    def __init__(self, db: Database, clock: Clock, *, reclassified_hook=None) -> None:
        self._db = db
        self._clock = clock
        # 主动重新归类使股票回到持续更新范围，需要补齐行情；
        # 与导入发布的补取复用同一入口，失败不影响归类本身。
        self._reclassified_hook = reclassified_hook

    def attach_reclassified_hook(self, hook) -> None:
        """装配后补挂重新归类的补取钩子（装配顺序上更新服务晚于本服务）。"""
        self._reclassified_hook = hook

    # --- 读取 ---

    def list_candidates(
        self,
        *,
        scope: CandidateScope = CandidateScope.UNPROCESSED,
        import_date: str | None = None,
        search: str | None = None,
        result: CandidateState | None = None,
    ) -> list[CandidateView]:
        with self._db.read() as conn:
            candidates = classification_repo.list_candidates(
                conn, scope.states, import_date, search=search, result=result
            )
            return self._views(conn, candidates)

    def get_candidate(self, candidate_id: str) -> CandidateView:
        with self._db.read() as conn:
            candidate = self._require(conn, candidate_id)
            return self._views(conn, [candidate])[0]

    def security_id_for_candidate(self, candidate_id: str) -> str:
        """候选项对应的证券标识；供按证券归属的从属功能（如笔记）定位。"""
        with self._db.read() as conn:
            return self._require(conn, candidate_id).security_id

    def view_for_security(self, security_id: str) -> CandidateView | None:
        """该证券的候选项视图；共用个股详情以股票为身份读取它。"""
        with self._db.read() as conn:
            candidate = classification_repo.find_by_security(conn, security_id)
            if candidate is None:
                return None
            return self._views(conn, [candidate])[0]

    def is_imported(self, security_id: str) -> bool:
        """该证券是否经过导入与证券识别：观察关系与笔记不能绕过导入新增股票。"""
        with self._db.read() as conn:
            return classification_repo.find_by_security(conn, security_id) is not None

    def search(self, query: str, *, limit: int = 20) -> list[CandidateView]:
        """全局搜索：只返回经过导入与证券识别的股票，不引入全市场搜索。"""
        term = (query or "").strip()
        if not term:
            return []
        with self._db.read() as conn:
            candidates = classification_repo.list_candidates(
                conn, None, None, search=term
            )
            return self._views(conn, candidates[:limit])

    def summary(self) -> dict[str, int]:
        with self._db.read() as conn:
            return self._summary_in(conn)

    def dates(self) -> list[str]:
        with self._db.read() as conn:
            return classification_repo.list_dates(conn)

    # --- 处理动作（动作与浏览分开提交，导航失败不重复处理） ---

    def later(self, candidate_id: str) -> ActionOutcome:
        """稍后处理：仍在待归类池，但移到队尾。"""

        def apply(conn) -> None:
            candidate = self._require(conn, candidate_id)
            if candidate.state not in UNPROCESSED_STATES:
                raise ClassificationActionNotAllowed("已处理的候选项不能稍后处理")
            classification_repo.update_state(
                conn, candidate_id, CandidateState.LATER, "later", self._clock.now()
            )
            classification_repo.record_history(
                conn,
                candidate_id,
                action="later",
                from_state=candidate.state,
                to_state=CandidateState.LATER,
                acted_at=self._clock.now(),
            )
            classification_repo.move_to_tail(conn, candidate_id)

        return self._act(candidate_id, apply, reorders=True)

    def dismiss(self, candidate_id: str) -> ActionOutcome:
        """暂不关注：结束本次归类，不删除导入事实，也不影响观察关系。

        只有未处理候选项可以直接暂不关注；已处理项需要先主动重新归类，
        避免用一次点击把已观察/已清理的记录改写成暂不关注。
        """

        def apply(conn) -> None:
            candidate = self._require(conn, candidate_id)
            if candidate.state not in UNPROCESSED_STATES:
                raise ClassificationActionNotAllowed(
                    "已处理的候选项需要先主动重新归类，才能再次归类"
                )
            classification_repo.update_state(
                conn, candidate_id, CandidateState.DISMISSED, "dismissed", self._clock.now()
            )
            classification_repo.record_history(
                conn,
                candidate_id,
                action="dismissed",
                from_state=candidate.state,
                to_state=CandidateState.DISMISSED,
                acted_at=self._clock.now(),
            )

        return self._act(candidate_id, apply)

    def reclassify(self, candidate_id: str) -> ActionOutcome:
        """主动重新归类：复用同一候选项，保留历次处理记录并调到队首。

        未处理对象直接复用；已处理对象开启新的待归类过程，不创建第二个对象。
        """

        def apply(conn) -> None:
            candidate = self._require(conn, candidate_id)
            if candidate.state not in UNPROCESSED_STATES:
                classification_repo.update_state(
                    conn,
                    candidate_id,
                    CandidateState.PENDING,
                    "reclassified",
                    self._clock.now(),
                )
                classification_repo.record_history(
                    conn,
                    candidate_id,
                    action="reclassified",
                    from_state=candidate.state,
                    to_state=CandidateState.PENDING,
                    acted_at=self._clock.now(),
                )
            classification_repo.move_to_front(conn, candidate_id)
            # 重新归类把它送回待归类池：隐藏的已处理结果筛选会把它挡在列表外，
            # 因此新位置回到待归类范围时一并清掉该筛选。
            state = classification_repo.get_state(conn)
            if state.scope is CandidateScope.UNPROCESSED:
                state.result = None
                classification_repo.save_state(conn, state, self._clock.now().isoformat())

        view = self._act(candidate_id, apply, reorders=True)
        # 钩子只负责把补取请求转交后台，本身不抛异常；这里不做兜底捕获，
        # 否则钩子里的编程错误会被静默吞掉，补取永远不执行却看不出来。
        if self._reclassified_hook is not None:
            self._reclassified_hook([view.view.candidate.security_id])
        return view

    def cleanup(self, import_date: str | None = None) -> int:
        """主动清理待归类池：移出工作池为已清理，保留导入与处理事实。

        逐项迁移并写入处理记录：清理同样是用户动作，与其它动作一样必须留痕，
        否则「清理 → 重新归类」之后无法追溯那次清理。
        """
        with self._db.transaction() as conn:
            now = self._clock.now()
            targets = classification_repo.list_clearable(
                conn, UNPROCESSED_STATES, import_date
            )
            for candidate_id, state in targets:
                classification_repo.update_state(
                    conn, candidate_id, CandidateState.CLEARED, "cleared", now
                )
                classification_repo.record_history(
                    conn,
                    candidate_id,
                    action="cleared",
                    from_state=state,
                    to_state=CandidateState.CLEARED,
                    acted_at=now,
                )
            if targets:
                classification_repo.bump_list_revision(conn)
                # 清理把待归类项移出未处理范围：当前卡若因此离开范围就清空当前卡，
                # 卡片与左侧列表保持一致。浏览路径保留：被清理的候选仍存在，
                # 可沿历史回看它的最新状态。
                state = classification_repo.get_state(conn)
                if state.current_candidate_id and not self._in_view(
                    conn, state, state.current_candidate_id
                ):
                    self._drop_current(state)
                self._follow_current_card(conn, state)
                state.list_revision = classification_repo.list_revision(conn)
                classification_repo.save_state(conn, state, now.isoformat())
            return len(targets)

    def observe(self, candidate_id: str, apply_relations) -> ActionOutcome:
        """在同一事务里保存观察关系并完成本次归类。

        关系保存由调用方（观察组服务）提供；任一步失败整体回滚，不半成功。
        只有未处理候选项可以「加入或保留观察」；已处理项需先主动重新归类。
        """
        return self._with_relations(
            candidate_id,
            apply_relations,
            state=CandidateState.OBSERVED,
            action_result="observed",
            action="observed",
            require_unprocessed=True,
        )

    def leave_groups(self, candidate_id: str, apply_relations) -> ActionOutcome:
        """组合动作：移出所选观察关系，同时记为暂不关注（同一事务）。"""
        return self._with_relations(
            candidate_id,
            apply_relations,
            state=CandidateState.DISMISSED,
            action_result="dismissed",
            action="left_groups",
        )

    # --- 浏览上下文 ---

    def get_view(self) -> ClassificationView:
        """当前浏览上下文；读回时同样跳过已失效的路径步骤。"""
        return self._read_state(lambda conn, state: self._state_view(conn, state))

    def browse(self, scope: CandidateScope | None = None) -> BrowseResult:
        """工作区打开或重读：同一读取视图内形成当前卡、轻量列表与数量。

        scope 是请求方当前展示的页签（可选）：不传时用状态里保存的展示页签。
        多条查询走同一个数据库快照，因此当前卡、列表与数量指向同一时刻的状态。
        路径在这里顺带跳过已失效的步骤（真正不存在的候选不再可访问），因此刷新与
        重启后不会卡在一张读不出来的卡片上；数据正常时不写库。
        """
        return self._read_state(lambda conn, state: self._browse_in(conn, state, scope=scope))

    def update_view(self, changes: dict[str, Any]) -> BrowseResult:
        """显式修改筛选、视图模式或当前卡的目标。

        主动改筛选（页签／日期／搜索／结果）开启新一轮：旧路径清空，落到新范围的
        首项或空状态；原当前卡仍属于新范围时保留它，使「卡片与左侧列表一致」。
        `currentCandidateId` 是「就地打开某个已有步骤」（重新归类、跨模块打开卡片
        后的落位），存在该步骤时保留前后历史，不存在则作为新访问追加。
        """
        with self._db.transaction() as conn:
            state = classification_repo.get_state(conn)
            self._repair_path(conn, state, persist=True)
            # 用户此刻看到的是哪一栏由当前卡决定：改筛选以这一栏为起点，
            # 否则「历史回看留下的页签」会被当成用户主动选的范围。
            self._follow_current_card(conn, state)
            now = self._clock.now()

            filter_changed = any(
                key in changes and changes[key] != current
                for key, current in (
                    ("scope", state.scope.value),
                    ("importDate", state.import_date),
                    ("search", state.search),
                    ("result", state.result.value if state.result else None),
                )
            )
            # 改筛选前的当前卡：新范围若仍包含它（页签切换、清除筛选），读列表的一方
            # 期待的仍是同一张卡；不再包含时由下方的范围落位决定去留（首项或空状态）。
            previous_current = state.current_candidate_id
            if filter_changed:
                # 手动编辑日期、搜索或结果时，以用户此刻看到的页签开始新路径；
                # 历史回看造成的自动页签变化不应把用户带回旧筛选范围。
                if "scope" not in changes:
                    state.filter_scope = state.scope
                state.path = ()
                state.cursor = -1
                state.ended = False
                state.current_candidate_id = None

            if "currentCandidateId" in changes:
                current = changes["currentCandidateId"]
                if current is None:
                    self._clear_position(state)
                else:
                    candidate = self._require(conn, current)
                    self._go_to(conn, state, candidate, now, new_step=False)

            if "viewMode" in changes:
                mode = changes["viewMode"]
                if mode not in VIEW_MODES:
                    raise ValueError(f"未知视图模式：{mode}")
                state.view_mode = mode
            if "scope" in changes and changes["scope"] is not None:
                state.filter_scope = CandidateScope(changes["scope"])
                if "currentCandidateId" not in changes or changes["currentCandidateId"] is None:
                    state.scope = state.filter_scope
            if "importDate" in changes:
                state.import_date = changes["importDate"]
            if "search" in changes:
                state.search = changes["search"] or ""
            if "result" in changes:
                raw = changes["result"]
                state.result = CandidateState(raw) if raw else None

            if filter_changed and state.filter_scope is CandidateScope.UNPROCESSED:
                # 处理结果只适用于已处理范围；新路径进入待归类时移除隐藏的旧筛选。
                state.result = None

            if "currentCandidateId" not in changes and filter_changed:
                # 新范围仍含原当前卡就保留（先按原步骤确认它确实还在），
                # 否则落到新范围首项或空状态。
                kept = previous_current if previous_current and self._in_view(
                    conn, state, previous_current
                ) else None
                if kept is not None:
                    candidate = classification_repo.get_candidate(conn, kept)
                    if candidate is not None:
                        self._go_to(conn, state, candidate, now)
                else:
                    self._settle(conn, state, move_cursor=True)

            self._follow_current_card(conn, state)
            state.list_revision = classification_repo.list_revision(conn)
            classification_repo.save_state(conn, state, now.isoformat())
            return self._browse_in(conn, state)

    def focus(self, candidate_id: str) -> BrowseResult:
        """手动打开候选卡（左栏点选、全局搜索、返回队列）。

        目标作为新的一步追加，从当前游标截断前进分支；不改变当前筛选、不改队列
        顺序（未处理池顺序只由稍后处理、重新归类与再次入选决定）。展示页签跟随
        当前卡的真实处理范围，因此卡片与左侧列表始终属于同一份结果，不会留下一张
        「不属于所见列表」的卡片；筛选条件（日期、搜索、结果）与筛选范围都不因一次
        打开而改变，返回上一项仍在原来的工作范围。
        """
        with self._db.transaction() as conn:
            candidate = self._require(conn, candidate_id)
            state = classification_repo.get_state(conn)
            self._repair_path(conn, state, persist=True)
            now = self._clock.now()
            self._go_to(conn, state, candidate, now)
            self._follow_current_card(conn, state)
            state.list_revision = classification_repo.list_revision(conn)
            classification_repo.save_state(conn, state, now.isoformat())
            # 左侧列表展示 `state.scope`（刚跟随当前卡的真实范围）：不出现「空列表 +
            # 一张说自己在别处的卡片」，提示只在同一份结果内部生效（`in_filter`）。
            return self._browse_in(conn, state)

    def _browse_in(
        self,
        conn,
        state: ClassificationState,
        *,
        scope: CandidateScope | None = None,
    ) -> BrowseResult:
        """同一读取视图内形成当前卡、轻量列表与数量。

        `scope` 是左侧列表要展示的页签（默认当前展示页签 `state.scope`）；当前卡是
        否在左侧结果之外由同一份筛选口径判断，因此卡片与列表不会互相矛盾。
        """
        view, rows = self._assemble_view(conn, state, scope)
        pending = classification_repo.list_candidate_rows(
            conn, CandidateScope.UNPROCESSED.states, state.import_date, search=state.search
        )
        return BrowseResult(
            state=view.state,
            current=view.current,
            rows=tuple(rows),
            pending=tuple(pending),
            dates=tuple(classification_repo.list_dates(conn)),
            round=view.round,
            summary=view.summary,
            in_filter=view.in_filter,
            has_next=view.has_next,
        )

    def _state_view(self, conn, state: ClassificationState) -> ClassificationView:
        """浏览状态与当前卡的详细资料（不含左侧列表）。

        命令响应的正文只有当前卡与浏览状态（列表变化单独走 `delta`），因此这里不装配
        整份轻量列表：`with_rows=False` 只按当前卡做一次成员判断来得到 `in_filter`，
        与 `_browse_in` 共用同一份组装逻辑与筛选口径，两处不会漂移。
        """
        return self._assemble_view(conn, state, with_rows=False)[0]

    def _assemble_view(
        self,
        conn,
        state: ClassificationState,
        scope: CandidateScope | None = None,
        *,
        with_rows: bool = True,
    ) -> tuple[ClassificationView, list[CandidateRow]]:
        """当前卡、本轮统计、数量以及「当前卡是否在左侧结果内」的唯一实现。

        展示页签在这里对齐当前卡的真实范围（同一份事实，卡片与列表因此不会错位）；
        读取路径不落库，只影响本次响应，写入路径已经把同样的结果存下来。
        `with_rows=False` 供只需要浏览状态的命令响应用：不取轻量列表，`in_filter`
        改由仓储的存在性判断得出（与列表共用同一份筛选条件），返回值里的 rows 为空。
        """
        current = self._current_view(conn, state)
        self._show_current_scope(
            state, current.candidate.state if current is not None else None
        )
        if with_rows:
            rows = self._browse_rows(conn, state, scope)
            in_filter = _in_filter(current, rows)
        else:
            rows = []
            in_filter = current is None or self._in_rows_of(
                conn, state, current.candidate.candidate_id, scope
            )
        return (
            ClassificationView(
                state=state,
                current=current,
                round=self._round_stats_for(conn, state),
                summary=self._summary_in(conn),
                in_filter=in_filter,
                has_next=self._has_next_place(conn, state),
            ),
            rows,
        )

    def _current_view(self, conn, state: ClassificationState) -> CandidateView | None:
        """当前卡的详细资料；停在结束卡或当前卡已不存在时没有当前卡。"""
        if not state.current_candidate_id:
            return None
        candidate = classification_repo.get_candidate(conn, state.current_candidate_id)
        return self._views(conn, [candidate])[0] if candidate is not None else None

    def _browse_rows(
        self, conn, state: ClassificationState, scope: CandidateScope | None = None
    ) -> list[CandidateRow]:
        """给定展示范围内的轻量行；浏览结果与落位判断共用同一筛选口径。

        结果筛选只属于用户选定的筛选范围：回看历史让展示页签临时跟随当前卡时，
        原范围的结果筛选不套到另一个页签上。
        """
        active = scope or state.scope
        result = state.result if active is state.filter_scope else None
        return classification_repo.list_candidate_rows(
            conn, active.states, state.import_date, search=state.search, result=result
        )

    def _in_rows_of(
        self,
        conn,
        state: ClassificationState,
        candidate_id: str,
        scope: CandidateScope | None = None,
    ) -> bool:
        """该候选是否属于某个展示范围：一次存在性判断，不装配整份列表。

        范围、日期、搜索与结果筛选的口径与 `_browse_rows` 同源（都出自仓储里同一份
        `_list_filters`），因此「列表里有没有这一行」这个问题只有一处结论。
        """
        active = scope or state.scope
        result = state.result if active is state.filter_scope else None
        return classification_repo.candidate_in_filters(
            conn,
            candidate_id,
            active.states,
            state.import_date,
            search=state.search,
            result=result,
        )

    def _in_view(self, conn, state: ClassificationState, candidate_id: str) -> bool:
        """该候选是否属于当前展示范围（决定卡片是否需要按范围落位）。"""
        return self._in_rows_of(conn, state, candidate_id)

    def _first_row_id(self, conn, state: ClassificationState) -> str | None:
        """当前筛选范围的首项标识；没有匹配项时为 None（空状态）。"""
        rows = self._browse_rows(conn, state)
        return rows[0].candidate_id if rows else None

    def _candidate_exists(self, conn, candidate_id: str) -> bool:
        """候选项是否仍然存在；浏览路径据此跳过真正失效的步骤。

        主动清理过的候选项仍在候选表里（只是离开待归类池），因此保留为可回看的
        步骤，回看时显示它当下的处理状态。
        """
        return classification_repo.get_candidate(conn, candidate_id) is not None

    def _settle(self, conn, state: ClassificationState, *, move_cursor: bool) -> None:
        """落到当前筛选范围的首项，或空状态。

        「目标不在范围内」只有这一份实现：需要挪位置时就地打开范围首项（不改变前后
        历史），否则清空当前位置与路径。
        """
        candidate_id = self._first_row_id(conn, state)
        candidate = (
            classification_repo.get_candidate(conn, candidate_id)
            if candidate_id
            else None
        )
        if move_cursor and candidate is not None:
            self._go_to(conn, state, candidate, self._clock.now(), new_step=False)
        else:
            self._clear_position(state)

    @staticmethod
    def _drop_current(state: ClassificationState) -> None:
        """当前卡离开可见范围：清空当前卡但保留浏览路径（历史步骤仍可回看）。"""
        state.current_candidate_id = None
        state.ended = False

    def _repair_path(self, conn, state: ClassificationState, *, persist: bool) -> bool:
        """读回时修补路径；返回是否修补过。

        `persist=False` 用于只读读取：只报告是否需要修补而不改写状态，调用方据此改用
        写事务重读后落库，因此日常打开工作区仍是一次纯读取。当前步骤失效时落到它之前
        最近的有效步骤（通常就是用户上一个位置）；路径本来就没有可用步骤时按模块规则
        落到当前范围首项或空状态，不把用户留在读不出来的卡片上。
        """
        steps = path.candidate_steps(state.path)
        # 一次查询核对路径上的候选项是否都还在：全都在时不逐个复核，否则每次打开
        # 工作区都会随浏览路径长度变慢。有缺失时才进入逐项修补分支。
        existing = (
            classification_repo.existing_candidate_ids(conn, steps) if steps else set()
        )
        repaired, cursor, changed = path.normalize(
            state.path,
            state.cursor,
            exists=lambda cid: cid in existing,
            ended=state.ended,
        )
        if changed or len(existing) != len(steps):
            state.path = repaired
            state.cursor = cursor
            # 顺序要紧：先落路径/游标，再按当前步骤导出「是否停在结束卡」与当前卡。
            state.ended = state.at_end_step
            state.current_candidate_id = (
                repaired[cursor]
                if cursor >= 0 and repaired[cursor] != END_STEP
                else None
            )
            changed = True
        elif state.current_candidate_id is not None and not self._candidate_exists(
            conn, state.current_candidate_id
        ):
            # 当前候选真的不存在（上面的核对已排除「都还在」）：按既有规则落位。
            # 停在结束卡时没有当前候选，因此这里不会把结束卡当成失效步骤。
            self._settle(conn, state, move_cursor=False)
            changed = True
        if not changed:
            return False
        if persist:
            classification_repo.save_state(conn, state, self._clock.now().isoformat())
        return True

    def _read_state(
        self, work: Callable[[sqlite3.Connection, ClassificationState], Any]
    ) -> Any:
        """在同一份读取视图里读取浏览上下文并完成工作；必要时才升级为写事务。

        正常读取（路径上的候选都还在）走 `read_view()` 的读事务，不取写锁也不落库；
        只有确实需要修补路径时才另开一个事务重读并落库。这样「打开工作区」在数据
        正常时是纯读取，浏览路径很长时也不会每次都写一次库。
        """
        with self._db.read_view() as conn:
            state = classification_repo.get_state(conn)
            if not self._repair_path(conn, state, persist=False):
                return work(conn, state)
        with self._db.transaction() as conn:
            state = classification_repo.get_state(conn)
            self._repair_path(conn, state, persist=True)
            return work(conn, state)

    def _go_to(
        self,
        conn,
        state: ClassificationState,
        candidate: Candidate,
        now,
        *,
        new_step: bool = True,
    ) -> None:
        """打开一张候选卡并标记已查看。

        `new_step` 为真时把目标作为新的一步追加（截断前进分支），对应一次手动访问；
        为假时若目标已是路径中的步骤就只移动游标、保留前后历史，对应「就地打开某个
        已有步骤」（重新归类、跨模块打开卡片后的落位）。
        """
        if new_step:
            steps, cursor = path.jump_to(state.path, state.cursor, candidate.candidate_id)
        else:
            steps, cursor = path.locate(state.path, candidate.candidate_id, state.cursor)
        state.path = steps
        state.cursor = cursor
        state.current_candidate_id = candidate.candidate_id
        state.ended = False
        # 展示页签跟随当前卡真实状态：自动前进后列表与卡片属于同一份结果。
        self._show_current_scope(state, candidate.state)
        if state.round_started_at is None:
            state.round_started_at = now
        classification_repo.mark_viewed(conn, candidate.candidate_id, now)

    @staticmethod
    def _clear_position(state: ClassificationState) -> None:
        """没有可打开的目标：清空当前位置（空状态），不动筛选。"""
        state.current_candidate_id = None
        state.path = ()
        state.cursor = -1
        state.ended = False

    @staticmethod
    def _move_to_step(state: ClassificationState, index: int) -> str | None:
        """沿路径移动到某一步；步骤是结束卡时当前卡为空。"""
        step = state.path[index]
        state.cursor = index
        state.current_candidate_id = None if step == END_STEP else step
        state.ended = step == END_STEP
        return state.current_candidate_id

    @staticmethod
    def _show_current_scope(
        state: ClassificationState, candidate_state: CandidateState | None
    ) -> None:
        """展示页签跟随当前卡的真实处理范围。

        浏览结果里的卡片与左侧列表始终属于同一份结果，因此不会出现「卡片在一栏、
        列表在另一栏」的错位：以参数传入的那张卡的状态决定页签，没有卡片（结束卡、
        空状态）时回到本轮筛选范围。

        筛选范围（`filter_scope`）不因此改变——用户选定的范围只由他自己改，
        它决定后续寻找新候选与下一轮从哪里开始。
        """
        if candidate_state is None:
            state.scope = state.filter_scope
        else:
            state.scope = (
                CandidateScope.UNPROCESSED
                if candidate_state in UNPROCESSED_STATES
                else CandidateScope.PROCESSED
            )

    def _follow_current_card(self, conn, state: ClassificationState) -> None:
        """把展示页签重新对齐当前卡的真实状态；停在结束卡或没有当前卡时回到筛选范围。

        读取路径不写库，因此那里由 `_assemble_view` 就地套用同一条规则；这里负责把
        写入路径的结果落库，使刷新与重启后的页签与刚操作完时一致。
        """
        candidate = (
            classification_repo.get_candidate(conn, state.current_candidate_id)
            if state.current_candidate_id
            else None
        )
        self._show_current_scope(state, candidate.state if candidate is not None else None)

    def navigate(
        self,
        direction: str,
        *,
        expected_cursor: int,
        expected_revision: int,
    ) -> CommandResult:
        """沿持久化路径前后移动；到路径末端才寻找未查看的待归类候选。

        expected_cursor 是客户端看到的游标。请求超时后重试若原请求已提交，
        游标已改变，直接返回真实位置，避免跨过第二只。
        """
        if direction not in {"previous", "next"}:
            raise ValueError(f"未知浏览方向：{direction}")
        with self._db.transaction() as conn:
            state = classification_repo.get_state(conn)
            self._repair_path(conn, state, persist=True)
            if state.cursor != expected_cursor or state.navigation_revision != expected_revision:
                # 迟到或重复的导航：不改动任何位置，只回报当前真实位置。
                return CommandResult(
                    view=self._state_view(conn, state),
                    delta=ListDelta(revision=state.list_revision),
                )

            now = self._clock.now()
            opened = self._step_in(conn, state, direction, now)
            self._follow_current_card(conn, state)

            # 浏览位置变化不改列表版本；这里把库里的真实版本写回浏览状态，
            # 使视图与列表变化报同一个版本，前端才知道手上的列表是否仍然有效。
            state.list_revision = classification_repo.list_revision(conn)
            classification_repo.save_state(conn, state, now.isoformat())
            return CommandResult(
                view=self._state_view(conn, state),
                delta=self._delta_for(conn, opened),
            )

    def _step_in(
        self, conn, state: ClassificationState, direction: str, now
    ) -> Candidate | None:
        """按方向移动一步；返回这次打开的候选（停在结束卡或原地不动时为 None）。

        前进的顺序是：先沿已有历史走（结束卡也是历史里的一步，前进同样停在它上面，
        与回看对称），到路径末端才在当前筛选里寻找尚未查看的候选，没有目标就把结束
        卡作为一步追加到路径末端。
        """
        if direction == "previous":
            index = path.previous_position(
                state.path,
                state.cursor,
                exists=lambda cid: self._candidate_exists(conn, cid),
                current_candidate_id=state.current_candidate_id,
            )
            return self._open_step(conn, state, index, now) if index is not None else None

        index = path.next_position(
            state.path,
            state.cursor,
            exists=lambda cid: self._candidate_exists(conn, cid),
            current_candidate_id=state.current_candidate_id,
        )
        if index is not None:
            # 路径前方还有步骤：沿历史逐步前进，不重复追加。
            return self._open_step(conn, state, index, now)
        candidate_id = self._next_unviewed_id(conn, state)
        if candidate_id is None:
            # 没有可去的候选：结束卡成为路径里的一步（已经停在结束卡上时只是落到同一
            # 位置），因此它能被回看、跨重启恢复，也能在有前进历史或新候选时继续前进。
            # 结束卡之后已有的访问步骤保留：收尾不删除历史。
            state.path, state.cursor = path.append_end(state.path, state.cursor)
            state.current_candidate_id = None
            state.ended = True
            return None
        candidate = classification_repo.get_candidate(conn, candidate_id)
        if candidate is None:
            return None
        self._go_to(conn, state, candidate, now)
        return candidate

    def _open_step(self, conn, state: ClassificationState, index: int, now) -> Candidate | None:
        """打开路径中的某一步；结束卡步骤只移动游标。"""
        candidate_id = self._move_to_step(state, index)
        if candidate_id is None:
            state.scope = state.filter_scope
            return None
        candidate = classification_repo.get_candidate(conn, candidate_id)
        if candidate is None:
            return None
        state.scope = self._scope_for(candidate)
        if state.round_started_at is None:
            state.round_started_at = now
        classification_repo.mark_viewed(conn, candidate_id, now)
        return candidate

    @staticmethod
    def _scope_for(candidate: Candidate) -> CandidateScope:
        return (
            CandidateScope.UNPROCESSED
            if candidate.state in UNPROCESSED_STATES
            else CandidateScope.PROCESSED
        )

    def _next_unviewed_id(self, conn, state: ClassificationState) -> str | None:
        """当前筛选内下一个尚未查看的待归类候选项。

        选择在数据库里完成：应用层不装配整份候选列表再逐项扫描。
        已处理筛选由此天然不触发逐项自动遍历（未处理状态集合为空）。
        「尚未查看」以本轮浏览路径为准（`viewed_at` 跨轮保留，不能用来判断本轮）：
        路径上有过访问的候选不再自动打开，同一候选重复出现的旧步骤也只排除一次。
        """
        seen = path.candidate_steps(state.path)
        return classification_repo.pick_next_candidate_row(
            conn,
            state.filter_scope.states,
            state.import_date,
            search=state.search,
            result=state.result,
            unprocessed_states=tuple(UNPROCESSED_STATES),
            exclude_ids=seen,
        )

    def _has_next_place(self, conn, state: ClassificationState) -> bool:
        """是否还有可前进的去处。

        界面据此决定「下一个」的可用状态，口径是「按下去会不会有变化」：
        - 当前位置还不是结束卡：末端再前进会打开尚未查看的候选，或者进入结束卡，
          两者都是有效的下一步，因此为真；
        - 已经停在结束卡：只有路径前方还有前进历史（从结束卡回看后再前进），或者
          新出现了尚未查看的候选（新导入）时才有去处，否则按钮不该显示。

        结束卡停在末端且本轮已看过全部候选项、也没有新候选时为假。
        """
        if not state.at_end_step:
            return True
        if path.next_position(
            state.path,
            state.cursor,
            exists=lambda cid: self._candidate_exists(conn, cid),
            current_candidate_id=state.current_candidate_id,
        ) is not None:
            return True
        return self._next_unviewed_id(conn, state) is not None

    def _delta_for(self, conn, opened: Candidate | None) -> ListDelta:
        """这次命令造成的列表变化：被打开的候选行已标记为「已查看」，重新下发该行。

        列表版本只在成员或顺序改变时推进；切卡带来的行变化不推进版本，
        因此响应里带上变更行，前端据此更新列表而不重读整份列表。
        """
        revision = classification_repo.list_revision(conn)
        if opened is None:
            return ListDelta(revision=revision)
        row = classification_repo.candidate_row(conn, opened.candidate_id)
        return ListDelta(revision=revision, changed=(row,) if row else ())

    def return_to_queue(self) -> BrowseResult:
        """返回队列：结束本轮并清空浏览路径，从当前筛选范围重新落位。

        本轮统计与浏览路径同源，因此重置路径即重置本轮：收尾卡不再需要另行保存
        一份「结束时的统计」。返回一致浏览结果，前端不必再发一次列表读取。
        """
        with self._db.transaction() as conn:
            state = classification_repo.get_state(conn)
            now = self._clock.now()
            state.round_started_at = None
            self._clear_position(state)
            state.scope = state.filter_scope
            candidate_id = classification_repo.pick_next_candidate_row(
                conn,
                state.filter_scope.states,
                state.import_date,
                search=state.search,
                result=state.result,
            )
            candidate = (
                classification_repo.get_candidate(conn, candidate_id)
                if candidate_id
                else None
            )
            if candidate is None:
                # 新范围里没有可打开的候选：清空当前卡，如实呈现空状态
                self._clear_position(state)
            else:
                self._go_to(conn, state, candidate, now)
            state.list_revision = classification_repo.list_revision(conn)
            classification_repo.save_state(conn, state, now.isoformat())
            return self._browse_in(conn, state)

    # --- 内部 ---

    def _with_relations(
        self,
        candidate_id: str,
        apply_relations,
        *,
        state: CandidateState,
        action_result: str,
        action: str,
        require_unprocessed: bool = False,
    ) -> ActionOutcome:
        def step(conn) -> None:
            candidate = self._require(conn, candidate_id)
            if require_unprocessed and candidate.state not in UNPROCESSED_STATES:
                raise ClassificationActionNotAllowed(
                    "只有未处理的候选项可以加入观察；已处理项请先主动重新归类"
                )
            apply_relations(conn, candidate.security_id)
            classification_repo.update_state(
                conn, candidate_id, state, action_result, self._clock.now()
            )
            classification_repo.record_history(
                conn,
                candidate_id,
                action=action,
                from_state=candidate.state,
                to_state=state,
                acted_at=self._clock.now(),
            )

        return self._act(candidate_id, step)

    def _act(self, candidate_id: str, apply, *, reorders: bool = False) -> ActionOutcome:
        """写入一次归类动作并回报列表变化。

        `reorders` 表示这次动作可能移动未处理池里的位置（「稍后处理」移尾、主动重新
        归类移首）：只有它才需要比较写入前后的真实行序。普通归类只是让这一只离开
        待归类池，剩余行的相对顺序不变，那次变化由 `removed` 表达，因此既不做全池
        扫描，也不把剩余全体当作 `order` 下发。
        """
        with self._db.transaction() as conn:
            before = self._require(conn, candidate_id)
            # 写入前的列表版本随响应下发：前端据此判断自己手上的列表是不是在这次
            # 写入之前就已过期（外部入口同时改过列表时，这次动作的增量补不齐）。
            revision_before = classification_repo.list_revision(conn)
            pool_before = self._pool_order(conn) if reorders else ()
            apply(conn)
            # 状态、结果与队列位置变化都会改变列表行或列表顺序：同事务推进版本。
            revision = classification_repo.bump_list_revision(conn)
            updated = classification_repo.get_candidate(conn, candidate_id)
            assert updated is not None
            state = classification_repo.get_state(conn)
            order: tuple[str, ...] = ()
            if reorders:
                # 直接比较写入前后的真实行序，改了就下发新顺序（前端据此重排已有行），
                # 没有实际位移就不下发，避免让前端做一次无谓的整列重排。
                pool_after = self._pool_order(conn)
                if pool_after != pool_before:
                    order = pool_after
            # 已处理项离开待归类列表：这次删除由服务端判定并随响应下发，
            # 前端据此更新列表，不自行复制"哪些状态还在待归类池"的规则。
            removed = (
                (candidate_id,)
                if before.state in UNPROCESSED_STATES
                and updated.state not in UNPROCESSED_STATES
                else ()
            )
            if state.current_candidate_id == candidate_id:
                # 当前卡刚被处理完：它已经不在原来那一栏里了，展示页签随之落到它真实
                # 所在的范围，列表与卡片仍属于同一份结果（否则会出现「空列表 + 一张
                # 已处理的卡」，自动推进也会找不到下一只）。筛选范围不动：用户选定的
                # 范围只由他自己改，浏览路径保留，回看仍能回到这次操作前后的历史。
                self._show_current_scope(state, updated.state)
                if state.scope is CandidateScope.UNPROCESSED:
                    # 处理结果只适用于已处理范围：卡片回到待归类时不同时带着
                    # 一个把待归类挡在外面的旧结果筛选。
                    state.result = None
                state.list_revision = revision
                classification_repo.save_state(conn, state, self._clock.now().isoformat())
            return ActionOutcome(
                view=self._views(conn, [updated])[0],
                revision=revision,
                revision_before=revision_before,
                removed=removed,
                navigation_revision=state.navigation_revision,
                order=order,
            )

    def _pool_order(self, conn) -> tuple[str, ...]:
        """未处理池的当前顺序（只取标识）；只给会移动位置的动作在写入前后各取一次。

        普通归类只是让一只离开池子，剩余行的相对顺序不变，因此不调用它、也不下发
        整份行序。
        """
        return classification_repo.list_pool_order(conn, CandidateScope.UNPROCESSED.states)

    def _require(self, conn, candidate_id: str) -> Candidate:
        candidate = classification_repo.get_candidate(conn, candidate_id)
        if candidate is None:
            raise ClassificationUnavailable(f"候选项不存在：{candidate_id}")
        return candidate

    def _views(self, conn, candidates: list[Candidate]) -> list[CandidateView]:
        """批量装配视图：入选日期、来源与观察/笔记标注各自一次查询，避免逐项查库。"""
        ids = tuple(c.candidate_id for c in candidates)
        selections = classification_repo.selections_for(conn, ids)
        sources = classification_repo.sources_for(conn, ids)
        history = classification_repo.history_for(conn, ids)
        views: list[CandidateView] = []
        for candidate in candidates:
            candidate.selections = tuple(selections.get(candidate.candidate_id, []))
            candidate.sources = tuple(
                batch_id for batch_id, _ in sources.get(candidate.candidate_id, [])
            )
            candidate.history = tuple(history.get(candidate.candidate_id, []))
            memberships = observations_repo.memberships_for_security(
                conn, candidate.security_id
            )
            views.append(
                CandidateView(
                    candidate=candidate,
                    security=securities_repo.get(conn, candidate.security_id),
                    sources=tuple(
                        SourceRef(batch_id=batch_id, import_date=import_date)
                        for batch_id, import_date in sources.get(
                            candidate.candidate_id, []
                        )
                    ),
                    observed=bool(memberships),
                    group_ids=tuple(memberships),
                    note_count=notes_repo.count_for_security(
                        conn, candidate.security_id
                    ),
                )
            )
        return views

    def _round_stats_for(self, conn, state: ClassificationState) -> dict[str, int]:
        """本轮统计：已浏览与已处理都只数本轮真正访问过的步骤，剩余数是全局真实值。

        浏览路径记录的就是本轮实际访问的候选卡，因此统计与路径出自同一份事实；
        其他范围的待归类数量照常如实显示，不因为当前范围看完就报 0。
        """
        remaining = sum(
            classification_repo.count_candidates(conn, (s,))
            for s in UNPROCESSED_STATES
        )
        visited = path.candidate_steps(state.path)
        return {
            "viewed": len(visited),
            "processed": classification_repo.count_processed(
                conn, visited, self.PROCESSED_STATES
            ),
            "remaining": remaining,
        }

    def _summary_in(self, conn) -> dict[str, int]:
        counts = classification_repo.count_by_state(conn)
        return {
            "unprocessed": sum(counts.get(s.value, 0) for s in UNPROCESSED_STATES),
            "processed": sum(counts.get(s.value, 0) for s in self.PROCESSED_STATES),
            "pending": counts.get(CandidateState.PENDING.value, 0),
            "later": counts.get(CandidateState.LATER.value, 0),
            "dismissed": counts.get(CandidateState.DISMISSED.value, 0),
            "observed": counts.get(CandidateState.OBSERVED.value, 0),
            "cleared": counts.get(CandidateState.CLEARED.value, 0),
        }
