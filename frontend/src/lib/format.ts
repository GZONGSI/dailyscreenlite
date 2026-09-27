/** 展示格式化助手。 */

export function formatDate(value: string): string {
  const [y, m, d] = value.split("-");
  if (!y || !m || !d) {
    return value;
  }
  return `${y} 年 ${Number(m)} 月 ${Number(d)} 日`;
}

/** 时间戳对应的北京时间日期；不随浏览器所在时区改变。 */
export function formatBeijingDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/** 北京时间时刻的短格式，用于笔记最近保存时间。 */
export function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatBytes(size: number): string {
  if (size < 1024) {
    return `${size} B`;
  }
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(1)} KB`;
  }
  return `${(size / 1024 / 1024).toFixed(1)} MB`;
}

const EXCHANGE_LABEL: Record<string, string> = {
  SH: "沪",
  SZ: "深",
  BJ: "北",
};

export function exchangeLabel(exchange: string): string {
  return EXCHANGE_LABEL[exchange] ?? exchange;
}

/** 收盘价：两位小数；缺失时不给数字，避免用零值冒充行情。 */
export function formatPrice(value: number | null | undefined): string {
  return typeof value === "number" ? value.toFixed(2) : "—";
}

/** 日涨跌幅：带符号百分比，缺失时为占位符。 */
export function formatChangePct(value: number | null | undefined): string {
  if (typeof value !== "number") {
    return "—";
  }
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

/** 涨红跌绿（与图表口径一致）；无行情时用中性色，不做涨跌暗示。 */
export function changeTone(value: number | null | undefined): string {
  if (typeof value !== "number" || value === 0) {
    return "text-foreground/60";
  }
  return value > 0 ? "text-danger" : "text-emerald-600 dark:text-emerald-400";
}
