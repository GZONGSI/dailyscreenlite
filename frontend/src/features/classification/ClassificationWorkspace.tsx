import { useCallback, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "../../components/ui/Button";

import { CandidateCard } from "./CandidateCard";
import { CandidateQueue } from "./CandidateQueue";
import { CandidateToolbar } from "./CandidateToolbar";
import { EndCard } from "./EndCard";
import { changeTone, formatChangePct, formatPrice } from "../../lib/format";
import { itemStateLabel } from "./state";
import { useClassificationWorkspace } from "./useClassificationWorkspace";

interface Props {
  /** 外部（全局搜索跳转、数据更新）要求重读时递增；变化即重读并保留当前股票。 */
  refreshToken: number;
  /** 打开观察组模块（卡片上的「管理观察组」入口）。 */
  onManageGroups: () => void;
}

/**
 * 候选归类工作区：左栏队列 + 右侧当前股票卡。
 *
 * 读取、增量应用与写入次序都在 `useClassificationWorkspace`；这里只保留
 * 纯展示状态（列表／卡片视图、队列折叠、选卡占位）与画法。
 */
export function ClassificationWorkspace({ refreshToken, onManageGroups }: Props) {
  const [queueCollapsed, setQueueCollapsed] = useState(false);
  const [fromList, setFromList] = useState(false);
  const [interactionBusy, setInteractionBusy] = useState(false);
  /** 手动点选候选项期间用占位卡遮住旧卡，避免看到上一只的残留内容。 */
  const [selecting, setSelecting] = useState(false);
  /** 队列的可滚动容器：窗口化列表按它计算可见范围，不用列表自身。 */
  const queuePaneRef = useRef<HTMLElement | null>(null);

  const {
    view,
    rows,
    pending,
    dates,
    quotes,
    batches,
    loading,
    listLoading,
    loadError,
    savedRefreshError,
    busyId,
    navigationBusy,
    navigationError,
    currentId,
    current,
    currentIndex,
    currentGroupNames,
    counts,
    reload,
    changeFilters,
    selectCandidate,
    onClassified,
    onRelationsChanged,
    onNotesChanged,
    reclassify,
    cleanup,
    move,
    returnToQueue,
  } = useClassificationWorkspace(refreshToken);

  /** 点选是一次「读取当前卡」：期间锁住卡片区域，结束后无论成败都解锁。 */
  const selectCandidateWithPlaceholder = useCallback(
    async (candidateId: string) => {
      setSelecting(true);
      try {
        return await selectCandidate(candidateId);
      } finally {
        setSelecting(false);
      }
    },
    [selectCandidate],
  );

  const reclassifyCurrent = useCallback(async () => {
    if (current) await reclassify(current.candidateId, true);
  }, [current, reclassify]);

  const queuePaneElement = (
    <CandidateQueue
      candidates={rows}
      selectedId={view?.ended ? null : currentId}
      scope={view?.scope ?? "unprocessed"}
      counts={counts}
      dates={dates}
      importDate={view?.importDate ?? null}
      loading={listLoading}
      busyId={busyId}
      quotes={quotes}
      filtered={Boolean(view?.search || view?.importDate || view?.result)}
      scrollRef={queuePaneRef}
      onScopeChange={(scope) => void changeFilters({ scope })}
      onDateChange={(importDate) => void changeFilters({ importDate })}
      onClearFilters={() =>
        void changeFilters({ search: "", importDate: null, result: null })
      }
      onSelect={async (candidateId) => {
        if (await selectCandidateWithPlaceholder(candidateId)) {
          if (view?.viewMode === "list") await changeFilters({ viewMode: "card" });
        }
      }}
      onReclassify={(candidateId) => void reclassify(candidateId)}
    />
  );

  const cardPane = selecting ? (
    <div className="animate-pulse rounded-xl bg-muted p-12" aria-busy="true">
      正在读取股票…
    </div>
  ) : view?.ended ? (
    <EndCard
      round={view?.round ?? { viewed: 0, processed: 0, remaining: 0 }}
      hasPrevious={(view?.cursor ?? 0) > 0}
      hasNext={view?.hasNext ?? false}
      busy={navigationBusy}
      onPrevious={() => void move("previous")}
      onNext={() => void move("next")}
      onReturn={returnToQueue}
    />
  ) : current ? (
    <CandidateCard
      key={current.candidateId}
      candidate={current}
      sourceNames={batches}
      groupNames={currentGroupNames}
      position={currentIndex >= 0 ? currentIndex + 1 : null}
      queueSize={pending.length}
      hasPrevious={(view?.cursor ?? 0) > 0}
      hasNext={view?.hasNext ?? true}
      outsideFilter={view ? !view.inFilter : false}
      navigationBusy={navigationBusy}
      quote={quotes.get(current.securityId)}
      onUpdated={onClassified}
      onRelationsChanged={onRelationsChanged}
      onReclassify={() => void reclassifyCurrent()}
      onBusyChange={setInteractionBusy}
      onManageGroups={onManageGroups}
      onNotesChanged={onNotesChanged}
      onPrevious={() => void move("previous")}
      onNext={() => void move("next")}
      refreshToken={refreshToken}
    />
  ) : (
    <section className="rounded-2xl border border-dashed border-border bg-surface/60 p-10 text-center">
      <p className="text-sm text-foreground/60">
        {counts.unprocessed === 0
          ? "没有待归类的股票，先在导入页提交候选。"
          : "当前筛选没有匹配股票，请调整或清除筛选。"}
      </p>
    </section>
  );

  if (loading && !view) {
    return (
      <div className="m-4 animate-pulse rounded-xl bg-muted p-12" aria-busy="true">
        <Loader2 className="animate-spin" />
        正在加载候选队列…
      </div>
    );
  }

  if (!view) {
    return (
      <div className="m-4 rounded-xl border border-border bg-surface p-8">
        <p className="mb-4">候选队列读取失败，请检查本机服务后重试。</p>
        <Button onClick={reload}>重新加载候选队列</Button>
        {loadError ? (
          <p role="alert" className="mt-3 text-sm text-danger">
            {loadError}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div className="classification-workspace">
      <div className="classification-toolbar" inert={interactionBusy || navigationBusy}>
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={!queueCollapsed}
          onClick={() => setQueueCollapsed((value) => !value)}
        >
          {queueCollapsed ? "展开队列" : "收起队列"}
        </Button>
        <CandidateToolbar
          state={view}
          onChange={changeFilters}
          onCleanup={cleanup}
          disabled={listLoading}
        />
      </div>
      {loadError ? (
        <p role="alert" className="px-1 py-2 text-sm text-danger">
          {loadError}
        </p>
      ) : null}
      {savedRefreshError ? (
        <div
          role="alert"
          className="flex items-center gap-2 px-1 py-2 text-sm text-danger"
        >
          已保存，队列与统计刷新失败。
          <Button
            variant="outline"
            disabled={interactionBusy}
            onClick={() => void onRelationsChanged()}
          >
            重新读取结果
          </Button>
        </div>
      ) : null}
      {navigationError ? (
        <div role="alert" className="flex items-center gap-2 px-1 py-2 text-sm text-danger">
          {navigationError}
          <Button variant="outline" disabled={navigationBusy} onClick={() => void move("next") }>
            重试切换
          </Button>
        </div>
      ) : null}

      {view.viewMode === "list" ? (
        <div
          className="classification-table-scroll"
          aria-busy={listLoading}
          data-testid="main-column"
        >
          {listLoading ? (
            <p role="status" className="px-4 py-2 text-sm text-primary">
              正在更新筛选，暂时保留上次结果…
            </p>
          ) : null}
          <table aria-label="候选股票列表" className="classification-table">
            <thead>
              <tr>
                {["股票", "代码", "最近入选", "收盘", "涨跌幅", "处理状态", "已观察", "来源数", "操作"].map(
                  (label) => (
                    <th key={label} className={label === "来源数" ? "text-right" : undefined}>
                      {label}
                    </th>
                  ),
                )}
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => {
                const quote = quotes.get(item.securityId);
                return (
                  <tr key={item.candidateId} aria-selected={item.candidateId === currentId}>
                    <td>
                      <Button
                        variant="ghost"
                        disabled={listLoading}
                        onClick={async () => {
                          if (!(await selectCandidateWithPlaceholder(item.candidateId))) return;
                          await changeFilters({ viewMode: "card" });
                          setFromList(true);
                        }}
                      >
                        {item.security?.name ?? item.securityId}
                      </Button>
                    </td>
                    <td className="font-mono">{item.security?.code}</td>
                    <td>{item.latestImportDate ?? "—"}</td>
                    <td className="tabular-nums">
                      {formatPrice(quote?.close ?? null)}
                    </td>
                    <td className={`tabular-nums ${changeTone(quote?.changePct ?? null)}`}>
                      {formatChangePct(quote?.changePct ?? null)}
                    </td>
                    <td>{itemStateLabel(item)}</td>
                    <td>{item.observed ? "已观察" : "—"}</td>
                    <td className="text-right tabular-nums">{item.sourceCount}</td>
                    <td>
                      {item.state !== "pending" && item.state !== "later" ? (
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={listLoading || busyId === item.candidateId}
                          onClick={() => void reclassify(item.candidateId)}
                        >
                          重新归类
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {!rows.length ? (
            <div className="p-8 text-center text-foreground/60">
              <p>没有符合筛选条件的股票。</p>
              <Button
                variant="ghost"
                onClick={() => void changeFilters({ search: "", importDate: null, result: null })}
              >
                清除筛选
              </Button>
            </div>
          ) : null}
        </div>
      ) : (
        <div className={`classification-body ${queueCollapsed ? "queue-collapsed" : ""}`}>
          <aside
            ref={queuePaneRef}
            className="classification-queue-pane"
            inert={interactionBusy || navigationBusy}
          >
            {listLoading ? (
              <p role="status" className="px-1 py-2 text-sm text-primary">
                正在更新筛选，暂时保留上次结果…
              </p>
            ) : null}
            {queuePaneElement}
          </aside>
          <main
            className="classification-card-pane"
            data-testid="main-column"
            aria-busy={listLoading}
          >
            <fieldset disabled={listLoading} className="min-w-0">
              {fromList ? (
                <Button
                  size="sm"
                  variant="ghost"
                  disabled={interactionBusy}
                  onClick={() => void changeFilters({ viewMode: "list" })}
                >
                  返回列表
                </Button>
              ) : null}
              {cardPane}
            </fieldset>
          </main>
        </div>
      )}
    </div>
  );
}
