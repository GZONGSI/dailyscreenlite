import { useId, useMemo, type RefObject } from "react";
import { Inbox, RotateCcw } from "lucide-react";

import type { CandidateRow, CandidateScope } from "../../api/classification";
import type { QuoteSnapshot } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { VirtualList } from "../../components/ui/VirtualList";
import { changeTone, formatChangePct, formatPrice } from "../../lib/format";
import { SCOPE_LABEL, isUnprocessed, itemStateLabel } from "./state";

/** 单个列表行的固定高度（像素），与 .classification-queue-row 的高度一致。 */
const ROW_HEIGHT = 88;

interface Props {
  /** 当前筛选范围内的完整轻量列表；渲染时只保留可见行。 */
  candidates: CandidateRow[];
  selectedId: string | null;
  scope: CandidateScope;
  counts: { unprocessed: number; processed: number };
  dates: string[];
  importDate: string | null;
  loading: boolean;
  busyId: string | null;
  quotes: Map<string, QuoteSnapshot>;
  filtered: boolean;
  /** 实际滚动的左栏容器：完整列表滚动时只渲染可见行。 */
  scrollRef?: RefObject<HTMLElement | null>;
  onScopeChange: (scope: CandidateScope) => void;
  onDateChange: (importDate: string | null) => void;
  onClearFilters: () => void;
  onSelect: (candidateId: string) => void;
  onReclassify: (candidateId: string) => void;
}

/**
 * 候选归类左栏：待归类/已处理两个页签、入选日期筛选、列表/卡片切换与队列本身。
 *
 * 左侧仍是完整可滚动、可任意点选的列表，但只为可见范围渲染真实行；
 * 队列突出名称与对应行情日涨跌幅，代码与最新收盘价为次要信息，不展示日期；
 * 没有行情时如实留空，不冒充当天行情。
 */
