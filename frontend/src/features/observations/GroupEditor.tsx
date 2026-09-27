import { useEffect, useRef, useState } from "react";
import { Check, Loader2, Pencil, Plus } from "lucide-react";
import { useQueryClient } from "@tanstack/react-query";

import { api, ApiError } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
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
  securityId: string;
  /** 该股票当前所属的组名，明显展示。 */
  groupNames: string[];
  /** 保存成功或组列表变化后，请工作台重读详情与成员数量。 */
  onSaved: () => void | Promise<void>;
  disabled?: boolean;
}

/**
 * 共用详情里的观察关系编辑：转组、多组归属或退出全部组。
 *
 * 这是关系管理，不联动候选处理状态：保存后处理状态保持不变。
 * 未选中的关系会被移除，因此这里的勾选就是保存后的当前事实。
 * 组列表与已有关系都来自共享查询：本次打开读成功才初始化勾选并开放保存。
 */
export function GroupEditor({
  securityId,
  groupNames,
  onSaved,
  disabled,
}: Props) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string[]>([]);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const flight = useRef(false);
  const queryClient = useQueryClient();
  const relation = useGroupRelationRead(securityId, open);
  const { initialized: loaded, markUsed } = useSessionSelection(relation.readSession);
  const options = relation.options;
  const message = error ?? relation.error;

  useEffect(() => {
    if (!open || relation.groupIds === null || loaded) return;
    setSelected(relation.groupIds);
    markUsed();
  }, [open, relation.groupIds, loaded, markUsed]);

  const createGroup = async () => {
    const name = newName.trim();
    if (!name || busy) return;
    setBusy(true);
    setError(null);
    try {
      const group = await api.createObservationGroup(name);
      addGroupToGroups(queryClient, group);
      setSelected((values) => [...values, group.groupId]);
      setNewName("");
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "新建观察组失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  const save = async (groupIds: string[]) => {
    // 读取未完成或失败时不得保存：此时的 selected 不代表库中事实
    if (flight.current || !loaded) return;
    flight.current = true;
    setBusy(true);
    setError(null);
    try {
      const saved = await api.saveMembership(securityId, groupIds);
      confirmMemberships(queryClient, securityId, saved.groupIds);
      setSelected(saved.groupIds);
      setOpen(false);
      await onSaved();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存观察关系失败，请重试");
    } finally {
      flight.current = false;
      setBusy(false);
    }
  };

  return (
    <Popover
      open={open}
      onOpenChange={(value) => {
        if (!flight.current) setOpen(value);
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="outline" disabled={disabled || busy}>
          <Pencil className="h-4 w-4" aria-hidden="true" />
          调整观察关系
        </Button>
      </PopoverTrigger>
      <PopoverContent aria-label="调整观察关系">
        <h4 className="font-semibold">观察关系</h4>
        <p className="mt-1 text-xs text-foreground/60">
          勾选即属于该组；未勾选的组会移出。只改关系，不改变处理状态。
        </p>
        {groupNames.length ? (
          <p className="mt-2 flex flex-wrap gap-1">
            {groupNames.map((name) => (
              <Badge key={name} tone="neutral">
                {name}
              </Badge>
            ))}
          </p>
        ) : null}
        {!loaded && !message ? (
          <p role="status" className="my-4 text-sm text-foreground/60">
            正在读取已有观察关系…
          </p>
        ) : null}
        {loaded && options.length ? (
          <fieldset disabled={busy} className="my-4 space-y-2">
            <legend className="sr-only">观察组选择</legend>
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
          <p className="my-4 text-sm text-foreground/70">还没有观察组，先新建一个。</p>
        ) : null}
        <form
          className="flex items-center gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            void createGroup();
          }}
        >
          <label className="sr-only" htmlFor={`group-editor-new-${securityId}`}>
            新建观察组名称
          </label>
          <Input
            id={`group-editor-new-${securityId}`}
            value={newName}
            // 关系还没确认前不让动手：这段时间里的勾选不作为保存依据
            disabled={busy || !loaded}
            placeholder="新建观察组"
            onChange={(event) => setNewName(event.target.value)}
            className="min-w-0 flex-1 rounded-lg border border-border bg-background px-2.5 py-1.5 text-sm"
          />
          <Button
            size="sm"
            variant="ghost"
            type="submit"
            disabled={busy || !loaded || !newName.trim()}
            className="inline-flex min-h-[32px] items-center gap-1 rounded-lg border border-border px-2.5 text-xs disabled:opacity-50"
          >
            <Plus className="h-3.5 w-3.5" aria-hidden="true" />
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
            disabled={busy || !loaded}
            onClick={() => void save([])}
          >
            退出全部组
          </Button>
          <Button variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
            取消
          </Button>
          <Button
            disabled={busy || !loaded}
            aria-busy={busy}
            onClick={() => void save(selected)}
          >
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
            ) : (
              <Check className="h-4 w-4" aria-hidden="true" />
            )}
            保存观察关系
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
