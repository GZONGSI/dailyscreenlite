import type { components } from "./generated/schema";

/**
 * 权威证券库身份：归类卡片、观察表与个股详情共用同一份传输形状。
 *
 * 类型由后端 DTO 生成（`SecurityPayload`），这里只起短名字，不新增字段、不改可空性。
 * `security` 在未识别时为 null（正常流程总有值）。
 */
export type Security = components["schemas"]["SecurityPayload"];
