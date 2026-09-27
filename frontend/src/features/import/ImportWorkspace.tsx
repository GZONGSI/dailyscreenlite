import { useCallback, useEffect, useState } from "react";

import { api, ApiError, type ImportBatch } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { BatchCard } from "./BatchCard";
import { BatchList } from "./BatchList";
import { ImportPanel, useImportController, type ImportMode } from "./ImportPanel";

interface Props {
  /** 外层要求重读导入记录时递增（进入导入模块、导入落地后）。 */
  refreshToken?: number;
  /** 用户点击「开始归类」：切到候选归类模块。 */
  onStartClassification: () => void;
  /** 导入落地后通知外层刷新候选队列与导航计数。 */
  onImported?: () => void;
}

/** 待处理批次优先展示，便于用户先处理歧义与条件确认。 */
function sortBatches(batches: ImportBatch[]): ImportBatch[] {
  const weight = (batch: ImportBatch) =>
    batch.status === "awaiting_selection" ||
    batch.status === "awaiting_confirmation"
      ? 0
      : 1;
  return [...batches].sort(
    (a, b) => weight(a) - weight(b) || b.receivedAt.localeCompare(a.receivedAt),
  );
}

/**
 * 导入工作区：左侧提交来源与最近导入记录，右侧展示本次导入结果。
 *
 * 导入成功后停留在结果页，由用户点击「开始归类」进入候选归类；队列在后台更新，
 * 不强制切换用户当前页面。
 */
export function ImportWorkspace({
  refreshToken = 0,
  onStartClassification,
  onImported,
}: Props) {
  const [batches, setBatches] = useState<ImportBatch[]>([]);
  const [selected, setSelected] = useState<ImportBatch | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const detail = useCallback(async (batchId: string) => {
    try {
      setSelected(await api.getBatch(batchId));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "批次详情读取失败");
    }
  }, []);

  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const { batches: loaded } = await api.listBatches();
        if (!active) return;
        const sorted = sortBatches(loaded);
        setBatches(sorted);
        if (sorted.length) {
          await detail(sorted[0].batchId);
        }
      } catch (err) {
        if (active) {
          setError(err instanceof ApiError ? err.message : "无法连接本机服务");
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [detail, refreshToken]);

  const onImportedBatches = useCallback(
    async (submitted: ImportBatch[]) => {
      setBatches((current) => sortBatches([...submitted, ...current]));
      const first = submitted[0];
      if (first) await detail(first.batchId);
      onImported?.();
    },
    [detail, onImported],
  );

  const onBatchUpdated = useCallback(
    async (updated: ImportBatch) => {
      setBatches((current) =>
        sortBatches(
          current.map((batch) =>
            batch.batchId === updated.batchId ? updated : batch,
          ),
        ),
      );
      await detail(updated.batchId);
      onImported?.();
    },
    [detail, onImported],
  );

  const controller = useImportController({ onImported: onImportedBatches });
  const [mode, setMode] = useState<ImportMode>("link");

  const pendingCount = batches.filter((batch) =>
    ["awaiting_selection", "awaiting_confirmation"].includes(batch.status),
  ).length;

  return (
    <div className="import-workspace">
      {error ? (
        <p role="alert" className="px-1 py-2 text-sm text-danger">
          {error}
        </p>
      ) : null}
      <div className="import-columns">
        <div className="space-y-4">
          <ImportPanel controller={controller} mode={mode} onModeChange={setMode} />
          {loading ? (
            <p className="px-1 text-sm text-foreground/60" aria-busy="true">
              正在读取导入记录…
            </p>
          ) : (
            <BatchList
              batches={batches}
              selectedId={selected?.batchId ?? null}
              expanded={expanded}
              onSelect={(batchId) => void detail(batchId)}
              onToggleExpanded={() => setExpanded((value) => !value)}
            />
          )}
        </div>
        <div className="space-y-4">
          {pendingCount > 0 ? (
            <p role="status" className="text-sm text-foreground/70">
              有 {pendingCount} 个来源待处理（待选择或待确认条件），处理后才发布。
            </p>
          ) : null}
          {selected ? (
            <BatchCard
              key={selected.batchId}
              batch={selected}
              onUpdated={onBatchUpdated}
              onStartClassification={onStartClassification}
            />
          ) : (
            <section className="rounded-2xl border border-dashed border-border bg-surface/60 p-10 text-center">
              <p className="text-sm text-foreground/60">
                还没有导入结果：提交文件、文本或问财链接后在这里查看结果。
              </p>
              <Button
                className="mt-4"
                variant="secondary"
                onClick={onStartClassification}
              >
                去候选归类
              </Button>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
