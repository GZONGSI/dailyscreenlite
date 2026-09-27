import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import {
  ChevronDown,
  Clock,
  Loader2,
  RefreshCw,
  RotateCcw,
} from "lucide-react";

import { api, ApiError, type DataStatusItem, type StockDataState } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { refreshDataViews, useDataCenterQuery } from "./queries";
import { ITEM_STATE, STOCK_STATE, countByState } from "./state";
import type { useUpdateController } from "./useUpdateController";

const STATE_ORDER: StockDataState[] = ["updated", "suspended", "pending", "unconfirmed"];

const RUN_STATUS: Record<string, string> = {
  success: "成功",
  partial: "部分成功",
  failed: "失败",
  running: "进行中",
};

/**
 * 数据中心（视觉基线 05）：分项状态、更新范围、逐股结果、失败原因、重试与更新记录。
 *
 * 一切结论来自服务端的实际持久化结果：不以更新请求结束直接判为完整，
 * 未补齐的逐股显示原因与行情日期，失败部分保留旧数据并可重试。
 * 读取状态、缓存与「更新进行中随进度重读」由 `['data','center']` 承担；
 * 历史展开与按钮忙碌态仍由本组件表达。
 *
 * 失败展示与迁移前一致：更新进行中的轮询失败保留已读回的资料、不打扰用户
 * （逐股行里已有原因）；进入或刷新的读取失败给出错误，已有资料仍留在页面上，
 * 不把一次读取失败当成数据现状，也不静默地把更旧的资料说成最新。
 * 「更新进行中」的判定与查询共用 `centerIsRunning`：只有本次进入后读到的进度才算数。
 */