export function CandidateQueue({
  candidates,
  selectedId,
  scope,
  counts,
  dates,
  importDate,
  loading,
  busyId,
  quotes,
  filtered,
  scrollRef,
  onScopeChange,
  onDateChange,
  onClearFilters,
  onSelect,
  onReclassify,
}: Props) {
  const titleId = useId();
  const unprocessedCount = useMemo(
    () => candidates.filter((c) => isUnprocessed(c.state)).length,
    [candidates],
  );

  return (
    <section
      aria-labelledby={titleId}
      className="classification-queue rounded-2xl border border-border bg-surface p-4 shadow-sm"
    >
      <h2 id={titleId} className="text-base font-semibold">
        候选归类
      </h2>

      <div
        role="tablist"
        aria-label="归类范围"
        className="mt-3 flex items-center gap-1 rounded-lg bg-muted/60 p-1"
      >
        {(["unprocessed", "processed"] as const).map((value) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={scope === value}
            disabled={loading}
            onClick={() => onScopeChange(value)}
            className={`flex min-h-9 min-w-0 flex-1 items-center justify-center whitespace-nowrap rounded-md px-1 text-sm font-medium transition-colors duration-150 ${
              scope === value
                ? "bg-surface text-foreground shadow-sm"
                : "text-foreground/65 hover:text-foreground"
            }`}
          >
            <span>{SCOPE_LABEL[value]}</span>
            <span className="ml-1 text-xs font-normal tabular-nums text-foreground/60">
              （{value === "unprocessed" ? counts.unprocessed : counts.processed}）
            </span>
          </button>
        ))}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-2 text-xs text-foreground/70">
          <span className="sr-only">按入选日期筛选</span>
          <select
            aria-label="按入选日期筛选"
            disabled={loading}
            value={importDate ?? ""}
            onChange={(event) => onDateChange(event.target.value || null)}
            className="min-h-9 rounded-lg border border-border bg-surface px-2 text-xs"
          >
            <option value="">全部日期</option>
            {dates.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>

      {loading && candidates.length === 0 ? (
        <p className="px-1 py-6 text-sm text-foreground/60" aria-busy="true">
          正在读取候选队列…
        </p>
      ) : candidates.length === 0 && filtered ? (
        <div className="py-6 text-sm text-foreground/60">
          <p>没有符合筛选条件的股票。</p>
          <Button variant="ghost" size="sm" onClick={onClearFilters}>
            清除筛选
          </Button>
        </div>
      ) : candidates.length === 0 ? (
        <p className="flex items-center gap-2 px-1 py-6 text-sm text-foreground/60">
          <Inbox className="h-4 w-4" aria-hidden="true" />
          {scope === "processed"
            ? "没有符合条件的已处理股票。"
            : "暂无待归类股票，先在导入页提交候选。"}
        </p>
      ) : (
        <VirtualList
          as="ul"
          items={candidates}
          rowHeight={ROW_HEIGHT}
          scrollRef={scrollRef}
          className="classification-queue-list mt-3"
          containerProps={{
            role: "listbox",
            "aria-label": scope === "unprocessed" ? "待归类股票列表" : "已归类股票列表",
          }}
        >
          {(candidate, index) => {
            const selected = candidate.candidateId === selectedId;
            const processed = !isUnprocessed(candidate.state);
            const quote = quotes.get(candidate.securityId);
            const label = candidate.security?.name || candidate.securityId;
            return (
              <li
                data-selected={selected ? "true" : undefined}
                className={`classification-queue-row flex items-center gap-2 rounded-lg transition-colors duration-150 ${
                  selected ? "" : "hover:bg-muted"
                }`}
                style={{ height: ROW_HEIGHT }}
              >
                <button
                  type="button"
                  role="option"
                  disabled={loading}
                  aria-selected={selected}
                  aria-posinset={index + 1}
                  aria-setsize={candidates.length}
                  onClick={() => onSelect(candidate.candidateId)}
                  className="flex min-w-0 flex-1 flex-col justify-center gap-1.5 px-2 py-2 text-left"
                >
                  <span className="flex w-full min-w-0 items-center gap-2">
                    <span className="min-w-0 flex-1 truncate text-base font-semibold leading-6" title={label}>
                      {label}
                    </span>
                    <span
                      className={`shrink-0 whitespace-nowrap text-base font-semibold tabular-nums leading-6 ${changeTone(quote?.changePct ?? null)}`}
                      title={quote ? `涨跌幅 · 行情 ${quote.tradeDate}` : "暂无行情"}
                    >
                      {formatChangePct(quote?.changePct ?? null)}
                    </span>
                  </span>
                  <span className="flex w-full items-center justify-between gap-2 whitespace-nowrap text-sm tabular-nums leading-5 text-foreground/60">
                    <span className="font-mono">{candidate.security?.code ?? ""}</span>
                    <span title="收盘价">{formatPrice(quote?.close ?? null)}</span>
                  </span>
                  {scope === "processed" ? (
                    <span className="text-xs leading-4 text-foreground/60">{itemStateLabel(candidate)}</span>
                  ) : null}
                </button>
                {processed && scope !== "unprocessed" ? (
                  <button
                    type="button"
                    aria-label={`重新归类 ${label}`}
                    title="主动重新归类"
                    onClick={() => onReclassify(candidate.candidateId)}
                    disabled={loading || busyId === candidate.candidateId}
                    className="mr-1 flex h-8 w-8 shrink-0 items-center justify-center rounded text-foreground/60 hover:bg-surface hover:text-foreground disabled:opacity-50"
                  >
                    <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                  </button>
                ) : null}
              </li>
            );
          }}
        </VirtualList>
      )}

      {scope === "unprocessed" && unprocessedCount > 0 ? (
        <p className="mt-3 text-xs text-foreground/50">
          共 {unprocessedCount} 只待归类；点选任一候选即可打开，归类保存后自动推进。
        </p>
      ) : null}
      {unprocessedCount === 0 && candidates.length > 0 ? (
        <p className="mt-3 text-xs text-foreground/50">
          <Badge tone="info">队列为空</Badge>
        </p>
      ) : null}
    </section>
  );
}
