import { Textarea } from "../../components/ui/Textarea";
import { Input } from "../../components/ui/Input";
import { useCallback, useRef, useState } from "react";
import {
  FileText,
  Link2,
  Loader2,
  Paperclip,
  Plus,
  Send,
  X,
} from "lucide-react";

import { api, ApiError, type ImportBatch } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { formatBytes } from "../../lib/format";

export type ImportMode = "link" | "file" | "text";

const ACCEPT = ".csv,.tsv,.txt,.xlsx,.xlsm";
const MAX_BYTES = 20 * 1024 * 1024;
const MAX_FILES = 10;
const MAX_BLOCKS = 10;

interface Props {
  onImported: (batches: ImportBatch[]) => void;
}

function isWencaiLink(text: string): boolean {
  const trimmed = text.trim();
  return (
    !trimmed.includes("\n") &&
    /^https?:\/\//i.test(trimmed) &&
    trimmed.toLowerCase().includes("iwencai.com")
  );
}

/**
 * 把一个文本块拆成独立来源：每个问财链接各自成批次，其余连续文本合并为一个文本块。
 * 纯代码或表格文本不含链接时保持单块，避免把一份名单拆成多批。
 */
function splitBlock(text: string): string[] {
  const lines = text.split("\n");
  const out: string[] = [];
  let buffer: string[] = [];
  const flush = () => {
    const joined = buffer.join("\n").trim();
    if (joined) {
      out.push(joined);
    }
    buffer = [];
  };
  for (const line of lines) {
    if (isWencaiLink(line)) {
      flush();
      out.push(line.trim());
    } else {
      buffer.push(line);
    }
  }
  flush();
  return out;
}

export function useImportController({ onImported }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const inFlight = useRef(false);
  const composingRef = useRef(false);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [blocks, setBlocks] = useState<string[]>([]);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const addFiles = useCallback((incoming: FileList | null) => {
    if (!incoming) {
      return;
    }
    setError(null);
    setNotice(null);
    const next: File[] = [];
    const problems: string[] = [];
    for (const file of Array.from(incoming)) {
      if (file.size > MAX_BYTES) {
        problems.push(
          `${file.name}（${formatBytes(file.size)}）超过 20MB 上限`,
        );
        continue;
      }
      next.push(file);
    }
    if (problems.length) {
      setError(problems.join("；"));
    }
    setFiles((current) => {
      const merged = [...current, ...next];
      if (merged.length > MAX_FILES) {
        setError(`一次最多提交 ${MAX_FILES} 个文件`);
        return merged.slice(0, MAX_FILES);
      }
      return merged;
    });
  }, []);

  const stageText = useCallback(() => {
    const trimmed = text.trim();
    if (!trimmed) {
      return;
    }
    setError(null);
    setBlocks((current) => {
      if (current.length >= MAX_BLOCKS) {
        setError(`一次最多提交 ${MAX_BLOCKS} 个文本块`);
        return current;
      }
      return [...current, trimmed];
    });
    setText("");
  }, [text]);

  const submit = useCallback(async () => {
    if (inFlight.current) return;
    setError(null);
    setNotice(null);
    const pendingTexts = [
      ...blocks,
      ...(text.trim() ? [text.trim()] : []),
    ].flatMap(splitBlock);
    if (files.length === 0 && pendingTexts.length === 0) {
      setError("请选择文件，或键入 / 粘贴股票代码、表格文本与问财链接。");
      return;
    }
    inFlight.current = true;
    setBusy(true);
    try {
      const { batches } = await api.submitSources(files, pendingTexts);
      const summary = batches
        .map(
          (b) =>
            `${b.sourceName}：${b.status === "published" ? "已发布" : "见结果"}`,
        )
        .join("；");
      setNotice(`已提交 ${batches.length} 个来源（${summary}）`);
      setFiles([]);
      setBlocks([]);
      setText("");
      onImported(batches);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "提交失败，请稍后重试");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }, [blocks, files, text, onImported]);

  return {
    inputRef,
    composingRef,
    dragging,
    setDragging,
    busy,
    files,
    setFiles,
    blocks,
    setBlocks,
    text,
    setText,
    error,
    notice,
    addFiles,
    stageText,
    submit,
  };
}
const PLACEHOLDER: Record<ImportMode, string> = {
  link: "粘贴问财结果链接。多个链接可各占一行，将分别成批次获取完整结果。",
  file: "把 CSV / XLSX / TXT 文件拖到这里，或点击「添加文件」选择；一次最多 10 个。",
  text: "每行一个代码，或粘贴含代码列的表格文本；Ctrl 或 ⌘ + Enter 提交。",
};

