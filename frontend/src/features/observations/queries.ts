import { useCallback, useEffect, useMemo, useState } from "react";
import {
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryFunctionContext,
  type UseQueryResult,
} from "@tanstack/react-query";

import { api } from "../../api/client";
import type { ObservationGroup, ObservationView } from "../../api/observations";

/**
 * 观察组与证券成员关系：关系入口（观察详情的 GroupEditor、归类卡片的
 * ObservationPicker 与 RemoveGroupsPicker）订阅同一份服务器资源。
 *
 * 读取状态、同键缓存、取消与失效集中在这里；新建组、写入关系与删除组后的
 * 缓存回收也在同一处，避免同一资源出现第二份权威副本。草稿（勾选、新组名）与
 * 「读不到就不能覆盖已有关系」仍由各入口按自己的业务表达。
 *
 * 观察工作表本体（组 + 观察列表 + 浏览上下文）与共用详情也走同一份约定：
 * 键、读取、失效与写后回写都在这里，组件不再自己保存服务器副本或加载状态。
 * 写入与读取的先后也在这里收口：写入前取消在途读取，写入在途期间的失效延后到落定。
 */
const observationKeys = {
  groups: ["observations", "groups"] as const,
  memberships: (securityId: string) => ["observations", "memberships", securityId] as const,
  /** 成员关系前缀：删除组时受影响证券全集不可得，只能整片标记过期。 */
  allMemberships: ["observations", "memberships"] as const,
  /** 观察工作表：组、观察列表与浏览上下文是同一份读取视图。 */
  view: ["observations", "view"] as const,
  detail: (securityId: string) => ["observations", "detail", securityId] as const,
  /** 详情前缀：组与关系变化影响当前股票的关系展示。 */
  allDetails: ["observations", "detail"] as const,
};

interface SessionResource<T> {
  queryKey: readonly unknown[];
  queryFn: (context: QueryFunctionContext) => Promise<T>;
  /** 共享缓存的订阅（不自行读取），用于展示服务器数据。 */
  query: UseQueryResult<T, Error>;
}

/**
 * 订阅资源但不让框架自己读取。
 *
 * `enabled: false` 是有意的：框架在挂载与 `enabled` 由假转真时都会自己发一次请求，
 * 与「本次打开显式读取」叠加就是一轮打开两轮请求（前一轮还会被取消）。
 * 订阅只用于展示共享缓存，读取统一由 useGroupRelationRead 的读取会话发起。
 */
function useGroupsResource(): SessionResource<ObservationGroup[]> {
  const queryKey = observationKeys.groups;
  const queryFn = useCallback(
    ({ signal }: QueryFunctionContext) =>
      api.listObservationGroups(signal).then((result) => result.groups),
    [],
  );
  const query = useQuery({ queryKey, queryFn, enabled: false });
  return { queryKey, queryFn, query };
}

/** 某证券当前所属的观察组；以证券身份隔离，不以候选项或导入日期隔离。 */
function useMembershipsResource(securityId: string): SessionResource<string[]> {
  const queryKey = useMemo(() => observationKeys.memberships(securityId), [securityId]);
  const queryFn = useCallback(
    ({ signal }: QueryFunctionContext) =>
      api.memberships(securityId, signal).then((result) => result.groupIds),
    [securityId],
  );
  const query = useQuery({ queryKey, queryFn, enabled: false });
  return { queryKey, queryFn, query };
}

type ReadStatus = "reading" | "confirmed" | "failed";

interface SessionReadState<T> {
  /** 写下这个状态时的会话号；与当前会话不同就不再是本次打开的确认结果。 */
  session: number;
  status: ReadStatus;
  /** 本次会话读到的数据；未确认时为 null。 */
  data: T | null;
}

interface SessionRead<T> {
  /** 本次会话是否读成功；失败与未完成都不能用来初始化勾选。 */
  status: ReadStatus;
  data: T | null;
  retry: () => void;
}

