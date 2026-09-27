import type { CandidateScope, CandidateState } from "../../api/classification";

/** 状态文案只需要这两个字段，卡片详细资料与列表轻量行都满足。 */
export interface StatefulItem {
  state: CandidateState;
  viewedAt: string | null;
}

/** 候选项处理状态的中文标签，卡片与队列共用，避免两处取值不一致。 */
export const STATE_LABEL: Record<CandidateState, string> = {
  pending: "待归类",
  later: "稍后处理",
  dismissed: "暂不关注",
  observed: "已观察",
  cleared: "已清理",
};

/** 待归类池包含的状态：未查看、已查看未决策与稍后处理仍待用户处理。 */
const UNPROCESSED: ReadonlySet<CandidateState> = new Set(["pending", "later"]);

export function isUnprocessed(state: CandidateState): boolean {
  return UNPROCESSED.has(state);
}

/**
 * 面向界面的状态文案：待归类项按是否打开过区分「未查看 / 已查看」，
 * 使浏览位置与处理状态可见但互相独立。
 */
export function itemStateLabel(candidate: StatefulItem): string {
  if (candidate.state === "pending") {
    return candidate.viewedAt ? "已查看" : "未查看";
  }
  return STATE_LABEL[candidate.state] ?? candidate.state;
}

export const SCOPE_LABEL: Record<CandidateScope, string> = {
  unprocessed: "待归类",
  processed: "已处理",
};

/** 已处理视图可按结果筛选；选项与状态标签同源。 */
export const RESULT_OPTIONS: readonly CandidateState[] = [
  "dismissed",
  "observed",
  "cleared",
];

/** 历次处理记录的动作文案（来源与处理记录展开后展示）。 */
export const ACTION_LABEL: Record<string, string> = {
  selected: "首次入选",
  reopened: "新日期入选，重新待归类",
  later: "稍后处理",
  dismissed: "暂不关注",
  observed: "加入或保留观察",
  left_groups: "移出观察组并暂不关注",
  reclassified: "主动重新归类",
  cleared: "主动清理",
};

export function actionLabel(action: string): string {
  return ACTION_LABEL[action] ?? action;
}
