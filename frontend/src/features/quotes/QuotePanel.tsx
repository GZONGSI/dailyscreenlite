import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import {
  api,
  ApiError,
  type QuoteSeries,
  type DailyBar,
} from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { CandleChart } from "./CandleChart";
import { INTERVALS, normalizeBars } from "./series";
import { useChartSession } from "./ChartSession";
interface Props {
  securityId: string;
  /** 面板内 aria 标识用的唯一后缀：同一股票在卡片与详情各有一份面板。 */
  panelId: string;
  compact?: boolean;
  refreshToken?: number;
}
const EMPTY: DailyBar[] = [];
const INITIAL = 309;
const PAGE = 250;
const errorText = (error: unknown) =>
  error instanceof ApiError ? error.message : "读取行情失败，请重试";

/** HTTP/version boundary. Only complete, single-version snapshots reach the chart. */
export function QuotePanel({
  securityId,
  panelId,
  compact = false,
  refreshToken = 0,
}: Props) {
  const { cache, interval } = useChartSession();
  const [series, setSeries] = useState<QuoteSeries | null>(() => {
    if (cache.current?.securityId !== securityId)
      cache.current = {
        securityId,
        series: null,
        view: null,
        refreshToken,
        historyError: null,
        exhausted: false,
        historyFlight: null,
        generation: 0,
      };
    return cache.current.series;
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(
    cache.current?.historyError ?? null,
  );
  const [paging, setPaging] = useState(Boolean(cache.current?.historyFlight));
  const [attempt, setAttempt] = useState(0);
  // 单股手动更新：只更新这一只，不把它加入持续更新范围，也不改变处理状态
  const [updating, setUpdating] = useState(false);
  const [updateNote, setUpdateNote] = useState<string | null>(null);
  const state = useRef({
    series,
    loading: false,
    historyError: Boolean(cache.current?.historyError),
    exhausted: cache.current?.exhausted ?? false,
  });
  state.current.series = series;
  const epoch = useRef(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      epoch.current++;
    };
  }, []);
  const commit = useCallback(
    (result: QuoteSeries) => {
      const next = { ...result, bars: normalizeBars(result.bars) };
      state.current.series = next;
      state.current.exhausted = next.bars.length >= next.total;
      if (alive.current) setSeries(next);
      if (cache.current?.securityId === securityId) {
        cache.current.series = next;
        cache.current.refreshToken = refreshToken;
        cache.current.exhausted = state.current.exhausted;
      }
    },
    [cache, securityId, refreshToken],
  );
  const readConsistent = useCallback(
    async (earliest?: string) => {
      let next = await api.quotes(securityId, { limit: INITIAL });
      next = { ...next, bars: normalizeBars(next.bars) };
      // Refresh the loaded extent atomically, preserving old viewport dates where possible.
      while (
        earliest &&
        next.available &&
        next.bars.length &&
        next.bars[0].date > earliest &&
        next.bars.length < next.total
      ) {
        const page = await api.quotes(securityId, {
          limit: PAGE,
          end: next.bars[0].date,
        });
        if (page.fetchedAt !== next.fetchedAt)
          throw new Error("行情版本再次变化，请重试");
        const merged = normalizeBars([...page.bars, ...next.bars]);
        if (merged.length === next.bars.length) break;
        next = { ...next, bars: merged, earliestDate: merged[0].date };
      }
      return next;
    },
    [securityId],
  );
  useEffect(() => {
    if (
      attempt === 0 &&
      cache.current?.series?.available &&
      cache.current.refreshToken === refreshToken
    ) {
      // 缓存命中同样结束读取，不能遗留被新一轮刷新取代的 loading 状态。
      state.current.loading = false;
      setLoading(false);
      return;
    }
    const token = ++epoch.current;
    if (cache.current) cache.current.generation++;
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    state.current.loading = true;
    setLoading(true);
    setError(null);
    const load = async (retry: number) => {
      try {
        const next = await readConsistent(state.current.series?.bars[0]?.date);
        if (!active || token !== epoch.current) return;
        commit(next);
        state.current.historyError = false;
        setHistoryError(null);
        if (cache.current) cache.current.historyError = null;
        if (!next.available && retry < 5)
          timer = setTimeout(() => void load(retry + 1), 3000);
      } catch (err) {
        if (active && token === epoch.current) setError(errorText(err));
      } finally {
        if (active && token === epoch.current) {
          state.current.loading = false;
          setLoading(false);
        }
      }
    };
    void load(0);
    return () => {
      active = false;
      clearTimeout(timer);
    };
  }, [securityId, refreshToken, attempt, cache, commit, readConsistent]);
  // The in-flight history request and pause state belong to the stock session,
  // so switching between its card and notes never starts a duplicate request.
  useEffect(() => {
    const entry = cache.current;
    if (!entry?.historyFlight) return;
    let active = true;
    void entry.historyFlight.then(() => {
      if (!active || cache.current !== entry) return;
      setSeries(entry.series);
      setHistoryError(entry.historyError);
      setPaging(false);
      state.current.historyError = Boolean(entry.historyError);
      state.current.exhausted = entry.exhausted;
    });
    return () => {
      active = false;
    };
  }, [cache]);
  const loadEarlier = useCallback(
    (manual = false) => {
      const entry = cache.current;
      const current = entry?.series;
      if (
        !alive.current ||
        !entry ||
        !current?.available ||
        !current.bars.length ||
        entry.historyFlight ||
        state.current.loading ||
        entry.exhausted ||
        (entry.historyError && !manual)
      )
        return;
      setPaging(true);
      const generation = entry.generation;
      const valid = () =>
        cache.current === entry && entry.generation === generation;
      const operation = (async () => {
        try {
          const older = await api.quotes(securityId, {
            limit: PAGE,
            end: current.bars[0].date,
          });
          if (!valid()) return;
          if (older.fetchedAt !== current.fetchedAt) {
            const fresh = await readConsistent(current.bars[0].date);
            if (!valid()) return;
            // 当前版本没有变化时重读同一首屏无法解决历史版本冲突，暂停自动加载。
            if (fresh.fetchedAt === current.fetchedAt) {
              throw new Error("历史行情版本与当前不一致，请重试");
            }
            commit(fresh);
          } else {
            const merged = normalizeBars([...older.bars, ...current.bars]);
            commit({
              ...current,
              bars: merged,
              earliestDate: merged[0]?.date ?? current.earliestDate,
            });
            if (merged.length === current.bars.length) {
              entry.exhausted = true;
              state.current.exhausted = true;
            }
          }
          entry.historyError = null;
          state.current.historyError = false;
          if (alive.current) setHistoryError(null);
        } catch (err) {
          if (valid()) {
            entry.historyError = errorText(err);
            state.current.historyError = true;
            if (alive.current) setHistoryError(entry.historyError);
          }
        } finally {
          entry.historyFlight = null;
          if (alive.current) setPaging(false);
        }
      })();
      entry.historyFlight = operation;
    },
    [cache, securityId, commit, readConsistent],
  );
  const autoHistory = useCallback(() => {
    void loadEarlier();
  }, [loadEarlier]);
  // 周/月 K 线及均线须有完整周期数据，沿既有版本校验边界补齐日线历史。
  useEffect(() => {
    if (interval !== "day" && series?.available && series.bars.length < series.total &&
      !loading && !paging && !historyError) autoHistory();
  }, [interval, series, loading, paging, historyError, autoHistory]);
  /**
   * 单股更新：向服务端只取这一只的行情。
   *
   * 用于历史股票（不在持续更新范围）先看缓存、需要时再补一次；
   * 不改变候选处理状态，也不影响更新范围。
   */
  const refreshSingle = useCallback(async () => {
    setUpdating(true);
    setUpdateNote(null);
    try {
      const result = await api.refreshSecurity(securityId);
      setUpdateNote(
        result.available
          ? `单股更新完成，最新行情 ${result.latestDate}`
          : "单股更新完成，来源暂未取得该股票行情",
      );
      setAttempt((value) => value + 1);
    } catch (err) {
      setUpdateNote(errorText(err));
    } finally {
      setUpdating(false);
    }
  }, [securityId]);
  return (
    <section
      aria-labelledby={`quote-title-${panelId}`}
      data-testid="quote-panel"
      className="rounded-xl border border-border bg-surface p-4 min-w-0"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 id={`quote-title-${panelId}`} className="text-sm font-medium">
          {INTERVALS[interval]}行情
        </h4>
        <div className="flex flex-wrap items-center gap-2">
          {series?.available && (
            <>
              <Badge tone="info">{series.adjustLabel}</Badge>
              <Badge tone="neutral">最新 {series.latestDate}</Badge>
              <Badge tone="neutral">
                已加载 {series.bars.length}/{series.total} 日
              </Badge>
            </>
          )}
          <Button
            size="sm"
            variant="ghost"
            data-testid="single-quote-update"
            disabled={updating || loading}
            onClick={() => void refreshSingle()}
          >
            {updating ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            单股更新
          </Button>
        </div>
      </div>
      {updateNote ? (
        <p role="status" className="text-xs text-foreground/60">
          {updateNote}
        </p>
      ) : null}
      {(loading || paging) && (
        <p role="status" className="flex gap-2 text-xs text-foreground/60">
          <Loader2 className="h-4 w-4 animate-spin" />
          {paging
            ? "正在加载更早行情…"
            : series?.available
              ? "正在更新行情…"
              : "正在读取行情…"}
        </p>
      )}
      {error && (
        <div role="alert" className="text-sm text-danger">
          {error}
          {series?.available ? "；已保留上次行情。" : ""}
          <Button
            size="sm"
            variant="ghost"
            onClick={() => setAttempt((v) => v + 1)}
          >
            重试行情
          </Button>
        </div>
      )}
      {historyError && (
        <div role="alert" className="text-sm text-danger">
          {historyError}；已保留原图，自动加载已暂停。
          <Button
            size="sm"
            variant="ghost"
            onClick={() => void loadEarlier(true)}
          >
            重试历史
          </Button>
        </div>
      )}
      <CandleChart
        bars={series?.available ? series.bars : EMPTY}
        completeHistory={series?.bars.length === series?.total}
        height={compact ? 360 : 440}
        ariaLabel={`${securityId} ${INTERVALS[interval]} K 线与成交量，${series?.adjustLabel ?? "口径未知"}`}
        onHistory={autoHistory}
        placeholder={
          loading
            ? "正在读取行情…"
            : `${series?.reason ?? "行情暂未取得"}；导入、候选归类与笔记不受影响。`
        }
      />
      {series?.available && (
        <div className="flex flex-wrap justify-between items-center gap-2 text-xs text-foreground/60">
          <span>
            价格单位：元 · 成交量单位：手 · {series.adjustLabel} · 来源{" "}
            {series.source ?? "未知"}
          </span>
          {!state.current.exhausted && (
            <Button
              size="sm"
              variant="ghost"
              disabled={paging || loading}
              onClick={() => void loadEarlier(true)}
            >
              看更早
            </Button>
          )}
        </div>
      )}
    </section>
  );
}
