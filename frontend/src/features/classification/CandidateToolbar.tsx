import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import { useEffect, useState } from "react";
import { LayoutGrid, List, Search, Trash2, X } from "lucide-react";

import type {
  CandidateState,
  ClassificationState,
  ClassificationViewChanges,
} from "../../api/classification";
import { RESULT_OPTIONS, STATE_LABEL } from "./state";

interface Props {
  state: ClassificationState | null;
  onChange: (changes: ClassificationViewChanges) => void;
  onCleanup: () => Promise<void>;
  disabled?: boolean;
}

/** 队列内的查找、结果筛选与本轮进度；日期筛选与页签在队列头部。 */
export function CandidateToolbar({ state, onChange, onCleanup, disabled }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const [search, setSearch] = useState(state?.search ?? "");
  const serverSearch = state?.search ?? "";

  useEffect(() => {
    setSearch(serverSearch);
  }, [serverSearch]);

  const clean = async () => {
    setCleaning(true);
    setError(null);
    try {
      await onCleanup();
      setConfirming(false);
    } catch {
      setError("清理失败，请重试；原有候选项未在页面中移除。");
    } finally {
      setCleaning(false);
    }
  };

  return (
    <section
      aria-label="候选队列视图与筛选"
      className="flex flex-wrap items-center gap-2"
    >
      {error ? (
        <p role="alert" className="text-sm text-danger">
          {error}
        </p>
      ) : null}

      <div
        role="group"
        aria-label="队列显示方式"
        className="inline-flex rounded-lg border border-border p-0.5"
      >
        <Button
          size="sm"
          variant="ghost"
          type="button"
          aria-pressed={state?.viewMode === "list"}
          disabled={disabled}
          onClick={() => onChange({ viewMode: "list" })}
          className={`inline-flex min-h-[32px] items-center gap-1.5 rounded-md px-2.5 text-xs font-medium ${
            state?.viewMode === "list" ? "bg-selected text-primary" : "text-foreground/60"
          }`}
        >
          <List className="h-4 w-4" aria-hidden="true" />
          列表
        </Button>
        <Button
          size="sm"
          variant="ghost"
          type="button"
          aria-pressed={state?.viewMode === "card"}
          disabled={disabled}
          onClick={() => onChange({ viewMode: "card" })}
          className={`inline-flex min-h-[32px] items-center gap-1.5 rounded-md px-2.5 text-xs font-medium ${
            state?.viewMode === "card" ? "bg-selected text-primary" : "text-foreground/60"
          }`}
        >
          <LayoutGrid className="h-4 w-4" aria-hidden="true" />
          卡片
        </Button>
      </div>

      {state?.scope === "processed" ? (
        <label className="flex items-center gap-1.5 text-xs text-foreground/70">
          结果
          <select
            aria-label="处理结果筛选"
            disabled={disabled}
            value={state.result ?? ""}
            onChange={(event) =>
              onChange({
                result: (event.target.value || null) as CandidateState | null,
              })
            }
            className="min-h-9 rounded-lg border border-border bg-surface px-2 text-xs"
          >
            <option value="">全部结果</option>
            {RESULT_OPTIONS.map((result) => (
              <option key={result} value={result}>
                {STATE_LABEL[result]}
              </option>
            ))}
          </select>
        </label>
      ) : null}

      <div className="flex items-center gap-1 rounded-lg border border-border px-2">
        <Search className="h-4 w-4 text-foreground/50" aria-hidden="true" />
        <Input
          type="search"
          aria-label="在队列中按股票代码或名称查找"
          placeholder="代码 / 名称"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && search !== serverSearch) {
              onChange({ search });
            }
          }}
          className="w-36 border-0 bg-transparent py-1.5 text-xs outline-none"
        />
        {search ? (
          <Button
            size="sm"
            variant="ghost"
            type="button"
            aria-label="清除查找"
            onClick={() => {
              setSearch("");
              onChange({ search: "" });
            }}
            className="rounded p-0.5 text-foreground/50 hover:text-foreground"
          >
            <X className="h-3.5 w-3.5" aria-hidden="true" />
          </Button>
        ) : null}
      </div>

      {state && state.round.remaining > 0 ? (
        <span className="text-xs text-foreground/60">
          本轮已浏览 {state.round.viewed} · 已处理 {state.round.processed} · 剩余{" "}
          {state.round.remaining}
        </span>
      ) : null}

      {state && state.summary.unprocessed > 0 ? (
        confirming ? (
          <span className="ml-auto flex items-center gap-1.5 text-xs">
            <span className="text-foreground/70">
              清理 {state.summary.unprocessed} 只？导入事实保留
            </span>
            <Button
              size="sm"
              variant="ghost"
              type="button"
              onClick={() => void clean()}
              disabled={cleaning}
              className="rounded-md bg-danger px-2 py-1 font-medium text-white disabled:opacity-60"
            >
              {cleaning ? "清理中…" : "确认清理"}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-md border border-border px-2 py-1"
            >
              取消
            </Button>
          </span>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            type="button"
            onClick={() => setConfirming(true)}
            className="ml-auto inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs text-foreground/75 hover:bg-muted"
          >
            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
            清理待归类池
          </Button>
        )
      ) : null}
    </section>
  );
}
