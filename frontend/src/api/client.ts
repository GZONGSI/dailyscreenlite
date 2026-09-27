import type {
  Candidate,
  CandidateAction,
  CandidateGroupsCommand,
  CandidateScope,
  CandidateSearchResults,
  ClassificationBrowse,
  ClassificationCommand,
  ClassificationViewChanges,
  CleanupCommand,
  CleanupResult,
  NavigationCommand,
  ObservationSaveResult,
} from "./classification";
import type { DeleteNoteResponse, Note, NoteStream, NoteWrite } from "./notes";
import type {
  DeleteGroupResponse,
  GroupList,
  GroupNameCommand,
  Membership,
  MembershipCommand,
  ObservationGroup,
  ObservationView,
  ObservationViewChanges,
  StockDetail,
} from "./observations";

export type BatchStatus =
  | "published"
  | "empty"
  | "all_unknown"
  | "rejected"
  | "awaiting_selection"
  | "awaiting_confirmation";

export type StockOutcome = "imported" | "duplicate" | "skipped";

export interface BatchStock {
  position: string;
  rawCode: string;
  normalizedCode: string;
  outcome: StockOutcome;
  securityId: string | null;
  /** 名称来自权威证券库；未识别的行没有名称。 */
  name: string | null;
  reason: string | null;
  /** 该行对候选队列的影响：new / merged / reopened / source_only / null。 */
  effect: string | null;
  /** 来源附带字段的最小只读存档，不参与识别或行情计算。 */
  rawExtras: Record<string, string>;
}

export interface SelectionOption {
  value: string;
  label: string;
  detail: string | null;
}

export interface SelectionRequest {
  kind: "sheet" | "column";
  prompt: string;
  options: SelectionOption[];
  preview: { headers: string[]; rows: string[][] };
}

export interface ImportBatch {
  batchId: string;
  sourceKind: string;
  sourceName: string;
  sourceRef: string | null;
  receivedAt: string;
  importDate: string;
  status: BatchStatus;
  declaredTotal: number | null;
  parsedCount: number;
  uniqueCount: number;
  recognizedCount: number;
  skippedCount: number;
  /** 本批次对候选队列的影响：新增候选项、合并到未处理候选项、重新进入待归类。 */
  newCandidateCount: number;
  mergedCandidateCount: number;
  reopenedCandidateCount: number;
  errorCode: string | null;
  errorMessage: string | null;
  queryText: string | null;
  conditionLabels: string[];
  identityFingerprint: string | null;
  completeness: string | null;
  selection: SelectionRequest | null;
  reidentifiedAt: string | null;
  reidentifiedImported: number;
  reidentifiedRemoved: number;
  stocks?: BatchStock[];
}

/** 一条日线；volumeLots 单位为手，amountYuan 单位为元。 */
export interface DailyBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volumeLots: number;
  amountYuan: number | null;
}

/** 某股票行情视图；available=false 时带明确缺失原因，不用零值伪造。 */
export interface QuoteSeries {
  securityId: string;
  available: boolean;
  reason: string | null;
  adjust: string;
  /** 复权口径的中文标签（前复权/不复权等）；样本口径不同时不误标。 */
  adjustLabel: string;
  source: string | null;
  latestDate: string | null;
  earliestDate: string | null;
  total: number;
  fetchedAt: string | null;
  bars: DailyBar[];
}

/** 队列列表用的轻量行情摘要。 */
export interface QuoteSnapshot {
  securityId: string;
  tradeDate: string;
  close: number;
  changePct: number | null;
}

export type UpdateStatus = "running" | "success" | "partial" | "failed";

export interface UpdateRun {
  runId: string;
  kind: string;
  status: UpdateStatus;
  startedAt: string;
  finishedAt: string | null;
  durationMs?: number | null;
  securitiesStatus: string | null;
  securitiesMessage: string | null;
  securitiesCount: number;
  quotesOk: number;
  quotesFailed: number;
  /** 真实无数据（新上市/停牌）条数；既非成功也非失败。 */
  quotesSkipped: number;
  /** 请求成功但目标交易日行情仍未补齐的条数；不为 0 时这次更新不算全部完成。 */
  quotesPending: number;
  failedSecurities: string[];
}

