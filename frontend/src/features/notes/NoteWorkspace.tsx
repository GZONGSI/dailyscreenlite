import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Loader2, NotebookPen, Pencil, Trash2, X } from "lucide-react";
import { api, ApiError } from "../../api/client";
import type { Note } from "../../api/notes";
import { Button } from "../../components/ui/Button";
import { Textarea } from "../../components/ui/Textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "../../components/ui/Dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "../../components/ui/Sheet";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { formatTimestamp } from "../../lib/format";
import {
  cancelNoteRead,
  confirmNoteDeleted,
  confirmNoteSaved,
  resumeNoteRead,
  useNotes,
} from "./queries";

/** 笔记归属的身份：一只股票（跨导入日期与入口共用同一份正文）。 */
export interface NoteOwner {
  securityId: string;
  name: string | null;
  code: string | null;
  noteCount: number;
}

interface Props {
  owner: NoteOwner;
  onNotesChanged: (securityId: string) => void;
  disabled?: boolean;
}
export function NoteWorkspace({ owner, onNotesChanged, disabled }: Props) {
  const queryClient = useQueryClient();
  const query = useNotes(owner.securityId);
  const notes = query.data ?? null;
  const [surface, setSurface] = useState<"new" | "all" | null>(null);
  const [editor, setEditor] = useState<{
    id: string | null;
    initial: string;
  } | null>(null);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [closing, setClosing] = useState<"surface" | "editor" | null>(null);
  const [focusRequest, setFocusRequest] = useState(0);
  const [deleting, setDeleting] = useState<Note | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const field = useRef<HTMLTextAreaElement>(null);
  const sheetAdd = useRef<HTMLButtonElement>(null);
  const flight = useRef(false);
  const composing = useRef(false);
  const dirty = Boolean(editor && draft !== editor.initial);
  useEffect(() => {
    if (focusRequest === 0 || busy) return;
    const node = sheetAdd.current;
    if (node && !node.disabled) node.focus();
  }, [focusRequest, busy]);

  const notify = () => {
    void Promise.resolve(onNotesChanged(owner.securityId)).catch(() => {});
  };
  /** 手动重试读取；失败仍由列表自己显示，不向调用方抛出。 */
  const retry = () => {
    void query.refetch().catch(() => {});
  };
  /**
   * 把焦点交回抽屉的「新增笔记」。
   *
   * 保存刚结束的那一帧该按钮仍是 disabled（busy 未清），对 disabled 元素
   * .focus() 是空操作，焦点会丢；因此登记一次请求，等它真正可用时再聚焦。
   */
  const focusSheetAdd = () => setFocusRequest((value) => value + 1);
  const start = (note: Note | null) => {
    setEditor({ id: note?.noteId ?? null, initial: note?.body ?? "" });
    setDraft(note?.body ?? "");
    setError(null);
  };
  const close = (target: "surface" | "editor") => {
    if (flight.current || deleting || closing) return;
    if (dirty) {
      setClosing(target);
      return;
    }
    setEditor(null);
    setDraft("");
    setError(null);
    if (target === "surface") setSurface(null);
    else focusSheetAdd();
  };
  const save = async () => {
    if (flight.current || !editor) return;
    if (!draft.trim()) {
      setError("笔记内容不能为空");
      return;
    }
    flight.current = true;
    setBusy(true);
    setError(null);
    try {
      // 写前取消旧读取：迟到的 GET 不能把保存结果覆盖回写入之前的列表
      await cancelNoteRead(queryClient, owner.securityId);
      const note = editor.id
        ? await api.updateNote(editor.id, draft)
        : await api.createNote(owner.securityId, draft);
      // 保存已经成功：确认结果由笔记资源回写缓存（没有完整列表时它会重新读取）
      confirmNoteSaved(queryClient, owner.securityId, note);
      setEditor(null);
      setDraft("");
      if (surface === "new") setSurface(null);
      else focusSheetAdd();
      notify();
    } catch (err) {
      // 写前取消的那次读取要补回来：否则冷缓存会停在「没有数据也没有读取错误」的
      // 「正在读取笔记…」，连重试入口都没有（删除入口只能从已读到的列表进入，不需要补）
      resumeNoteRead(queryClient, owner.securityId);
      setError(err instanceof ApiError ? err.message : "保存失败，请重试");
    } finally {
      flight.current = false;
      setBusy(false);
    }
  };
  const remove = async () => {
    if (flight.current || !deleting) return;
    flight.current = true;
    setBusy(true);
    setError(null);
    try {
      // 删除前同样取消在途读取：被删的笔记不能被迟到的旧结果读回列表
      await cancelNoteRead(queryClient, owner.securityId);
      await api.deleteNote(deleting.noteId);
      // 删除已经成功：确认结果由笔记资源回写缓存
      confirmNoteDeleted(queryClient, owner.securityId, deleting.noteId);
      setDeleting(null);
      notify();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "删除失败，请重试");
    } finally {
      flight.current = false;
      setBusy(false);
    }
  };
  const form = editor && (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        void save();
      }}
    >
      <label htmlFor="note-editor" className="text-sm">
        {editor.id ? "编辑笔记" : "新增笔记"}
      </label>
      <Textarea
        ref={field}
        id="note-editor"
        autoFocus
        rows={7}
        disabled={busy}
        value={draft}
        aria-invalid={Boolean(error)}
        aria-describedby={error ? "note-error" : "note-help"}
        onChange={(e) => setDraft(e.target.value)}
        onCompositionStart={() => {
          composing.current = true;
        }}
        onCompositionEnd={() => {
          composing.current = false;
        }}
        onKeyDown={(e) => {
          if (
            e.key === "Enter" &&
            (e.ctrlKey || e.metaKey) &&
            !e.nativeEvent.isComposing &&
            !composing.current &&
            e.keyCode !== 229
          ) {
            e.preventDefault();
            void save();
          }
        }}
        className="w-full resize-y"
      />
      <p id="note-help" className="text-xs text-foreground/60">
        Enter 换行 · Ctrl/Cmd+Enter 保存
      </p>
      {error && !deleting && (
        <p id="note-error" role="alert" className="text-sm text-danger">
          {error}
        </p>
      )}
      <div className="flex justify-end gap-2">
        <Button
          variant="ghost"
          disabled={busy}
          type="button"
          onClick={() => close(surface === "new" ? "surface" : "editor")}
        >
          取消编辑
        </Button>
        <Button type="submit" disabled={busy || !draft.trim()} aria-busy={busy}>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : (
            <NotebookPen className="h-4 w-4" aria-hidden="true" />
          )}
          {editor.id ? "保存修改" : "保存笔记"}
        </Button>
      </div>
    </form>
  );
  const confirms = (
    <>
      <ConfirmDialog
        open={closing !== null}
        title="放弃未保存的修改？"
        description="已保存的笔记会保留，本次未保存的内容将丢弃。"
        confirmLabel="放弃草稿"
        cancelLabel="继续编辑"
        onCancel={() => setClosing(null)}
        onReturnFocus={() => {
          requestAnimationFrame(() => field.current?.focus());
        }}
        onConfirm={() => {
          const target = closing;
          setClosing(null);
          setEditor(null);
          setDraft("");
          setError(null);
          if (target === "surface") setSurface(null);
          else focusSheetAdd();
        }}
      />
      <ConfirmDialog
        open={deleting !== null}
        title="删除这条笔记？"
        description="删除后无法恢复，归类状态和观察关系不受影响。"
        confirmLabel="确认删除这条笔记"
        busy={busy}
        error={error}
        onCancel={() => {
          setDeleting(null);
          setError(null);
        }}
        onConfirm={() => void remove()}
        onReturnFocus={focusSheetAdd}
      />
    </>
  );
  const identity = `${owner.name ?? "未知名称"} ${owner.code ?? owner.securityId}`;
  return (
    <>
      <section
        data-testid="note-summary"
        className="note-summary flex flex-wrap items-center gap-3 border-t border-border py-3"
      >
        <div className="min-w-0 flex-1">
          <div className="flex gap-2 text-xs text-foreground/60">
            <span>最近笔记</span>
            <span>{notes?.length ?? owner.noteCount} 条</span>
            {notes?.[0] && <time>{formatTimestamp(notes[0].updatedAt)}</time>}
          </div>
          {query.isError ? (
            <p role="alert" className="text-sm text-danger">
              笔记读取失败
              <Button size="sm" variant="ghost" onClick={retry}>
                重试笔记
              </Button>
            </p>
          ) : notes === null ? (
            <p aria-busy="true" className="h-10 animate-pulse bg-muted rounded">
              正在读取笔记…
            </p>
          ) : notes[0] ? (
            <button
              disabled={disabled}
              className="mt-1 line-clamp-2 w-full break-all text-left text-sm"
              onClick={(e) => {
                trigger.current = e.currentTarget;
                setSurface("all");
              }}
            >
              {notes[0].body}
            </button>
          ) : (
            <p className="mt-1 text-sm text-foreground/60">
              暂无笔记，记录你的跟踪想法
            </p>
          )}
        </div>
        <Button
          variant="outline"
          disabled={disabled}
          onClick={(e) => {
            trigger.current = e.currentTarget;
            start(null);
            setSurface("new");
          }}
        >
          <NotebookPen className="h-4 w-4" />
          新增笔记
        </Button>
        <Button
          variant="ghost"
          disabled={disabled}
          onClick={(e) => {
            trigger.current = e.currentTarget;
            setSurface("all");
          }}
        >
          查看全部笔记
        </Button>
      </section>
      <Dialog
        open={surface === "new"}
        onOpenChange={(open) => {
          if (!open) close("surface");
        }}
      >
        <DialogContent
          onPointerDownOutside={(e) => e.preventDefault()}
          onInteractOutside={(e) => e.preventDefault()}
          onEscapeKeyDown={(e) => {
            e.preventDefault();
            close("surface");
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            trigger.current?.focus();
          }}
        >
          <DialogTitle className="pr-10 text-base font-semibold">
            新增笔记 · {identity}
          </DialogTitle>
          <DialogDescription className="mb-4 mt-1 text-xs text-foreground/60">
            笔记跨导入日期共用，保存不改变归类状态。
          </DialogDescription>
          <Button
            variant="ghost"
            size="icon"
            aria-label="关闭笔记"
            disabled={busy}
            className="absolute right-3 top-3"
            onClick={() => close("surface")}
          >
            <X className="h-4 w-4" />
          </Button>
          {form}
          {surface === "new" && confirms}
        </DialogContent>
      </Dialog>
      <Sheet
        open={surface === "all"}
        onOpenChange={(open) => {
          if (!open) close("surface");
        }}
      >
        <SheetContent
          className="max-w-[480px]"
          onInteractOutside={(e) => {
            if (editor || busy) e.preventDefault();
          }}
          onEscapeKeyDown={(e) => {
            e.preventDefault();
            close(editor ? "editor" : "surface");
          }}
          onCloseAutoFocus={(e) => {
            e.preventDefault();
            trigger.current?.focus();
          }}
        >
          <header className="shrink-0 border-b border-border p-4 pr-16 sm:p-6 sm:pr-16">
            <SheetTitle className="text-base font-semibold">
              全部笔记 · {identity}
            </SheetTitle>
            <SheetDescription className="mt-1 text-xs text-foreground/60">
              笔记 {notes?.length ?? owner.noteCount} · 同股跨导入日期共用
            </SheetDescription>
          </header>
          <div
            className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-6"
            data-testid="note-all-panel"
          >
            <Button
              ref={sheetAdd}
              disabled={Boolean(editor) || busy}
              variant="outline"
              className="mb-4"
              onClick={() => start(null)}
            >
              新增笔记
            </Button>
            {form}
            {query.isError && (
              <p role="alert">
                笔记读取失败
                <Button onClick={retry}>重试笔记</Button>
              </p>
            )}
            {notes === null && !query.isError && <p role="status">正在读取笔记…</p>}
            {notes?.length === 0 && (
              <p className="py-8 text-sm text-foreground/60">暂无笔记</p>
            )}
            <ul aria-label="个股笔记列表" className="space-y-4">
              {notes?.map((note) => (
                <li
                  key={note.noteId}
                  data-testid={`note-${note.noteId}`}
                  className="border-b border-border py-4"
                >
                  <p className="whitespace-pre-wrap break-all text-sm leading-relaxed">
                    {note.body}
                  </p>
                  <p className="mt-2 text-xs text-foreground/60">
                    保存于 {formatTimestamp(note.updatedAt)}
                  </p>
                  <div className="mt-2 flex gap-2">
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label="编辑这条笔记"
                      disabled={Boolean(editor) || busy}
                      onClick={() => start(note)}
                    >
                      <Pencil className="h-4 w-4" />
                      编辑
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label="删除这条笔记"
                      disabled={Boolean(editor) || busy}
                      onClick={() => {
                        setError(null);
                        setDeleting(note);
                      }}
                    >
                      <Trash2 className="h-4 w-4" />
                      删除
                    </Button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
          {surface === "all" && confirms}
        </SheetContent>
      </Sheet>
    </>
  );
}
