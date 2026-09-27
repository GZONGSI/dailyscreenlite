import {
  useQuery,
  type QueryFunctionContext,
  type QueryClient,
} from "@tanstack/react-query";

import { api } from "../../api/client";
import type { Note } from "../../api/notes";

/**
 * 个股笔记：一只股票一份服务器资源，跨导入日期与入口共用（候选卡片与观察详情
 * 订阅同一个键，不再各留一份服务器列表副本）。
 *
 * 这里只承载这个资源的键、读取、取消与写后回写；草稿、编辑初值、放弃确认、
 * 重复提交保护与焦点仍由 `NoteWorkspace` 按自己的业务表达。
 */
const notesKey = (securityId: string) => ["notes", securityId] as const;

/** 笔记按最近保存时间倒序；时间相同用标识定序，缓存与列表顺序因此稳定。 */
const sortNotes = (notes: Note[]) =>
  [...notes].sort(
    (a, b) =>
      b.updatedAt.localeCompare(a.updatedAt) || b.noteId.localeCompare(a.noteId),
  );

/** 一次完整列表读取：信号贯通到 `fetch`，调用方取消时在途请求一并中止。 */
const readNotes = (securityId: string, signal?: AbortSignal) =>
  api.listNotes(securityId, signal).then((stream) => sortNotes(stream.notes));

/**
 * 该证券的笔记查询：订阅与写后重新读取共用同一份键与读取函数，
 * 免得两处各写一遍、改键或改读取方式时漏掉一处。
 */
const notesQuery = (securityId: string) => ({
  queryKey: notesKey(securityId),
  queryFn: ({ signal }: QueryFunctionContext) => readNotes(securityId, signal),
});

/**
 * 订阅并读取该证券的笔记：同键跨入口共享缓存，挂载即读一次。
 *
 * 只按挂载与显式重试读取：框架默认还会在窗口聚焦与重连时自动重读，那会在写入之后
 * 多出一条不受业务控制的读取；这里保留迁移前「打开即读一次」的轨迹。读取失败默认
 * 不重试（QueryClient 的 `retry: false`），由界面给出手动重试。
 */
export function useNotes(securityId: string) {
  return useQuery({
    ...notesQuery(securityId),
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}

/**
 * 取消该证券还在路上的读取，并等取消生效：写入前与写后回写前都用它。
 *
 * 迟到的 GET 可能带着写入之前的数据落地，把刚保存的结果覆盖回旧列表；取消会中止
 * 在途请求并让这次读取不再写入缓存。只取消这一只证券的键，不整片取消，
 * 也不撤销已经提交的写入。
 */
export async function cancelNoteRead(queryClient: QueryClient, securityId: string) {
  await queryClient.cancelQueries({ queryKey: notesKey(securityId) }, { silent: true });
}

/**
 * 写入失败后恢复被写前取消的读取。
 *
 * 冷缓存时那次取消会把查询留在「没有数据、也没有读取错误」的状态：界面一直显示
 * 「正在读取笔记…」，重试入口也不会出现。这里按需补一次读取——读取失败会让列表
 * 进入读取失败状态，重试入口照常出现。已经有列表可显示时不重复读。
 */
export function resumeNoteRead(queryClient: QueryClient, securityId: string) {
  if (queryClient.getQueryData<Note[]>(notesKey(securityId)) !== undefined) return;
  void queryClient.fetchQuery(notesQuery(securityId)).catch(() => {});
}

/**
 * 写后回写缓存。
 *
 * 确认响应只含被写的那一条，因此只有缓存里已经有**完整列表**时才能就地更新；
 * 没有完整列表（还没读到、上一次读取失败或被取消）时必须重新读取——否则一份
 * 「只有这一条」的缓存会把没读到的历史笔记悄悄变成不存在。重新读取失败时不写缓存，
 * 由列表自己显示读取失败与重试。
 *
 * 两条路都先取消这个键上还在路上的读取：写后的缓存必须是这次写入之后的结论，
 * 迟到的读取（例如写入期间重新挂载发起的读取）既不能覆盖它，也不能被重新读取复用。
 */
async function writeBackNotes(
  queryClient: QueryClient,
  securityId: string,
  update: (notes: Note[]) => Note[],
) {
  await cancelNoteRead(queryClient, securityId);
  const queryKey = notesKey(securityId);
  const cached = queryClient.getQueryData<Note[]>(queryKey);
  if (cached === undefined) {
    await queryClient.fetchQuery(notesQuery(securityId));
    return;
  }
  queryClient.setQueryData(queryKey, update(cached));
}

/**
 * 新增或编辑成功后按服务端确认响应回写缓存：同 id 覆盖，按最近保存时间重新排序。
 *
 * 不阻塞写入流程，读回失败也不抛给调用方——保存已经成功，列表自己显示读取失败与重试。
 */
export function confirmNoteSaved(
  queryClient: QueryClient,
  securityId: string,
  note: Note,
) {
  void writeBackNotes(queryClient, securityId, (notes) =>
    sortNotes([note, ...notes.filter((item) => item.noteId !== note.noteId)]),
  ).catch(() => {});
}

/** 删除成功后按被删标识回写缓存；同样不阻塞写入流程，读回失败由列表显示。 */
export function confirmNoteDeleted(
  queryClient: QueryClient,
  securityId: string,
  noteId: string,
) {
  void writeBackNotes(queryClient, securityId, (notes) =>
    notes.filter((item) => item.noteId !== noteId),
  ).catch(() => {});
}
