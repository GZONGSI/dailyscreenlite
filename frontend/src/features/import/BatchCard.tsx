import { useCallback, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  MinusCircle,
  Play,
  RefreshCw,
  Sparkles,
} from "lucide-react";

import {
  api,
  ApiError,
  type BatchStatus,
  type ImportBatch,
  type SelectionRequest,
} from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";

interface Props {
  batch: ImportBatch;
  onUpdated: (batch: ImportBatch) => void;
  /** 用户点击「开始归类」：切到候选归类模块（队列在后台已更新）。 */
  onStartClassification: () => void;
}

type Tone = "success" | "warning" | "danger" | "info" | "neutral";

interface StatusMeta {
  label: string;
  tone: Tone;
  note: {
    icon: typeof CheckCircle2;
    className: string;
    text: (batch: ImportBatch) => string;
    role: "status" | "alert";
  } | null;
}

const STATUS_META: Record<BatchStatus, StatusMeta> = {
  published: {
    label: "导入完成",
    tone: "success",
    note: {
      icon: CheckCircle2,
      className: "text-emerald-700 dark:text-emerald-300",
      text: (b) => `已导入 ${b.recognizedCount} 只，跳过 ${b.skippedCount} 只`,
      role: "status",
    },
  },
  all_unknown: {
    label: "全部未识别",
    tone: "warning",
    note: {
      icon: AlertTriangle,
      className: "text-amber-700 dark:text-amber-300",
      text: (b) => b.errorMessage ?? "全部未识别",
      role: "status",
    },
  },
  empty: {
    label: "真实零结果",
    tone: "info",
    note: {
      icon: Info,
      className: "text-primary",
      text: () => "来源明确返回零条记录，本次为真实零结果，未创建股票卡。",
      role: "status",
    },
  },
  rejected: {
    label: "未发布",
    tone: "danger",
    note: {
      icon: AlertTriangle,
      className: "text-danger",
      text: (b) => b.errorMessage ?? "该来源未能获取或解析，未发布任何股票",
      role: "alert",
    },
  },
  awaiting_selection: {
    label: "待选择",
    tone: "warning",
    note: {
      icon: AlertTriangle,
      className: "text-amber-700 dark:text-amber-300",
      text: () => "存在歧义，请选择代码列或工作表后继续。",
      role: "status",
    },
  },
  awaiting_confirmation: {
    label: "待确认条件",
    tone: "warning",
    note: {
      icon: Sparkles,
      className: "text-amber-700 dark:text-amber-300",
      text: () => "首次导入该查询或条件口径变化，请确认实际解析条件后发布。",
      role: "status",
    },
  },
};

/** 明细行对候选队列的影响：新增 / 合并 / 重新归类 / 仅追加来源 / 跳过。 */
const EFFECT_META: Record<string, { label: string; tone: Tone; note: string }> = {
  new: { label: "新增", tone: "danger", note: "—" },
  merged: { label: "合并", tone: "info", note: "合并至待归类股票" },
  reopened: { label: "重新归类", tone: "warning", note: "新入选日期，重新进入待归类" },
  source_only: { label: "仅追加来源", tone: "neutral", note: "当天已入选，只追加来源" },
};

function stat(label: string, value: number | null, testId?: string) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-foreground/60">{label}</dt>
      <dd className="text-lg font-semibold tabular-nums" data-testid={testId}>
        {value === null ? "未知" : value}
      </dd>
    </div>
  );
}

function dateStat(label: string, iso: string, testId: string) {
  return (
    <div className="flex flex-col">
      <dt className="text-xs text-foreground/60">{label}</dt>
      <dd className="text-sm font-semibold" data-testid={testId}>
        {iso}
      </dd>
    </div>
  );
}

