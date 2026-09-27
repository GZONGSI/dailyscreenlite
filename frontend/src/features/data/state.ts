import {
  AlertCircle,
  CheckCircle2,
  CircleHelp,
  MinusCircle,
  type LucideIcon,
} from "lucide-react";

import type { DataStatusItem, StockDataState, StockDataStatus } from "../../api/client";

/** 逐股状态的展示标签、图标与配色；四种状态在数据中心与详情里保持一致。 */
export const STOCK_STATE: Record<
  StockDataState,
  { label: string; icon: LucideIcon; tone: string; dot: string }
> = {
  updated: {
    label: "已更新",
    icon: CheckCircle2,
    tone: "text-emerald-600 dark:text-emerald-400",
    dot: "bg-emerald-500",
  },
  suspended: {
    label: "全天停牌",
    icon: MinusCircle,
    tone: "text-foreground/50",
    dot: "bg-slate-400",
  },
  pending: {
    label: "待补齐",
    icon: AlertCircle,
    tone: "text-danger",
    dot: "bg-red-500",
  },
  unconfirmed: {
    label: "状态待确认",
    icon: CircleHelp,
    tone: "text-foreground/50",
    dot: "bg-slate-400",
  },
};

/** 分项状态（证券库/股票状态/日线）的展示标签与配色。 */
export const ITEM_STATE: Record<
  DataStatusItem["state"],
  { label: string; tone: string; dot: string }
> = {
  ok: { label: "已更新", tone: "text-emerald-600 dark:text-emerald-400", dot: "bg-emerald-500" },
  running: { label: "更新中", tone: "text-primary", dot: "bg-blue-500" },
  partial: { label: "部分完成", tone: "text-amber-600 dark:text-amber-400", dot: "bg-amber-500" },
  failed: { label: "待补齐", tone: "text-danger", dot: "bg-red-500" },
  unknown: { label: "未确认", tone: "text-foreground/50", dot: "bg-slate-400" },
};

/** 按状态统计当前范围内的股票数，供「本次更新」图例使用。 */
export function countByState(stocks: StockDataStatus[]): Record<StockDataState, number> {
  const counts: Record<StockDataState, number> = {
    updated: 0,
    suspended: 0,
    pending: 0,
    unconfirmed: 0,
  };
  for (const stock of stocks) {
    counts[stock.state] += 1;
  }
  return counts;
}
