"""观察组：组管理、成员关系、观察列表与浏览上下文。

业务要点：
- 观察组只有创建、重命名与删除，没有归档；删除只移除该组关系，不影响其他组、
  候选处理状态、笔记、来源与处理历史；
- 一只股票可属于多个组，可转组、可退出全部组；观察组页面按整组替换保存关系，
  因此页面上看到的选择就是当前事实；
- 「加入或保留观察」只增加所选关系、保留原有关系，并在同一事务里完成本次归类；
- 「移出观察组」是组合归类动作：移除所选关系，同时记为暂不关注；
- 观察关系的增删与删除组都不反向改变候选处理状态；
- 观察列表与共用个股详情以证券为身份，来源与处理记录取自该证券的候选项。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace

from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.models import (
    DEFAULT_OBSERVATION_SORT,
    OBSERVATION_SORTS,
    SEED_DEFAULT_OBSERVATION_GROUP,
    UNPROCESSED_STATES,
    ObservationGroup,
    ObservationState,
    ObservedStock,
    Security,
)
from dailyscreen_lite.repository import (
    Database,
    classification_repo,
    observations_repo,
    securities_repo,
)
from dailyscreen_lite.classification.service import CandidateView, ClassificationService

MAX_GROUP_NAME = 30
# 观察列表的「全部观察股票」汇总视图：不属于任何组行
ALL_GROUPS = "all"


class ObservationUnavailable(LookupError):
    """观察组或证券不存在。"""


class ObservationInvalid(ValueError):
    """观察操作不符合规则（空名、重名、未选择组、未导入股票）。"""


@dataclass(frozen=True)
class ObservationSave:
    """保存观察关系的结果：候选项、有效组成员关系、本次离开列表的行与写入前后的版本。

    `navigation_revision`、`list_revision` 与 `list_revision_before` 一起随响应下发：
    这两个联动动作也完成一次归类，前端据此发出保存后的自动推进，不必为一次归类重读
    整份浏览结果；`list_revision_before` 是写入前的列表版本，前端用它判断自己手上的
    列表是不是在这次写入之前就已过期（另一个入口刚改过列表时必须重读完整结果）。
    """

    group_ids: tuple[str, ...]
    candidate: CandidateView | None
    list_revision: int = 0
    list_revision_before: int = 0
    removed: tuple[str, ...] = ()
    navigation_revision: int = 0


@dataclass(frozen=True)
class StockDetail:
    """共用个股详情：以股票为身份，跨导入日期与入口共享。

    candidate 为 None 只可能出现在证券库与候选不一致的异常库上，正常流程里
    被观察股票都来自成功导入，因此总能读到来源与处理记录。
    """

    security_id: str
    security: Security | None
    group_ids: tuple[str, ...]
    candidate: CandidateView | None


@dataclass(frozen=True)
class ObservationView:
    """观察组模块的完整视图：组、列表与浏览上下文。"""

    groups: tuple[ObservationGroup, ...]
    stocks: tuple[ObservedStock, ...]
    state: ObservationState


def _normalize_name(raw: str) -> str:
    name = re.sub(r"\s+", " ", (raw or "").strip())
    if not name:
        raise ObservationInvalid("观察组名称不能为空")
    if len(name) > MAX_GROUP_NAME:
        raise ObservationInvalid(f"观察组名称不能超过 {MAX_GROUP_NAME} 个字符")
    return name


class ObservationService:
    def __init__(
        self, db: Database, clock: Clock, classification: ClassificationService
    ) -> None:
        self._db = db
        self._clock = clock
        self._classification = classification

    # --- 组管理 ---

    def ensure_default_group(self) -> None:
        """首次启动种下默认组；用户删除后重启不再重建。"""
        with self._db.transaction() as conn:
            observations_repo.ensure_default_group(
                conn,
                self._clock.now().isoformat(),
                SEED_DEFAULT_OBSERVATION_GROUP,
            )

    def list_groups(self) -> list[ObservationGroup]:
        with self._db.read() as conn:
            return observations_repo.list_groups(conn)

    def create_group(self, name: str) -> ObservationGroup:
        clean = _normalize_name(name)
        with self._db.transaction() as conn:
            if observations_repo.find_by_name(conn, clean) is not None:
                raise ObservationInvalid(f"已存在同名观察组：{clean}")
            group_id = f"group-{uuid.uuid4().hex[:12]}"
            now = self._clock.now().isoformat()
            observations_repo.insert_group(conn, group_id=group_id, name=clean, created_at=now)
            group = observations_repo.get_group(conn, group_id)
        assert group is not None
        return group

    def rename_group(self, group_id: str, name: str) -> ObservationGroup:
        clean = _normalize_name(name)
        with self._db.transaction() as conn:
            group = observations_repo.get_group(conn, group_id)
            if group is None:
                raise ObservationUnavailable(f"观察组不存在：{group_id}")
            existing = observations_repo.find_by_name(conn, clean)
            if existing is not None and existing.group_id != group_id:
                raise ObservationInvalid(f"已存在同名观察组：{clean}")
            observations_repo.rename_group(conn, group_id, clean, self._clock.now().isoformat())
            updated = observations_repo.get_group(conn, group_id)
        assert updated is not None
        return updated

    def delete_group(self, group_id: str) -> None:
        """删除组及其成员关系：不删除股票来源、笔记或处理历史，也不影响其他组。"""
        with self._db.transaction() as conn:
            if observations_repo.get_group(conn, group_id) is None:
                raise ObservationUnavailable(f"观察组不存在：{group_id}")
            observations_repo.delete_group(conn, group_id)
            self._clear_missing_current(conn)
            # 观察关系是候选列表行的一部分（「已观察」标注）：同事务推进列表版本。
            classification_repo.bump_list_revision(conn)

    # --- 列表与详情 ---

    def stocks(self, group_id: str | None = None) -> list[ObservedStock]:
        """观察列表：按证券去重；group_id 为空或 all 时是汇总视图。

        指定不存在的组明确报错，而不是把空列表当成「这个组没有股票」。
        """
        with self._db.read() as conn:
            target = _group_filter(group_id)
            if target is not None and observations_repo.get_group(conn, target) is None:
                raise ObservationUnavailable(f"观察组不存在：{target}")
            return self._with_securities(
                conn, observations_repo.observed_stocks(conn, target)
            )

    def stock_detail(self, security_id: str) -> StockDetail:
        """共用个股详情：观察关系 + 该证券的行情归属、笔记与处理记录入口。"""
        candidate = self._classification.view_for_security(security_id)
        with self._db.read() as conn:
            security = securities_repo.get(conn, security_id)
            group_ids = tuple(
                observations_repo.memberships_for_security(conn, security_id)
            )
        if security is None and candidate is None:
            raise ObservationUnavailable(f"没有该股票：{security_id}")
        return StockDetail(
            security_id=security_id,
            security=security,
            group_ids=group_ids,
            candidate=candidate,
        )

    # --- 浏览上下文 ---

    def get_view(self) -> ObservationView:
        with self._db.read() as conn:
            return self._view_in(conn)

    def update_view(self, changes: dict) -> ObservationView:
        """更新当前组、排序或当前股票；当前组不存在时回落到汇总视图。"""
        with self._db.transaction() as conn:
            state = observations_repo.get_state(conn)
            if "groupId" in changes:
                state.group_id = _group_filter(changes["groupId"])
                if state.group_id is not None and (
                    observations_repo.get_group(conn, state.group_id) is None
                ):
                    state.group_id = None
            if "sort" in changes and changes["sort"] is not None:
                sort = str(changes["sort"])
                if sort not in OBSERVATION_SORTS:
                    raise ObservationInvalid(f"未知排序方式：{sort}")
                state.sort = sort
            if "currentSecurityId" in changes:
                current = changes["currentSecurityId"]
                state.current_security_id = (
                    str(current) if current and self._is_visible(conn, state, str(current)) else None
                )
            self._clear_missing_current(conn, state)
            observations_repo.save_state(conn, state, self._clock.now().isoformat())
            return self._view_in(conn)

    def focus(self, security_id: str) -> ObservationView:
        """跨模块打开某只股票的详情：切到汇总视图并把它设为当前股票。

        详情以股票为身份、与组无关，所以不保留原来的单组筛选，避免目标被筛掉看不见。
        """
        with self._db.transaction() as conn:
            memberships = observations_repo.memberships_for_security(conn, security_id)
            if not memberships:
                raise ObservationUnavailable(f"股票不在观察组中：{security_id}")
            state = observations_repo.get_state(conn)
            state.group_id = None
            state.current_security_id = security_id
            observations_repo.save_state(conn, state, self._clock.now().isoformat())
            return self._view_in(conn)

    # --- 成员关系 ---

    def memberships(self, security_id: str) -> tuple[str, ...]:
        with self._db.read() as conn:
            return tuple(observations_repo.memberships_for_security(conn, security_id))

    def save_membership(self, security_id: str, group_ids: list[str]) -> tuple[str, ...]:
        """整组替换（观察组页面的转组、增减归属与退出全部组）。

        不改变候选处理状态；未选中的关系会被删除，因此页面上的选择就是当前事实。
        保留的关系沿用加入时间；新增或移出后重新加入的关系记录本次时间。
        只有经过导入与归类（或已经在观察组里）的股票可以调整观察关系，
        观察页面没有绕过导入与归类的添加入口。
        """
        self._require_observable(security_id)
        with self._db.transaction() as conn:
            selected = self._selectable(conn, group_ids)
            now = self._clock.now().isoformat()
            existing = observations_repo.memberships_for_security(conn, security_id)
            observations_repo.remove_members(
                conn, security_id, [group_id for group_id in existing if group_id not in selected]
            )
            for group_id in selected:
                observations_repo.add_member(conn, group_id, security_id, now)
            self._clear_missing_current(conn)
            # 观察关系是候选列表行的一部分（「已观察」标注）：同事务推进列表版本。
            classification_repo.bump_list_revision(conn)
            return tuple(selected)

    def observe_candidate(self, candidate_id: str, group_ids: list[str]) -> ObservationSave:
        """加入或保留观察：增加所选关系并完成本次归类（同一事务，失败一起回滚）。

        只增加不退订：归类动作保留股票原有的观察关系。
        """
        if not group_ids:
            raise ObservationInvalid("请至少选择一个观察组；暂不关注请使用「暂不关注」")
        # 先确认候选项存在（不存在 → 404），再校验组，避免把未知候选项报成组错误
        self._classification.security_id_for_candidate(candidate_id)
        selected = self._validated_groups(group_ids)
        kept: list[str] = []

        def add_relations(conn, security_id: str) -> None:
            now = self._clock.now().isoformat()
            for group_id in selected:
                observations_repo.add_member(conn, group_id, security_id, now)
            kept[:] = list(observations_repo.memberships_for_security(conn, security_id))

        outcome = self._classification.observe(candidate_id, add_relations)
        return ObservationSave(
            group_ids=tuple(kept),
            candidate=outcome.view,
            list_revision=outcome.revision,
            list_revision_before=outcome.revision_before,
            removed=outcome.removed,
            navigation_revision=outcome.navigation_revision,
        )

    def remove_candidate_from_groups(
        self, candidate_id: str, group_ids: list[str]
    ) -> ObservationSave:
        """组合归类动作：移除所选观察关系，同时记为暂不关注。

        移除只在用户选中的关系上进行；取消时前端不调用本接口，状态不变。
        """
        if not group_ids:
            raise ObservationInvalid("请至少选择一个要移出的观察组")
        self._classification.security_id_for_candidate(candidate_id)
        selected = self._validated_groups(group_ids)
        kept: list[str] = []

        def remove_relations(conn, security_id: str) -> None:
            observations_repo.remove_members(conn, security_id, selected)
            kept[:] = list(observations_repo.memberships_for_security(conn, security_id))

        outcome = self._classification.leave_groups(candidate_id, remove_relations)
        return ObservationSave(
            group_ids=tuple(kept),
            candidate=outcome.view,
            list_revision=outcome.revision,
            list_revision_before=outcome.revision_before,
            removed=outcome.removed,
            navigation_revision=outcome.navigation_revision,
        )

    # --- 内部 ---

    def _require_observable(self, security_id: str) -> None:
        """观察关系只能建立在已归类或已在观察组的股票上。

        未导入的证券不能进入观察体系（404）；已导入但从未归类的股票要先去候选归类
        （400），避免观察页面成为绕过导入与归类的添加入口。
        """
        view = self._classification.view_for_security(security_id)
        if view is None:
            raise ObservationUnavailable(
                f"股票未经导入与识别，不能加入观察组：{security_id}"
            )
        with self._db.read() as conn:
            already_observed = bool(
                observations_repo.memberships_for_security(conn, security_id)
            )
        if not already_observed and view.candidate.state in UNPROCESSED_STATES:
            raise ObservationInvalid(
                f"股票尚未完成归类，请先在候选归类中处理：{security_id}"
            )

    def _validated_groups(self, group_ids: list[str]) -> list[str]:
        with self._db.read() as conn:
            return self._selectable(conn, group_ids)

    @staticmethod
    def _selectable(conn, group_ids: list[str]) -> list[str]:
        """校验组存在并按用户选择顺序去重。"""
        selected: list[str] = []
        for group_id in group_ids:
            if observations_repo.get_group(conn, group_id) is None:
                raise ObservationInvalid(f"观察组不存在：{group_id}")
            if group_id not in selected:
                selected.append(group_id)
        return selected

    @staticmethod
    def _is_visible(conn, state: ObservationState, security_id: str) -> bool:
        """当前股票是否在所选视图里可见：切组或被移出后不保留不可见目标。"""
        memberships = observations_repo.memberships_for_security(conn, security_id)
        if not memberships:
            return False
        return state.group_id is None or state.group_id in memberships

    def _clear_missing_current(
        self, conn, state: ObservationState | None = None
    ) -> None:
        """目标已不在观察范围时清空当前股票，避免详情停在已删除的目标上。

        传入 state 时只改内存对象（调用方随后统一保存）；不传则读库并就地保存，
        用于删除组、整组替换这类会一次性改变可见范围的写操作。
        """
        if state is not None:
            if state.current_security_id and not self._is_visible(
                conn, state, state.current_security_id
            ):
                state.current_security_id = None
            return
        stored = observations_repo.get_state(conn)
        if stored.current_security_id and not self._is_visible(
            conn, stored, stored.current_security_id
        ):
            stored.current_security_id = None
            observations_repo.save_state(conn, stored, self._clock.now().isoformat())

    def _view_in(self, conn) -> ObservationView:
        groups = observations_repo.list_groups(conn)
        state = observations_repo.get_state(conn)
        # 存的组已被删除时回到汇总视图，不把空列表当成「这只组没有股票」
        if state.group_id is not None and observations_repo.get_group(
            conn, state.group_id
        ) is None:
            state.group_id = None
        self._clear_missing_current(conn, state)
        stocks = observations_repo.observed_stocks(conn, _group_filter(state.group_id))
        return ObservationView(
            groups=tuple(groups),
            stocks=tuple(self._with_securities(conn, stocks)),
            state=state,
        )

    @staticmethod
    def _with_securities(conn, stocks: list[ObservedStock]) -> list[ObservedStock]:
        """批量补上权威证券库身份，列表据此显示名称与代码。"""
        securities = securities_repo.get_many(conn, [s.security_id for s in stocks])
        return [
            replace(stock, security=securities.get(stock.security_id))
            for stock in stocks
        ]


def _group_filter(value) -> str | None:
    """把请求里的组标识归一成查询用值：空、all 或未知 → 汇总视图。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == ALL_GROUPS:
        return None
    return text


__all__ = [
    "ALL_GROUPS",
    "DEFAULT_OBSERVATION_SORT",
    "ObservationInvalid",
    "ObservationSave",
    "ObservationService",
    "ObservationUnavailable",
    "ObservationView",
    "StockDetail",
]