/**
 * 在一次读取会话里读取该资源；缓存可以展示，但不能证明本次已确认。
 *
 * 每次读取都先取消这个键上还在路上的请求：冷缓存时框架会直接复用旧请求的响应，
 * 而那次响应可能来自上一次打开、早于期间的外部写入，不能算作本次确认。
 * 会话号变化时结果立即失效；过期读取最后落回也写不回旧会话。
 */
function useSessionRead<T>(
  resource: SessionResource<T>,
  open: boolean,
  session: number,
): SessionRead<T> {
  const queryClient = useQueryClient();
  const [state, setState] = useState<SessionReadState<T>>({
    session: -1,
    status: "reading",
    data: null,
  });
  const { queryKey, queryFn } = resource;

  const read = useCallback(async () => {
    setState({ session, status: "reading", data: null });
    // 取消沿用的旧读取（AbortSignal 一并中止在途请求），本次结果必须来自本次发起的请求
    await queryClient.cancelQueries({ queryKey }, { silent: true });
    try {
      const data = await queryClient.fetchQuery({ queryKey, queryFn });
      setState((previous) =>
        previous.session === session ? { session, status: "confirmed", data } : previous,
      );
    } catch {
      // 读取失败（含被取消）都要重新读：不能拿缓存或旧结论顶上
      setState((previous) =>
        previous.session === session ? { session, status: "failed", data: null } : previous,
      );
    }
  }, [queryClient, queryKey, queryFn, session]);

  useEffect(() => {
    if (!open) return;
    void read();
  }, [open, read]);

  return {
    status: state.session === session ? state.status : "reading",
    data: state.session === session ? state.data : null,
    retry: read,
  };
}

export interface GroupRelationRead {
  /**
   * 本次读取会话：一次打开 + 一只证券一个会话号，变化即表示上一次的确认结果不再适用。
   * 入口用它判断自己是否已经初始化过勾选。
   */
  readSession: number;
  /** 当前可选观察组：服务器资源，新建组的真实响应写进缓存后立即出现。 */
  options: ObservationGroup[];
  /** 本次会话确认的已有关系；null 表示还没确认成功，不能据此保存。 */
  groupIds: string[] | null;
  /** 本次会话的读取失败信息；已经确认或还没读完时为 null。 */
  error: string | null;
  retry: () => void;
}

/**
 * 三个关系入口共用的读取：打开一次＝一次新的读取会话，每次打开各发一次读取请求，
 * 组列表与该证券的已有关系都在这次会话里读成功才允许初始化与保存。
 */
export function useGroupRelationRead(securityId: string, open: boolean): GroupRelationRead {
  // 会话在打开的这次渲染里就换代（React 官方的「渲染中调整状态」用法），
  // 旧会话的确认结果因此当场失效，不会有一帧拿上一次的结果开放保存。
  const [cycle, setCycle] = useState({ session: 0, open, securityId });
  if (cycle.open !== open || cycle.securityId !== securityId) {
    setCycle({ session: cycle.session + 1, open, securityId });
  }
  const session = cycle.session;

  const groupsResource = useGroupsResource();
  const membershipsResource = useMembershipsResource(securityId);
  const groups = useSessionRead(groupsResource, open, session);
  const memberships = useSessionRead(membershipsResource, open, session);

  const groupsRetry = groups.retry;
  const membershipsRetry = memberships.retry;
  const retry = useCallback(() => {
    groupsRetry();
    membershipsRetry();
  }, [groupsRetry, membershipsRetry]);

  // 本次会话两个读取都确认成功才给出已有关系；缺任一项都返回 null：
  // 宁可禁用保存，也不能用空集或缓存冒充「这只股票现在没有观察关系」。
  // 组列表读失败时按关系读取报错，它是保存前必须确认的那一项。
  const error =
    memberships.status === "failed"
      ? "未能读取已有观察关系，请重试。"
      : groups.status === "failed"
        ? "未能读取观察组列表，请重试。"
        : null;

  return {
    readSession: session,
    options: groupsResource.query.data ?? [],
    groupIds:
      groups.status === "confirmed" && memberships.status === "confirmed"
        ? memberships.data
        : null,
    error,
    retry,
  };
}