export interface UpdateState {
  automaticRounds: AutomaticRound[];
  running: boolean;
  lastRun: UpdateRun | null;
  history?: UpdateRun[];
  progress?: UpdateProgress | null;
}

export interface AutomaticRound {
  run_date: string;
  slot: number;
  run_id: string;
  started_at: string;
}

/** 进行中更新的进度；只在更新运行时有值。 */
export interface UpdateProgress {
  total: number;
  done: number;
  attempt: number;
  currentSecurityId?: string | null;
  currentStartedAt?: string | null;
}

export interface QuoteDiagnostic {
  securityId: string;
  fetchMode: "initial" | "incremental" | "rebuild" | null;
  requestStart: string | null;
  elapsedMs: number | null;
  attempts: { source: string; elapsedMs: number; outcome: string; latestDate: string | null; error?: string }[];
}

export type StockDataState = "updated" | "suspended" | "pending" | "unconfirmed";
export type MarketStatus = "trading" | "suspended" | "unknown";

/** 数据中心的分项状态：证券库 / 股票状态 / 日线。 */
export interface DataStatusItem {
  key: "securities" | "market_status" | "quotes" | "calendar" | `securities_${string}`;
  label: string;
  state: "ok" | "partial" | "failed" | "unknown" | "running";
  message: string;
}

/** 逐股完整性结论：状态、最后行情日期、原因与上次失败原因。 */
export interface StockDataStatus {
  securityId: string;
  name: string;
  state: StockDataState;
  marketStatus: MarketStatus;
  latestDate: string | null;
  reason: string | null;
  lastError: string | null;
  source: string | null;
}

/** 轻量数据状态：顶部提醒与分项结论（业务页面常驻读取）。 */
export interface DataStatusSummary {
  updating?: boolean;
  reminder: string | null;
  complete: boolean;
  targetTradeDate: string | null;
  targetSource: string;
  calendarAvailable: boolean;
  incompleteCount: number;
  items: DataStatusItem[];
  securitiesMessage?: string | null;
  securitiesFailed?: boolean;
}

/** 数据中心详情：分项状态、逐股结果、更新记录与重试入口。 */
export interface DataCenterInfo extends DataStatusSummary {
  automaticRounds: AutomaticRound[];
  scopeCount: number;
  scopeLabel: string;
  securitiesStatus: string | null;
  securitiesCount: number;
  stocks: StockDataStatus[];
  retryIds: string[];
  progress: UpdateProgress | null;
  lastRun: UpdateRun | null;
  history: UpdateRun[];
  quoteDiagnostics?: QuoteDiagnostic[];
}

export interface HealthInfo {
  status: string;
  importDate: string;
  securitiesLoaded: boolean;
  securitiesCount: number;
  securitiesMessage: string | null;
  wencaiAvailable: boolean;
  wencaiMessage: string | null;
  wencaiCookieConfigured: boolean;
}

export interface SettingsInfo {
  wencaiAvailable: boolean;
  wencaiMessage: string | null;
  wencaiCookie: { configured: boolean; length: number };
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly code: string | null,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code: string | null = null;
    try {
      const body = await response.json();
      const detail = body?.detail;
      if (typeof detail === "string") {
        message = detail;
      } else if (detail && typeof detail === "object") {
        message = detail.message ?? message;
        code = detail.code ?? null;
      } else if (body?.message) {
        message = body.message;
        code = body.code ?? null;
      }
    } catch {
      // 保留默认信息
    }
    throw new ApiError(message, code, response.status);
  }
  return (await response.json()) as T;
}

/**
 * 笔记写入的共用请求：新增与编辑只有路径与方法不同。
 *
 * 请求体按生成的 `NoteWrite` 构造，服务端字段改名会让这一处无法通过类型检查。
 */
