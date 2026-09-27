import { useRef, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  Clock,
  RotateCcw,
  ThumbsDown,
} from "lucide-react";

import type {
  Candidate,
  CandidateAction,
  WrittenCandidate,
} from "../../api/classification";
import { api, ApiError, type QuoteSnapshot } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { ObservationPicker } from "../observations/ObservationPicker";
import { SecurityDetail } from "../securities/SecurityDetail";
import { itemStateLabel, isUnprocessed } from "./state";
import { RemoveGroupsPicker } from "./RemoveGroupsPicker";

interface Props {
  candidate: Candidate;
  /** 批次 id → 来源名称，用于展示该股票的全部来源。 */
  sourceNames: Map<string, string>;
  /** 队列中的位置（1 起）与总数，用于末项显示「结束」。 */
  position: number | null;
  queueSize: number;
  hasPrevious: boolean;
  /** 还有可前进的去处时「下一个」才可用（服务端判定，结束卡拥有前进历史时也为真）。 */
  hasNext: boolean;
  /** 当前卡不属于左侧当前筛选结果：照常展示并明确提示，不因此改动筛选。 */
  outsideFilter: boolean;
  navigationBusy: boolean;
  /** 队列列表的轻量行情摘要：最新收盘价、行情日与日涨跌幅。 */
  quote?: QuoteSnapshot;
  /** 所属观察组的名称，明显展示在身份旁。 */
  groupNames: string[];
  onUpdated: (candidate: WrittenCandidate) => void | Promise<void>;
  /** 只修改观察关系后，请工作区重读组成员数量与卡片归属。 */
  onRelationsChanged: (updated?: WrittenCandidate) => void | Promise<void>;
  /** 卡片快捷新增笔记后，请工作区刷新该股票的笔记计数。 */
  onNotesChanged: (securityId: string) => void;
  onManageGroups: () => void;
  /** 主动重新归类：同时清掉会把它挡在外面的筛选（走工作区同一入口）。 */
  onReclassify: () => void;
  onBusyChange: (busy: boolean) => void;
  onPrevious: () => void;
  onNext: () => void;
  /** 数据更新/补取标记：变化时当前卡片的图表原位重读。 */
  refreshToken?: number;
}

/** 各处理状态的动作结果说明。 */
const RESULT_NOTE: Record<string, string> = {
  later: "已移到队尾稍后处理，仍留在待归类池，可随时回来。",
  dismissed: "已暂不关注，已结束本次归类；导入事实保留。",
  observed: "已加入观察组，已完成本次归类。",
  cleared: "已清理出待归类池；导入事实保留，可从已处理重新归类。",
  reclassified: "已重新进入待归类，队列位置已更新。",
};

/**
 * 结果说明的键：已处理项按状态，刚被主动重新归类的待归类项按 reclassified。
 * 待归类不是终态，只有 reclassified 才需要提示，避免新入选/合并也弹出说明。
 */
function resultKey(candidate: Candidate): string | null {
  if (candidate.state === "pending") {
    return candidate.actionResult === "reclassified" ? "reclassified" : null;
  }
  return candidate.state;
}

function resultNote(candidate: Candidate): string {
  const key = resultKey(candidate);
  if (!key) return "";
  return RESULT_NOTE[key] ?? "已处理，已结束本次归类。";
}

/**
 * 股票卡：候选归类的当前股票，动作保存成功后由工作区推进。
 *
 * 内容与观察组同页详情共用（SecurityDetail）；这里只加上归类场景的操作。
 */
