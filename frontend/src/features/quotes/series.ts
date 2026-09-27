import type { DailyBar } from "../../api/client";
export const PERIODS = [10, 20, 60] as const;
export type Period = (typeof PERIODS)[number];
export const INTERVALS = { day: "日线", week: "周线", month: "月线" } as const;
export type ChartInterval = keyof typeof INTERVALS;

/** 同一复权日线按自然周（月）汇总；日期保留该周期最后实际交易日。 */
export function aggregateBars(bars: readonly DailyBar[], interval: ChartInterval, completeHistory = true): DailyBar[] {
  if (interval === "day") return [...bars];
  const groups = new Map<string, DailyBar>();
  for (const bar of bars) {
    let key = bar.date.slice(0, 7);
    if (interval === "week") {
      const day = new Date(`${bar.date}T00:00:00Z`);
      day.setUTCDate(day.getUTCDate() - (day.getUTCDay() + 6) % 7);
      key = day.toISOString().slice(0, 10);
    }
    const first = groups.get(key);
    if (!first) groups.set(key, { ...bar });
    else groups.set(key, {
      date: bar.date,
      open: first.open,
      high: Math.max(first.high, bar.high),
      low: Math.min(first.low, bar.low),
      close: bar.close,
      volumeLots: first.volumeLots + bar.volumeLots,
      amountYuan: first.amountYuan == null || bar.amountYuan == null
        ? null : first.amountYuan + bar.amountYuan,
    });
  }
  // 分页切口可能落在周期中间，尚未补齐时不把该周期当成完整 K 线。
  return [...groups.values()].slice(completeHistory ? 0 : 1);
}
export function movingAverage(
  bars: readonly { date: string; close: number }[],
  period: number,
) {
  const result: { date: string; value: number }[] = [];
  let sum = 0;
  bars.forEach((bar, i) => {
    sum += bar.close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) result.push({ date: bar.date, value: sum / period });
  });
  return result;
}
export function normalizeBars(bars: readonly DailyBar[]): DailyBar[] {
  return [...new Map(bars.map((bar) => [bar.date, bar])).values()].sort(
    (a, b) => a.date.localeCompare(b.date),
  );
}
/** Calendar fields, never a UTC/local Date round trip. */
export function businessDay(date: string) {
  const [year, month, day] = date.split("-").map(Number);
  return { year, month, day };
}
export function volumeText(value: number) {
  if (Math.abs(value) >= 100000000)
    return `${(value / 100000000).toFixed(2)} 亿`;
  if (Math.abs(value) >= 10000) return `${(value / 10000).toFixed(2)} 万`;
  return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}
