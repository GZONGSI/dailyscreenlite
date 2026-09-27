import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../api/client";
import { reloadUpdateStatus, terminalStateId, useUpdateStatusQuery } from "./queries";

const READ_ERROR = "无法读取更新状态，请重新打开面板重试。";

/**
 * 更新控制器：手动触发统一更新流程，并跟踪进行中的进度。
 *
 * 读取状态、频率与缓存由 `['updates','status']` 承担；这里保留业务部分：
 * 手动触发的忙碌态与失败提示，以及**应用级终态观察者**。
 *
 * 终态观察者以 `runId／status／finishedAt` 三元组配合 `running === false` 判断
 * 「出现了新的完整终态」：定时与启动补更不由本页触发，两次轮询之间跑完的短轮次
 * 也据此被发现，随后按应用级刷新接线通知工作台（图表、队列与数据中心都据同一事实重读）。
 * 同一个终态只通知一次——每次 `dataUpdatedAt` 变化或每个订阅者各自通知都不是等价替换。
 */
export function useUpdateController({ onUpdated }: { onUpdated?: () => void }) {
  const queryClient = useQueryClient();
  const query = useUpdateStatusQuery();
  const [busy, setBusy] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const lastSeen = useRef<string | null>(null);
  const updated = useRef(onUpdated);
  updated.current = onUpdated;

  useEffect(() => {
    const next = query.data;
    if (!next) return;
    const terminal = terminalStateId(next.lastRun);
    if (lastSeen.current !== null && lastSeen.current !== terminal && !next.running) {
      updated.current?.();
    }
    // 串行队列可能已写终态但仍在处理其他股票，保留上次完整读回的标记。
    if (lastSeen.current === null || !next.running) lastSeen.current = terminal;
  }, [query.data]);

  /** 重读更新状态；失败由查询状态承担，不向调用方抛出。 */
  const reload = () => {
    void query.refetch().catch(() => {});
  };

  const run = async () => {
    setBusy(true);
    setRunError(null);
    try {
      const record = await api.runUpdate();
      // 这次写入本身就是新的完整终态：先记下它，再走应用级刷新，
      // 免得随后读回同一终态时又通知一次。
      lastSeen.current = terminalStateId(record);
      await reloadUpdateStatus(queryClient);
      if (record.status === "failed") {
        setRunError(record.securitiesMessage ?? "更新失败，已保留原有数据");
      }
      updated.current?.();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // 已在运行：重读状态回到真实进度，提示用户稍候
        reload();
        setRunError("更新正在进行中，请稍候");
      } else {
        setRunError(err instanceof ApiError ? err.message : "更新失败，请重试");
      }
    } finally {
      setBusy(false);
    }
  };

  return {
    state: query.data ?? null,
    busy,
    error: runError ?? (query.isError ? READ_ERROR : null),
    run,
    reload,
  };
}
