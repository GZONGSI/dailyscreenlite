import {
  useQuery,
  useQueryClient,
  type QueryClient,
  type QueryFunctionContext,
} from "@tanstack/react-query";
import { useRef } from "react";

import { api, type DataCenterInfo, type UpdateRun } from "../../api/client";

/**
 * 数据与更新：三个不同的端点，三个资源。
 *
 * 顶部提醒读 `/api/data/status`，数据中心读 `/api/data`，更新控制器读 `/api/updates`；
 * 换库不会让三者合成一个请求，所以各自一个键、各自的频率与各自的失败展示。
 * 这里只承载键、频率、终态标识与「数据事实变化后让视图重读」的失效约定；
 * 按钮忙碌态、历史展开态与启动页的「本次是否主动更新过」仍由各组件表达。
 */
const dataKeys = {
  updateStatus: ["updates", "status"] as const,
  /** 数据视图前缀：顶部提醒与数据中心一起失效。 */
  views: ["data"] as const,
  status: ["data", "status"] as const,
  center: ["data", "center"] as const,
};

/**
 * 轮询的共同配置。
 *
 * `refetchIntervalInBackground` 显式打开：迁移前用 `setInterval`，页面隐藏时也照常到点
 * 重判（只是被浏览器节流），框架默认会在隐藏时暂停轮询，那是另一套政策。
 * 聚焦与重连不额外重读：迁移前没有这两条读取时机，读取只发生在挂载、定时与显式刷新。
 * 失败默认不重试、也不向调用方抛出（QueryClient 的 `retry: false`），
 * 由各入口按自己的展示差异处理。
 */
const pollDefaults = {
  refetchIntervalInBackground: true,
  refetchOnWindowFocus: false,
  refetchOnReconnect: false,
} as const;

/**
 * 更新状态：运行中 2 秒、闲置 60 秒。
 *
 * 闲置也定期读一次：定时与启动补更不由本页触发，错过短暂的整轮更新就只能靠持久化记录
 * 的变化发现恢复。
 */
export function useUpdateStatusQuery() {
  return useQuery({
    queryKey: dataKeys.updateStatus,
    queryFn: ({ signal }: QueryFunctionContext) => api.updateStatus(signal),
    refetchInterval: (query) => (query.state.data?.running ? 2_000 : 60_000),
    ...pollDefaults,
  });
}

/**
 * 顶部提醒：后台补取中 1.5 秒、闲置 60 秒。
 *
 * 服务端在导入补取执行中返回进度文案，结束后按实际目标交易日完整性决定是否提醒；
 * 定时与启动补更在后台完成时不会主动通知页面，因此闲置也要按间隔重判一次。
 */
export function useDataStatusQuery() {
  return useQuery({
    queryKey: dataKeys.status,
    queryFn: ({ signal }: QueryFunctionContext) => api.dataStatus(signal),
    refetchInterval: (query) => (query.state.data?.updating ? 1_500 : 60_000),
    ...pollDefaults,
  });
}

/**
 * 数据中心是否处于「更新进行中」：控制器在跑，或**本次进入后**读回的进度还在。
 *
 * 只有这次进入之后成功读到过的进度才算数：上一次进入留在缓存里的进度既不可信，
 * 拿它算「在跑」会同时藏住读取失败、一直禁用更新入口，也会让轮询一直重试一个
 * 读不到的目标。控制器信号（更新状态查询）是活的，不受这里影响。
 */
export function centerIsRunning(
  updating: boolean,
  progress: DataCenterInfo["progress"] | null | undefined,
  fresh: boolean,
): boolean {
  return updating || (fresh && progress != null);
}

/** 数据中心这个键上已经成功读回几次；进入时记下来，用来判断「本次进入后读到过没有」。 */
export function dataCenterReadCount(queryClient: QueryClient): number {
  return queryClient.getQueryState(dataKeys.center)?.dataUpdateCount ?? 0;
}

/**
 * 数据中心：只在更新进行中随进度重读（1.5 秒），闲置不轮询。
 *
 * 返回查询结果与「更新进行中」的判定，两者共用 `centerIsRunning`：判定要看
 * 「本次进入后读到过没有」，所以进入时的读取次数由这里记，组件不再各算一份。
 */
export function useDataCenterQuery(updating: boolean) {
  const queryClient = useQueryClient();
  const entryReads = useRef<number | null>(null);
  if (entryReads.current === null) {
    entryReads.current = dataCenterReadCount(queryClient);
  }
  const readsAtEntry = entryReads.current ?? 0;
  const query = useQuery({
    queryKey: dataKeys.center,
    queryFn: ({ signal }: QueryFunctionContext) => api.dataCenter(signal),
    refetchInterval: (current) =>
      centerIsRunning(
        updating,
        current.state.data?.progress,
        current.state.dataUpdateCount > readsAtEntry,
      )
        ? 1_500
        : false,
    ...pollDefaults,
  });
  return {
    query,
    running: centerIsRunning(
      updating,
      query.data?.progress,
      dataCenterReadCount(queryClient) > readsAtEntry,
    ),
  };
}

/**
 * 一次更新的终态标识：`runId／status／finishedAt` 三元组，没有记录时为空串。
 *
 * 应用级终态观察者用它判断「出现了新的完整终态」：两次轮询之间跑完的短轮次也会因此
 * 被发现，这是只看 `running`、或每次 `dataUpdatedAt` 变化就通知一次替代不了的。
 */
export function terminalStateId(run: UpdateRun | null | undefined): string {
  return run ? `${run.runId}:${run.status}:${run.finishedAt ?? ""}` : "";
}

/**
 * 数据事实变化后让数据视图重读：顶部提醒与数据中心同属 `['data']` 前缀，一次覆盖两个资源。
 *
 * 名字是 refresh 而不是 `observations/queries.ts` 里的 `expire*`：这里**真的要重读**
 * （活跃的订阅者立即重读，未挂载的数据中心只标记过期，下次进入时读），
 * 不是只标脏等调用方自己决定。只让资源重读，不顺带改变当前卡、当前组与筛选。
 */
export function refreshDataViews(queryClient: QueryClient) {
  void queryClient.invalidateQueries({ queryKey: dataKeys.views });
}

/**
 * 重读更新状态：先取消这个键上还在路上的读取，再让活跃订阅者重读。
 *
 * 写后确认不能沿用写前的读取：冷缓存时框架会直接复用那次在途请求的响应，
 * 那份响应早于这次写入，会让终态观察者把旧状态当成「新的完整终态」再通知一次，
 * 并把已登记的终态回退掉（与笔记片「写前取消」同一约定）。
 */
export async function reloadUpdateStatus(queryClient: QueryClient) {
  await queryClient.cancelQueries({ queryKey: dataKeys.updateStatus }, { silent: true });
  await queryClient.invalidateQueries({ queryKey: dataKeys.updateStatus });
}