function SelectionPrompt({
  selection,
  busy,
  onChoose,
}: {
  selection: SelectionRequest;
  busy: boolean;
  onChoose: (choice: { sheet?: string; codeColumn?: number }) => void;
}) {
  const preview = selection.preview;
  return (
    <div className="mt-4 rounded-xl border border-amber-200 bg-amber-500/10/60 p-4">
      <p className="text-sm font-medium text-amber-900">{selection.prompt}</p>
      <p className="mt-3 text-xs font-medium text-foreground/70">
        选择{selection.kind === "sheet" ? "工作表" : "股票代码列"}
      </p>
      <div
        className="mt-1.5 flex flex-wrap gap-2"
        role="group"
        aria-label="代码列或工作表选择"
      >
        {selection.options.map((option) => (
          <Button
            key={option.value}
            variant="secondary"
            disabled={busy}
            data-testid={`selection-option-${option.value}`}
            onClick={() =>
              onChoose(
                selection.kind === "sheet"
                  ? { sheet: option.value }
                  : { codeColumn: Number(option.value) },
              )
            }
          >
            <span>{option.label}</span>
            {option.detail ? (
              <span className="text-xs text-foreground/50">{option.detail}</span>
            ) : null}
          </Button>
        ))}
      </div>

      {preview.rows.length > 0 ? (
        <div className="mt-3 overflow-x-auto rounded-lg border border-border bg-surface">
          <table className="min-w-full text-left text-xs">
            <caption className="sr-only">来源前几行预览</caption>
            {preview.headers.length > 0 ? (
              <thead className="bg-muted/60">
                <tr>
                  {preview.headers.map((header, index) => (
                    <th key={`h-${index}`} scope="col" className="px-2 py-1.5 font-medium">
                      {header || `第${index + 1}列`}
                    </th>
                  ))}
                </tr>
              </thead>
            ) : null}
            <tbody>
              {preview.rows.map((row, rowIndex) => (
                <tr key={`r-${rowIndex}`} className="border-t border-border/70">
                  {row.map((cell, cellIndex) => (
                    <td key={`c-${cellIndex}`} className="px-2 py-1.5 font-mono">
                      {cell}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
  );
}

function ConditionPrompt({
  batch,
  busy,
  onConfirm,
}: {
  batch: ImportBatch;
  busy: boolean;
  onConfirm: () => void;
}) {
  return (
    <div className="mt-4 rounded-xl border border-amber-200 bg-amber-500/10/60 p-4">
      {batch.queryText ? (
        <p className="text-sm text-foreground/80">
          原句：<span className="font-medium">{batch.queryText}</span>
        </p>
      ) : null}
      <p className="mt-2 text-xs font-medium text-foreground/70">问财实际解析条件</p>
      <ul className="mt-1 space-y-1 text-sm">
        {batch.conditionLabels.length > 0 ? (
          batch.conditionLabels.map((label, index) => (
            <li key={index} className="rounded bg-surface px-2 py-1 font-mono text-xs">
              {label}
            </li>
          ))
        ) : (
          <li className="text-xs text-foreground/60">响应未提供可展示的条件文本。</li>
        )}
      </ul>
      <p className="mt-3 text-xs text-foreground/60">
        确认只表示采用以上解析条件；条件含义变化会再次要求确认。
      </p>
      <Button
        className="mt-3"
        disabled={busy}
        data-testid="confirm-condition"
        onClick={onConfirm}
      >
        {busy ? "发布中…" : "确认条件并发布"}
      </Button>
    </div>
  );
}

/**
 * 本次导入结果：汇总新增/合并/重新归类/跳过，逐行给出股票、代码、结果与备注。
 *
 * 成功后停留在结果页，由用户点击「开始归类」进入候选归类；队列已在后台更新。
 */
export function BatchCard({ batch, onUpdated, onStartClassification }: Props) {
  const meta = STATUS_META[batch.status];
  const NoteIcon = meta.note?.icon;
  const stocks = batch.stocks ?? [];
  const skipped = stocks.filter((s) => s.outcome === "skipped");
  const duplicates = stocks.filter((s) => s.outcome === "duplicate");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = useCallback(
    async (action: () => Promise<ImportBatch>) => {
      setBusy(true);
      setError(null);
      try {
        onUpdated(await action());
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "操作失败，请重试");
      } finally {
        setBusy(false);
      }
    },
    [onUpdated],
  );

  const canReidentify =
    (batch.status === "published" || batch.status === "all_unknown") &&
    batch.skippedCount > 0;
  const canRetry = batch.status === "rejected";

  return (
    <section
      aria-labelledby={`batch-title-${batch.batchId}`}
      data-testid={`batch-card-${batch.batchId}`}
      className="rounded-2xl border border-border bg-surface p-5 shadow-sm"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <h2 id={`batch-title-${batch.batchId}`} className="text-base font-semibold">
            本次导入结果
          </h2>
          <Badge tone={meta.tone}>{meta.label}</Badge>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-xs text-foreground/60">{batch.sourceName}</span>
          {batch.status === "published" ? (
            <Button size="sm" onClick={onStartClassification}>
              <Play className="h-4 w-4" aria-hidden="true" />
              开始归类
            </Button>
          ) : null}
        </div>
      </div>

      {meta.note && NoteIcon ? (
        <p
          className={`mb-4 flex items-center gap-2 text-sm ${meta.note.className}`}
          role={meta.note.role}
        >
          <NoteIcon className="h-4 w-4" aria-hidden="true" />
          {meta.note.text(batch)}
        </p>
      ) : null}

      {batch.status === "published" ? (
        <p className="mb-4 text-sm text-foreground/75" data-testid="batch-summary">
          新增 {batch.newCandidateCount} · 合并 {batch.mergedCandidateCount} · 重新归类{" "}
          {batch.reopenedCandidateCount} · 跳过 {batch.skippedCount}
        </p>
      ) : null}

      <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
        {stat("来源总数", batch.declaredTotal, "stat-declared")}
        {stat("解析条数", batch.parsedCount, "stat-parsed")}
        {stat("去重股票", batch.uniqueCount, "stat-unique")}
        {stat("识别股票", batch.recognizedCount, "stat-recognized")}
        {stat("跳过", batch.skippedCount, "stat-skipped")}
        {stat("新增候选项", batch.newCandidateCount, "stat-new")}
        {stat("合并候选项", batch.mergedCandidateCount, "stat-existing")}
        {stat("重新归类", batch.reopenedCandidateCount, "stat-reopened")}
        {stat("批次内重复", duplicates.length, "stat-duplicates")}
        {dateStat("导入日期", batch.importDate, "stat-date")}
      </dl>

      {stocks.length > 0 ? (
        <div className="mt-4 overflow-x-auto rounded-lg border border-border">
          <table className="min-w-full text-left text-sm" aria-label="导入明细">
            <thead className="bg-muted/60">
              <tr>
                {["#", "股票名称", "股票代码", "导入结果", "备注"].map((label) => (
                  <th key={label} scope="col" className="px-3 py-2 font-medium">
                    {label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {stocks.map((stock, index) => {
                const effect = stock.effect ? EFFECT_META[stock.effect] : null;
                const skippedRow = stock.outcome === "skipped";
                const duplicateRow = stock.outcome === "duplicate";
                return (
                  <tr key={`${stock.position}-${index}`} className="border-t border-border/70">
                    <td className="px-3 py-2 text-foreground/50">{index + 1}</td>
                    <td className="px-3 py-2">{stock.name ?? "—"}</td>
                    <td className="px-3 py-2 font-mono">{stock.rawCode}</td>
                    <td className="px-3 py-2">
                      {skippedRow ? (
                        <Badge tone="warning">未识别</Badge>
                      ) : duplicateRow ? (
                        <Badge tone="neutral">批次内重复</Badge>
                      ) : effect ? (
                        <Badge tone={effect.tone}>{effect.label}</Badge>
                      ) : (
                        <Badge tone="success">已导入</Badge>
                      )}
                    </td>
                    <td className="px-3 py-2 text-xs text-foreground/60">
                      {skippedRow
                        ? (stock.reason ?? "—")
                        : duplicateRow
                          ? "批次内重复"
                          : (effect?.note ?? "—")}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : null}

      {batch.reidentifiedAt ? (
        <p className="mt-3 text-xs text-foreground/60" role="status">
          已于 {batch.reidentifiedAt} 重新识别：补入 {batch.reidentifiedImported} 只，
          删除仍未知明细 {batch.reidentifiedRemoved} 条。
        </p>
      ) : null}

      {batch.selection ? (
        <SelectionPrompt
          selection={batch.selection}
          busy={busy}
          onChoose={(choice) =>
            void run(() => api.resolveSelection(batch.batchId, choice))
          }
        />
      ) : null}

      {batch.status === "awaiting_confirmation" ? (
        <ConditionPrompt
          batch={batch}
          busy={busy}
          onConfirm={() => void run(() => api.confirmLink(batch.batchId))}
        />
      ) : null}

      {skipped.length > 0 ? (
        <details className="mt-4 rounded-lg border border-border bg-background/50 p-3">
          <summary className="cursor-pointer text-sm font-medium">
            查看 {skipped.length} 只未识别股票
          </summary>
          <ul className="mt-2 space-y-1 text-sm">
            {skipped.map((s, index) => (
              <li
                key={`${s.position}-${index}`}
                className="flex items-center gap-2 text-foreground/75"
              >
                <MinusCircle className="h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
                <span className="font-mono">{s.rawCode}</span>
                <span className="text-foreground/50">{s.position}</span>
                <span className="text-foreground/50">· {s.reason}</span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}

      {canReidentify ? (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button
            variant="secondary"
            disabled={busy}
            data-testid="reidentify"
            onClick={() => void run(() => api.reidentify(batch.batchId))}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
            )}
            重新识别跳过项
          </Button>
          <span className="text-xs text-foreground/55">
            仅处理原批次跳过项，保留原导入日期；仍未知的明细会直接清除。
          </span>
        </div>
      ) : null}

      {canRetry ? (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button
            variant="secondary"
            disabled={busy}
            data-testid="retry"
            onClick={() => void run(() => api.retryBatch(batch.batchId))}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
            )}
            重试该批次
          </Button>
          <span className="text-xs text-foreground/55">
            保留原批次与导入日期；链接批次请先确认 Cookie 已更新。
          </span>
        </div>
      ) : null}

      {error ? (
        <p className="mt-3 flex items-center gap-2 text-sm text-danger" role="alert">
          <AlertTriangle className="h-4 w-4" aria-hidden="true" />
          {error}
        </p>
      ) : null}
    </section>
  );
}
