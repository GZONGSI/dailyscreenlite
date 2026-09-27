import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Maximize2, Minimize2 } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import type { Candidate } from "../../api/classification";
import { api, ApiError, type QuoteSnapshot } from "../../api/client";
import type {
  ObservationView,
  ObservationViewChanges,
  ObservedStock,
} from "../../api/observations";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { itemStateLabel, isUnprocessed } from "../classification/state";
import { loadSourceNames } from "../import/sources";
import { SecurityDetail } from "../securities/SecurityDetail";
import { GroupEditor } from "./GroupEditor";
import { GroupList } from "./GroupList";
import { groupNamesFor } from "./groups";
import { ObservedStockTable } from "./ObservedStockTable";
import {
  beginObservationViewWrite,
  cancelObservationViewRead,
  refreshObservationView,
  useObservationViewQuery,
  useStockDetailQuery,
  writeObservationView,
} from "./queries";

interface Props {
  /**
   * 数据更新后要求行情图表原位重读的信号（行情分页尚未迁移，这里只桥接图表）；
   * 工作表自身的读取由查询负责，不再由这个信号驱动。
   */
  quoteRefreshToken: number;
  /** 跨模块要求打开某只股票的详情；只消费一次，之后由本模块自己保存位置。 */
  focusSecurityId: string | null;
  /** 已消费这次的跨模块打开请求；避免之后的数据更新再次把当前位置拉回该股票。 */
  onFocusHandled: () => void;
  /** 详情里的归类动作把用户送回候选归类模块。 */
  onOpenClassification: () => void;
}

/**
 * 观察组工作区：左侧组列表、中间观察股票表、右侧同页个股详情。
 *
 * 组、排序与当前股票保存在服务端，切模块、刷新与重启都恢复同一工作位置；
 * 各模块互不自动同步当前股票，只有显式搜索或跨模块动作才更新目标。
 * 详情可展开为完整页面，返回时组、排序与列表位置保持不变。
 *
 * 工作表与共用详情是两份服务器资源，读取状态、缓存与写后回收都在 `queries.ts`：
 * 组件只表达草稿、展开态、按钮忙碌态，以及「保存后的位置写入」这条业务动作。
 * 写入与读取的先后在这里表达：写入在途时不发新读取；只有最后一次发起的写入
 * （且发起它的实例仍在页面上）可以回写共享缓存。
 */
