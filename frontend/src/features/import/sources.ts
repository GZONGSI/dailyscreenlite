import { api } from "../../api/client";

/**
 * 批次 id → 来源名称：卡片与详情的「来源与处理记录」共用同一份翻译。
 *
 * 读取失败返回空表，界面显示「未知来源」而不是阻断行情与笔记。
 */
export async function loadSourceNames(): Promise<Map<string, string>> {
  try {
    const { batches } = await api.listBatches();
    return new Map(batches.map((batch) => [batch.batchId, batch.sourceName]));
  } catch {
    return new Map();
  }
}