/**
 * 「本次读取会话的确认结果已经用掉」的标记：三个入口共用同一份判定。
 *
 * 入口只表达自己的预选规则（原样预选、未入组时预选默认组、按现存组过滤），
 * 勾选是否已经初始化过只在这里判定，避免各写一份读取状态。
 */
export function useSessionSelection(readSession: number) {
  const [usedSession, setUsedSession] = useState(-1);
  const markUsed = useCallback(() => setUsedSession(readSession), [readSession]);
  return { initialized: usedSession === readSession, markUsed };
}

/** 新建组的真实响应写入共享组缓存：各入口立刻能选到它，不用本地临时合并。 */
export function addGroupToGroups(queryClient: QueryClient, group: ObservationGroup) {
  queryClient.setQueryData<ObservationGroup[]>(observationKeys.groups, (previous = []) =>
    previous.some((item) => item.groupId === group.groupId) ? previous : [...previous, group],
  );
  // 工作表里也带着组列表（成员数量与列表属于同一份读取视图）：把新建的组同样写进去，
  // 在关系编辑器里新建组后即使不保存关系，工作表的组栏也不会停在旧快照上。
  queryClient.setQueryData<ObservationView | undefined>(
    observationKeys.view,
    (previous) =>
      previous && !previous.groups.some((item) => item.groupId === group.groupId)
        ? { ...previous, groups: [...previous.groups, group] }
        : previous,
  );
}

/** 组列表写入（重命名、删除）后标记组资源过期：下一次打开读取时取服务器事实。 */
export function expireGroups(queryClient: QueryClient) {
  void queryClient.invalidateQueries({ queryKey: observationKeys.groups, refetchType: "none" });
}

/** 删除组会同时改变多个证券的关系：按前缀整片标记过期，不假装能精确到每个证券。 */
export function expireMemberships(queryClient: QueryClient) {
  void queryClient.invalidateQueries({
    queryKey: observationKeys.allMemberships,
    refetchType: "none",
  });
}

/** 写入关系后把同一份服务器数据收回缓存：其他入口订阅同一键，不各留一份副本。 */
export function confirmMemberships(
  queryClient: QueryClient,
  securityId: string,
  groupIds: string[],
) {
  queryClient.setQueryData(observationKeys.memberships(securityId), groupIds);
  // 组成员数变了：标记组列表过期，下一次打开读取时取真实数量
  expireGroups(queryClient);
}

/**
 * 观察工作表：组、观察列表与浏览上下文来自同一次读取。
 *
 * 只按挂载（没有缓存时）与显式失效读取：进入模块、跨模块打开、数据更新与写后回收
 * 都在这里显式表达，不依赖框架自己判断该不该重读；`staleTime: Infinity` 让「刚写完的
 * 工作表」不会被挂载或启用时的一次自动读取盖住（跨模块打开因此只有那一次写入请求）。
 * 框架默认的窗口聚焦与重连重读照旧关掉（迁移前没有这两条读取时机）。
 * 失败默认不重试（QueryClient 的 `retry: false`），由工作表自己给出手动重试。
 */