export function ImportPanel({
  controller,
  mode,
  onModeChange,
}: {
  controller: ReturnType<typeof useImportController>;
  mode: ImportMode;
  onModeChange: (mode: ImportMode) => void;
}) {
  const {
    inputRef,
    composingRef,
    dragging,
    setDragging,
    busy,
    files,
    setFiles,
    blocks,
    setBlocks,
    text,
    setText,
    error,
    notice,
    addFiles,
    stageText,
    submit,
  } = controller;
  const linkDetected = isWencaiLink(text);
  const pendingCount = files.length + blocks.length;

  return (
    <section
      aria-labelledby="import-title"
      className="rounded-2xl border border-border bg-surface p-5 shadow-sm"
    >
      <div className="mb-3 flex items-center gap-2">
        <Send className="h-5 w-5 text-primary" aria-hidden="true" />
        <h2 id="import-title" className="text-base font-semibold">
          选择导入方式
        </h2>
      </div>

      <div
        role="tablist"
        aria-label="导入方式"
        className="mb-3 flex items-center gap-1 rounded-lg bg-muted/60 p-1"
      >
        {(
          [
            ["link", "链接", Link2],
            ["file", "文件", Paperclip],
            ["text", "文本", FileText],
          ] as const
        ).map(([value, label, Icon]) => (
          <button
            key={value}
            type="button"
            role="tab"
            aria-selected={mode === value}
            disabled={busy}
            onClick={() => onModeChange(value)}
            className={`inline-flex min-h-9 flex-1 items-center justify-center gap-1.5 rounded-md px-3 text-sm font-medium transition-colors duration-150 ${
              mode === value
                ? "bg-surface text-foreground shadow-sm"
                : "text-foreground/65 hover:text-foreground"
            }`}
          >
            <Icon className="h-4 w-4" aria-hidden="true" />
            {label}
          </button>
        ))}
      </div>

      <label htmlFor="import-text" className="mb-1 block text-sm font-medium">
        股票代码、表格文本或问财链接
      </label>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!busy) {
            addFiles(event.dataTransfer.files);
          }
        }}
        className={`rounded-xl border transition-colors duration-200 ${
          dragging
            ? "border-primary bg-selected"
            : "border-border bg-background/40"
        }`}
      >
        <Textarea
          id="import-text"
          value={text}
          disabled={busy}
          rows={5}
          aria-describedby="import-help"
          aria-busy={busy}
          placeholder={PLACEHOLDER[mode]}
          onChange={(event) => setText(event.target.value)}
          onCompositionStart={() => {
            composingRef.current = true;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
          }}
          onKeyDown={(event) => {
            // 输入法组合期间与普通 Enter 都不提交；仅 Ctrl/Cmd+Enter 提交
            if (
              event.key === "Enter" &&
              (event.ctrlKey || event.metaKey) &&
              !composingRef.current &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              void submit();
            }
          }}
          className="w-full resize-y rounded-xl bg-transparent px-3 py-3 text-sm outline-none placeholder:text-foreground/45 focus-visible:ring-2 focus-visible:ring-primary/40"
        />
        <div className="flex items-center justify-between gap-3 border-t border-border/70 px-3 py-2">
          <p
            id="import-help"
            className="flex items-center gap-2 text-xs text-foreground/60"
          >
            {linkDetected ? (
              <>
                <Link2 className="h-3.5 w-3.5" aria-hidden="true" />
                识别为问财链接，将按链接获取完整结果
              </>
            ) : (
              <>
                <FileText className="h-3.5 w-3.5" aria-hidden="true" />
                支持 CSV / TSV / TXT / XLSX；Ctrl 或 ⌘ + Enter 提交
              </>
            )}
          </p>
          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={stageText}
              disabled={busy || !text.trim()}
              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-border px-3 text-xs font-medium text-foreground/80 transition-colors duration-200 hover:bg-muted disabled:opacity-50"
            >
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
              添加文本块
            </button>
            <button
              type="button"
              role="button"
              onClick={() => inputRef.current?.click()}
              disabled={busy}
              className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-border px-3 text-xs font-medium text-foreground/80 transition-colors duration-200 hover:bg-muted disabled:opacity-50"
            >
              <Paperclip className="h-3.5 w-3.5" aria-hidden="true" />
              添加文件
            </button>
          </div>
        </div>
      </div>

      {pendingCount > 0 ? (
        <ul className="mt-3 space-y-1.5" aria-label="待提交来源">
          {files.map((file, index) => (
            <li
              key={`${file.name}-${index}`}
              className="flex items-center justify-between gap-2 rounded-lg border border-border bg-background/50 px-3 py-1.5 text-sm"
            >
              <span className="flex min-w-0 items-center gap-2">
                <FileText
                  className="h-4 w-4 shrink-0 text-foreground/50"
                  aria-hidden="true"
                />
                <span className="truncate">{file.name}</span>
                <span className="shrink-0 text-xs text-foreground/50">
                  {formatBytes(file.size)}
                </span>
              </span>
              <button
                type="button"
                aria-label={`移除 ${file.name}`}
                disabled={busy}
                onClick={() =>
                  setFiles((current) => current.filter((_, i) => i !== index))
                }
                className="rounded p-1 text-foreground/50 transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </li>
          ))}
          {blocks.map((block, index) => (
            <li
              key={`block-${index}`}
              className="flex items-center justify-between gap-2 rounded-lg border border-border bg-background/50 px-3 py-1.5 text-sm"
            >
              <span className="flex min-w-0 items-center gap-2">
                <Link2
                  className="h-4 w-4 shrink-0 text-foreground/50"
                  aria-hidden="true"
                />
                <span className="truncate">
                  {block.replace(/\s+/g, " ").slice(0, 60)}
                </span>
              </span>
              <button
                type="button"
                aria-label={`移除文本块 ${index + 1}`}
                disabled={busy}
                onClick={() =>
                  setBlocks((current) => current.filter((_, i) => i !== index))
                }
                className="rounded p-1 text-foreground/50 transition-colors hover:bg-muted hover:text-foreground disabled:opacity-50"
              >
                <X className="h-4 w-4" aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}

      <Input
        ref={inputRef}
        type="file"
        accept={ACCEPT}
        multiple
        className="sr-only"
        onChange={(event) => {
          addFiles(event.target.files);
          event.target.value = "";
        }}
      />

      <div className="mt-3 flex min-h-[44px] items-center justify-between gap-3">
        <div aria-live="polite" className="text-sm">
          {busy ? (
            <span
              className="flex items-center gap-2 text-foreground/70"
              aria-busy="true"
            >
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
              正在处理来源…
            </span>
          ) : error ? (
            <span className="text-danger" role="alert">
              {error}
            </span>
          ) : notice ? (
            <span className="text-foreground/70">{notice}</span>
          ) : null}
        </div>
        <Button className="w-28" disabled={busy} onClick={() => void submit()}>
          {busy && (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          )}
          提交
        </Button>
      </div>
    </section>
  );
}
