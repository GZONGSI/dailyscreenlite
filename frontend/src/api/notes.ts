import type { components } from "./generated/schema";

/**
 * 笔记传输契约：类型由后端 DTO 生成，不再手写。
 *
 * 生成链路见 `./generated/README.md`：后端模型 → `backend/tools/export_openapi.py`
 * → `pnpm run gen:api`。字段名与时间串口径只在服务端维护一处；
 * 这里只给业务方法起短名字，不新增字段、不改可空性。
 */
export type Note = components["schemas"]["NotePayload"];
export type NoteStream = components["schemas"]["NoteStreamPayload"];
export type NoteWrite = components["schemas"]["NoteWrite"];
export type DeleteNoteResponse = components["schemas"]["DeleteNoteResponse"];
