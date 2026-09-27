"""观察组路由：组管理、观察工作表与浏览上下文、共用详情，以及归类联动动作。

只调用应用服务；组只有删除，关系按整组替换保存，观察列表与详情以证券为身份。
这里的请求／响应模型就是观察工作表的传输契约：前端类型由它生成
（`backend/tools/export_openapi.py` → `frontend/src/api/generated/`），字段名、可空性与
取值集合只在服务端模型与领域枚举里维护一处。

响应按用途分开：组列表（`GroupListPayload`）、观察工作表与浏览上下文
（`ObservationViewPayload`，含 `groups`／`stocks`／`state`）、轻量股票表
（`ObservationStockListPayload`）、共用详情（`StockDetailPayload`）与成员关系
（`MembershipPayload`）。归类联动的两个动作复用候选归类的动作契约
（`app/routes/classification.py`），响应形状与归类动作一致。

请求模型保持迁移前的边界：缺省与显式 null 的区别照旧（按 key 是否存在判定），
不合法排序仍是原来的 400 与提示语；类型本身不对的输入由框架按结构校验拒绝。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from dailyscreen_lite.app.contract import FailurePayload, WireContract
from dailyscreen_lite.app.routes.classification import (
    LINKAGE_FAILURES,
    CandidateGroupsCommand,
    CandidatePayload,
    ObservationSavePayload,
    SecurityPayload,
    candidate_payload,
    observation_save_payload,
    security_payload,
)
from dailyscreen_lite.classification.service import (
    ClassificationActionNotAllowed,
    ClassificationUnavailable,
)
from dailyscreen_lite.observations.service import ObservationInvalid, ObservationUnavailable

router = APIRouter(prefix="/api/observations", tags=["observations"])


# --- 请求 DTO ---


class GroupNameCommand(WireContract):
    """新建与重命名观察组的请求体：只认识组名。

    空名、纯空白与超长仍由领域校验拒绝成 400；组名是字符串线格式，
    因此非字符串取值由结构校验拒绝，不再先 `str()` 成字符串再建组。
    """

    name: str | None = None


class ObservationViewChanges(WireContract):
    """观察工作表的部分修改：当前组、排序与当前股票。

    未提交的字段不动，显式 null 才是主动清空（组标识为 `all` 或空串同样回到汇总视图）；
    `sort` 为 null 照迁移前忽略，未知排序仍由服务读取值判定成 400 与原提示语
    （因此这里不重复一份排序白名单，非字符串取值由结构校验拒绝）。
    """

    group_id: str | None = None
    sort: str | None = None
    current_security_id: str | None = None


class MembershipCommand(WireContract):
    """整组替换某证券观察关系的请求体：证券标识必填、组列表可空。

    空组列表（或缺省、显式 null）表示退出全部组；缺失证券标识仍是原来的 400。
    """

    security_id: str = ""
    group_ids: list[str] | None = None


# --- 响应 DTO ---


class ObservationGroupPayload(WireContract):
    """观察组：只有创建、重命名与删除，没有归档。"""

    group_id: str
    name: str
    is_default: bool
    member_count: int


class GroupListPayload(WireContract):
    """组列表：组名与成员数量来自服务端。"""

    groups: list[ObservationGroupPayload]


class ObservedStockPayload(WireContract):
    """观察列表的一行：按证券去重，带所属全部现存组与加入时间。

    行情不在这里下发：列表用 `/api/quotes/summary` 批量取轻量摘要，
    缺失行情时界面显示为空，不阻塞浏览。
    """

    security_id: str
    group_ids: list[str]
    joined_at: str
    security: SecurityPayload | None


class ObservationStockListPayload(WireContract):
    """按可选组筛选的观察列表。"""

    stocks: list[ObservedStockPayload]


class ObservationStatePayload(WireContract):
    """观察组模块自己的浏览上下文：当前组、排序与当前股票。

    `sort` 是文本列（取值集合由领域维护），照实声明为字符串；未知取值与空名一样
    仍由服务判定成 400 与原提示语。
    """

    group_id: str | None
    sort: str
    current_security_id: str | None


class ObservationViewPayload(WireContract):
    """观察工作表的完整视图：组、观察列表与浏览上下文。"""

    groups: list[ObservationGroupPayload]
    stocks: list[ObservedStockPayload]
    state: ObservationStatePayload


class StockDetailPayload(WireContract):
    """共用个股详情：股票信息、观察关系与来源／处理记录入口。"""

    security_id: str
    security: SecurityPayload | None
    group_ids: list[str]
    candidate: CandidatePayload | None


class MembershipPayload(WireContract):
    """某证券当前的观察关系；写入返回的是生效后的关系。"""

    security_id: str
    group_ids: list[str]


class DeleteGroupResponse(WireContract):
    """删除组成功只回执被删的组。"""

    deleted: str


# --- 失败响应 ---


_GROUP_WRITE_FAILURES = {
    400: {"model": FailurePayload, "description": "组名不合规或重名"}
}
_GROUP_UPDATE_FAILURES = {
    400: {"model": FailurePayload, "description": "组名不合规或重名"},
    404: {"model": FailurePayload, "description": "观察组不存在"},
}
_GROUP_READ_FAILURES = {404: {"model": FailurePayload, "description": "观察组不存在"}}
_VIEW_FAILURES = {400: {"model": FailurePayload, "description": "未知排序方式"}}
_STOCK_FAILURES = {404: {"model": FailurePayload, "description": "证券或观察关系不存在"}}
_MEMBERSHIP_FAILURES = {
    400: {"model": FailurePayload, "description": "缺少证券标识、未完成归类或组不存在"},
    404: {"model": FailurePayload, "description": "股票未经导入与识别"},
}


# --- 领域到传输模型 ---


def group_payload(group) -> ObservationGroupPayload:
    """观察组 → 传输模型。"""
    return ObservationGroupPayload(
        group_id=group.group_id,
        name=group.name,
        is_default=group.is_default,
        member_count=group.member_count,
    )


def observed_stock_payload(stock) -> ObservedStockPayload:
    """观察列表的一行 → 传输模型。"""
    return ObservedStockPayload(
        security_id=stock.security_id,
        group_ids=list(stock.group_ids),
        joined_at=stock.joined_at.isoformat(),
        security=security_payload(stock.security),
    )


def observation_view_payload(view) -> ObservationViewPayload:
    """观察工作表的完整视图。"""
    return ObservationViewPayload(
        groups=[group_payload(group) for group in view.groups],
        stocks=[observed_stock_payload(stock) for stock in view.stocks],
        state=ObservationStatePayload(
            group_id=view.state.group_id,
            sort=view.state.sort,
            current_security_id=view.state.current_security_id,
        ),
    )


def stock_detail_payload(detail) -> StockDetailPayload:
    """共用个股详情。"""
    return StockDetailPayload(
        security_id=detail.security_id,
        security=security_payload(detail.security),
        group_ids=list(detail.group_ids),
        candidate=candidate_payload(detail.candidate) if detail.candidate else None,
    )


# --- 路由 ---


def _observations(request: Request):
    return request.app.state.container.observations


def _run(action):
    """统一错误映射：不存在 → 404，规则不允许 → 400。"""
    try:
        return action()
    except (ObservationUnavailable, ClassificationUnavailable) as exc:
        raise HTTPException(status_code=404, detail={"message": str(exc)}) from exc
    except (ObservationInvalid, ClassificationActionNotAllowed) as exc:
        raise HTTPException(status_code=400, detail={"message": str(exc)}) from exc


# --- 组管理 ---


@router.get("/groups", response_model=GroupListPayload)
def list_groups(request: Request) -> GroupListPayload:
    groups = _observations(request).list_groups()
    return GroupListPayload(groups=[group_payload(group) for group in groups])


@router.post("/groups", response_model=ObservationGroupPayload, responses=_GROUP_WRITE_FAILURES)
def create_group(request: Request, payload: GroupNameCommand) -> ObservationGroupPayload:
    group = _run(lambda: _observations(request).create_group(payload.name or ""))
    return group_payload(group)


@router.patch(
    "/groups/{group_id}",
    response_model=ObservationGroupPayload,
    responses=_GROUP_UPDATE_FAILURES,
)
def rename_group(
    group_id: str, request: Request, payload: GroupNameCommand
) -> ObservationGroupPayload:
    group = _run(lambda: _observations(request).rename_group(group_id, payload.name or ""))
    return group_payload(group)


@router.delete(
    "/groups/{group_id}", response_model=DeleteGroupResponse, responses=_GROUP_READ_FAILURES
)
def delete_group(group_id: str, request: Request) -> DeleteGroupResponse:
    """删除组及其成员关系：不删除来源、笔记与处理历史，也不影响其他组。"""
    _run(lambda: _observations(request).delete_group(group_id))
    return DeleteGroupResponse(deleted=group_id)


# --- 观察工作表与共用详情 ---


@router.get("/view", response_model=ObservationViewPayload)
def get_view(request: Request) -> ObservationViewPayload:
    """观察组模块的组、列表与浏览上下文，用于刷新与重启后恢复。"""
    return observation_view_payload(_observations(request).get_view())


@router.put("/view", response_model=ObservationViewPayload, responses=_VIEW_FAILURES)
def update_view(request: Request, payload: ObservationViewChanges) -> ObservationViewPayload:
    """更新当前组、排序或当前股票；只影响观察组模块自身的工作位置。"""
    changes = payload.model_dump(by_alias=True, exclude_unset=True, mode="json")
    return observation_view_payload(_run(lambda: _observations(request).update_view(changes)))


@router.get(
    "/securities", response_model=ObservationStockListPayload, responses=_STOCK_FAILURES
)
def list_stocks(request: Request, groupId: str | None = None) -> ObservationStockListPayload:
    """观察列表：按证券去重，带所属全部现存组与加入时间；未知组明确报 404。"""
    stocks = _run(lambda: _observations(request).stocks(groupId))
    return ObservationStockListPayload(
        stocks=[observed_stock_payload(stock) for stock in stocks]
    )


@router.get(
    "/securities/{security_id}", response_model=StockDetailPayload, responses=_STOCK_FAILURES
)
def stock_detail(security_id: str, request: Request) -> StockDetailPayload:
    """共用个股详情：股票信息、观察关系与该证券的行情归属、笔记与处理记录入口。"""
    detail = _run(lambda: _observations(request).stock_detail(security_id))
    return stock_detail_payload(detail)


@router.post(
    "/securities/{security_id}/focus",
    response_model=ObservationViewPayload,
    responses=_STOCK_FAILURES,
)
def focus_stock(security_id: str, request: Request) -> ObservationViewPayload:
    """跨模块打开详情：切到汇总视图并把这支股票设为观察模块的当前股票。"""
    return observation_view_payload(_run(lambda: _observations(request).focus(security_id)))


# --- 成员关系 ---


@router.get("/memberships", response_model=MembershipPayload)
def memberships(request: Request, securityId: str) -> MembershipPayload:
    """某股票当前所属的观察组，用于卡片与详情预选。"""
    group_ids = _observations(request).memberships(securityId)
    return MembershipPayload(security_id=securityId, group_ids=list(group_ids))


@router.put("/memberships", response_model=MembershipPayload, responses=_MEMBERSHIP_FAILURES)
def save_membership(request: Request, payload: MembershipCommand) -> MembershipPayload:
    """整组替换（观察组页面的转组、增减归属、退出全部组），不改变候选处理状态。"""
    if not payload.security_id:
        raise HTTPException(status_code=400, detail={"message": "缺少证券标识"})
    kept = _run(
        lambda: _observations(request).save_membership(
            payload.security_id, payload.group_ids or []
        )
    )
    return MembershipPayload(security_id=payload.security_id, group_ids=list(kept))


# --- 归类联动动作 ---


@router.post(
    "/candidates/{candidate_id}/observe",
    response_model=ObservationSavePayload,
    responses=LINKAGE_FAILURES,
)
def observe_candidate(
    candidate_id: str, request: Request, payload: CandidateGroupsCommand
) -> ObservationSavePayload:
    """加入或保留观察：增加所选关系并完成本次归类（同一事务）。"""
    return observation_save_payload(
        _run(
            lambda: _observations(request).observe_candidate(
                candidate_id, payload.group_ids or []
            )
        )
    )


@router.post(
    "/candidates/{candidate_id}/remove-from-groups",
    response_model=ObservationSavePayload,
    responses=LINKAGE_FAILURES,
)
def remove_from_groups(
    candidate_id: str, request: Request, payload: CandidateGroupsCommand
) -> ObservationSavePayload:
    """移出观察组：移除所选关系并同时记为暂不关注（同一事务）。"""
    return observation_save_payload(
        _run(
            lambda: _observations(request).remove_candidate_from_groups(
                candidate_id, payload.group_ids or []
            )
        )
    )