export function CandidateCard({
  candidate,
  sourceNames,
  position,
  queueSize,
  hasPrevious,
  hasNext,
  outsideFilter,
  navigationBusy,
  quote,
  groupNames,
  onUpdated,
  onRelationsChanged,
  onNotesChanged,
  onPrevious,
  onNext,
  onManageGroups,
  onReclassify,
  onBusyChange,
  refreshToken = 0,
}: Props) {
  const [busy, setBusy] = useState(false);
  const flight = useRef(false);
  const changeBusy = (value: boolean) => {
    setBusy(value);
    onBusyChange(value);
  };
  const [error, setError] = useState<string | null>(null);
  const processed = !isUnprocessed(candidate.state);
  const note = resultNote(candidate);
  const observed = candidate.observed;

  const act = async (action: (id: string) => Promise<CandidateAction>) => {
    if (flight.current) return;
    flight.current = true;
    setError(null);
    changeBusy(true);
    try {
      const updated = await action(candidate.candidateId);
      // 服务端先确认归类结果；工作区随后用单独的导航请求推进。
      await onUpdated(updated);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "操作失败，请重试");
    } finally {
      flight.current = false;
      changeBusy(false);
    }
  };

  return (
    <SecurityDetail
      testId="stock-card"
      titleId="stock-card-title"
      securityId={candidate.securityId}
      security={candidate.security}
      candidate={candidate}
      groupNames={groupNames}
      // 终态本身就是「已观察」时不重复显示同一个词
      showObservedBadge={observed && candidate.state !== "observed"}
      quote={quote}
      sourceNames={sourceNames}
      contextNote={
        outsideFilter ? (
          <span
            className="text-xs text-warning"
            data-testid="outside-filter-hint"
          >
            该股票不在当前筛选结果中，返回上一项仍回到原工作范围
          </span>
        ) : position ? (
          <span className="text-xs text-foreground/45">
            第 {position} / {queueSize} 只
          </span>
        ) : null
      }
      statusBadge={
        <Badge
          tone={
            candidate.state === "observed"
              ? "success"
              : processed
                ? "neutral"
                : "warning"
          }
        >
          {itemStateLabel(candidate)}
        </Badge>
      }
      resultNote={note}
      error={
        error ? (
          <>
            <AlertCircle className="mr-1 inline h-4 w-4" aria-hidden="true" />
            {error}
          </>
        ) : null
      }
      onNotesChanged={onNotesChanged}
      refreshToken={refreshToken}
      disabled={busy}
      actions={
        <>
          {processed ? (
            <Button variant="outline" disabled={busy} onClick={onReclassify}>
              <RotateCcw className="h-4 w-4" />
              重新归类
            </Button>
          ) : (
            <ObservationPicker
              key={candidate.candidateId}
              candidate={candidate}
              onObserved={onUpdated}
              onRelationsChanged={onRelationsChanged}
              onManage={onManageGroups}
              disabled={busy}
              onBusyChange={changeBusy}
            />
          )}
          {observed ? (
            <RemoveGroupsPicker
              key={`remove-${candidate.candidateId}`}
              candidate={candidate}
              onRemoved={onUpdated}
              disabled={busy}
              onBusyChange={changeBusy}
            />
          ) : null}
          {!processed ? (
            <>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => void act(api.laterCandidate)}
              >
                <Clock className="h-4 w-4" />
                稍后处理
              </Button>
              <Button
                variant="outline"
                disabled={busy}
                onClick={() => void act(api.dismissCandidate)}
              >
                <ThumbsDown className="h-4 w-4" />
                暂不关注
              </Button>
            </>
          ) : null}
          <BrowseButtons
            hasPrevious={hasPrevious}
            hasNext={hasNext}
            busy={busy || navigationBusy}
            onPrevious={onPrevious}
            onNext={onNext}
          />
        </>
      }
    />
  );
}

function BrowseButtons({
  hasPrevious,
  hasNext,
  busy,
  onPrevious,
  onNext,
}: {
  hasPrevious: boolean;
  hasNext: boolean;
  busy: boolean;
  onPrevious: () => void;
  onNext: () => void;
}) {
  return (
    <div
      role="group"
      aria-label="浏览路径"
      aria-busy={busy}
      className="ml-auto inline-flex shrink-0 items-center gap-1 rounded-xl border border-border bg-surface p-0.5"
    >
      <Button
        variant="ghost"
        aria-label="上一个"
        onClick={onPrevious}
        disabled={busy || !hasPrevious}
        className="min-h-11 min-w-11 gap-1.5 px-2.5 text-foreground/75 sm:px-3"
      >
        <ArrowLeft className="h-4 w-4" aria-hidden="true" />
        <span className="hidden sm:inline">上一个</span>
      </Button>
      <Button
        variant="primary"
        aria-label="下一个"
        onClick={onNext}
        disabled={busy || !hasNext}
        className="min-h-11 min-w-11 gap-1.5 px-2.5 sm:px-3"
      >
        <span className="hidden sm:inline">下一个</span>
        <ArrowRight className="h-4 w-4" aria-hidden="true" />
      </Button>
    </div>
  );
}
