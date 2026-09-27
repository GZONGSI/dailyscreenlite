import { QueryClient } from "@tanstack/react-query";

/**
 * 应用生命周期内唯一的 QueryClient：读取状态、同键缓存与请求协调由它承担。
 *
 * 默认不重试：保留各入口可见的读取失败与手动重试，不让框架静默重试改变体验。
 * networkMode 固定 `always`：本机服务在外网断开时仍可用，供应商失败由后端报告，
 * 不因浏览器判定离线而暂停读取。具体资源的 staleTime、开启条件与刷新策略
 * 在所属功能里配置，不用框架默认值顺带改变行为。
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      networkMode: "always",
    },
  },
});
