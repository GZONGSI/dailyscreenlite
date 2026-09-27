import type { ObservationGroup } from "../../api/observations";

/**
 * 把组标识翻译成名称：已删除的组跳过，界面不留空标签。
 * 观察关系在卡片、详情与列表上都用同一份翻译。
 */
export function groupNamesFor(
  groups: ObservationGroup[],
  groupIds: string[],
): string[] {
  const byId = new Map(groups.map((group) => [group.groupId, group.name]));
  return groupIds
    .map((groupId) => byId.get(groupId))
    .filter((name): name is string => Boolean(name));
}
