import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type {
  Candidate,
  CandidateRow,
  ClassificationBrowse,
  ClassificationCommand,
  ClassificationDelta,
  ClassificationViewChanges,
  WrittenCandidate,
} from "../../api/classification";
import { api, ApiError, type QuoteSnapshot } from "../../api/client";
import type { ObservationGroup } from "../../api/observations";
import { loadSourceNames } from "../import/sources";
import { groupNamesFor } from "../observations/groups";
import { isUnprocessed } from "./state";

/**
 * 候选归类的浏览与写入编排，从工作区组件里独立出来。
 *
 * 组件只负责画什么、点了谁；这里负责服务端事实的读取、增量应用与写入次序：
 * - 统一浏览结果（当前卡＋完整轻量列表＋数量＋列表版本）只在打开、改筛选、返回队列
 *   或版本不一致时读一次，普通切卡只应用服务端给出的增量；
 * - `listRevision` 是本地列表对应的服务端版本，`navigationRevision` 是推进用的修订号，
 *   两者都由服务端响应驱动，前端不自行复制列表成员规则；
 * - 写入串行（`persistChanges`）、读取按代际作废（`beginLists`／`isCurrent`），
 *   保存后的推进失败只重试导航，不重复归类。
 *
 * 归类规则本身（一只股票一个候选项、每日入选、单向观察联动）在后端；这里不重复表达。
 */

/** 卡片详细资料 → 列表轻量行：本地立即反映服务端已确认的写入结果。 */
function rowFrom(candidate: Candidate): CandidateRow {
  return {
    candidateId: candidate.candidateId,
    securityId: candidate.securityId,
    state: candidate.state,
    viewedAt: candidate.viewedAt,
    latestImportDate: candidate.latestImportDate,
    sourceCount: candidate.sources.length,
    observed: candidate.observed,
    security: candidate.security,
  };
}

/**
 * 写入响应的行序：只有归类动作会改变队列顺序（「稍后处理」移尾），
 * 观察联动结果按契约没有这个字段，也不该被当成空行序之外的任何含义。
 */
function writtenOrder(candidate: WrittenCandidate): string[] {
  return "listOrder" in candidate ? candidate.listOrder : [];
}

export interface ClassificationWorkspaceModel {
  view: ClassificationBrowse | null;
  rows: CandidateRow[];
  pending: CandidateRow[];
  dates: string[];
  groups: ObservationGroup[];
  quotes: Map<string, QuoteSnapshot>;
  batches: Map<string, string>;
  loading: boolean;
  listLoading: boolean;
  loadError: string | null;
  savedRefreshError: boolean;
  busyId: string | null;
  navigationBusy: boolean;
  navigationError: string | null;
  currentId: string | null;
  current: Candidate | null;
  currentIndex: number;
  currentGroupNames: string[];
  counts: ClassificationBrowse["summary"];
  reload: () => void;
  changeFilters: (changes: ClassificationViewChanges) => Promise<void>;
  selectCandidate: (candidateId: string) => Promise<boolean>;
  onClassified: (updated: WrittenCandidate) => Promise<void>;
  onRelationsChanged: (updated?: WrittenCandidate) => Promise<void>;
  onNotesChanged: () => Promise<void>;
  reclassify: (candidateId: string, preservePath?: boolean) => Promise<void>;
  cleanup: () => Promise<void>;
  move: (direction: "previous" | "next") => Promise<void>;
  returnToQueue: () => Promise<void>;
}

const EMPTY_COUNTS = {
  unprocessed: 0,
  processed: 0,
  pending: 0,
  later: 0,
  dismissed: 0,
  observed: 0,
  cleared: 0,
};

