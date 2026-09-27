import { ArrowDown, ArrowUp } from "lucide-react";

import type { QuoteSnapshot } from "../../api/client";
import type { ObservedStock } from "../../api/observations";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { changeTone, formatBeijingDate, formatChangePct, formatPrice } from "../../lib/format";

interface Props {
  stocks: ObservedStock[];
  /** 所属组的名称：把组标识转成可读标签。 */
  groupNames: (groupIds: string[]) => string[];
  quotes: Map<string, QuoteSnapshot>;
  selectedId: string | null;
  /** 排序取值是服务端文本列（未知取值由服务端判定成 400）。 */
  sort: string;
  loading?: boolean;
  disabled?: boolean;
  onSortChange: (sort: string) => void;
  onSelect: (securityId: string) => void;
}

const SORTS: { value: string; label: string }[] = [
  { value: "joined", label: "加入时间（新→旧）" },
  { value: "name", label: "名称" },
  { value: "change", label: "涨跌幅" },
];

/**
 * 观察股票表：名称/代码、收盘价、对应行情日涨跌幅、实际行情日期、加入日期与所属组。
 *
 * 行情日期如实展示，旧行情不冒充当天行情；缺行情的股票显示为空仍可浏览。
 * 排序由外层保存，切模块或重启后按同一方式恢复。
 */
export function ObservedStockTable({
  stocks,
  groupNames,
  quotes,
  selectedId,
  sort,
  loading,
  disabled,
  onSortChange,
  onSelect,
}: Props) {
  return (
    <section
      aria-labelledby="observed-stocks-title"
      data-testid="observed-stocks"
      className="observation-table-pane"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="observed-stocks-title" className="text-base font-semibold">
          全部观察股票
        </h2>
        <div className="flex items-center gap-2">
          <label htmlFor="observation-sort" className="text-xs text-foreground/60">
            排序
          </label>
          <select
            id="observation-sort"
            aria-label="观察列表排序"
            value={sort}
            disabled={disabled}
            onChange={(event) => onSortChange(event.target.value)}
            className="rounded-lg border border-border bg-background px-2 py-1.5 text-xs"
          >
            {SORTS.map((entry) => (
              <option key={entry.value} value={entry.value}>
                {entry.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="observation-table-scroll" aria-busy={loading}>
        <table aria-label="观察股票列表" className="classification-table">
          <thead>
            <tr>
              <th aria-sort={sort === "name" ? "ascending" : "none"}>
                <SortHeader
                  label="名称 / 代码"
                  active={sort === "name"}
                  onClick={() => onSortChange("name")}
                />
              </th>
              <th>收盘价</th>
              <th aria-sort={sort === "change" ? "ascending" : "none"}>
                <SortHeader
                  label="涨跌幅"
                  active={sort === "change"}
                  onClick={() => onSortChange("change")}
                />
              </th>
              <th>行情日</th>
              <th>所属组</th>
              <th title="单组显示加入该组的日期；全部观察股票显示现存组中最新加入日期（北京时间）">加入日期</th>
            </tr>
          </thead>
          <tbody>
            {stocks.map((stock) => {
              const quote = quotes.get(stock.securityId);
              return (
                <tr
                  key={stock.securityId}
                  aria-selected={stock.securityId === selectedId}
                  aria-disabled={disabled || undefined}
                  className={disabled ? undefined : "cursor-pointer hover:bg-muted/60"}
                  onClick={() => {
                    if (!disabled) onSelect(stock.securityId);
                  }}
                >
                  <td>
                    <Button
                      variant="ghost"
                      disabled={disabled}
                      onClick={(event) => {
                        event.stopPropagation();
                        onSelect(stock.securityId);
                      }}
                      className="text-left"
                    >
                      <span className="block truncate font-medium">
                        {stock.security?.name ?? stock.securityId}
                      </span>
                      <span className="block font-mono text-xs text-foreground/60">
                        {stock.security?.code ?? ""}
                      </span>
                    </Button>
                  </td>
                  <td className="tabular-nums">
                    {formatPrice(quote?.close ?? null)}
                  </td>
                  <td
                    className={`tabular-nums ${changeTone(quote?.changePct ?? null)}`}
                  >
                    {formatChangePct(quote?.changePct ?? null)}
                  </td>
                  <td className="tabular-nums">{quote?.tradeDate ?? "—"}</td>
                  <td>
                    <span className="flex flex-wrap gap-1">
                      {groupNames(stock.groupIds).map((name) => (
                        <Badge key={name} tone="neutral">
                          {name}
                        </Badge>
                      ))}
                    </span>
                  </td>
                  <td className="tabular-nums">
                    <time dateTime={stock.joinedAt}>{formatBeijingDate(stock.joinedAt)}</time>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {!stocks.length ? (
          <p className="p-8 text-center text-sm text-foreground/60">
            {loading
              ? "正在读取观察列表…"
              : "这个视图还没有股票。在候选归类里加入观察组后就会出现在这里。"}
          </p>
        ) : null}
      </div>
    </section>
  );
}

function SortHeader({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center gap-1 text-left font-medium"
    >
      {label}
      {active ? (
        <ArrowUp className="h-3 w-3" aria-hidden="true" />
      ) : (
        <ArrowDown className="h-3 w-3 opacity-30" aria-hidden="true" />
      )}
    </button>
  );
}
