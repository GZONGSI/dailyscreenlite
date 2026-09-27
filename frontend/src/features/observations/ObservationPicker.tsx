import { useEffect, useRef, useState } from "react";
import { Check, Loader2, Plus } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import type { Candidate, WrittenCandidate } from "../../api/classification";
import { api, ApiError } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Checkbox } from "../../components/ui/Checkbox";
import { Input } from "../../components/ui/Input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "../../components/ui/Popover";
import { addGroupToGroups, confirmMemberships, useGroupRelationRead, useSessionSelection } from "./queries";

interface Props {
  candidate: Candidate;
  onObserved: (candidate: WrittenCandidate) => void | Promise<void>;
  onRelationsChanged: (updated?: WrittenCandidate) => void | Promise<void>;
  onManage: () => void;
  disabled?: boolean;
  onBusyChange: (busy: boolean) => void;
}

/**
 * 「加入 / 保留观察」：增加所选观察关系并完成本次归类。
 *
 * 只增加不退订：股票的原有观察关系保留，因此已观察股票再次归类不会丢组。
 * 归类时可以直接新建观察组，不必先跳到观察组模块。
 * 组列表与已有关系都来自共享查询：本次打开读成功才初始化勾选并开放提交。
 */
export function ObservationPicker({
  candidate,
  onObserved,
  onRelationsChanged,
  onManage,
  disabled,
  onBusyChange,
}: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newName, setNewName] = useState("");
  const [creating, setCreating] = useState(false);
  const flight = useRef(false);
  const queryClient = useQueryClient();
  const relation = useGroupRelationRead(candidate.securityId, open);
  const { initialized: loaded, markUsed } = useSessionSelection(relation.readSession);
  const options = relation.options;
  const message = error ?? relation.error;

  useEffect(() => {
    if (!open || relation.groupIds === null || loaded) return;
    const available = new Set(options.map((group) => group.groupId));
    const saved = relation.groupIds.filter((id) => available.has(id));
    // 已有关系原样预选；完全未入组时预选默认组，方便一键加入
    const defaults = options
      .filter((group) => group.isDefault)
      .map((group) => group.groupId);
    setSelected(saved.length ? saved : defaults);
    markUsed();
  }, [open, relation.groupIds, loaded, markUsed, options]);

  const createGroup = async () => {
    const name = newName.trim();
    if (!name || creating) return;
    setCreating(true);
    setError(null);
    try {
      const group = await api.createObservationGroup(name);
      addGroupToGroups(queryClient, group);
      setSelected((values) => [...values, group.groupId]);
      setNewName("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "新建观察组失败，请重试");
    } finally {
      setCreating(false);
    }
  };

  const submit = async () => {
    if (flight.current || !loaded || !selected.length) return;
    flight.current = true;
    setBusy(true);
    onBusyChange(true);
    setError(null);
    try {
      const result = await api.observeCandidate(candidate.candidateId, selected);
      confirmMemberships(queryClient, candidate.securityId, result.groupIds);
      if (result.candidate) {
        await onObserved(result.candidate);
      } else {
        await onRelationsChanged();
      }
      setOpen(false);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请重试");
    } finally {
      flight.current = false;
      setBusy(false);
      onBusyChange(false);
    }
  };

  return (
    <Popover
      modal
      open={open}
      onOpenChange={(value) => {
        if (!flight.current) setOpen(value);
      }}
    >
      <PopoverTrigger asChild>
        <Button disabled={disabled} variant="default">
          加入 / 保留观察
        </Button>
      </PopoverTrigger>
      <PopoverContent
        aria-label="选择观察组"
        onEscapeKeyDown={(event) => {
          if (flight.current) event.preventDefault();
        }}
        onInteractOutside={(event) => {
          if (flight.current) event.preventDefault();
        }}
      >
        <h4 className="font-semibold">选择观察组</h4>
        <p className="mt-1 text-xs text-foreground/60">
          {candidate.security?.name} · {candidate.security?.code} · 已选{" "}
          {selected.length} 个
        </p>
        {!loaded && !message ? (
          <p role="status" className="py-4">
            正在读取已有观察关系…
          </p>
        ) : null}
        {loaded && options.length ? (
          <fieldset disabled={busy} className="my-4 space-y-2">
            <legend className="sr-only">观察组成员选择</legend>
            {options.map((group) => (
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
        ) : null}
        {loaded && !options.length ? (
          <p className="my-4 text-sm text-foreground/70">
            还没有观察组，先新建一个。
          </p>
        ) : null}
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void createGroup();
          }}
        >
          <label className="sr-only" htmlFor={`new-group-${candidate.candidateId}`}>
            新建观察组名称
          </label>
          <Input
            id={`new-group-${candidate.candidateId}`}
            value={newName}
            // 关系还没确认前不让动手：这段时间里的勾选不作为提交依据
            disabled={busy || creating || !loaded}
            placeholder="新建观察组"
            onChange={(event) => setNewName(event.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm"
          />
          <Button
            size="sm"
            variant="ghost"
            type="submit"
            disabled={busy || creating || !loaded || !newName.trim()}
            className="inline-flex min-h-[32px] items-center gap-1 rounded-lg border border-border px-2.5 text-xs disabled:opacity-50"
          >
            {creating ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden="true" />
            ) : (
              <Plus className="h-3.5 w-3.5" aria-hidden="true" />
            )}
            新建
          </Button>
        </form>
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
        <div className="mt-3 flex flex-wrap justify-end gap-2">
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => {
              setOpen(false);
              onManage();
            }}
          >
            管理观察组
          </Button>
          <Button variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
            取消
          </Button>
          <Button
            disabled={busy || !loaded || !selected.length}
            onClick={() => void submit()}
            aria-busy={busy}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Check className="h-4 w-4" aria-hidden="true" />
            )}
            确认并完成归类
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
