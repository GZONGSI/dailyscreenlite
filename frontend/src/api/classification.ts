import type { components } from "./generated/schema";

/**
 * 候选归类传输契约：类型由后端 DTO 生成，不再手写。
 *
 * 生成链路见 `./generated/README.md`：后端模型 → `backend/tools/export_openapi.py`
 * → `pnpm run gen:api`。字段名、可空性与枚举只在服务端维护一处；这里只给业务方法起
 * 短名字，不新增字段、不改可空性。
 *
 * 响应按用途分开：普通详情、完整浏览、导航增量、归类动作与观察联动结果各有自己的
 * 类型，不共用一个宽泛的可选对象，也不用「可选字段」掩盖动作专属的版本与增量。
 */
export type Candidate = components["schemas"]["CandidatePayload"];
export type CandidateRow = components["schemas"]["CandidateRowPayload"];
export type CandidateState = components["schemas"]["CandidateState"];
export type CandidateScope = components["schemas"]["CandidateScope"];
export type CandidateSummary = components["schemas"]["CandidateSummaryPayload"];
export type RoundStats = components["schemas"]["RoundStatsPayload"];
/** 全局搜索：只返回经过导入与证券识别的股票。 */
export type CandidateSearchResults = components["schemas"]["CandidateSearchPayload"];

/** 一次一致的浏览读取：当前卡、左侧完整轻量列表、数量与列表版本。 */
export type ClassificationBrowse = components["schemas"]["BrowsePayload"];
/** 浏览命令结果：新浏览状态与列表变化，不含整份列表。 */
export type ClassificationCommand = components["schemas"]["CommandPayload"];
/** 一次写入造成的列表变化。 */
export type ClassificationDelta = components["schemas"]["ListDeltaPayload"];
/** 改筛选、切视图模式或就地打开某个已有步骤；未提交的字段保持缺省。 */
export type ClassificationViewChanges = components["schemas"]["ViewChanges"];
/** 上一个／下一个命令：严格整数游标与修订号。 */
export type NavigationCommand = components["schemas"]["NavigationCommand"];
/** 归类联动动作的请求体：本次涉及的观察组。 */
export type CandidateGroupsCommand = components["schemas"]["CandidateGroupsCommand"];
/** 主动清理待归类池的输入与回执。 */
export type CleanupCommand = components["schemas"]["CleanupCommand"];
export type CleanupResult = components["schemas"]["CleanupResult"];

/**
 * 不返回列表的轻量浏览状态：完整浏览与导航增量共有的部分。
 *
 * 从生成类型派生，不另写一份字段，因此服务端改字段名会同时在这两处报错。
 */
export type ClassificationState = Omit<
  ClassificationBrowse,
  "rows" | "pending" | "dates"
>;

/** 归类动作结果：候选详情 + 写入前后的列表版本、本次离开列表的行与写入后的行序。 */
export type CandidateAction = components["schemas"]["CandidateActionPayload"];
/** 观察联动动作结果：有效关系、写入后的候选项与同一份写入版本。 */
export type ObservationSaveResult = components["schemas"]["ObservationSavePayload"];
/** 联动动作下发的候选项：与归类动作同一形状，但没有队列重排。 */
export type ObservationCandidate = components["schemas"]["ObservationCandidatePayload"];

/** 写入类动作下发的候选项：都有版本字段，只有归类动作会改变队列顺序。 */
export type WrittenCandidate = CandidateAction | ObservationCandidate;
