import { useRef, useState } from "react";
import { Check, Loader2, Pencil, Plus, Star, Trash2, X } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../api/client";
import type { ObservationGroup } from "../../api/observations";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { ConfirmDialog } from "../../components/ui/ConfirmDialog";
import { Input } from "../../components/ui/Input";
import { addGroupToGroups, expireGroups, expireMemberships } from "./queries";

interface Props {
  groups: ObservationGroup[];
  /** 当前视图：null 为「全部观察股票」汇总视图。 */
  selectedGroupId: string | null;
  /** 汇总视图的股票数量。 */
  totalCount: number;
  disabled?: boolean;
  onSelect: (groupId: string | null) => void;
  /** 组的创建、重命名、删除成功后请工作台重读。 */
  onChanged: () => void | Promise<void>;
}

/**
 * 观察组列表：汇总视图 + 单组选择，支持创建、重命名与删除。
 *
 * 没有归档：删除只移除该组关系，股票在其他组的关系、笔记、来源与处理历史
 * 都不受影响。默认组也可以删除，删到没有组时列表为空、可在这里重建。
 */
export function GroupList({
  groups,
  selectedGroupId,
  totalCount,
  disabled,
  onSelect,
  onChanged,
}: Props) {
  const [newName, setNewName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");
  const [deleting, setDeleting] = useState<ObservationGroup | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const errorRef = useRef<HTMLParagraphElement>(null);
  const queryClient = useQueryClient();

  const fail = (err: unknown, fallback: string) => {
    setError(err instanceof ApiError ? err.message : fallback);
    errorRef.current?.focus();
  };

  const create = async () => {
    const name = newName.trim();
    if (!name || busy) return;
    setBusy(true);
    setError(null);
    try {
      const group = await api.createObservationGroup(name);
      // 组的真实响应写入共享资源：三个关系入口立刻可选到它
      addGroupToGroups(queryClient, group);
      setNewName("");
      await onChanged();
    } catch (err) {
      fail(err, "新建观察组失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const rename = async (group: ObservationGroup) => {
    const name = draftName.trim();
    if (!name || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.renameObservationGroup(group.groupId, name);
      expireGroups(queryClient);
      setEditingId(null);
      await onChanged();
    } catch (err) {
      fail(err, "重命名失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (group: ObservationGroup) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteObservationGroup(group.groupId);
      // 只删该组关系：组列表过期，成员关系按前缀标记过期（受影响证券全集不可得）
      expireGroups(queryClient);
      expireMemberships(queryClient);
      setDeleting(null);
      await onChanged();
    } catch (err) {
      fail(err, "删除观察组失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <section
      aria-labelledby="observation-groups-title"
      data-testid="observation-groups"
      className="observation-groups"
    >
      <div className="flex items-center justify-between gap-2">
        <h2 id="observation-groups-title" className="text-base font-semibold">
          观察组
        </h2>
        <Badge tone="info">{groups.length} 个</Badge>
      </div>

      {error ? (
        <p
          ref={errorRef}
          tabIndex={-1}
          role="alert"
          className="mt-2 rounded-lg border border-danger/30 bg-danger/10 p-2 text-xs text-danger"
        >
          {error}
        </p>
      ) : null}

      <ul className="mt-3 space-y-1" aria-label="观察组列表">
        <li>
          <button
            type="button"
            data-testid="observation-group-all"
            aria-current={selectedGroupId === null}
            disabled={disabled}
            onClick={() => onSelect(null)}
            className="observation-group-row"
          >
            <span className="flex min-w-0 items-center gap-2">
              <Star className="h-4 w-4 shrink-0 text-foreground/50" aria-hidden="true" />
              <span className="truncate text-sm font-medium">全部观察股票</span>
            </span>
            <span className="shrink-0 text-xs text-foreground/50">{totalCount} 只</span>
          </button>
        </li>
        {groups.map((group) => (
          <li key={group.groupId}>
            {editingId === group.groupId ? (
              <form
                className="flex flex-wrap items-center gap-2 rounded-lg border border-border/70 px-3 py-2"
                onSubmit={(event) => {
                  event.preventDefault();
                  void rename(group);
                }}
              >
                <label className="sr-only" htmlFor={`rename-${group.groupId}`}>
                  重命名观察组
                </label>
                <Input
                  id={`rename-${group.groupId}`}
                  autoFocus
                  value={draftName}
                  onChange={(event) => setDraftName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") setEditingId(null);
                  }}
                  className="min-w-0 flex-1 rounded-md border border-border bg-background px-2 py-1.5 text-sm"
                />
                <Button
                  size="sm"
                  variant="ghost"
                  type="submit"
                  disabled={busy}
                  aria-label={`保存组名 ${group.name}`}
                  className="rounded-md bg-primary p-1.5 text-white disabled:opacity-50"
                >
                  <Check className="h-4 w-4" aria-hidden="true" />
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  type="button"
                  onClick={() => setEditingId(null)}
                  aria-label="取消重命名"
                  className="rounded-md border border-border p-1.5 text-foreground/70"
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                </Button>
              </form>
            ) : (
              <div
                data-testid={`observation-group-${group.groupId}`}
                className={`observation-group-row ${
                  selectedGroupId === group.groupId ? "is-selected" : ""
                }`}
              >
                <button
                  type="button"
                  disabled={disabled}
                  aria-current={selectedGroupId === group.groupId}
                  onClick={() => onSelect(group.groupId)}
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                >
                  <span className="truncate text-sm font-medium">{group.name}</span>
                  <span className="shrink-0 text-xs text-foreground/50">
                    {group.memberCount} 只
                  </span>
                </button>
                <span className="flex shrink-0 items-center gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    type="button"
                    aria-label={`重命名 ${group.name}`}
                    onClick={() => {
                      setEditingId(group.groupId);
                      setDraftName(group.name);
                      setError(null);
                    }}
                    className="rounded p-1.5 text-foreground/50 hover:bg-muted hover:text-foreground"
                  >
                    <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    type="button"
                    aria-label={`删除 ${group.name}`}
                    onClick={() => {
                      setDeleting(group);
                      setError(null);
                    }}
                    className="rounded p-1.5 text-foreground/50 hover:bg-muted hover:text-foreground"
                  >
                    <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                  </Button>
                </span>
              </div>
            )}
          </li>
        ))}
      </ul>

      {!groups.length ? (
        <p className="mt-3 text-xs text-foreground/50">
          还没有观察组。在归类时加入观察，或先在这里新建一个组。
        </p>
      ) : null}

      <form
        className="mt-3 flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void create();
        }}
      >
        <label className="sr-only" htmlFor="new-observation-group">
          新观察组名称
        </label>
        <Input
          id="new-observation-group"
          value={newName}
          disabled={busy}
          placeholder="新观察组名称"
          onChange={(event) => setNewName(event.target.value)}
          className="min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 py-2 text-sm"
        />
        <Button
          size="sm"
          variant="ghost"
          type="submit"
          disabled={busy || !newName.trim()}
          className="inline-flex min-h-[36px] items-center gap-1 rounded-lg border border-border px-3 text-xs font-medium text-foreground/80 hover:bg-muted disabled:opacity-50"
        >
          {busy ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
          ) : (
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
          )}
          新建
        </Button>
      </form>

      <ConfirmDialog
        open={deleting !== null}
        title={`删除观察组「${deleting?.name ?? ""}」？`}
        description="只删除这个组的关系；股票在其他组的归属、笔记、来源与处理历史都保留。"
        confirmLabel="删除这个组"
        busy={busy}
        error={error}
        onCancel={() => {
          setDeleting(null);
          setError(null);
        }}
        onConfirm={() => deleting && void remove(deleting)}
      />
    </section>
  );
}