export function useObservationViewQuery(enabled = true) {
  return useQuery({
    queryKey: observationKeys.view,
    queryFn: ({ signal }: QueryFunctionContext) => api.getObservationView(signal),
    enabled,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

/**
 * 共用个股详情：按股票隔离，以证券为身份。
 *
 * 没有当前股票时不读（空键不会被读取，也不会拿上一只股票的缓存顶上）；
 * `enabled` 另由调用方控制：跨模块打开期间先不读，等焦点写入落定再按新的当前股票读。
 */
export function useStockDetailQuery(securityId: string | null, enabled = true) {
  return useQuery({
    queryKey: observationKeys.detail(securityId ?? ""),
    queryFn: ({ signal }: QueryFunctionContext) =>
      api.getStockDetail(securityId ?? "", signal),
    enabled: enabled && securityId !== null,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}
/**
 * 工作表写入窗口与写入代际：写入发起到响应回写为止，表上不发起新读取；
 * 每次发起写入换代，只有最新一次发起的写入的响应可以回写缓存。
 *
 * 两件事都要：
 * - 写前取消只挡得住写入开始**之前**的读取。写入在途时到来的失效（数据更新终态、
 *   进入模块）会当场发起一次新的读取，那次读取带回写入之前的快照，并在写入响应落地
 *   之后才落地——服务端已经保存了新位置，页面却退回旧位置。窗口内的失效一律延后，
 *   等写入落定再按刷新读取服务器事实：延后的那次刷新不丢，也不会有第二份权威副本。
 * - 只判断「发起它的实例还在页面上」不够：同一个实例里切组的响应被扣住时，仍然可以
 *   通过全局搜索完成一次更晚的焦点写入；旧响应回来时实例仍然有效，会把新焦点覆盖回去。
 *   位置修改、搜索焦点与自动落位都从这里领号，回写前据代际判断响应是否还有资格。
 *
 * 计数与代际放在模块级而不是组件里：窗口要跨实例生效——离开模块时旧实例的写入仍在途，
 * 新实例进入时的读取同样要等它落定（工作表同一时刻只有一个实例）；代际也要覆盖旧实例
 * 尚未回写的响应。写入悬挂时读取一并等待；这与「写入悬挂时页面本来就停在这一步」是同一种表现。
 */
let openViewWrites = 0;
let viewWritesIdle: Promise<void> = Promise.resolve();
let viewWritesBecameIdle: (() => void) | null = null;
let viewWriteGeneration = 0;

export interface ObservationViewWrite {
  /** 这次写入是否仍是最后一次发起的工作表写入；不是就不能回写缓存。 */
  isLatest: () => boolean;
  /** 关闭写入窗口（可重复调用，只算一次）。 */
  close: () => void;
}

/** 打开一个写入窗口并领一个写入代际。 */
export function beginObservationViewWrite(): ObservationViewWrite {
  const generation = (viewWriteGeneration += 1);
  if (openViewWrites === 0) {
    viewWritesIdle = new Promise<void>((resolve) => {
      viewWritesBecameIdle = resolve;
    });
  }
  openViewWrites += 1;
  let closed = false;
  return {
    isLatest: () => generation === viewWriteGeneration,
    close: () => {
      if (closed) return;
      closed = true;
      openViewWrites -= 1;
      if (openViewWrites === 0) {
        viewWritesBecameIdle?.();
        viewWritesBecameIdle = null;
      }
    },
  };
}

/**
 * 让观察工作表重读：进入模块、数据更新、组与关系变化后的回收都走这里。
 *
 * 返回的 promise 在重读结束后才落定，因此「保存后仍在校验中的入口继续禁用」这类
 * 展示政策仍由调用方 await 得到，不需要另留一份加载状态。重读失败不抛给调用方：
 * 失败的展示统一在工作表自己的读取错误里，避免同一件事在两处各报一次。
 */
export async function refreshObservationView(queryClient: QueryClient) {
  // 写入在途时先等它落定再失效：这期间发起的读取会带着写入之前的快照落地，把新位置盖回去
  while (openViewWrites > 0) await viewWritesIdle;
  await Promise.all([
    queryClient.invalidateQueries({ queryKey: observationKeys.view }),
    queryClient.invalidateQueries({ queryKey: observationKeys.allDetails }),
  ]);
}

/** 服务端的写入响应就是新的权威工作表：直接写入缓存，不再补一次读取。 */
export function writeObservationView(queryClient: QueryClient, view: ObservationView) {
  queryClient.setQueryData(observationKeys.view, view);
}

/**
 * 取消工作表上还在路上的读取，并等取消生效。
 *
 * 跨模块打开详情时，挂载可能已经发出一次普通读取；它的结果会带着打开之前的当前股票
 * 落地，把焦点写入覆盖回去，因此写入前必须先取消（同笔记与归类的写前取消约定）。
 */
export async function cancelObservationViewRead(queryClient: QueryClient) {
  await queryClient.cancelQueries({ queryKey: observationKeys.view }, { silent: true });
}
