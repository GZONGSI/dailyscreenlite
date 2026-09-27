import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2, RotateCcw, Search } from "lucide-react";

import type { Candidate } from "../../api/classification";
import { api, ApiError } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Input } from "../../components/ui/Input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "../../components/ui/Popover";
import { isUnprocessed, itemStateLabel } from "../classification/state";

interface Props {
  /** 打开候选卡后进入候选归类模块并重读浏览结果。 */
  onJump: () => void;
  /** 已观察且没有待归类对象：打开该股票的共用详情。 */
  onOpenDetail: (securityId: string) => void;
}

/**
 * 全局搜索：只覆盖经过导入与证券识别的股票。
 *
 * 打开搜索结果即手动打开那张候选卡：作为浏览路径的新一步，不改变当前筛选、
 * 也不重排未处理池。命中已观察且没有待归类对象的股票时打开个股详情；只有暂不
 * 关注或清理记录时按原处理状态打开卡片，用户主动选择「重新归类」才恢复待处理。
 * 搜索本身不创建待归类对象。
 */
export function GlobalSearch({ onJump, onOpenDetail }: Props) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState<Candidate[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  useEffect(() => {
    const term = query.trim();
    if (!term) {
      setResults(null);
      setError(null);
      return;
    }
    const token = ++seq.current;
    const timer = window.setTimeout(() => {
      void api
        .searchCandidates(term)
        .then((payload) => {
          if (token !== seq.current) return;
          setResults(payload.candidates);
          setError(null);
        })
        .catch(() => {
          if (token !== seq.current) return;
          setResults([]);
          setError("搜索失败，请重试");
        });
    }, 200);
    return () => window.clearTimeout(timer);
  }, [query]);

  const close = () => {
    setOpen(false);
    setQuery("");
    setResults(null);
  };

  /**
   * 打开某只已导入股票的候选卡：作为浏览路径的新一步进入归类工作区。
   *
   * 不改变当前筛选、也不重排未处理池，因此从搜索结果进来不会打断原来的工作范围；
   * 目标属于已处理状态时按原处理状态打开卡片，只有主动点「重新归类」才恢复待处理。
   */
  const jump = async (candidate: Candidate) => {
    setBusy(true);
    setError(null);
    try {
      await api.focusCandidate(candidate.candidateId);
      onJump();
      close();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "跳转失败，请重试");
    } finally {
      setBusy(false);
    }
  };

  /** 主动重新归类：先恢复待处理，再打开卡片进入归类。 */
  const reclassifyAndJump = async (candidate: Candidate) => {
    setBusy(true);
    setError(null);
    try {
      await api.reclassifyCandidate(candidate.candidateId);
      await jump(candidate);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "重新归类失败，请重试");
      setBusy(false);
    }
  };

  /** 打开已观察股票的共用详情：不创建待归类对象。 */
  const openDetail = (securityId: string) => {
    onOpenDetail(securityId);
    close();
  };

  const hint = useMemo(() => {
    if (!query.trim()) return "输入代码或名称后选择结果";
    if (results === null) return "正在搜索已导入股票…";
    if (results.length === 0) return "没有匹配的已导入股票";
    return `匹配 ${results.length} 只已导入股票`;
  }, [query, results]);

  return (
    <div className="flex items-center gap-1 rounded-lg border border-border px-2">
      <Search className="h-4 w-4 shrink-0 text-foreground/50" aria-hidden="true" />
      <Input
        type="search"
        aria-label="搜索已导入股票"
        placeholder="搜索已导入股票"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        className="w-40 border-0 bg-transparent py-1.5 text-sm outline-none sm:w-52"
      />
      {busy ? (
        <Loader2 className="h-4 w-4 animate-spin text-foreground/50" aria-hidden="true" />
      ) : null}
      <Popover open={open && Boolean(query.trim())} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <span className="sr-only">搜索结果</span>
        </PopoverTrigger>
        <PopoverContent aria-label="搜索结果" className="w-80">
          <p className="text-xs text-foreground/60" aria-live="polite">
            {hint}
          </p>
          {error ? (
            <p role="alert" className="mt-2 text-sm text-danger">
              {error}
            </p>
          ) : null}
          {results && results.length > 0 ? (
            <ul className="mt-2 space-y-1" aria-label="搜索结果列表">
              {results.map((candidate) => {
                const unprocessed = isUnprocessed(candidate.state);
                return (
                  <li key={candidate.candidateId}>
                    <div className="flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-muted">
                      {unprocessed ? (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void jump(candidate)}
                          className="min-w-0 flex-1 text-left text-sm"
                        >
                          <ResultName candidate={candidate} />
                        </button>
                      ) : candidate.observed ? (
                        // 已观察且没有待归类对象：打开个股详情，不自动创建待归类对象
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => openDetail(candidate.securityId)}
                          className="min-w-0 flex-1 text-left text-sm"
                        >
                          <ResultName candidate={candidate} />
                        </button>
                      ) : (
                        // 仅暂不关注/清理记录：保留原状态打开卡片，主动重新归类才恢复待处理
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => void jump(candidate)}
                          className="min-w-0 flex-1 text-left text-sm"
                        >
                          <ResultName candidate={candidate} />
                        </button>
                      )}
                      <Badge tone={unprocessed ? "warning" : "neutral"}>
                        {itemStateLabel(candidate)}
                      </Badge>
                      {!unprocessed && !candidate.observed ? (
                        <>
                          {/* 保留原处理状态打开卡片：打开卡片不等于进入归类 */}
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => void jump(candidate)}
                          >
                            查看卡片
                          </Button>
                          <Button
                            size="sm"
                            variant="ghost"
                            disabled={busy}
                            onClick={() => void reclassifyAndJump(candidate)}
                          >
                            <RotateCcw className="h-3.5 w-3.5" aria-hidden="true" />
                            重新归类
                          </Button>
                        </>
                      ) : null}
                    </div>
                  </li>
                );
              })}
            </ul>
          ) : null}
          <p className="mt-2 text-xs text-foreground/50">
            仅搜索已成功导入并识别的股票，不覆盖全市场名单。
          </p>
        </PopoverContent>
      </Popover>
    </div>
  );
}

/** 一条搜索结果的股票身份：名称 + 代码。 */
function ResultName({ candidate }: { candidate: Candidate }) {
  return (
    <>
      <span className="block truncate font-medium">
        {candidate.security?.name || candidate.securityId}
      </span>
      <span className="block font-mono text-xs text-foreground/60">
        {candidate.security?.code ?? ""}
      </span>
    </>
  );
}
