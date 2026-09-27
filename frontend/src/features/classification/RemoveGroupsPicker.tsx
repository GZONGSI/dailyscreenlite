import { useEffect, useRef, useState } from "react";
import { Loader2, Trash2 } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import type { Candidate, WrittenCandidate } from "../../api/classification";
import { api, ApiError } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "../../components/ui/Popover";
import { confirmMemberships, useGroupRelationRead, useSessionSelection } from "../observations/queries";

interface Props {
  candidate: Candidate;
  onRemoved: (candidate: WrittenCandidate) => void | Promise<void>;
  disabled?: boolean;
  onBusyChange: (busy: boolean) => void;
}

/**
 * 归类卡片的「移出观察组」：确认才执行，取消不改变状态。
 *
 * 打开时读取该股票当前所属的组并预选；移除用户选中的观察关系，
 * 同时把本次归类记为暂不关注，不附带额外操作说明。
 * 组列表与已有关系都来自共享查询：本次打开读成功才初始化勾选并开放提交。
 */
export function RemoveGroupsPicker({
  candidate,
  onRemoved,
  disabled,
  onBusyChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const flight = useRef(false);
  const queryClient = useQueryClient();
  const relation = useGroupRelationRead(candidate.securityId, open);
  const { initialized: loaded, markUsed } = useSessionSelection(relation.readSession);
  const activeGroups = relation.options;
  const message = error ?? relation.error;

  useEffect(() => {
    if (!open || relation.groupIds === null || loaded) return;
    const available = new Set(activeGroups.map((group) => group.groupId));
    setSelected(relation.groupIds.filter((id) => available.has(id)));
    markUsed();
  }, [open, relation.groupIds, loaded, markUsed, activeGroups]);

  const submit = async () => {
    if (flight.current || !loaded || !selected.length) return;
    flight.current = true;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    try {
      const result = await api.removeCandidateFromGroups(candidate.candidateId, selected);
      confirmMemberships(queryClient, candidate.securityId, result.groupIds);
      if (result.candidate) await onRemoved(result.candidate);
      setOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "移出失败，请重试");
    } finally {
      flight.current = false;
      setBusy(false);
      onBusyChange(false);
    }
  };

  const allSelected =
    activeGroups.length > 0 &&
    activeGroups.every((group) => selected.includes(group.groupId));

  return (
    <Popover
      modal
      open={open}
      onOpenChange={(value) => {
        if (flight.current) return;
        setOpen(value);
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="outline" disabled={disabled}>
          <Trash2 className="h-4 w-4" aria-hidden="true" />
          移出观察组
        </Button>
      </PopoverTrigger>
      <PopoverContent
        aria-label="选择要移出的观察组"
        onEscapeKeyDown={(event) => {
          if (flight.current) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          if (flight.current) event.preventDefault();
        }}
      >
        <h4 className="font-semibold">移出观察组</h4>
        <p className="mt-1 text-xs text-foreground/60">
          {candidate.security?.name} · {candidate.security?.code} · 已选{" "}
          {selected.length} 个
        </p>
        {!loaded && !message ? (
          <p role="status" className="py-4 text-sm text-foreground/70">
            正在读取已有观察关系…
          </p>
        ) : null}
        {loaded && !activeGroups.length ? (
          <p className="py-4 text-sm text-foreground/70">没有可移出的观察组。</p>
        ) : null}
        {loaded && activeGroups.length ? (
          <>
            <label className="mt-3 flex min-h-9 items-center gap-2 text-xs">
              <Checkbox
                checked={allSelected}
                disabled={busy}
                onCheckedChange={(checked) =>
                  setSelected(checked ? activeGroups.map((group) => group.groupId) : [])
                }
              />
              全选
            </label>
            <fieldset disabled={busy} className="mt-2 space-y-1">
              <legend className="sr-only">要移出的观察组</legend>
              {activeGroups.map((group) => (
                <label
                  key={group.groupId}
                  className="flex min-h-11 items-center gap-2 rounded-lg p-2 hover:bg-muted"
                >
                  <Checkbox
                    checked={selected.includes(group.groupId)}
                    onCheckedChange={(checked) =>
                      setSelected((values) =>
                        checked
                          ? [...values, group.groupId]
                          : values.filter((id) => id !== group.groupId),
                      )
                    }
                  />
                  <span className="break-all">
                    {group.name}
                    {group.isDefault ? "（默认）" : ""}
                  </span>
                </label>
              ))}
            </fieldset>
          </>
        ) : null}
        {message ? (
          <p role="alert" className="my-2 text-sm text-danger">
            {message}
            {!loaded ? (
              <Button variant="ghost" onClick={() => relation.retry()}>
                重试读取
              </Button>
            ) : null}
          </p>
        ) : null}
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
            取消
          </Button>
          <Button
            disabled={busy || !loaded || !selected.length}
            aria-busy={busy}
            onClick={() => void submit()}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : null}
            确认移出
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
