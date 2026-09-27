import { Textarea } from "../../components/ui/Textarea";
import { useCallback, useEffect, useState } from "react";
import { KeyRound, Loader2, ShieldCheck, Trash2 } from "lucide-react";

import { api, ApiError, type SettingsInfo } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";

interface Props {
  onSaved?: () => void;
}

export function SettingsPanel({ onSaved }: Props) {
  const [info, setInfo] = useState<SettingsInfo | null>(null);
  const [cookie, setCookie] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      setInfo(await api.getSettings());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "无法读取设置");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const save = useCallback(async () => {
    setError(null);
    setNotice(null);
    if (!cookie.trim()) {
      setError("请粘贴问财 Cookie 后再保存。");
      return;
    }
    setBusy(true);
    try {
      const result = await api.saveWencaiCookie(cookie.trim());
      setCookie("");
      setNotice(
        `已本地保存（长度 ${result.wencaiCookie.length} 字符）。Cookie 不会出现在日志或导入结果中。`,
      );
      await refresh();
      onSaved?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "保存失败，请重试");
    } finally {
      setBusy(false);
    }
  }, [cookie, refresh, onSaved]);

  const clear = useCallback(async () => {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      await api.clearWencaiCookie();
      setNotice("已清除本地问财 Cookie。");
      await refresh();
      onSaved?.();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "清除失败，请重试");
    } finally {
      setBusy(false);
    }
  }, [refresh, onSaved]);

  return (
    <section
      aria-labelledby="settings-title"
      className="rounded-2xl border border-border bg-surface p-5 shadow-sm"
    >
      <div className="mb-3 flex items-center gap-2">
        <KeyRound className="h-5 w-5 text-primary" aria-hidden="true" />
        <h2 id="settings-title" className="text-base font-semibold">
          设置：问财登录
        </h2>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2 text-xs">
        <Badge tone={info?.wencaiAvailable ? "success" : "warning"}>
          链接获取 {info?.wencaiAvailable ? "可用" : "不可用"}
        </Badge>
        <Badge tone={info?.wencaiCookie.configured ? "success" : "neutral"}>
          Cookie {info?.wencaiCookie.configured ? "已配置" : "未配置"}
        </Badge>
        {info?.wencaiMessage ? (
          <span className="text-foreground/60">{info.wencaiMessage}</span>
        ) : null}
      </div>

      <label htmlFor="wencai-cookie" className="mb-1 block text-sm font-medium">
        问财 Cookie
      </label>
      <Textarea
        id="wencai-cookie"
        value={cookie}
        disabled={busy}
        rows={3}
        aria-describedby="cookie-help"
        placeholder="粘贴浏览器中的 Cookie 值（仅保存在本机数据目录）"
        onChange={(event) => setCookie(event.target.value)}
        className="w-full resize-y rounded-xl border border-border bg-background/40 px-3 py-2 font-mono text-xs outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
      />
      <p
        id="cookie-help"
        className="mt-1 flex items-center gap-1.5 text-xs text-foreground/60"
      >
        <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
        不复制浏览器 Profile，不依赖原型凭据目录；Cookie 只用于本机问财请求。
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button disabled={busy} onClick={() => void save()}>
          {busy ? (
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
          ) : null}
          保存 / 替换 Cookie
        </Button>
        <Button
          variant="ghost"
          disabled={busy || !info?.wencaiCookie.configured}
          onClick={() => void clear()}
        >
          <Trash2 className="h-4 w-4" aria-hidden="true" />
          清除
        </Button>
      </div>

      <div aria-live="polite" className="mt-3 text-sm">
        {error ? (
          <span className="text-danger" role="alert">
            {error}
          </span>
        ) : notice ? (
          <span className="text-foreground/70">{notice}</span>
        ) : null}
      </div>
    </section>
  );
}