export function DataWorkspace({
  controller,
}: {
  controller: ReturnType<typeof useUpdateController>;
}) {
  const queryClient = useQueryClient();
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const updating = controller.busy || controller.state?.running === true;
  const { query: center, running } = useDataCenterQuery(updating);
  const info = center.data ?? null;
  const readError =
    center.isError && !running
      ? center.error instanceof ApiError
        ? center.error.message
        : "无法读取数据状态，请重试"
      : null;
  const error = actionError ?? readError;

  const runUpdate = async () => {
    setBusy(true);
    setActionError(null);
    try {
      await controller.run();
    } finally {
      setBusy(false);
    }
  };

  const retry = async () => {
    setBusy(true);
    setActionError(null);
    try {
      await api.retryIncomplete();
      // 重试改的是数据事实：顶部提醒与数据中心据同一份事实重读
      refreshDataViews(queryClient);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.message : "重试失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const stocks = info?.stocks ?? [];
  const counts = countByState(stocks);
  const done = stocks.length - (counts.pending + counts.unconfirmed);
  const progress = info?.progress;
  const percent = progress
    ? progress.total
      ? Math.round((progress.done / progress.total) * 100)
      : 0
    : stocks.length
      ? Math.round((done / stocks.length) * 100)
      : 100;
  const retryCount = info?.retryIds.length ?? 0;

  return (
    <div className="data-center" data-testid="data-center">
      <header className="data-center-head">
        <h1 className="text-2xl font-semibold">数据中心</h1>
        <div className="flex flex-wrap items-center gap-3">
          <span className="flex items-center gap-1.5 text-sm text-foreground/60">
            <Clock className="h-4 w-4" aria-hidden="true" />
            16:30 起每小时补取，22:30 最后一轮
          </span>
          <Button onClick={() => void runUpdate()} disabled={busy || running}>
            {busy || running ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
            )}
            更新数据
          </Button>
        </div>
      </header>

      <p className="text-sm text-foreground/60" data-testid="automatic-recovery">
        今日已执行 {info?.automaticRounds.filter((round) => round.slot > 0).length ?? 0} 轮晚间补取
        {info?.automaticRounds.some((round) => round.slot === 0) ? "，已执行启动补更" : ""}。
        仅补未完成数据；错过时点合并补一次，截止后仍可手动更新。
      </p>

      {error ? (
        <p className="data-banner data-banner-danger" role="alert">
          {error}
        </p>
      ) : null}

      <section className="data-items" aria-label="分项状态">
        <ul className="data-item-list">
          {(info?.items ?? []).map((item) => (
            <ItemState key={item.key} item={item} />
          ))}
        </ul>
        <p className="data-scope">
          更新范围：{info?.scopeLabel ?? "待归类股票与观察组股票"}
          {info ? `（${info.scopeCount} 只）` : ""}
        </p>
      </section>

      <div className="data-columns">
        <section className="data-card" aria-labelledby="data-result-title">
          <h2 id="data-result-title" className="mb-3 text-base font-semibold">
            更新结果
          </h2>
          <div className="data-table-scroll">
            <table className="data-table" aria-label="逐股更新结果">
              <thead>
                <tr>
                  <th scope="col">股票</th>
                  <th scope="col">行情日期</th>
                  <th scope="col">状态</th>
                  <th scope="col">操作</th>
                </tr>
              </thead>
              <tbody>
                {stocks.map((stock) => {
                  const display = STOCK_STATE[stock.state];
                  const Icon = display.icon;
                  const retryable = stock.state === "pending" || stock.state === "unconfirmed";
                  return (
                    <tr key={stock.securityId} data-state={stock.state}>
                      <td>
                        <span className="data-stock-name">
                          {stock.name || stock.securityId.split(".")[0]}
                        </span>
                        <span className="data-stock-code">
                          {stock.securityId.split(".")[0]}
                        </span>
                      </td>
                      <td>{stock.latestDate ?? "—"}
                        <span className="data-state-reason">{stock.source ?? "暂无行情来源"}</span>
                      </td>
                      <td>
                        <span className={`data-state ${display.tone}`}>
                          <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                          {display.label}
                        </span>
                        {stock.reason ? (
                          <span className="data-state-reason" title={stock.reason}>
                            {stock.reason}
                          </span>
                        ) : null}
                        {stock.lastError ? (
                          <span className="data-state-reason" title={stock.lastError}>
                            {stock.lastError}
                          </span>
                        ) : null}
                      </td>
                      <td>
                        {retryable ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            className="data-retry"
                            onClick={() => void retry()}
                            disabled={busy || running}
                          >
                            重试
                          </Button>
                        ) : (
                          "—"
                        )}
                      </td>
                    </tr>
                  );
                })}
                {stocks.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="data-empty">
                      更新范围内暂无股票：导入并归类后，这里显示逐股结果。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </section>

        <div className="data-side">
          <section className="data-card" aria-labelledby="data-run-title">
            <div className="mb-3 flex items-baseline justify-between gap-2">
              <h2 id="data-run-title" className="text-base font-semibold">
                本次更新
              </h2>
              <span className="text-xs text-foreground/50">
                开始时间：{formatTime(info?.lastRun?.startedAt ?? null)}
              </span>
            </div>
            <div
              className="data-progress"
              role="progressbar"
              aria-valuenow={percent}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label="更新进度"
            >
              <span style={{ width: `${percent}%` }} />
            </div>
            <p className="data-progress-count">
              {progress ? `${progress.done} / ${progress.total}` : `${done} / ${stocks.length}`}
            </p>
            {progress?.currentSecurityId ? (
              <p className="text-xs text-foreground/60" data-testid="data-current-stock">
                正在处理 {progress.currentSecurityId}
              </p>
            ) : null}
            <ul className="data-legend">
              {STATE_ORDER.map((key) => {
                const display = STOCK_STATE[key];
                return (
                  <li key={key}>
                    <span className={`data-dot ${display.dot}`} aria-hidden="true" />
                    <span className="flex-1">{display.label}</span>
                    <span>{counts[key]}</span>
                  </li>
                );
              })}
            </ul>
            <Button
              variant="ghost"
              className="data-retry-incomplete"
              onClick={() => void retry()}
              disabled={busy || running || retryCount === 0}
            >
              <RotateCcw className="h-4 w-4" aria-hidden="true" />
              重试未完成（{retryCount}）
            </Button>
            {info?.lastRun ? (
              <>
                <p className="mt-3 text-xs text-foreground/60">
                  最近一次：{RUN_STATUS[info.lastRun.status] ?? info.lastRun.status}
                  （成功 {info.lastRun.quotesOk} 只
                  {info.lastRun.quotesFailed > 0
                    ? `，失败 ${info.lastRun.quotesFailed} 只（保留旧数据）`
                    : ""}
                  {info.lastRun.quotesSkipped > 0 ? `，无数据 ${info.lastRun.quotesSkipped} 只` : ""}
                  {info.lastRun.quotesPending > 0 ? `，未补齐 ${info.lastRun.quotesPending} 只` : ""}
                  ）{info.lastRun.durationMs != null ? `，用时 ${formatDuration(info.lastRun.durationMs)}` : ""}
                </p>
                {info.lastRun.quotesOk === 0 && info.lastRun.quotesFailed === 0 &&
                  info.lastRun.quotesPending === 0 && info.complete ? (
                    <p className="text-xs text-foreground/60">本次无需补取日线。</p>
                  ) : null}
                {(info.quoteDiagnostics?.length ?? 0) > 0 ? (
                  <details className="mt-2 text-xs text-foreground/60">
                    <summary>本次最慢的股票</summary>
                    <ul className="mt-1 space-y-1" data-testid="quote-diagnostics">
                      {info.quoteDiagnostics?.map((item) => (
                        <li key={item.securityId}>
                          {item.securityId} · {formatDuration(item.elapsedMs)} ·
                          {item.fetchMode === "initial" ? "首次" : item.fetchMode === "rebuild" ? "重建" : "增量"}
                          {item.requestStart ? ` · 从 ${item.requestStart}` : ""}
                          {item.attempts.length ? ` · ${item.attempts.map((attempt) =>
                            `${attempt.source} ${formatDuration(attempt.elapsedMs)}（${attempt.outcome}${attempt.error ? `：${attempt.error}` : ""}）`).join("；")}` : ""}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
              </>
            ) : (
              <p className="mt-3 text-xs text-foreground/60">
                尚未执行更新；北京时间 16:30 至 22:30 每小时尝试，白天启动最多补更一次。
              </p>
            )}
          </section>

          <section className="data-card" aria-labelledby="data-history-title">
            <button
              type="button"
              className="data-history-toggle"
              aria-expanded={showHistory}
              onClick={() => setShowHistory((value) => !value)}
            >
              <h2 id="data-history-title" className="text-base font-semibold">
                更新记录
              </h2>
              <ChevronDown
                className={`h-4 w-4 transition-transform ${showHistory ? "rotate-180" : ""}`}
                aria-hidden="true"
              />
            </button>
            {showHistory ? (
              <ul className="data-history" aria-label="更新记录">
                {(info?.history ?? []).map((run) => (
                  <li key={run.runId}>
                    <span className="text-foreground/70">
                      {run.finishedAt ? run.finishedAt.replace("T", " ").slice(0, 16) : "进行中"}
                    </span>
                    <span>{RUN_STATUS[run.status] ?? run.status}</span>
                    <span className="text-foreground/50">
                      成功 {run.quotesOk} / 失败 {run.quotesFailed}
                      {run.quotesPending > 0 ? ` / 未补齐 ${run.quotesPending}` : ""}
                      {run.securitiesStatus === "failed" ? " / 证券库失败" : ""}
                    </span>
                  </li>
                ))}
                {(info?.history ?? []).length === 0 ? (
                  <li className="text-foreground/50">暂无更新记录</li>
                ) : null}
              </ul>
            ) : null}
          </section>
        </div>
      </div>
    </div>
  );
}

function ItemState({ item }: { item: DataStatusItem }) {
  const display = ITEM_STATE[item.state];
  return (
    <li data-testid={`data-item-${item.key}`}>
      <span className="data-item-label">{item.label}</span>
      <span className={`data-state ${display.tone}`}>
        <span className={`data-dot ${display.dot}`} aria-hidden="true" />
        {display.label}
      </span>
      <span className="data-item-message">{item.message}</span>
    </li>
  );
}

function formatTime(value: string | null): string {
  if (!value) {
    return "—";
  }
  return value.replace("T", " ").slice(0, 16);
}

function formatDuration(value: number | null): string {
  if (value == null) return "—";
  return value >= 60_000 ? `${(value / 60_000).toFixed(1)} 分钟` : `${(value / 1000).toFixed(1)} 秒`;
}
