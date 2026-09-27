import { useEffect, useMemo, useRef, useState } from "react";
import {
  createChart,
  CandlestickSeries,
  HistogramSeries,
  LineSeries,
  ColorType,
  CrosshairMode,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type MouseEventParams,
} from "lightweight-charts";
import type { DailyBar } from "../../api/client";
import { Button } from "../../components/ui/Button";
import {
  businessDay,
  aggregateBars,
  INTERVALS,
  movingAverage,
  PERIODS,
  volumeText,
  type Period,
  type ChartInterval,
} from "./series";
import { useChartSession } from "./ChartSession";

interface Props {
  bars: DailyBar[];
  completeHistory: boolean;
  height: number;
  ariaLabel: string;
  onHistory: () => void;
  placeholder?: string;
}
const MA_COLORS = { 10: "#d97706", 20: "#7c3aed", 60: "#0891b2" };
export function CandleChart({
  bars: dailyBars,
  completeHistory,
  height,
  ariaLabel,
  onHistory,
  placeholder,
}: Props) {
  const session = useChartSession();
  const bars = useMemo(() => aggregateBars(dailyBars, session.interval, completeHistory), [dailyBars, session.interval, completeHistory]);
  const installedInterval = useRef(session.interval);
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const candle = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volume = useRef<ISeriesApi<"Histogram"> | null>(null);
  const lines = useRef(new Map<Period, ISeriesApi<"Line">>());
  const props = useRef({ bars, onHistory, session });
  props.current = { bars, onHistory, session };
  const installedBars = useRef<DailyBar[]>([]);
  const changing = useRef(false);
  const range = useRef<{ from: number; to: number } | null>(null);
  const defaultMax = useRef(session.cache.current?.view?.interval === session.interval
    ? session.cache.current.view.defaultMax : true);
  const [readDate, setReadDate] = useState<string | null>(null);
  const [visible, setVisible] = useState({ first: "", last: "", count: 0 });
  const [viewWidth, setViewWidth] = useState(500);
  const frame = useRef(0);
  const hovered = useRef<string | null>(null);
  const averages = useMemo(
    () => new Map(PERIODS.map((p) => [p, movingAverage(bars, p)])),
    [bars],
  );
  const readBar = bars.find((b) => b.date === readDate) ?? bars.at(-1);
  const scheduleRead = () => {
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      const rows = props.current.bars;
      const r = chart.current?.timeScale().getVisibleLogicalRange();
      if (!rows.length || !r) return;
      setViewWidth(r.to - r.from);
      const first = Math.max(0, Math.min(rows.length - 1, Math.ceil(r.from)));
      const last = Math.max(0, Math.min(rows.length - 1, Math.floor(r.to)));
      setReadDate(hovered.current ?? rows[last].date);
      setVisible({
        first: rows[first].date,
        last: rows[last].date,
        count: last - first + 1,
      });
    });
  };
  const setView = (next: { from: number; to: number }) => {
    // setData can already reach this range without another range notification.
    // Persist its new date origin together with the logical coordinates.
    chart.current!.timeScale().setVisibleLogicalRange(next);
    range.current = next;
    const cache = session.cache.current;
    if (cache && installedBars.current.length) {
      cache.view = {
        first: installedBars.current[0].date,
        ...next,
        interval: props.current.session.interval,
        defaultMax: defaultMax.current,
      };
    }
    scheduleRead();
    const warmup = Math.max(1, ...props.current.session.periods) - 1;
    if (next.from < 20 + warmup) props.current.onHistory();
  };
  useEffect(() => {
    const node = host.current!;
    const instance = createChart(node, {
      width: Math.max(1, node.clientWidth),
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        attributionLogo: true,
      },
      crosshair: { mode: CrosshairMode.Magnet },
      localization: { locale: "zh-CN", dateFormat: "yyyy-MM-dd" },
      timeScale: {
        rightOffset: 0,
        minBarSpacing: 0.1,
        fixLeftEdge: false,
        lockVisibleTimeRangeOnResize: true,
        fixRightEdge: false,
        shiftVisibleRangeOnNewBar: false,
      },
      rightPriceScale: { minimumWidth: 65 },
      handleScroll: {
        mouseWheel: false,
        pressedMouseMove: true,
        horzTouchDrag: true,
        vertTouchDrag: false,
      },
      handleScale: {
        mouseWheel: true,
        pinch: true,
        axisPressedMouseMove: false,
      },
      kineticScroll: { mouse: false, touch: false },
    });
    chart.current = instance;
    candle.current = instance.addSeries(CandlestickSeries, {
      upColor: "#ffffff",
      downColor: "#059669",
      borderUpColor: "#dc2626",
      borderDownColor: "#059669",
      wickUpColor: "#dc2626",
      wickDownColor: "#059669",
      priceFormat: { type: "price", precision: 2, minMove: 0.01 },
    });
    volume.current = instance.addSeries(
      HistogramSeries,
      {
        priceFormat: { type: "custom", formatter: volumeText, minMove: 0.01 },
        priceLineVisible: false,
      },
      1,
    );
    instance.panes()[0].setStretchFactor(0.78);
    instance.panes()[1].setStretchFactor(0.22);
    PERIODS.forEach((p) =>
      lines.current.set(
        p,
        instance.addSeries(LineSeries, {
          color: MA_COLORS[p],
          lineWidth: 1,
          visible: false,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        }),
      ),
    );
    const applyTheme = () => {
      const css = getComputedStyle(document.documentElement);
      const color = (name: string) =>
        `rgb(${css.getPropertyValue(name).trim()})`;
      instance.applyOptions({
        layout: {
          background: { type: ColorType.Solid, color: color("--surface") },
          textColor: color("--foreground"),
        },
        grid: {
          vertLines: { color: color("--border") },
          horzLines: { color: color("--border") },
        },
        rightPriceScale: { borderColor: color("--border") },
        timeScale: { borderColor: color("--border") },
      });
      candle.current?.applyOptions({ upColor: color("--surface") });
      const dark = document.documentElement.classList.contains("dark");
      lines.current
        .get(10)
        ?.applyOptions({ color: dark ? "#fbbf24" : MA_COLORS[10] });
      lines.current
        .get(20)
        ?.applyOptions({ color: dark ? "#c4b5fd" : MA_COLORS[20] });
      lines.current
        .get(60)
        ?.applyOptions({ color: dark ? "#22d3ee" : MA_COLORS[60] });
    };
    applyTheme();
    const theme = new MutationObserver(applyTheme);
    theme.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    const resize = new ResizeObserver(() => {
      if (!node.clientWidth) return;
      const previous =
        range.current ?? instance.timeScale().getVisibleLogicalRange();
      changing.current = true;
      instance.resize(node.clientWidth, node.clientHeight);
      if (previous) setView(previous);
      changing.current = false;
    });
    resize.observe(node);
    const onRange = (next: LogicalRange | null) => {
      if (changing.current || !next || !installedBars.current.length) return;
      const rows = installedBars.current;
      const width = next.to - next.from;
      const max = Math.min(500, rows.length);
      const min = Math.min(30, rows.length);
      const previous = range.current;
      const previousWidth = previous ? previous.to - previous.from : null;
      // 引擎可能延后通知代码设置的同一范围，不把它当作用户操作。
      if (previous && Math.abs(previous.from - next.from) < 0.01 && Math.abs(previous.to - next.to) < 0.01) {
        scheduleRead();
        return;
      }
      // Native wheel/pinch zoom also shifts the range around the pointer.
      // At a zoom boundary reject the entire change, including its date shift.
      if (previous && previousWidth !== null &&
        ((previousWidth >= max - 0.01 && width > previousWidth + 0.01) ||
          (previousWidth <= min + 0.01 && width < previousWidth - 0.01))) {
        changing.current = true;
        setView(previous);
        changing.current = false;
        return;
      }
      // Clamp against the loaded left edge without the engine's one-bar resize
      // correction when a modal changes the available scrollbar width.
      defaultMax.current = false;
      if (next.from < -0.5) {
        setView({
          from: -0.5,
          to: -0.5 + Math.min(500, rows.length, next.to - next.from),
        });
        return;
      }
      if (!changing.current) {
        if (width > max + 0.01 || width < min - 0.01) {
          changing.current = true;
          setView({
            from: next.to - Math.max(min, Math.min(max, width)),
            to: next.to,
          });
          changing.current = false;
          return;
        }
      }
      range.current = next;
      const cache = props.current.session.cache.current;
      if (cache)
        cache.view = {
          first: rows[0].date,
          from: next.from,
          to: next.to,
          interval: props.current.session.interval,
          defaultMax: defaultMax.current,
        };
      scheduleRead();
      const warmup = Math.max(1, ...props.current.session.periods) - 1;
      if (next.from < 20 + warmup) props.current.onHistory();
    };
    const onCrosshair = (event: MouseEventParams) => {
      const value = event.seriesData.get(candle.current!);
      if (event.point && value?.time && typeof value.time === "object") {
        const { year, month, day } = value.time;
        hovered.current = `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
      } else hovered.current = null;
      scheduleRead();
    };
    instance.timeScale().subscribeVisibleLogicalRangeChange(onRange);
    const onClick = (event: MouseEventParams) => {
      if (!event.point) return;
      const index = instance.timeScale().coordinateToLogical(event.point.x);
      if (index === null) return;
      const bar = props.current.bars[Math.round(index)];
      if (!bar) return;
      hovered.current = bar.date;
      instance.setCrosshairPosition(
        bar.close,
        businessDay(bar.date),
        candle.current!,
      );
      scheduleRead();
    };
    instance.subscribeCrosshairMove(onCrosshair);
    instance.subscribeClick(onClick);
    return () => {
      cancelAnimationFrame(frame.current);
      resize.disconnect();
      theme.disconnect();
      instance.unsubscribeCrosshairMove(onCrosshair);
      instance.unsubscribeClick(onClick);
      instance.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
      instance.remove();
      chart.current = null;
      lines.current.clear();
      installedBars.current = [];
    };
  }, []);
  useEffect(() => {
    const instance = chart.current;
    if (!instance || !bars.length) return;
    const old = installedBars.current;
    const previous =
      range.current ?? instance.timeScale().getVisibleLogicalRange();
    const saved = session.cache.current?.view;
    changing.current = true;
    candle.current!.setData(
      bars.map((b) => ({ ...b, time: businessDay(b.date) })),
    );
    volume.current!.setData(
      bars.map((b) => ({
        time: businessDay(b.date),
        value: b.volumeLots,
        color: b.close >= b.open ? "#ef4444" : "#10b981",
      })),
    );
    PERIODS.forEach((p) =>
      lines.current
        .get(p)!
        .setData(
          averages
            .get(p)!
            .map((v) => ({ time: businessDay(v.date), value: v.value })),
        ),
    );
    installedBars.current = bars;
    const sameInterval = installedInterval.current === session.interval;
    if (!sameInterval) defaultMax.current = true;
    const reference = sameInterval && old.length && previous
      ? { first: old[0].date, ...previous }
      : saved?.interval === session.interval ? saved : null;
    installedInterval.current = session.interval;
    const index = reference
      ? bars.findIndex((b) => b.date === reference.first)
      : -1;
    if (defaultMax.current) {
      // 历史分页到达时仍处于默认视图，就扩展到该周期最多根数。
      setView({ from: bars.length - Math.min(500, bars.length) - 0.5, to: bars.length - 0.5 });
    } else if (reference && index >= 0) {
      const to = reference.to + index;
      const width = Math.max(Math.min(30, bars.length), Math.min(500, bars.length, reference.to - reference.from));
      setView({ from: to - width, to });
    } else if (sameInterval && old.length && previous) {
      const oldDate =
        old[Math.max(0, Math.min(old.length - 1, Math.floor(previous.to)))]
          .date;
      const right = bars.findIndex((b) => b.date >= oldDate);
      const to = right < 0 ? bars.length - 1 : right;
      setView({
        from: to - Math.min(bars.length, previous.to - previous.from),
        to,
      });
    } else
      setView({
        from: Math.max(0, bars.length - 500) - 0.5,
        to: bars.length - 0.5,
      });
    changing.current = false;
    scheduleRead();
  }, [bars, averages, session.interval]);
  useEffect(() => {
    PERIODS.forEach((p) =>
      lines.current
        .get(p)
        ?.applyOptions({ visible: session.periods.includes(p) }),
    );
    const r = chart.current?.timeScale().getVisibleLogicalRange();
    if (r && r.from < 20 + Math.max(1, ...session.periods) - 1) onHistory();
  }, [session.periods, onHistory]);
  const move = (count: number | null, latest = false) => {
    const instance = chart.current;
    const r = instance?.timeScale().getVisibleLogicalRange();
    if (!instance || !r) return;
    const width = Math.min(
      bars.length,
      Math.max(
        Math.min(30, bars.length),
        Math.min(500, count ?? r.to - r.from),
      ),
    );
    const to = latest ? bars.length - 0.5 : r.to;
    defaultMax.current = false;
    if (Math.abs(width - (r.to - r.from)) < 0.01 && Math.abs(to - r.to) < 0.01) return;
    changing.current = true;
    setView({ from: to - width, to });
    changing.current = false;
  };
  return (
    <div
      data-testid={bars.length ? "candle-chart" : "chart-placeholder"}
      className="min-w-0"
    >
      <div
        className="flex flex-wrap items-center gap-2 py-2"
        role="group"
        aria-label="图表范围与均线"
      >
        {(Object.entries(INTERVALS) as [ChartInterval, string][]).map(([interval, label]) => (
          <Button
            disabled={!bars.length}
            key={interval}
            size="sm"
            variant={session.interval === interval ? "secondary" : "ghost"}
            aria-pressed={session.interval === interval}
            onClick={() => session.setInterval(interval)}
          >
            {label}
          </Button>
        ))}
        <Button
          disabled={!bars.length || viewWidth <= Math.min(30, bars.length) + 0.01}
          size="sm"
          variant="ghost"
          aria-label="放大图表"
          onClick={() => {
            move(
              (range.current ? range.current.to - range.current.from : 250) *
                0.8,
            );
          }}
        >
          ＋
        </Button>
        <Button
          disabled={!bars.length || viewWidth >= Math.min(500, bars.length) - 0.01}
          size="sm"
          variant="ghost"
          aria-label="缩小图表"
          onClick={() => {
            move(
              (range.current ? range.current.to - range.current.from : 250) *
                1.25,
            );
          }}
        >
          －
        </Button>
        <Button
          disabled={!bars.length}
          size="sm"
          variant="ghost"
          onClick={() => move(null, true)}
        >
          回到最新
        </Button>
        {PERIODS.map((p) => (
          <Button
            disabled={!bars.length}
            key={p}
            size="sm"
            variant="ghost"
            aria-pressed={session.periods.includes(p)}
            onClick={() => session.toggle(p)}
          >
            MA{p}
          </Button>
        ))}
      </div>
      <div
        data-testid="chart-readout"
        aria-live="off"
        className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 min-h-12 gap-x-4 gap-y-1 text-xs tabular-nums py-2"
      >
        {readBar ? (
          <>
            <span>日期 {readBar.date}</span>
            <span>开 {readBar.open.toFixed(2)}</span>
            <span>高 {readBar.high.toFixed(2)}</span>
            <span>低 {readBar.low.toFixed(2)}</span>
            <span>收 {readBar.close.toFixed(2)}</span>
            <span>量 {volumeText(readBar.volumeLots)} 手</span>
            {session.periods.map((p) => (
              <span key={p}>
                MA{p}{" "}
                {averages
                  .get(p)
                  ?.find((v) => v.date === readBar.date)
                  ?.value.toFixed(2) ?? "—"}
              </span>
            ))}
          </>
        ) : (
          <>
            <span>日期 —</span>
            <span>开 —</span>
            <span>高 —</span>
            <span>低 —</span>
            <span>收 —</span>
            <span>量 — 手</span>
            {session.periods.map((p) => (
              <span key={p}>MA{p} —</span>
            ))}
          </>
        )}
      </div>
      <div className="relative">
        <div
          ref={host}
          role="img"
          aria-label={ariaLabel}
          style={{ height }}
          className="w-full min-w-0"
        />
        {!bars.length && (
          <div className="absolute inset-0 flex items-center justify-center bg-surface p-6 text-sm text-foreground/65">
            {placeholder}
          </div>
        )}
      </div>
      <p
        data-testid="chart-visible-range"
        className="text-xs text-foreground/60 tabular-nums py-1"
      >
        可见 {visible.first} — {visible.last} · {visible.count} 根
      </p>
      <a
        className="text-xs text-primary underline"
        href="https://www.tradingview.com/"
        target="_blank"
        rel="noreferrer"
      >
        TradingView Lightweight Charts™ · © 2025 TradingView, Inc.
      </a>
    </div>
  );
}
