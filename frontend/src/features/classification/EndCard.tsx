import { ArrowLeft, ArrowRight, Coffee, LayoutGrid } from "lucide-react";

import type { RoundStats } from "../../api/classification";
import { Button } from "../../components/ui/Button";

interface Props {
  round: RoundStats;
  hasPrevious: boolean;
  /** 结束卡拥有前进历史（例如从结束卡打开过候选）时，「下一个」才有去处。 */
  hasNext: boolean;
  busy: boolean;
  onPrevious: () => void;
  onNext: () => void;
  onReturn: () => void;
}

/**
 * 本轮结束卡：轻松克制的收尾，展示真实浏览/处理/剩余数量，不自动循环。
 *
 * 结束卡是浏览路径里的一步：存在前进历史时提供「下一个」回到那一步，
 * 历史末端没有去处时不显示这个按钮。
 */
export function EndCard({
  round,
  hasPrevious,
  hasNext,
  busy,
  onPrevious,
  onNext,
  onReturn,
}: Props) {
  return (
    <section
      aria-labelledby="end-card-title"
      data-testid="end-card"
      className="rounded-2xl border border-border bg-surface p-10 text-center shadow-sm"
    >
      <div
        className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-selected"
        aria-hidden="true"
      >
        <Coffee className="h-8 w-8 text-primary" />
      </div>
      <h3 id="end-card-title" className="mt-4 text-xl font-semibold">
        当前范围看完了
      </h3>
      <p className="mt-2 text-sm text-foreground/65">
        喝口水，机会慢慢找。当前范围的浏览结束了，待归类项会继续保留。
      </p>

      <dl
        className="mx-auto mt-6 grid max-w-md grid-cols-3 gap-3 text-sm"
        aria-label="本轮浏览统计"
      >
        <div className="rounded-xl bg-muted/60 px-3 py-3">
          <dt className="text-xs text-foreground/60">本轮浏览</dt>
          <dd className="mt-1 text-lg font-semibold" data-testid="round-viewed">
            {round.viewed}
          </dd>
        </div>
        <div className="rounded-xl bg-muted/60 px-3 py-3">
          <dt className="text-xs text-foreground/60">已处理</dt>
          <dd
            className="mt-1 text-lg font-semibold"
            data-testid="round-processed"
          >
            {round.processed}
          </dd>
        </div>
        <div className="rounded-xl bg-muted/60 px-3 py-3">
          <dt className="text-xs text-foreground/60">剩余待归类</dt>
          <dd
            className="mt-1 text-lg font-semibold"
            data-testid="round-remaining"
          >
            {round.remaining}
          </dd>
        </div>
      </dl>

      <div className="mt-6 flex flex-wrap items-center justify-center gap-2">
        <Button
          variant="secondary"
          aria-label="上一个"
          onClick={onPrevious}
          disabled={busy || !hasPrevious}
          className="min-h-11"
        >
          <ArrowLeft className="h-4 w-4" aria-hidden="true" />
          上一个
        </Button>
        {hasNext ? (
          <Button
            variant="secondary"
            aria-label="下一个"
            onClick={onNext}
            disabled={busy}
            className="min-h-11"
          >
            下一个
            <ArrowRight className="h-4 w-4" aria-hidden="true" />
          </Button>
        ) : null}
        <Button variant="secondary" disabled={busy} onClick={onReturn} className="min-h-11">
          <LayoutGrid className="h-4 w-4" aria-hidden="true" />
          返回队列
        </Button>
      </div>
    </section>
  );
}
