"""候选归类路由：候选详情、完整浏览与轻量列表、导航增量、归类动作、观察联动结果与主动清理。

这里的请求／响应模型就是归类的传输契约：前端类型由它生成
（`backend/tools/export_openapi.py` → `frontend/src/api/generated/`），字段名、可空性与
取值集合只在服务端模型与领域枚举里维护一处。响应按用途分开建模，不用一个宽泛的可选对象兼作多种结果：

- `CandidatePayload`：普通候选详情（卡片所需的完整资料，没有动作专属字段）；
- `BrowsePayload`：一次一致的浏览读取（当前卡 + 左侧完整轻量列表 + 数量 + 列表版本）；
- `CommandPayload`：导航增量（新浏览状态 + 列表变化，不重传整份列表）；
- `CandidateActionPayload`：归类动作结果（候选详情 + 写入前后的列表版本与行序）；
- `ObservationSavePayload`：观察联动动作结果，复用同一份动作字段（观察工作表也用它）；
- `CleanupCommand`／`CleanupResult`：主动清理待归类池的输入与回执。

请求模型保持迁移前的边界：严格 `expectedCursor`／`expectedRevision` 与不合法的枚举取值
仍返回原来的 400 与提示语，未提交与显式 null 用 `exclude_unset=True` 区分（不能用
`exclude_none=True`，否则主动清空会丢）。字段类型本身不对的输入由框架按结构校验拒绝。
请求侧的取值判定只是把既有 400 翻译回来（见 `_strict_revision`／`_enum_value`），
领域规则仍在服务与领域层，不在 DTO 里新增校验。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import model_validator

from dailyscreen_lite.app.contract import FailurePayload, WireContract
from dailyscreen_lite.classification.service import (
    ActionOutcome,
    BrowseResult,
    CandidateRow,
    CandidateView,
    ClassificationActionNotAllowed,
    ClassificationUnavailable,
    ClassificationView,
    CommandResult,
)
from dailyscreen_lite.domain.models import CandidateScope, CandidateState, Security

if TYPE_CHECKING:
    from dailyscreen_lite.observations.service import ObservationSave

router = APIRouter(prefix="/api/classification", tags=["classification"])


# --- 请求 DTO ---


def _strict_revision(value: Any, field: str) -> int:
    """迁移前的严格整数判定：缺失、布尔值、字符串数字都不是合法游标或修订号。

    `bool` 是 `int` 的子类，pydantic 的宽松模式也会把 `"3"` 读成 3；两者都会让
    `true`／`"3"` 变成合法修订号，因此这里照迁移前的判定拒绝，并保持原来的 400 与
    提示语（交给框架的结构校验会变成 422 与另一种措辞，那是改了既有错误口径）。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(status_code=400, detail={"message": f"缺少有效的 {field}"})
    return value


def _enum_value(enum_cls, value):
    """按迁移前的口径判定枚举取值：不合法仍是 400 与枚举自己的提示语。

    `scope`／`result` 的取值本来由服务转换并映射成 400；DTO 如实声明成枚举后，框架会
    把它们变成 422 与另一套措辞。这里只做「同一条判定 → 原状态码与原提示语」的翻译。
    """
    try:
        return enum_cls(value)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


class NavigationCommand(WireContract):
    """上一个／下一个命令：沿持久化路径移动一步。

    `direction` 保持迁移前的宽松输入（缺省空串，未知方向由领域服务判定成 400）；
    两个修订号是严格整数，见 `_strict_revision`。
    """

    direction: str = ""
    expected_cursor: int
    expected_revision: int

    @model_validator(mode="before")
    @classmethod
    def _strict_wire_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        for field in ("expectedCursor", "expectedRevision"):
            payload[field] = _strict_revision(payload.get(field), field)
        return payload


