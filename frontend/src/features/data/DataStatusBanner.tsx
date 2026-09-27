import { AlertCircle } from "lucide-react";

import { useDataStatusQuery } from "./queries";

/**
 * 顶部数据状态提醒：未补齐时在业务页面常驻显示实际目标交易日。
 *
 * 导入补取执行中显示服务端进度文案；结束后按实际目标交易日完整性决定是否提醒。
 * 不以更新请求结束直接解除未补齐提醒；
 * 定时与启动补更在后台完成时不会主动通知页面，因此按间隔重判一次，
 * 使「更新后按实际完整性重新判断」在用户停留于某模块时仍然成立。
 * 证券库失败单独用一行提示，不把完整行情说成未更新。
 *
 * 读取失败时顶部**当前隐藏**：不显示可能已经过期的提醒，也不误报「已完整」。
 */
export function DataStatusBanner() {
  const query = useDataStatusQuery();
  if (query.isError) {
    return null;
  }
  const status = query.data ?? null;
  if (!status) {
    return null;
  }
  const securitiesFailed = status.securitiesFailed ?? false;
  if (!status.reminder && !securitiesFailed) {
    return null;
  }

  return (
    <div className="data-banners">
      {status.reminder ? (
        <p className="data-banner data-banner-danger" role="alert" data-testid="data-reminder">
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          {status.reminder}
        </p>
      ) : null}
      {securitiesFailed ? (
        <p className="data-banner data-banner-warn" role="status" data-testid="securities-notice">
          <AlertCircle className="h-4 w-4 shrink-0" aria-hidden="true" />
          证券库更新失败，当前使用上次数据
        </p>
      ) : null}
    </div>
  );
}