export function ObservationWorkspace({
  quoteRefreshToken,
  focusSecurityId,
  onFocusHandled,
  onOpenClassification,
}: Props) {
  const queryClient = useQueryClient();
  // 跨模块打开期间不发起普通读取：这次打开的写入响应就是唯一结果来源
  const focusPending = focusSecurityId !== null;
  const viewQuery = useObservationViewQuery(!focusPending);
  const view = viewQuery.data ?? null;
  const currentSecurityId = view?.state.currentSecurityId ?? null;
  const detailQuery = useStockDetailQuery(currentSecurityId, !focusPending);
  const detail = detailQuery.data ?? null;

  const [quotes, setQuotes] = useState<Map<string, QuoteSnapshot>>(new Map());
  const [batches, setBatches] = useState<Map<string, string>>(new Map());
  /** 工作位置写入与跨模块打开失败：工作表本身仍显示已读回的内容。 */
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState(false);
  /** 工作位置写入链：最后一次点击的位置是缓存的最终结论。 */
  const writes = useRef<Promise<unknown>>(Promise.resolve());
  /**
   * 本实例是否还是页面上的工作表。
   *
   * 离开观察模块会卸载工作表，此前发出的写入仍在路上，而它的响应属于旧实例：共享缓存
   * 已经归新实例（或归下一次读取）所有，旧响应只丢弃，不能拿来回写。
   */
  const currentInstance = useRef(true);
  useEffect(() => {
    // 严格模式会挂载两次：第二次重新认领，第一次的响应照样不写回
    currentInstance.current = true;
    return () => {
      currentInstance.current = false;
    };
  }, []);

  /**
   * 受保护地执行一次工作表写入：窗口内不发新的读取，响应只在仍有资格时回写。
   *
   * 「有资格」= 发起它的实例仍是当前实例，且它仍是最后一次发起的工作表写入——同一实例里
   * 一次更晚的搜索焦点或自动落位会取代它。响应没有资格时不回写（服务端已按当时的请求
   * 保存，缓存交给更晚的那次写入或下一次读取），并补一次刷新取服务器事实：被丢弃的响应
   * 仍然改过服务端，不留下页面与持久化的分歧。
   */
  const writeView = useCallback(
    async (request: () => Promise<ObservationView>): Promise<ObservationView | null> => {
      const write = beginObservationViewWrite();
      let applied: ObservationView | null = null;
      let dropped = false;
      try {
        // 写入前取消在途的工作表读取：迟到的读取会带着切换之前的状态落地，
        // 把刚保存的工作位置覆盖回去（同笔记与跨模块打开的写前取消约定）。
        await cancelObservationViewRead(queryClient);
        const response = await request();
        if (currentInstance.current && write.isLatest()) {
          writeObservationView(queryClient, response);
          applied = response;
        } else {
          dropped = true;
        }
      } finally {
        write.close();
      }
      // 窗口已经关闭再补刷新；补读等其他写入，不阻塞这次打开的消费。
      // 否则旧实例响应未返回时，已被点选取代的搜索仍会禁用新证券的详情读取。
      if (dropped) void refreshObservationView(queryClient);
      return applied;
    },
    [queryClient],
  );

  /** 保存工作位置：响应即新的权威工作表，写回资格由 writeView 统一判断。 */
  const writePosition = useCallback(
    (changes: ObservationViewChanges) =>
      writeView(() => api.updateObservationView(changes)),
    [writeView],
  );

  const groupNames = useCallback(
    (groupIds: string[]) => groupNamesFor(view?.groups ?? [], groupIds),
    [view],
  );

  /**
   * 当前视图里的证券标识与它的稳定键：键用于驱动行情重读，
   * 换一批股票（切组、重读、改筛选）就重新取一次摘要。
   */
  const stockIdKey = useMemo(
    () => (view?.stocks ?? []).map((stock) => stock.securityId).join("|"),
    [view?.stocks],
  );

  /**
   * 行情摘要：只读取当前视图里的股票，最近两个交易日。
   *
   * 由 stockIdKey 驱动而不是在各处手动调用：切组、重读、排序都会换掉视图对象，
   * 只在改工作位置的地方补调用就必然漏掉某条路径（切组后整列价格为空就是这样来的）。
   */
  const loadQuotes = useCallback(async (securityIds: string[]) => {
    if (!securityIds.length) {
      setQuotes(new Map());
      return;
    }
    try {
      const { quotes: loaded } = await api.quoteSummary(securityIds);
      setQuotes(new Map(loaded.map((quote) => [quote.securityId, quote])));
    } catch {
      // 行情读取失败只影响列表上的价格显示，不阻塞浏览
      setQuotes(new Map());
    }
  }, []);

  const loadBatchNames = useCallback(async () => {
    setBatches(await loadSourceNames());
  }, []);

  const viewLoadedAt = viewQuery.dataUpdatedAt;

  // 列表换一批股票就重读行情摘要；同一批股票不重复请求
  useEffect(() => {
    void loadQuotes(stockIdKey ? stockIdKey.split("|") : []);
  }, [loadQuotes, stockIdKey]);

  // 每次读回工作表都重取一次来源名称：批次列表是另一份资源，失败只影响来源显示
  useEffect(() => {
    if (!viewLoadedAt) return;
    void loadBatchNames();
  }, [loadBatchNames, viewLoadedAt]);

  /**
   * 恢复工作位置：没有当前股票时落到列表首项，让同页详情不空着。
   * 已有当前股票则原样保留，刷新或排序变化不擅自跳到别的股票。
   */
  const ensureCurrent = useCallback(
    async (loaded: ObservationView) => {
      if (loaded.state.currentSecurityId) return;
      const target = loaded.stocks[0]?.securityId;
      if (!target) return;
      try {
        await writePosition({ currentSecurityId: target });
      } catch (err) {
        // 列表已经读回来了：落位失败只提示，不把整页当成读取失败
        setActionError(err instanceof ApiError ? err.message : "恢复当前股票失败，请重试");
      }
    },
    [writePosition],
  );

  useEffect(() => {
    if (!view || focusPending) return;
    void ensureCurrent(view);
  }, [ensureCurrent, focusPending, view]);

  /**
   * 跨模块打开详情：写入是这次打开唯一的结果来源。
   *
   * 挂载可能已经发出一次普通读取，它的结果会带着打开之前的当前股票落地，
   * 因此先取消在途读取再写入；写入失败也消费掉这次请求，让工作表回到自己保存的位置。
   * 这次写入与位置修改、自动落位共用同一套资格判定：更晚的写入或已卸载的实例都不能回写。
   */
  useEffect(() => {
    if (!focusSecurityId) return;
    let active = true;
    void (async () => {
      try {
        const focused = await writeView(() => api.focusObservedStock(focusSecurityId));
        if (active && focused) setActionError(null);
      } catch (err) {
        if (active) {
          setActionError(err instanceof ApiError ? err.message : "打开观察详情失败，请重试");
        }
      } finally {
        if (active) onFocusHandled();
      }
    })();
    return () => {
      active = false;
    };
  }, [focusSecurityId, onFocusHandled, writeView]);

  /** 保存工作位置：排序取值由服务端判定（未知排序是 400），线格式是字符串列。 */
  const persist = useCallback(
    async (changes: ObservationViewChanges) => {
      setBusy(true);
      setActionError(null);
      // 工作位置写入串行：快速连点切组、排序或点选时，后一次写入等前一次落定，
      // 服务端与缓存因此都停在同一份最新位置上（否则先发的响应可能后落地）。
      const request = writes.current.then(async () => {
        const updated = await writePosition(changes);
        // 响应没有资格（实例已卸载或被更晚的写入取代）：不回写缓存，也不再据它补落位
        if (!updated) return;
        // 切组或改动关系后当前股票可能不再可见：落到首项，避免详情空着
        await ensureCurrent(updated);
      });
      writes.current = request.catch(() => undefined);
      try {
        await request;
      } catch (err) {
        setActionError(err instanceof ApiError ? err.message : "保存工作位置失败，请重试");
      } finally {
        setBusy(false);
      }
    },
    [ensureCurrent, writePosition],
  );

  /**
   * 组或关系变化后重读列表与详情：成员数量与所属组都要更新。
   *
   * 重读失败的展示统一在工作表的读取错误里（详情读取失败则保留上次内容），
   * 因此这里不抛回调用方，也不把保存成功显示成失败。
   */
  const reloadAll = useCallback(async () => {
    await refreshObservationView(queryClient);
  }, [queryClient]);

  /**
   * 把该股票送回候选归类：显式动作才切换模块，并沿用唯一候选项。
   *
   * prepare 为空表示「有待归类对象，去归类」；传入重新归类接口表示
   * 「已处理股票主动重新归类」——后者会先恢复待处理再进入队列。
   */
  const openClassification = useCallback(
    async (
      prepare: ((candidateId: string) => Promise<unknown>) | null,
      failure: string,
    ) => {
      const candidateId = detail?.candidate?.candidateId;
      if (!candidateId) return;
      setBusy(true);
      setActionError(null);
      try {
        if (prepare) await prepare(candidateId);
        await api.focusCandidate(candidateId);
        onOpenClassification();
      } catch (err) {
        setActionError(err instanceof ApiError ? err.message : failure);
      } finally {
        setBusy(false);
      }
    },
    [detail?.candidate?.candidateId, onOpenClassification],
  );

  const stocks = useMemo(
    () => sortStocks(view?.stocks ?? [], view?.state.sort ?? "joined", quotes),
    [view?.stocks, view?.state.sort, quotes],
  );

  /** 排序方式由服务端保存；当前股票只按服务端保存的工作位置恢复。 */
  const selectedId = view?.state.currentSecurityId ?? null;
  // 详情读取失败要如实提示（含服务端消息）：已有内容保留在页面上，不清空也不静默
  const detailError = detailQuery.isError && detailQuery.error
    ? detailQuery.error instanceof ApiError
      ? detailQuery.error.message
      : "个股详情读取失败，请重试"
    : null;
  // 重读工作表失败：保留已读回的内容并如实提示，不静默显示可能过期的结果
  const readError = viewQuery.isError
    ? viewQuery.error instanceof ApiError
      ? viewQuery.error.message
      : "读取观察列表失败"
    : null;
  const error = actionError ?? readError;

  if (!view && viewQuery.isPending) {
    return (
      <div className="m-4 animate-pulse rounded-xl bg-muted p-12" aria-busy="true">
        <Loader2 className="animate-spin" />
        正在读取观察列表…
      </div>
    );
  }

  if (!view) {
    return (
      <div className="m-4 rounded-xl border border-border bg-surface p-8">
        <p className="mb-4">观察列表读取失败，请检查本机服务后重试。</p>
        <Button onClick={() => void viewQuery.refetch()}>重新加载观察列表</Button>
        {error ? (
          <p role="alert" className="mt-3 text-sm text-danger">
            {error}
          </p>
        ) : null}
      </div>
    );
  }

  const candidate: Candidate | null = detail?.candidate ?? null;
  const pending = candidate ? isUnprocessed(candidate.state) : false;
  const detailLoading = detailQuery.isFetching && !detail;

  return (
    <div className={`observation-body ${expanded ? "is-expanded" : ""}`}>
      <aside className="observation-group-pane" hidden={expanded}>
        <GroupList
          groups={view.groups}
          selectedGroupId={view.state.groupId}
          totalCount={view.stocks.length}
          disabled={busy}
          onSelect={(groupId) => void persist({ groupId })}
          onChanged={reloadAll}
        />
      </aside>

      <div className="observation-table-pane" hidden={expanded}>
        <ObservedStockTable
          stocks={stocks}
          groupNames={groupNames}
          quotes={quotes}
          selectedId={selectedId}
          sort={view.state.sort}
          loading={viewQuery.isFetching}
          disabled={busy}
          onSortChange={(sort) => void persist({ sort })}
          onSelect={(securityId) => void persist({ currentSecurityId: securityId })}
        />
      </div>

      <main className="observation-detail-pane" data-testid="main-column">
        <div className="mb-2 flex items-center justify-between gap-2">
          <span className="text-xs text-foreground/50">
            {view.state.groupId
              ? `当前组：${groupNamesFor(view.groups, [view.state.groupId])[0] ?? "已删除"}`
              : "全部观察股票"}
          </span>
          <Button
            size="sm"
            variant="ghost"
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            {expanded ? (
              <>
                <Minimize2 className="h-4 w-4" aria-hidden="true" />
                返回列表
              </>
            ) : (
              <>
                <Maximize2 className="h-4 w-4" aria-hidden="true" />
                展开
              </>
            )}
          </Button>
        </div>
        {error ? (
          <p role="alert" className="mb-2 text-sm text-danger">
            {error}
          </p>
        ) : null}
        {detailError ? (
          <div role="alert" className="mb-2 text-sm text-danger">
            {detailError}
            <Button size="sm" variant="ghost" onClick={() => void detailQuery.refetch()}>
              重试详情
            </Button>
          </div>
        ) : null}
        {detailLoading ? (
          <div className="animate-pulse rounded-xl bg-muted p-12" aria-busy="true">
            正在读取个股详情…
          </div>
        ) : detail ? (
          <SecurityDetail
            securityId={detail.securityId}
            security={detail.security}
            candidate={candidate}
            groupNames={groupNames(detail.groupIds)}
            quote={quotes.get(detail.securityId)}
            sourceNames={batches}
            statusBadge={
              <Badge tone={pending ? "warning" : "success"}>
                {pending ? (candidate ? itemStateLabel(candidate) : "待归类") : "已观察"}
              </Badge>
            }
            error={null}
            onNotesChanged={() => void reloadAll()}
            refreshToken={quoteRefreshToken}
            disabled={busy}
            actions={
              <>
                <GroupEditor
                  key={detail.securityId}
                  securityId={detail.securityId}
                  groupNames={groupNames(detail.groupIds)}
                  onSaved={reloadAll}
                  disabled={busy}
                />
                {pending ? (
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      void openClassification(null, "打开候选归类失败，请重试")
                    }
                  >
                    去归类
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    disabled={busy || !candidate}
                    onClick={() =>
                      void openClassification(
                        api.reclassifyCandidate,
                        "重新归类失败，请重试",
                      )
                    }
                  >
                    重新归类
                  </Button>
                )}
              </>
            }
          />
        ) : (
          <section className="rounded-2xl border border-dashed border-border bg-surface/60 p-10 text-center">
            <p className="text-sm text-foreground/60">
              {view.state.groupId
                ? "这个组还没有股票，切到「全部观察股票」或在归类时加入本组。"
                : "还没有观察股票，在候选归类里加入观察后就会出现在这里。"}
            </p>
          </section>
        )}
      </main>
    </div>
  );
}

/**
 * 列表排序：joined 保持服务端的加入时间倒序；名称与涨跌幅在本地重排。
 * 缺行情的股票在按涨跌幅排序时排在最后，不把"没有数据"当成最小涨跌幅。
 */
function sortStocks(
  stocks: ObservedStock[],
  sort: string,
  quotes: Map<string, QuoteSnapshot>,
): ObservedStock[] {
  const rows = [...stocks];
  if (sort === "name") {
    rows.sort(
      (a, b) =>
        (a.security?.name ?? a.securityId).localeCompare(
          b.security?.name ?? b.securityId,
          "zh-Hans-CN",
        ) || a.securityId.localeCompare(b.securityId),
    );
  } else if (sort === "change") {
    rows.sort((a, b) => {
      const left = quotes.get(a.securityId)?.changePct;
      const right = quotes.get(b.securityId)?.changePct;
      if (left == null && right == null) {
        return a.securityId.localeCompare(b.securityId);
      }
      if (left == null) return 1;
      if (right == null) return -1;
      return right - left || a.securityId.localeCompare(b.securityId);
    });
  }
  return rows;
}