class ViewChanges(WireContract):
    """修改筛选、视图模式或就地打开某个已有步骤。

    未提交的字段保持缺省，显式 null 是主动清空（`result` 的空值照迁移前等同清空）；
    路由用 `exclude_unset=True` 把这份区别交给服务，不默认补全成 null。
    """

    current_candidate_id: str | None = None
    view_mode: str | None = None
    scope: CandidateScope | None = None
    import_date: str | None = None
    search: str | None = None
    result: CandidateState | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_wire_values(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        changes = dict(data)
        for field, enum_cls in (("scope", CandidateScope), ("result", CandidateState)):
            if field not in changes:
                continue
            raw = changes[field]
            if field == "result" and not raw:
                # 迁移前是 `CandidateState(raw) if raw else None`：空值等同主动清空
                changes[field] = None
                continue
            if raw is not None:
                changes[field] = _enum_value(enum_cls, raw)
        return changes


class CandidateGroupsCommand(WireContract):
    """归类联动动作的请求体：本次动作涉及的观察组。

    缺失与显式 null 都表示没选组（同迁移前），交给服务判定成 400，不在这里收紧成必填；
    非数组取值与含非字符串的数组由结构校验拒绝，取代迁移前「把字符串逐字符拆成组标识」
    与 `str()` 强转的宽松转换。
    """

    group_ids: list[str] | None = None


class CleanupCommand(WireContract):
    """主动清理待归类池的请求体：可限定入选日期。"""

    import_date: str | None = None


class CleanupResult(WireContract):
    """清理回执：本次清理出待归类池的条数。"""

    cleared_count: int


# --- 响应 DTO：候选详情与轻量列表 ---


class SecurityPayload(WireContract):
    """权威证券库中的一条证券身份；未识别时调用方传 None。"""

    security_id: str
    code: str
    exchange: str
    board: str
    name: str
    listing_date: str | None
    is_st: bool


class CandidateSelectionPayload(WireContract):
    """一次每日入选：某股票在某个导入日期首次成功发布的事实。"""

    import_date: str
    batch_id: str
    selected_at: str


class CandidateSourcePayload(WireContract):
    """一次来源追溯：批次与其导入日期。"""

    batch_id: str
    import_date: str


class CandidateHistoryPayload(WireContract):
    """一条历次处理记录；默认收起，通过小触发入口展开。"""

    action: str
    from_state: CandidateState | None
    to_state: CandidateState
    acted_at: str
    detail: str | None


class CandidatePayload(WireContract):
    """普通候选详情：一张股票卡所需的完整资料，不含动作专属的版本字段。"""

    candidate_id: str
    security_id: str
    state: CandidateState
    first_seen_at: str
    last_action_at: str | None
    action_result: str | None
    # 最近一次作为当前股票打开的时间；已查看不等于已处理
    viewed_at: str | None
    # 最近一次入选日期与全部入选日期：跨日合并的候选项在任一天筛选中都指向它
    latest_import_date: str | None
    import_dates: list[str]
    selections: list[CandidateSelectionPayload]
    source_batch_ids: list[str]
    sources: list[CandidateSourcePayload]
    history: list[CandidateHistoryPayload]
    # 已属于观察组即标注，与处理状态无关（新日期入选的已观察股票也要标出）
    observed: bool
    # 所属观察组，供卡片把观察关系明显展示出来
    group_ids: list[str]
    # 该股票跨导入日期共用的笔记条数
    note_count: int
    security: SecurityPayload | None


class CandidateRowPayload(WireContract):
    """候选列表的一行轻量投影：只含左侧列表与卡片归属所需的字段。

    来源明细、笔记内容与完整处理历史不在列表里下发；当前卡的详细资料走 `CandidatePayload`。
    """

    candidate_id: str
    security_id: str
    state: CandidateState
    viewed_at: str | None
    latest_import_date: str | None
    source_count: int
    observed: bool
    security: SecurityPayload | None


class RoundStatsPayload(WireContract):
    """本轮统计：只数本轮真正访问过的步骤，剩余数是全局真实值。"""

    viewed: int
    processed: int
    remaining: int


class CandidateSummaryPayload(WireContract):
    """各处理状态的候选项数量。"""

    unprocessed: int
    processed: int
    pending: int
    later: int
    dismissed: int
    observed: int
    cleared: int


# --- 响应 DTO：写入结果与浏览状态 ---


class CandidateWritePayload(WireContract):
    """写入随候选项下发的版本与列表增量。

    只有动作响应带这些字段，普通读取没有，因此不放进 `CandidatePayload` 变成可选字段。
    `list_revision_before` 是写入前的列表版本：与本地不同说明这次的增量补不齐，
    必须重读完整浏览结果。
    """

    list_revision: int
    list_revision_before: int
    list_removed: list[str]
    navigation_revision: int


class CandidateActionPayload(CandidatePayload, CandidateWritePayload):
    """归类动作结果：候选详情 + 写入版本 + 写入后的真实行序。

    `list_order` 只在这次动作真的改变队列顺序时非空（「稍后处理」移尾）；
    没有改变顺序时是空列表，前端据此重排已有行而不重读整份列表。
    """

    list_order: list[str]


class ClassificationStatePayload(WireContract):
    """浏览状态：当前卡、筛选、路径与版本；不返回列表的轻量响应用它。"""

    current_candidate_id: str | None
    current_candidate: CandidatePayload | None
    view_mode: str
    scope: CandidateScope
    import_date: str | None
    search: str
    result: CandidateState | None
    filter_scope: CandidateScope
    # 浏览路径的每一步是候选卡或结束卡（`~end`）；界面据此判定结束卡与前进历史
    path: list[str]
    cursor: int
    ended: bool
    # 当前卡是否属于左侧当前结果，以及是否还有可前进的去处（结束卡有前进历史时也为真）
    in_filter: bool
    has_next: bool
    navigation_revision: int
    list_revision: int
    round_started_at: str | None
    round: RoundStatsPayload
    summary: CandidateSummaryPayload


class BrowsePayload(ClassificationStatePayload):
    """一次一致的浏览读取：当前卡、左侧完整轻量列表、未处理池与数量。"""

    rows: list[CandidateRowPayload]
    pending: list[CandidateRowPayload]
    dates: list[str]


class ListDeltaPayload(WireContract):
    """一次写入造成的列表变化。

    `order` 只在这次写入改变了队列顺序时非空；前端据此重排已有行。
    """

    changed: list[CandidateRowPayload]
    removed: list[str]
    order: list[str]


class CommandPayload(ClassificationStatePayload):
    """浏览命令结果：新浏览状态与列表变化，不含整份列表。"""

    changes: ListDeltaPayload


class CandidateListPayload(WireContract):
    """候选列表读取：完整详情、数量与全部入选日期。"""

    candidates: list[CandidatePayload]
    summary: CandidateSummaryPayload
    dates: list[str]


class CandidateSearchPayload(WireContract):
    """全局搜索：只返回经过导入与证券识别的股票。"""

    candidates: list[CandidatePayload]


class ObservationCandidatePayload(CandidatePayload, CandidateWritePayload):
    """联动动作下发的候选项：与归类动作同一形状，但没有队列重排。

    加入或保留观察、移出观察都不改变待归类队列顺序，因此不下发行序。
    """


class ObservationSavePayload(CandidateWritePayload):
    """归类联动动作结果：有效关系、写入后的候选项与同一份写入版本。

    与候选归类的动作响应同一形状：前端据此更新列表并带着写入后的导航修订号发出
    自动推进，因此一次归类联动不需要中间再读一遍完整浏览结果。版本号在顶层与
    候选项上各出现一次是既有线格式（前端用候选项里的那份）；是否收拢留给工单 06。
    """

    group_ids: list[str]
    candidate: ObservationCandidatePayload | None


# --- 失败响应 ---


_COMMAND_FAILURES = {
    400: {"model": FailurePayload, "description": "命令无效或当前状态不允许"},
    404: {"model": FailurePayload, "description": "候选项不存在"},
}
_READ_FAILURES = {404: {"model": FailurePayload, "description": "候选项不存在"}}
_NAVIGATION_FAILURES = {
    400: {"model": FailurePayload, "description": "无效的修订号或浏览方向"}
}
# 观察联动端点复用（`app/routes/observations.py`），因此不加下划线
LINKAGE_FAILURES = {
    400: {"model": FailurePayload, "description": "关系无效或当前状态不允许"},
    404: {"model": FailurePayload, "description": "候选项不存在"},
}


# --- 领域到传输模型 ---


def security_payload(security: Security | None) -> SecurityPayload | None:
    """权威证券身份 → 传输模型；未识别时保持 None。"""
    if security is None:
        return None
    return SecurityPayload(
        security_id=security.security_id,
        code=security.code,
        exchange=security.exchange,
        board=security.board,
        name=security.name,
        listing_date=security.listing_date,
        is_st=security.is_st,
    )


def candidate_fields(view: CandidateView) -> dict[str, Any]:
    """候选项的字段映射：详情、浏览、命令与动作结果共用这一份字段对应。

    线格式与可空性由模型声明，映射只做字段对应；时间串口径保持既有 ISO 字符串。
    """
    candidate = view.candidate
    latest = candidate.latest_import_date
    return {
        "candidate_id": candidate.candidate_id,
        "security_id": candidate.security_id,
        "state": candidate.state,
        "first_seen_at": candidate.first_seen_at.isoformat(),
        "last_action_at": (
            candidate.last_action_at.isoformat() if candidate.last_action_at else None
        ),
        "action_result": candidate.action_result,
        "viewed_at": candidate.viewed_at.isoformat() if candidate.viewed_at else None,
        "latest_import_date": latest.iso if latest else None,
        "import_dates": [d.iso for d in candidate.import_dates],
        "selections": [
            CandidateSelectionPayload(
                import_date=selection.import_date.iso,
                batch_id=selection.batch_id,
                selected_at=selection.selected_at.isoformat(),
            )
            for selection in candidate.selections
        ],
        "source_batch_ids": [source.batch_id for source in view.sources],
        "sources": [
            CandidateSourcePayload(
                batch_id=source.batch_id, import_date=source.import_date
            )
            for source in view.sources
        ],
        "history": [
            CandidateHistoryPayload(
                action=entry.action,
                from_state=entry.from_state,
                to_state=entry.to_state,
                acted_at=entry.acted_at.isoformat(),
                detail=entry.detail,
            )
            for entry in candidate.history
        ],
        "observed": bool(view.observed),
        "group_ids": list(view.group_ids),
        "note_count": int(view.note_count),
        "security": security_payload(view.security),
    }


def candidate_payload(view: CandidateView) -> CandidatePayload:
    """普通候选详情（卡片与详情入口）。"""
    return CandidatePayload(**candidate_fields(view))


def row_payload(row: CandidateRow) -> CandidateRowPayload:
    """轻量列表的一行。"""
    return CandidateRowPayload(
        candidate_id=row.candidate_id,
        security_id=row.security_id,
        state=row.state,
        viewed_at=row.viewed_at.isoformat() if row.viewed_at else None,
        latest_import_date=row.latest_import_date,
        source_count=row.source_count,
        observed=bool(row.observed),
        security=security_payload(row.security),
    )


def _state_fields(view: ClassificationView) -> dict[str, Any]:
    """浏览状态的字段映射：完整浏览与导航增量共用。"""
    state = view.state
    return {
        "current_candidate_id": state.current_candidate_id,
        "current_candidate": candidate_payload(view.current) if view.current else None,
        "view_mode": state.view_mode,
        "scope": state.scope,
        "import_date": state.import_date,
        "search": state.search,
        "result": state.result,
        "filter_scope": state.filter_scope,
        "path": list(state.path),
        "cursor": state.cursor,
        "ended": state.ended,
        "in_filter": bool(view.in_filter),
        "has_next": bool(view.has_next),
        "navigation_revision": state.navigation_revision,
        "list_revision": state.list_revision,
        "round_started_at": (
            state.round_started_at.isoformat() if state.round_started_at else None
        ),
        "round": RoundStatsPayload(**view.round),
        "summary": CandidateSummaryPayload(**view.summary),
    }


def browse_payload(result: BrowseResult) -> BrowsePayload:
    """一次一致的浏览读取。"""
    return BrowsePayload(
        **_state_fields(result),
        rows=[row_payload(row) for row in result.rows],
        pending=[row_payload(row) for row in result.pending],
        dates=list(result.dates),
    )


def command_payload(result: CommandResult) -> CommandPayload:
    """浏览命令结果：新浏览状态与列表变化。"""
    delta = result.delta
    return CommandPayload(
        **_state_fields(result.view),
        changes=ListDeltaPayload(
            changed=[row_payload(row) for row in delta.changed],
            removed=list(delta.removed),
            order=list(delta.order),
        ),
    )


def action_payload(outcome: ActionOutcome) -> CandidateActionPayload:
    """归类动作结果：候选详情 + 写入前后的版本与行序。"""
    return CandidateActionPayload(
        **candidate_fields(outcome.view),
        list_revision=outcome.revision,
        list_revision_before=outcome.revision_before,
        list_removed=list(outcome.removed),
        navigation_revision=outcome.navigation_revision,
        list_order=list(outcome.order),
    )


def observation_save_payload(result: ObservationSave) -> ObservationSavePayload:
    """归类联动动作结果；观察工作表（工单 06）复用同一份契约。"""
    candidate = None
    if result.candidate is not None:
        candidate = ObservationCandidatePayload(
            **candidate_fields(result.candidate),
            list_revision=result.list_revision,
            list_revision_before=result.list_revision_before,
            list_removed=list(result.removed),
            navigation_revision=result.navigation_revision,
        )
    return ObservationSavePayload(
        group_ids=list(result.group_ids),
        candidate=candidate,
        list_revision=result.list_revision,
        list_revision_before=result.list_revision_before,
        list_removed=list(result.removed),
        navigation_revision=result.navigation_revision,
    )


# --- 路由 ---


def _classification(request: Request):
    return request.app.state.container.classification


def _run(action):
    """候选项操作统一错误映射：不存在 → 404，状态不允许 → 400。"""
    try:
        return action()
    except ClassificationUnavailable as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    except ClassificationActionNotAllowed as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


@router.get("/candidates", response_model=CandidateListPayload)
def list_candidates(
    request: Request,
    scope: CandidateScope = CandidateScope.UNPROCESSED,
    importDate: str | None = None,
    search: str | None = None,
    result: CandidateState | None = None,
) -> CandidateListPayload:
    service = _classification(request)
    views = service.list_candidates(
        scope=scope, import_date=importDate, search=search, result=result
    )
    return CandidateListPayload(
        candidates=[candidate_payload(view) for view in views],
        summary=CandidateSummaryPayload(**service.summary()),
        dates=service.dates(),
    )


@router.get("/search", response_model=CandidateSearchPayload)
def search(request: Request, q: str = "") -> CandidateSearchPayload:
    """全局搜索：只返回经过导入与证券识别的股票，供跳转到候选归类。"""
    views = _classification(request).search(q)
    return CandidateSearchPayload(candidates=[candidate_payload(view) for view in views])


@router.get("/view", response_model=BrowsePayload)
def get_view(request: Request, scope: CandidateScope | None = None) -> BrowsePayload:
    """统一浏览结果：同一读取视图内的当前卡、左侧完整轻量列表、数量与列表版本。

    scope 是请求方当前显示的页签（可选）：归类动作会临时把真实页签切到当前卡的
    真实归属，读列表的一方仍可停在原页签上。
    """
    return browse_payload(_classification(request).browse(scope))


@router.put("/view", response_model=BrowsePayload, responses=_COMMAND_FAILURES)
def update_view(request: Request, payload: ViewChanges) -> BrowsePayload:
    """修改筛选、视图模式或就地打开某个已有步骤；返回新的一致浏览结果。

    主动改筛选开启新一轮；`currentCandidateId` 就地打开（不改变前后历史）。
    这些命令都会改变列表范围或当前卡，因此直接返回新的一致结果，
    前端不需要再补一次列表读取。手动打开候选请用 `focus`。
    """
    changes = payload.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return browse_payload(_run(lambda: _classification(request).update_view(changes)))


@router.post(
    "/view/navigate", response_model=CommandPayload, responses=_NAVIGATION_FAILURES
)
def navigate(request: Request, payload: NavigationCommand) -> CommandPayload:
    """按持久化游标前后浏览；只回传新卡片、浏览状态与列表变化，不重传整份列表。

    过期游标返回当前真实位置以便安全重试。
    """
    return command_payload(
        _run(
            lambda: _classification(request).navigate(
                payload.direction,
                expected_cursor=payload.expected_cursor,
                expected_revision=payload.expected_revision,
            )
        )
    )


@router.post("/view/return", response_model=BrowsePayload)
def return_to_queue(request: Request) -> BrowsePayload:
    """返回队列：结束并重置本轮，从当前筛选范围重新落位；返回一致浏览结果。"""
    return browse_payload(_classification(request).return_to_queue())


@router.get(
    "/candidates/{candidate_id}", response_model=CandidatePayload, responses=_READ_FAILURES
)
def get_candidate(candidate_id: str, request: Request) -> CandidatePayload:
    return candidate_payload(_run(lambda: _classification(request).get_candidate(candidate_id)))


# 归类动作响应：已保存的候选项、本次离开列表的行与写入前后的版本。
# 动作与导航仍是两个确认步骤：这里只报事实，前端据此更新列表并带着写入后的导航
# 修订号发出自动推进，因此一次归类不需要中间再读一遍完整浏览结果。
# `listRevisionBefore` 让前端判断自己手上的列表在这次写入之前是否已经过期（另一个
# 入口同时改过列表时增量补不齐，必须重读完整结果）；队列顺序被这次动作真的改变时
# （「稍后处理」移尾）额外下发写入后的真实行序。


@router.post(
    "/candidates/{candidate_id}/later",
    response_model=CandidateActionPayload,
    responses=_COMMAND_FAILURES,
)
def later(candidate_id: str, request: Request) -> CandidateActionPayload:
    """稍后处理：仍在待归类池，移到队尾。"""
    return action_payload(_run(lambda: _classification(request).later(candidate_id)))


@router.post(
    "/candidates/{candidate_id}/dismiss",
    response_model=CandidateActionPayload,
    responses=_COMMAND_FAILURES,
)
def dismiss(candidate_id: str, request: Request) -> CandidateActionPayload:
    """暂不关注：只结束本次归类，不删除导入事实。"""
    return action_payload(_run(lambda: _classification(request).dismiss(candidate_id)))


@router.post(
    "/candidates/{candidate_id}/reclassify",
    response_model=CandidateActionPayload,
    responses=_COMMAND_FAILURES,
)
def reclassify(candidate_id: str, request: Request) -> CandidateActionPayload:
    """主动重新归类：复用同一候选项，保留历史并调到队首。"""
    return action_payload(_run(lambda: _classification(request).reclassify(candidate_id)))


@router.post(
    "/candidates/{candidate_id}/focus",
    response_model=BrowsePayload,
    responses=_READ_FAILURES,
)
def focus(candidate_id: str, request: Request) -> BrowsePayload:
    """手动打开候选卡：作为浏览路径的新一步，不改筛选、不改未处理池顺序。

    目标不在当前筛选结果里也照常打开，响应里的 `inFilter` 让界面明确提示；
    已处理候选同样可以打开查看（自动推进仍只在待归类范围内找人）。
    """
    return browse_payload(_run(lambda: _classification(request).focus(candidate_id)))


@router.post("/cleanup", response_model=CleanupResult)
def cleanup(request: Request, payload: CleanupCommand | None = None) -> CleanupResult:
    """主动清理待归类池；可限定入选日期。不删除导入事实。

    没有请求体或 `importDate` 为 null 都表示不限日期（同迁移前）。
    """
    cleared = _classification(request).cleanup(
        import_date=payload.import_date if payload else None
    )
    return CleanupResult(cleared_count=cleared)