function writeNote(path: string, method: "POST" | "PATCH", body: string) {
  const payload: NoteWrite = { body };
  return request<Note>(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export const api = {
  health: () => request<HealthInfo>("/api/health"),

  /** 一次提交多个文件与文本块；每个来源独立成批次并返回状态。 */
  submitSources: (files: File[], texts: string[]) => {
    const form = new FormData();
    for (const file of files) {
      form.append("files", file);
    }
    for (const text of texts) {
      form.append("texts", text);
    }
    return request<{ batches: ImportBatch[] }>("/api/imports", {
      method: "POST",
      body: form,
    });
  },

  /** 批次列表（不含明细），用于刷新或重启后读回导入事实。 */
  listBatches: () => request<{ batches: ImportBatch[] }>("/api/imports"),

  getBatch: (batchId: string) =>
    request<ImportBatch>(`/api/imports/${encodeURIComponent(batchId)}`),

  /** 歧义选择：确认工作表或代码列后继续解析。 */
  resolveSelection: (batchId: string, choice: { sheet?: string; codeColumn?: number }) =>
    request<ImportBatch>(`/api/imports/${encodeURIComponent(batchId)}/selection`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(choice),
    }),

  /** 问财条件确认：记录查询身份并发布。 */
  confirmLink: (batchId: string) =>
    request<ImportBatch>(`/api/imports/${encodeURIComponent(batchId)}/confirm`, {
      method: "POST",
    }),

  /** 重新识别原批次跳过项。 */
  reidentify: (batchId: string) =>
    request<ImportBatch>(`/api/imports/${encodeURIComponent(batchId)}/reidentify`, {
      method: "POST",
    }),

  /** 重试未发布的失败批次，保留原批次身份与导入日期。 */
  retryBatch: (batchId: string) =>
    request<ImportBatch>(`/api/imports/${encodeURIComponent(batchId)}/retry`, {
      method: "POST",
    }),

  /** 全局搜索：只返回经过导入与证券识别的股票。 */
  searchCandidates: (query: string) =>
    request<CandidateSearchResults>(
      `/api/classification/search?q=${encodeURIComponent(query)}`,
    ),

  /** 统一浏览结果：同一读取视图内形成当前卡、完整轻量列表、数量与列表版本。 */
  getClassificationBrowse: (scope?: CandidateScope) =>
    request<ClassificationBrowse>(
      `/api/classification/view${scope ? `?scope=${scope}` : ""}`,
    ),

  /**
   * 选股或改筛选：服务端一并返回新的一致浏览结果，
   * 因此前端不需要再补一次列表读取，也不自行推导筛选结果。
   */
  updateClassificationView: (changes: ClassificationViewChanges) =>
    request<ClassificationBrowse>("/api/classification/view", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),

  /** 上一个／下一个：只回传新卡片、浏览状态与列表变化，不重传整份列表。 */
  navigateClassification: (
    direction: "previous" | "next",
    expectedCursor: number,
    expectedRevision: number,
  ) => {
    const payload: NavigationCommand = { direction, expectedCursor, expectedRevision };
    return request<ClassificationCommand>("/api/classification/view/navigate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  returnClassificationQueue: () =>
    request<ClassificationBrowse>("/api/classification/view/return", { method: "POST" }),

  getCandidate: (candidateId: string) =>
    request<Candidate>(`/api/classification/candidates/${encodeURIComponent(candidateId)}`),

  laterCandidate: (candidateId: string) =>
    request<CandidateAction>(
      `/api/classification/candidates/${encodeURIComponent(candidateId)}/later`,
      { method: "POST" },
    ),

  dismissCandidate: (candidateId: string) =>
    request<CandidateAction>(
      `/api/classification/candidates/${encodeURIComponent(candidateId)}/dismiss`,
      { method: "POST" },
    ),

  /** 主动重新归类：复用同一候选项，保留历史并调到队首。 */
  reclassifyCandidate: (candidateId: string) =>
    request<CandidateAction>(
      `/api/classification/candidates/${encodeURIComponent(candidateId)}/reclassify`,
      { method: "POST" },
    ),

  /**
   * 手动打开候选卡（左栏点选、全局搜索、跨模块查看卡片）。
   *
   * 目标作为浏览路径的新一步，历史中途跳转会替换前进分支；不改变当前筛选，
   * 也不重排未处理池。目标不在当前筛选结果里时返回 `inFilter: false`。
   */
  focusCandidate: (candidateId: string) =>
    request<ClassificationBrowse>(
      `/api/classification/candidates/${encodeURIComponent(candidateId)}/focus`,
      { method: "POST" },
    ),

  /** 主动清理待归类池；可限定入选日期。不删除导入事实。 */
  cleanup: (importDate?: string | null) => {
    const payload: CleanupCommand = { importDate: importDate ?? null };
    return request<CleanupResult>("/api/classification/cleanup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  /** 全部观察组（含成员数量），用于组列表与关系选择。 */
  listObservationGroups: (signal?: AbortSignal) =>
    request<GroupList>("/api/observations/groups", { signal }),

  createObservationGroup: (name: string) => {
    const payload: GroupNameCommand = { name };
    return request<ObservationGroup>("/api/observations/groups", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  renameObservationGroup: (groupId: string, name: string) => {
    const payload: GroupNameCommand = { name };
    return request<ObservationGroup>(`/api/observations/groups/${encodeURIComponent(groupId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  /** 删除观察组：只移除该组关系，不删除来源、笔记与处理历史，也不影响其他组。 */
  deleteObservationGroup: (groupId: string) =>
    request<DeleteGroupResponse>(`/api/observations/groups/${encodeURIComponent(groupId)}`, {
      method: "DELETE",
    }),

  /** 观察组模块的组、列表与浏览上下文，用于刷新与重启后恢复；signal 同上。 */
  getObservationView: (signal?: AbortSignal) =>
    request<ObservationView>("/api/observations/view", { signal }),

  updateObservationView: (changes: ObservationViewChanges) =>
    request<ObservationView>("/api/observations/view", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }),

  /** 共用个股详情：股票信息、观察关系与来源/处理记录入口；signal 同上。 */
  getStockDetail: (securityId: string, signal?: AbortSignal) =>
    request<StockDetail>(`/api/observations/securities/${encodeURIComponent(securityId)}`, {
      signal,
    }),

  /** 跨模块打开详情：切到汇总视图并把这支股票设为观察模块的当前股票。 */
  focusObservedStock: (securityId: string) =>
    request<ObservationView>(
      `/api/observations/securities/${encodeURIComponent(securityId)}/focus`,
      { method: "POST" },
    ),

  /** 某股票当前所属的观察组，用于卡片与详情预选；signal 让调用方取消已过期的读取。 */
  memberships: (securityId: string, signal?: AbortSignal) =>
    request<Membership>(
      `/api/observations/memberships?securityId=${encodeURIComponent(securityId)}`,
      { signal },
    ),

  /**
   * 整组替换该股票的观察关系（观察组页面的转组、增减归属、退出全部组）。
   * 不改变候选处理状态，未选中的关系会被移除。
   */
  saveMembership: (securityId: string, groupIds: string[]) => {
    const payload: MembershipCommand = { securityId, groupIds };
    return request<Membership>("/api/observations/memberships", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  },

  /** 加入或保留观察：增加所选关系并完成本次归类（同一事务）。 */
  observeCandidate: (candidateId: string, groupIds: string[]) => {
    const payload: CandidateGroupsCommand = { groupIds };
    return request<ObservationSaveResult>(
      `/api/observations/candidates/${encodeURIComponent(candidateId)}/observe`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
  },

  /** 移出观察组：移除所选关系并同时记为暂不关注（同一事务）。 */
  removeCandidateFromGroups: (candidateId: string, groupIds: string[]) => {
    const payload: CandidateGroupsCommand = { groupIds };
    return request<ObservationSaveResult>(
      `/api/observations/candidates/${encodeURIComponent(candidateId)}/remove-from-groups`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
    );
  },

  /** 某股票跨导入日期与入口共用的完整笔记流；signal 让调用方取消已过期的读取。 */
  listNotes: (securityId: string, signal?: AbortSignal) =>
    request<NoteStream>(`/api/notes/securities/${encodeURIComponent(securityId)}`, {
      signal,
    }),

  /** 新增笔记：归属该证券，不改变处理状态。 */
  createNote: (securityId: string, body: string) =>
    writeNote(`/api/notes/securities/${encodeURIComponent(securityId)}`, "POST", body),

  /** 覆盖正文与保存时间，不保留旧版本。 */
  updateNote: (noteId: string, body: string) =>
    writeNote(`/api/notes/${encodeURIComponent(noteId)}`, "PATCH", body),

  deleteNote: (noteId: string) =>
    request<DeleteNoteResponse>(`/api/notes/${encodeURIComponent(noteId)}`, {
      method: "DELETE",
    }),

  /** 股票日线；默认最近约 250 个交易日，end 用于向前浏览更早历史。 */
  quotes: (securityId: string, options: { limit?: number; end?: string | null } = {}) => {
    const params = new URLSearchParams();
    if (options.limit) {
      params.set("limit", String(options.limit));
    }
    if (options.end) {
      params.set("end", options.end);
    }
    const query = params.toString();
    return request<QuoteSeries>(
      `/api/quotes/${encodeURIComponent(securityId)}${query ? `?${query}` : ""}`,
    );
  },

  /** 队列列表的批量轻量行情摘要：最新收盘价、行情日与日涨跌幅。 */
  quoteSummary: (securityIds: string[]) => {
    const params = new URLSearchParams({ securityIds: securityIds.join(",") });
    return request<{ quotes: QuoteSnapshot[] }>(`/api/quotes/summary?${params.toString()}`);
  },

  /** 当前更新状态与最近一次结果（含数据日期）；signal 让调用方取消已过期的读取。 */
  updateStatus: (signal?: AbortSignal) =>
    request<UpdateState>("/api/updates", { signal }),

  /** 手动更新：触发统一更新流程；已在运行时不并发执行。 */
  runUpdate: () => request<UpdateRun>("/api/updates", { method: "POST" }),

  /** 轻量数据状态：顶部未补齐提醒与分项结论；signal 同上。 */
  dataStatus: (signal?: AbortSignal) =>
    request<DataStatusSummary>("/api/data/status", { signal }),

  /** 数据中心：分项状态、逐股结果、更新记录与重试入口；signal 同上。 */
  dataCenter: (signal?: AbortSignal) =>
    request<DataCenterInfo>("/api/data", { signal }),

  /** 手动重试未完成部分：只重取待补齐与状态待确认的股票。 */
  retryIncomplete: () =>
    request<{ started: boolean; count: number; quotesOk: number }>("/api/data/retry", {
      method: "POST",
    }),

  /** 历史详情的单股更新：只更新这一只，不加入持续更新范围。 */
  refreshSecurity: (securityId: string) =>
    request<{
      securityId: string;
      updated: boolean;
      available: boolean;
      latestDate: string | null;
    }>(`/api/securities/${encodeURIComponent(securityId)}/refresh`, { method: "POST" }),

  getSettings: () => request<SettingsInfo>("/api/settings"),

  saveWencaiCookie: (cookie: string) =>
    request<{ wencaiCookie: { configured: boolean; length: number } }>(
      "/api/settings/wencai-cookie",
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookie }),
      },
    ),

  clearWencaiCookie: () =>
    request<{ wencaiCookie: { configured: boolean; length: number } }>(
      "/api/settings/wencai-cookie",
      { method: "DELETE" },
    ),
};
