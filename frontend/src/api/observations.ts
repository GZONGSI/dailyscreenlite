import type { components } from "./generated/schema";

/**
 * 观察工作表传输契约：类型由后端 DTO 生成，不再手写。
 *
 * 生成链路见 `./generated/README.md`：后端模型 → `backend/tools/export_openapi.py`
 * → `pnpm run gen:api`。字段名、可空性与取值集合只在服务端维护一处；这里只给业务方法起
 * 短名字，不新增字段、不改可空性。
 *
 * 归类联动的两个动作（加入或保留观察、移出观察组）复用 `./classification` 的动作契约，
 * 这里不重复声明。
 */
export type ObservationGroup = components["schemas"]["ObservationGroupPayload"];
export type GroupList = components["schemas"]["GroupListPayload"];
export type ObservedStock = components["schemas"]["ObservedStockPayload"];
export type ObservationState = components["schemas"]["ObservationStatePayload"];
export type ObservationView = components["schemas"]["ObservationViewPayload"];
/** 共用个股详情：股票信息、观察关系与候选项入口。 */
export type StockDetail = components["schemas"]["StockDetailPayload"];
export type Membership = components["schemas"]["MembershipPayload"];
export type DeleteGroupResponse = components["schemas"]["DeleteGroupResponse"];

/** 新建或重命名观察组：只认识组名。 */
export type GroupNameCommand = components["schemas"]["GroupNameCommand"];
/** 观察工作表的部分修改：未提交的字段不动，显式 null 才是主动清空。 */
export type ObservationViewChanges = components["schemas"]["ObservationViewChanges"];
/** 整组替换某证券的观察关系：空组列表表示退出全部组。 */
export type MembershipCommand = components["schemas"]["MembershipCommand"];