export function useClassificationWorkspace(refreshToken: number): ClassificationWorkspaceModel {
  const [view, setView] = useState<ClassificationBrowse | null>(null);
  const [rows, setRows] = useState<CandidateRow[]>([]);
  const [pending, setPending] = useState<CandidateRow[]>([]);
  const [dates, setDates] = useState<string[]>([]);
  const [groups, setGroups] = useState<ObservationGroup[]>([]);
  const [quotes, setQuotes] = useState<Map<string, QuoteSnapshot>>(new Map());
  const [batches, setBatches] = useState<Map<string, string>>(new Map());
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [loading, setLoading] = useState(true);
  const [listLoading, setListLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savedRefreshError, setSavedRefreshError] = useState(false);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [navigationBusy, setNavigationBusy] = useState(false);
  const [navigationError, setNavigationError] = useState<string | null>(null);
  const listSeq = useRef(0);
  const writes = useRef<Promise<unknown>>(Promise.resolve());
  const selectionSeq = useRef(0);
  const navigationFlight = useRef(false);
  const resumeAfterSaveFlight = useRef(false);
  /** 最近一次服务端浏览状态：归类后推进读取游标与修订号，避免为它重读整份结果。 */
  const viewRef = useRef<ClassificationBrowse | ClassificationCommand | null>(null);
  const navigationRevision = useRef(0);
  /** 本地列表对应的服务端版本：与响应版本不一致说明列表已过期。 */
  const listRevision = useRef(0);
  /** 最近一次列表内容：切卡后补行情摘要时读取，避免把列表塞进依赖。 */
  const rowsRef = useRef<CandidateRow[]>([]);
  const pendingRef = useRef<CandidateRow[]>([]);
  /** 当前显示的页签：重读列表时显式回传，避免被归类动作的临时页签切换带走。 */
  const displayScope = useRef<"unprocessed" | "processed">("unprocessed");
  const pendingNavigation = useRef<{
    direction: "previous" | "next";
    expectedCursor: number | null;
    expectedRevision: number | null;
    afterSave: boolean;
  } | null>(null);

  const persistChanges = useCallback((changes: ClassificationViewChanges) => {
    const request = writes.current.then(() => api.updateClassificationView(changes));
    writes.current = request.catch(() => undefined);
    return request;
  }, []);

  const beginLists = useCallback(() => {
    listSeq.current += 1;
    return listSeq.current;
  }, []);
  const isCurrent = useCallback((token: number) => token === listSeq.current, []);

  const setListRows = useCallback((next: CandidateRow[], nextPending: CandidateRow[]) => {
    rowsRef.current = next;
    pendingRef.current = nextPending;
    setRows(next);
    setPending(nextPending);
  }, []);

  const applyRows = useCallback(
    (update: (items: CandidateRow[]) => CandidateRow[]) => {
      setListRows(update(rowsRef.current), update(pendingRef.current));
    },
    [setListRows],
  );

  /** 应用一次完整浏览结果（打开工作区、改筛选、返回队列、版本不一致时重读）。 */
  const applyBrowse = useCallback(
    (browse: ClassificationBrowse) => {
      displayScope.current = browse.scope;
      setListRows(browse.rows, browse.pending);
      setDates(browse.dates);
      listRevision.current = browse.listRevision;
      viewRef.current = browse;
      navigationRevision.current = browse.navigationRevision;
      setView(browse);
    },
    [setListRows],
  );

  /**
   * 应用服务端给出的列表增量：只改变化的行，不重传也不重建整份列表。
   * 服务端给了新行序时（「稍后处理」移尾）按它重排已有行，不自行推导队列规则。
   */
  const applyDelta = useCallback(
    (changes: ClassificationDelta) => {
      const update = (items: CandidateRow[]) => {
        let next = items;
        if (changes.order?.length) {
          const byId = new Map(items.map((item) => [item.candidateId, item]));
          const ordered = changes.order
            .map((candidateId) => byId.get(candidateId))
            .filter((item): item is CandidateRow => item !== undefined);
          // 行序只覆盖当前列表里的行：本地缺的行由版本不一致时的重读补齐
          if (ordered.length === items.length) next = ordered;
        }
        return next
          .filter((item) => !changes.removed.includes(item.candidateId))
          .map(
            (item) => changes.changed.find((row) => row.candidateId === item.candidateId) ?? item,
          );
      };
      applyRows(update);
    },
    [applyRows],
  );

  /** 队列列表与当前股票的轻量行情摘要；只读取最近两个交易日。 */
  const loadQuotes = useCallback(async (candidates: CandidateRow[], current?: Candidate | null) => {
    const ids = new Set(candidates.map((c) => c.securityId));
    if (current) ids.add(current.securityId);
    if (!ids.size) {
      setQuotes(new Map());
      return;
    }
    try {
      const { quotes: list } = await api.quoteSummary([...ids]);
      setQuotes(new Map(list.map((quote) => [quote.securityId, quote])));
    } catch {
      // 行情读取失败只影响列表上的价格显示，不阻塞归类
      setQuotes(new Map());
    }
  }, []);

  // 来源名称读取失败时卡片显示「未知来源」，不影响归类
  const loadBatchNames = useCallback(async () => {
    setBatches(await loadSourceNames());
  }, []);

  /**
   * 打开工作区时确认当前卡与左侧列表属于同一份结果：
   * 当前股票仍在结果里就保留（即使已被处理）；被排除则由服务端的筛选落位规则接管；
   * 完全没有当前股票（重启后首次进入）时落到当前范围首项，避免出现空卡。
   *
   * 只有确实需要落位时才补一次写命令，因此正常打开工作区不产生额外的列表读取。
   */
  const ensureCurrent = useCallback(async (browse: ClassificationBrowse) => {
    if (browse.ended) return browse;
    if (browse.path.length && !browse.currentCandidate) {
      // 旧路径的当前候选项已不存在：沿原路径跳过失效项，不重置浏览位置。
      const command = await api.navigateClassification(
        "next",
        browse.cursor,
        browse.navigationRevision,
      );
      if (command.listRevision !== browse.listRevision) {
        return api.getClassificationBrowse(displayScope.current);
      }
      return { ...command, rows: browse.rows, pending: browse.pending, dates: browse.dates };
    }
    if (browse.currentCandidateId) {
      return browse;
    }
    // 只在当前展示结果里落位：结果为空就是空状态（例如「已处理」筛选下还没有任何
    // 已处理项）。「待归类池」不是本页签的结果，拿它兜底会在这种情况下悄悄改选一只
    // 未处理的候选，用户看到的页签与卡片就不再是同一份结果。
    const target = browse.rows[0]?.candidateId ?? null;
    if (target === null) return browse;
    return api.updateClassificationView({ currentCandidateId: target });
  }, []);

  // 首次加载或用户重试：恢复当前股票。
  useEffect(() => {
    setLoading(true);
    setLoadError(null);
    let active = true;
    const token = beginLists();
    (async () => {
      try {
        const [browse, groupList] = await Promise.all([
          api.getClassificationBrowse(),
          api.listObservationGroups(),
        ]);
        if (!active || !isCurrent(token)) return;
        setGroups(groupList.groups);
        applyBrowse(browse);
        void loadBatchNames();
        // 恢复该模块上次工作位置：已处理股票只要还是当前股票就仍然呈现，
        // 只有完全无当前股票时才落到队首
        const restored = await ensureCurrent(browse);
        if (!active || !isCurrent(token)) return;
        if (restored !== browse) applyBrowse(restored);
        void loadQuotes(
          [...restored.rows, ...restored.pending],
          restored.currentCandidate,
        );
      } catch (err) {
        if (active) {
          setLoadError(err instanceof ApiError ? err.message : "无法连接本机服务");
        }
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [
    applyBrowse,
    beginLists,
    ensureCurrent,
    isCurrent,
    loadBatchNames,
    loadQuotes,
    loadAttempt,
    refreshToken,
  ]);

  /**
   * 标签重新获得焦点或重新可见时重读浏览结果：新导入或其他入口造成的候选变化
   * 因此会出现，而当前卡与浏览历史保持不变（这里只是把 `loadAttempt` 递增，
   * 走与打开工作区同一条读取路径）。页面停留期间不轮询。
   */
  useEffect(() => {
    const reload = () => {
      if (document.visibilityState === "hidden") return;
      setLoadAttempt((value) => value + 1);
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") reload();
    };
    window.addEventListener("focus", reload);
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      window.removeEventListener("focus", reload);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  const selectCandidate = useCallback(
    async (candidateId: string) => {
      const token = ++selectionSeq.current;
      try {
        // 手动打开候选：作为浏览路径的新一步，历史中途跳转会替换前进分支。
        // 它不改筛选、不重排未处理池，因此返回上一项后仍在原来的工作范围。
        const updated = await api.focusCandidate(candidateId);
        if (token !== selectionSeq.current) return false;
        applyBrowse(updated);
        pendingNavigation.current = null;
        setNavigationError(null);
        return true;
      } catch (err) {
        if (token === selectionSeq.current) {
          setLoadError(err instanceof ApiError ? err.message : "股票读取失败，请重试");
        }
        return false;
      }
    },
    [applyBrowse],
  );

  const changeFilters = useCallback(
    async (changes: ClassificationViewChanges) => {
      const token = beginLists();
      setListLoading(true);
      setLoadError(null);
      try {
        // 结果筛选只属于「已处理」范围：切到待归类时清空，避免串范围
        if (changes.scope === "unprocessed" && changes.result === undefined) {
          changes = { ...changes, result: null };
        }
        const updated = await persistChanges(changes);
        if (!isCurrent(token)) return;
        pendingNavigation.current = null;
        setNavigationError(null);
        applyBrowse(updated);
        void loadQuotes([...updated.rows, ...updated.pending], updated.currentCandidate);
      } catch (err) {
        if (isCurrent(token)) {
          setLoadError(err instanceof ApiError ? err.message : "筛选切换失败，请重试");
        }
      } finally {
        if (isCurrent(token)) setListLoading(false);
      }
    },
    [applyBrowse, beginLists, isCurrent, loadQuotes, persistChanges],
  );

  /**
   * 观察关系动作后重读一致结果：关系变化会改变列表行的「已观察」标注与组成员数量。
   * 写入后的候选项先就地生效，使卡片在重读期间就显示真实状态。
   */
  const refreshAfterSave = useCallback(
    async (updated?: WrittenCandidate) => {
      if (updated) {
        // 观察联动动作与归类动作同一形状（都有版本与移除行），但只有归类动作会改变
        // 队列顺序，所以联动结果不下发行序，这里按契约区分，不擅自当成空行序下发。
        applyDelta({
          changed: [rowFrom(updated)],
          removed: updated.listRemoved,
          order: writtenOrder(updated),
        });
        setView((prev) =>
          prev?.currentCandidateId === updated.candidateId
            ? { ...prev, currentCandidate: updated }
            : prev,
        );
      }
      const token = beginLists();
      setSavedRefreshError(false);
      try {
        const [fresh, groupList] = await Promise.all([
          api.getClassificationBrowse(displayScope.current),
          api.listObservationGroups(),
        ]);
        if (!isCurrent(token)) return;
        applyBrowse(fresh);
        setGroups(groupList.groups);
        void loadQuotes([...fresh.rows, ...fresh.pending], fresh.currentCandidate);
      } catch {
        if (isCurrent(token)) setSavedRefreshError(true);
      }
    },
    [applyBrowse, applyDelta, beginLists, isCurrent, loadQuotes],
  );

  /**
   * 保存动作和导航是两个请求；导航失败后保留同一游标供重试。
   * `afterSave` 为真表示这次导航跟在一次已确认的归类保存之后：失败时提示「结果已保留」，
   * 保留的命令可以按同一修订号重试。
   */
  const navigate = useCallback(
    async (
      direction: "previous" | "next",
      expectedCursor: number,
      expectedRevision: number,
      afterSave = false,
    ) => {
      if (navigationFlight.current) return;
      navigationFlight.current = true;
      setNavigationBusy(true);
      setNavigationError(null);
      let command: ClassificationCommand;
      try {
        command = await api.navigateClassification(direction, expectedCursor, expectedRevision);
      } catch {
        // 请求超时可能已经在服务端生效；同一修订号重试只读回目标。
        pendingNavigation.current = { direction, expectedCursor, expectedRevision, afterSave };
        setNavigationError(afterSave ? "已保存，切换失败。归类结果已保留。" : "切换失败，请重试。");
        setNavigationBusy(false);
        navigationFlight.current = false;
        return;
      }
      pendingNavigation.current = null;
      const token = beginLists();
      try {
        // 版本一致说明本地列表仍然有效：只应用服务端给出的变化行。
        // 版本不一致说明列表成员或顺序变了，读回完整浏览结果。
        if (command.listRevision === listRevision.current) {
          applyDelta(command.changes);
        } else {
          const fresh = await api.getClassificationBrowse(displayScope.current);
          if (!isCurrent(token)) return;
          applyBrowse(fresh);
          void loadQuotes([...fresh.rows, ...fresh.pending], fresh.currentCandidate);
          setSavedRefreshError(false);
          return;
        }
        // 浏览状态（当前卡、游标、修订号、版本与统计）来自命令响应；
        // 列表已按增量更新，因此保留本地列表而不把它塞进浏览状态。
        const next: ClassificationBrowse = {
          ...command,
          rows: rowsRef.current,
          pending: pendingRef.current,
          dates,
        };
        viewRef.current = next;
        navigationRevision.current = command.navigationRevision;
        setView(next);
        void loadQuotes(
          [...rowsRef.current, ...pendingRef.current],
          command.currentCandidate,
        );
        setSavedRefreshError(false);
      } catch {
        if (isCurrent(token)) setSavedRefreshError(true);
      } finally {
        setNavigationBusy(false);
        navigationFlight.current = false;
      }
    },
    [applyBrowse, applyDelta, beginLists, dates, isCurrent, loadQuotes],
  );

  const move = useCallback(
    async (direction: "previous" | "next") => {
      if (!viewRef.current && !pendingNavigation.current) return;
      let retry = pendingNavigation.current;
      if (retry && retry.expectedRevision === null) {
        if (resumeAfterSaveFlight.current) return;
        resumeAfterSaveFlight.current = true;
        setNavigationBusy(true);
        try {
          const state = await api.getClassificationBrowse(displayScope.current);
          applyBrowse(state);
          retry = {
            ...retry,
            expectedCursor: state.cursor,
            expectedRevision: state.navigationRevision,
          };
          pendingNavigation.current = retry;
        } catch {
          setNavigationError("已保存，切换失败。归类结果已保留。");
          return;
        } finally {
          resumeAfterSaveFlight.current = false;
          setNavigationBusy(false);
        }
      }
      await navigate(
        retry?.direction ?? direction,
        retry?.expectedCursor ?? viewRef.current?.cursor ?? 0,
        retry?.expectedRevision ?? navigationRevision.current,
        retry?.afterSave ?? false,
      );
    },
    [applyBrowse, navigate],
  );

  const onClassified = useCallback(
    async (updated: WrittenCandidate) => {
      // 服务端写入响应带写入前后的列表版本、导航修订号与「本次移除的行」：本地据此
      // 更新列表并直接推进，不为一次归类重读整份浏览结果，也不自行复制状态规则。
      //
      // 但这次动作的增量只描述它自己改了什么。若另一个入口在我方列表快照之后改过
      // 列表（例如刚导入了新候选），`listRevisionBefore` 就与我手上的版本不同：
      // 增量补不齐那只新候选，此时**不能**把本地版本追到写入后——否则后续导航会
      // 误判「版本一致」，那只候选会被永久漏掉。这种情况读回完整结果再推进。
      const snapshotValid = updated.listRevisionBefore === listRevision.current;
      if (!snapshotValid) {
        // 先就地把这次「已保存」的真实结果落到卡片上：即使随后补齐列表失败，
        // 用户看到的也是保存后的状态，而不是一张还能再点一次的动作卡。
        setView((previous) =>
          previous?.currentCandidateId === updated.candidateId
            ? {
                ...previous,
                currentCandidate: updated,
                scope: isUnprocessed(updated.state) ? "unprocessed" : "processed",
              }
            : previous,
        );
        try {
          const fresh = await api.getClassificationBrowse(displayScope.current);
          applyBrowse(fresh);
          pendingNavigation.current = null;
          setNavigationError(null);
          await navigate("next", fresh.cursor, fresh.navigationRevision, true);
        } catch {
          // 补齐列表失败：归类已经保存成功，这里只欠一次导航。留一条「修订号未知」的
          // 待重试导航（`move` 里既有的 expectedRevision === null 分支会先重读再推进）；
          // 否则重试会带着旧修订号发出去，服务端原地返回，看起来像什么都没发生。
          pendingNavigation.current = {
            direction: "next",
            expectedCursor: viewRef.current?.cursor ?? 0,
            expectedRevision: null,
            afterSave: true,
          };
          setNavigationError("已保存，切换失败。归类结果已保留。");
        }
        return;
      }
      listRevision.current = updated.listRevision;
      // 动作响应与浏览命令响应是同一个增量形状：就地更新变化行，并在服务端给出
      // 新行序时重排（「稍后处理」把候选移到队尾）。
      applyDelta({
        changed: [rowFrom(updated)],
        removed: updated.listRemoved,
        order: writtenOrder(updated),
      });
      const responseRevision = updated.navigationRevision;
      setView((previous) =>
        previous?.currentCandidateId === updated.candidateId
          ? {
              ...previous,
              currentCandidate: updated,
              scope: isUnprocessed(updated.state) ? "unprocessed" : "processed",
            }
          : previous,
      );
      // 归类不移动浏览位置，因此游标仍是手上的这个；修订号就是这次写入响应里的那个
      // （动作契约必带），保存成功后的推进带着它发出。
      navigationRevision.current = responseRevision;
      await navigate("next", viewRef.current?.cursor ?? 0, responseRevision, true);
    },
    [applyBrowse, applyDelta, navigate],
  );

  const currentId = view?.currentCandidateId ?? null;

  /**
   * 笔记增删改后重读当前股票，使卡片「N 条」与详情一致。
   * 只更新当前股票、不重排队列也不切换当前：写笔记不夺取当前选择。
   */
  const onNotesChanged = useCallback(async () => {
    if (!currentId) return;
    try {
      const fresh = await api.getCandidate(currentId);
      setView((prev) => (prev ? { ...prev, currentCandidate: fresh } : prev));
    } catch {
      // 单股重读失败保留原卡片，不阻塞后续操作
    }
  }, [currentId]);

  const reclassify = useCallback(
    async (candidateId: string, preservePath = false) => {
      setBusyId(candidateId);
      try {
        const saved = await api.reclassifyCandidate(candidateId);
        // 这里不追写列表版本：随后读回的完整结果才是权威版本；写入响应在外部入口
        // 同时改过列表时已经过期，提前追版本会让后续导航误判「版本一致」。
        // 重新归类把股票送回待归类池并清除隐藏的已处理结果筛选：
        // 当前展示同步回到待归类范围，避免用旧筛选读回一份被挡住的列表。
        if (isUnprocessed(saved.state)) {
          displayScope.current = "unprocessed";
        }
        const updated = preservePath
          ? await api.getClassificationBrowse(displayScope.current)
          : await persistChanges({
              currentCandidateId: candidateId,
              scope: "unprocessed",
              result: null,
            });
        applyBrowse(updated);
        pendingNavigation.current = null;
        setNavigationError(null);
        void loadQuotes([...updated.rows, ...updated.pending], updated.currentCandidate);
      } catch (err) {
        setLoadError(err instanceof ApiError ? err.message : "重新归类失败，请重试");
      } finally {
        setBusyId(null);
      }
    },
    [applyBrowse, loadQuotes, persistChanges],
  );

  const cleanup = useCallback(async () => {
    const state = viewRef.current;
    if (!state) return;
    await api.cleanup(state.importDate);
    const token = beginLists();
    const fresh = await api.getClassificationBrowse(displayScope.current);
    if (!isCurrent(token)) return;
    applyBrowse(fresh);
    void loadQuotes([...fresh.rows, ...fresh.pending], fresh.currentCandidate);
  }, [applyBrowse, beginLists, isCurrent, loadQuotes]);

  const returnToQueue = useCallback(async () => {
    setNavigationBusy(true);
    try {
      const state = await api.returnClassificationQueue();
      pendingNavigation.current = null;
      setNavigationError(null);
      applyBrowse(state);
      void loadQuotes([...state.rows, ...state.pending], state.currentCandidate);
    } catch (err) {
      setLoadError(err instanceof ApiError ? err.message : "返回队列失败，请重试");
    } finally {
      setNavigationBusy(false);
    }
  }, [applyBrowse, loadQuotes]);

  const current = view?.currentCandidate ?? null;

  /** 当前股票所属观察组的名称：卡片把观察关系明显展示出来。 */
  const currentGroupNames = useMemo(
    () => (current ? groupNamesFor(groups, current.groupIds) : []),
    [current, groups],
  );

  const currentIndex = currentId
    ? pending.findIndex((item) => item.candidateId === currentId)
    : -1;

  const reload = useCallback(() => setLoadAttempt((value) => value + 1), []);

  return {
    view,
    rows,
    pending,
    dates,
    groups,
    quotes,
    batches,
    loading,
    listLoading,
    loadError,
    savedRefreshError,
    busyId,
    navigationBusy,
    navigationError,
    currentId,
    current,
    currentIndex,
    currentGroupNames,
    counts: view?.summary ?? EMPTY_COUNTS,
    reload,
    changeFilters,
    selectCandidate,
    onClassified,
    onRelationsChanged: refreshAfterSave,
    onNotesChanged,
    reclassify,
    cleanup,
    move,
    returnToQueue,
  };
}
