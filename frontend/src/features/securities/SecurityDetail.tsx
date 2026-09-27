import type { ReactNode } from "react";
import { History } from "lucide-react";

import type { Candidate } from "../../api/classification";
import type { QuoteSnapshot } from "../../api/client";
import type { Security } from "../../api/securities";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "../../components/ui/Popover";
import {
  changeTone,
  exchangeLabel,
  formatChangePct,
  formatDate,
  formatPrice,
  formatTimestamp,
} from "../../lib/format";
import { actionLabel } from "../classification/state";
import { NoteWorkspace, type NoteOwner } from "../notes/NoteWorkspace";
import { QuotePanel } from "../quotes/QuotePanel";

interface Props {
  securityId: string;
  /** 权威证券库中的身份；缺失时退化为显示证券标识。 */
  security: Security | null;
  /** 该证券的候选项：提供入选日期、来源与处理记录入口，正常流程总有值。 */
  candidate: Candidate | null;
  /** 观察关系：所属全部现存观察组名称，明显展示。 */
  groupNames: string[];
  /** 已属于观察组但状态徽章不是「已观察」时补一个标注，避免重复同一个词。 */
  showObservedBadge?: boolean;
  quote?: QuoteSnapshot;
  /** 批次 id → 来源名称，用于「来源与处理记录」。 */
  sourceNames: Map<string, string>;
  /** 场景说明，跟在身份之后（如队列位置）。 */
  contextNote?: ReactNode;
  /** 场景标记，显示在右上角（如处理状态）。 */
  statusBadge?: ReactNode;
  /** 最近一次动作的结果说明；没有动作时不占位。 */
  resultNote?: string;
  /** 按场景出现的操作：归类动作或观察关系编辑。 */
  actions?: ReactNode;
  error?: ReactNode;
  onNotesChanged: (securityId: string) => void;
  /** 数据更新/补取标记：变化时当前卡片的图表原位重读。 */
  refreshToken?: number;
  disabled?: boolean;
  testId?: string;
  titleId?: string;
}

/**
 * 共用个股详情：以股票而非某次候选项为身份，跨导入日期与入口共享内容。
 *
 * 候选归类卡片与观察组同页详情都用它渲染股票信息、观察关系、行情、笔记与
 * 来源/处理记录；差异只在传入的操作（归类动作或观察关系编辑）。
 * 观察关系明显展示，来源与处理记录通过小触发入口展开、默认收起。
 */
export function SecurityDetail({
  securityId,
  security,
  candidate,
  groupNames,
  showObservedBadge = false,
  quote,
  sourceNames,
  contextNote,
  statusBadge,
  resultNote,
  actions,
  error,
  onNotesChanged,
  refreshToken = 0,
  disabled = false,
  testId = "security-detail",
  titleId = "security-detail-title",
}: Props) {
  const owner: NoteOwner = {
    securityId,
    name: security?.name ?? null,
    code: security?.code ?? null,
    noteCount: candidate?.noteCount ?? 0,
  };
  const selections = candidate?.selections ?? [];
  const sources = candidate?.sources ?? [];
  const history = candidate?.history ?? [];
  const latestImportDate = candidate?.latestImportDate ?? null;

  return (
    <article
      aria-labelledby={titleId}
      data-testid={testId}
      className="security-detail border border-border bg-surface p-4"
    >
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 id={titleId} className="text-xl font-semibold">
            {security?.name || "未知名称"}
          </h3>
          <p className="mt-1 flex flex-wrap items-center gap-2 text-sm text-foreground/70">
            <span className="font-mono text-base text-foreground">
              {security?.code ?? securityId}
            </span>
            {security ? (
              <Badge tone="info">{exchangeLabel(security.exchange)}市</Badge>
            ) : null}
            {security?.isSt ? <Badge tone="danger">ST</Badge> : null}
            {showObservedBadge ? <Badge tone="success">已观察</Badge> : null}
            {/* 观察关系明显展示：所属全部现存组 */}
            {groupNames.map((name) => (
              <Badge key={name} tone="neutral">
                {name}
              </Badge>
            ))}
            {contextNote}
          </p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {quote ? (
            <span className="text-right">
              <span
                className={`block text-lg font-semibold tabular-nums ${changeTone(quote.changePct)}`}
              >
                {formatPrice(quote.close)}
              </span>
              {/* 行情日如实标注：旧行情不冒充当天行情 */}
              <span className={`block text-xs ${changeTone(quote.changePct)}`}>
                {formatChangePct(quote.changePct)} · 行情 {quote.tradeDate}
              </span>
            </span>
          ) : null}
          {statusBadge}
        </div>
      </header>

      <div className="mb-3 flex flex-wrap items-center gap-4 text-xs text-foreground/60">
        <span>
          最近入选 {latestImportDate ? formatDate(latestImportDate) : "未知"}
        </span>
        {candidate && candidate.importDates.length > 1 ? (
          <span>共 {candidate.importDates.length} 个入选日期</span>
        ) : null}
        <span>上市日期 {security?.listingDate ?? "未知"}</span>
        <Popover>
          <PopoverTrigger asChild>
            <Button size="sm" variant="ghost">
              <History className="h-4 w-4" aria-hidden="true" />
              来源与处理记录（{sources.length}）
            </Button>
          </PopoverTrigger>
          <PopoverContent aria-label="来源与处理记录">
            <section>
              <h4 className="font-semibold">入选日期</h4>
              <ul className="mt-1 space-y-1 text-xs">
                {selections.map((selection) => (
                  <li key={selection.importDate} className="flex justify-between gap-3">
                    <span className="font-mono">{selection.importDate}</span>
                    <span className="text-foreground/60">
                      {sourceNames.get(selection.batchId) ?? "未知来源"}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
            <section className="mt-3">
              <h4 className="font-semibold">来源（{sources.length}）</h4>
              <ul className="mt-1 space-y-1 text-xs">
                {sources.map((source) => (
                  <li key={source.batchId} className="break-all">
                    {sourceNames.get(source.batchId) ?? "未知来源"} ·{" "}
                    {source.importDate}
                  </li>
                ))}
              </ul>
            </section>
            <section className="mt-3">
              <h4 className="font-semibold">处理记录</h4>
              <ul className="mt-1 space-y-1 text-xs">
                {history.map((entry, index) => (
                  <li key={`${entry.actedAt}-${index}`}>
                    {actionLabel(entry.action)} · {formatTimestamp(entry.actedAt)}
                  </li>
                ))}
              </ul>
            </section>
          </PopoverContent>
        </Popover>
      </div>

      <QuotePanel
        key={securityId}
        securityId={securityId}
        panelId={securityId}
        refreshToken={refreshToken}
        compact
      />

      <NoteWorkspace
        key={securityId}
        owner={owner}
        onNotesChanged={onNotesChanged}
        disabled={disabled}
      />

      {error ? (
        <p className="mt-4 text-sm text-danger" role="alert">
          {error}
        </p>
      ) : null}

      {actions || resultNote ? (
        <div className="stock-actions space-y-2">
          {resultNote ? (
            <p className="text-sm text-foreground/70" role="status">
              {resultNote}
            </p>
          ) : null}
          {actions ? (
            <div className="flex flex-wrap items-center gap-2">{actions}</div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}
