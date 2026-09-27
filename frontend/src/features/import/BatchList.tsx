import type { BatchStatus, ImportBatch } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";

interface Props {
  batches: ImportBatch[];
  /** 选中的批次 id；列表高亮，详情在结果区展示。 */
  selectedId: string | null;
  onSelect: (batchId: string) => void;
  /** 是否展开全部记录（默认只显示最近几条）。 */
  expanded: boolean;
  onToggleExpanded: () => void;
}

const STATUS_TONE: Record<BatchStatus, "success" | "warning" | "danger" | "info" | "neutral"> = {
  published: "success",
  all_unknown: "warning",
  empty: "info",
  rejected: "danger",
  awaiting_selection: "warning",
  awaiting_confirmation: "warning",
};

const STATUS_LABEL: Record<BatchStatus, string> = {
  published: "导入完成",
  all_unknown: "全部未识别",
  empty: "真实零结果",
  rejected: "未发布",
  awaiting_selection: "待选择",
  awaiting_confirmation: "待确认条件",
};

const COLLAPSED_COUNT = 5;

/** 最近导入记录：导入时间、来源与识别结果；点击某条查看它的完整结果。 */
export function BatchList({
  batches,
  selectedId,
  onSelect,
  expanded,
  onToggleExpanded,
}: Props) {
  if (batches.length === 0) {
    return (
      <section className="rounded-2xl border border-border bg-surface p-4 shadow-sm">
        <h2 className="text-base font-semibold">最近导入记录</h2>
        <p className="mt-2 text-sm text-foreground/60">还没有导入记录。</p>
      </section>
    );
  }
  const visible = expanded ? batches : batches.slice(0, COLLAPSED_COUNT);
  return (
    <section
      aria-labelledby="batch-history-title"
      data-testid="batch-list"
      className="rounded-2xl border border-border bg-surface p-4 shadow-sm"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 id="batch-history-title" className="text-base font-semibold">
          最近导入记录
        </h2>
        {batches.length > COLLAPSED_COUNT ? (
          <Button size="sm" variant="ghost" onClick={onToggleExpanded}>
            {expanded ? "收起" : "查看更多"}
          </Button>
        ) : null}
      </div>
      <ul className="space-y-1" aria-label="导入记录列表">
        {visible.map((batch) => (
          <li key={batch.batchId}>
            <button
              type="button"
              aria-current={batch.batchId === selectedId}
              onClick={() => onSelect(batch.batchId)}
              className={`flex w-full flex-wrap items-center justify-between gap-2 rounded-lg border border-border/70 px-3 py-2 text-left text-sm transition-colors duration-150 ${
                batch.batchId === selectedId ? "bg-selected" : "hover:bg-muted"
              }`}
            >
              <span className="min-w-0">
                <span className="block truncate font-medium">{batch.sourceName}</span>
                <span className="block text-xs text-foreground/55">
                  {batch.receivedAt.replace("T", " ").slice(0, 16)} · {batch.importDate}
                </span>
              </span>
              <span className="flex shrink-0 items-center gap-2">
                <span className="text-xs text-foreground/65">
                  新增 {batch.newCandidateCount} · 合并 {batch.mergedCandidateCount} · 跳过{" "}
                  {batch.skippedCount}
                </span>
                <Badge tone={STATUS_TONE[batch.status]}>{STATUS_LABEL[batch.status]}</Badge>
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
