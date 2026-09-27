import { useState, type ReactNode } from "react";
import { AlertCircle, Grid2x2, Loader2, RefreshCw, Star, Upload } from "lucide-react";

import { Logo } from "../../components/ui/Logo";
import { Button } from "../../components/ui/Button";
import type { useUpdateController } from "../data/useUpdateController";

export type LaunchTarget = "import" | "classification" | "observations";

const RUN_LABEL: Record<string, string> = {
  success: "更新完成",
  partial: "部分完成",
  failed: "更新失败",
  running: "更新进行中",
};

/** 云山启动页：背景不读取行情；入口与主动更新沿用原有业务控制器。 */
export function LaunchPage({
  onEnter,
  onOpenData,
  controller,
}: {
  onEnter: (target: LaunchTarget) => void;
  onOpenData: () => void;
  controller: ReturnType<typeof useUpdateController>;
}) {
  const { state, busy, error, run } = controller;
  const [attempted, setAttempted] = useState(false);
  const running = busy || state?.running === true;
  const last = state?.lastRun ?? null;
  const settled = attempted && !running;
  const failed = settled && (Boolean(error) || last?.status === "failed");

  const start = async () => {
    setAttempted(true);
    await run();
  };

  return (
    <div className="launch-page" data-testid="launch-page">
      <div className="launch-scenery" aria-hidden="true">
        <img className="launch-landscape" src="/launch-landscape-v2.png" alt="" fetchPriority="high" />
        <div className="launch-sky-veil" />
        <div className="launch-fog launch-fog-far" />
        <div className="launch-fog launch-fog-near" />
      </div>
      <div className="launch-body">
        <span className="launch-mark">
          <Logo className="h-8 w-8" aria-hidden="true" />
          <span className="launch-brand-name">DailyScreen<small>Lite</small></span>
        </span>
        <header className="launch-heading">
          <h1 className="launch-title">看见起伏<span className="launch-title-comma">，</span><br className="launch-title-break" />守住判断</h1>
          <span className="launch-gold-line" aria-hidden="true" />
        </header>
        <nav className="launch-entries" aria-label="启动入口">
          <LaunchEntry label="导入候选" hint="纳入新的可能" icon={<Upload />} onClick={() => onEnter("import")} />
          <LaunchEntry label="开始归类" hint="逐股审视与选择" icon={<Grid2x2 />} onClick={() => onEnter("classification")} />
          <LaunchEntry label="观察组" hint="留意值得关注的" icon={<Star />} onClick={() => onEnter("observations")} />
          <LaunchEntry
            label="更新数据"
            hint={running ? "正在更新数据" : "补齐最新行情"}
            icon={running ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            onClick={() => void start()}
            disabled={running}
          />
        </nav>
        <div className="launch-feedback">
          {running ? (
            <p className="launch-note" role="status" data-testid="launch-update-progress">
              <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
              正在更新数据，可继续进入导入、归类或观察组
            </p>
          ) : null}
          {failed ? (
            <p className="launch-note launch-note-danger" role="alert" data-testid="launch-update-error">
              <AlertCircle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>{error ?? last?.securitiesMessage ?? "更新失败，已保留原有数据"}</span>
              <button type="button" className="launch-link" onClick={onOpenData}>查看详情</button>
            </p>
          ) : null}
          {settled && !failed && last ? (
            <p className="launch-note" role="status" data-testid="launch-update-result">
              {RUN_LABEL[last.status] ?? last.status}
              {last.quotesFailed > 0 ? `，${last.quotesFailed} 只保留旧数据` : ""}
              {last.quotesPending > 0 ? `，${last.quotesPending} 只未补齐` : ""}
              <button type="button" className="launch-link" onClick={onOpenData}>查看详情</button>
            </p>
          ) : null}
        </div>
        <footer className="launch-footer">DAILYSCREEN LITE</footer>
      </div>
    </div>
  );
}

function LaunchEntry({ label, hint, icon, onClick, disabled }: {
  label: string;
  hint: string;
  icon: ReactNode;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <Button variant="ghost" className="launch-entry" onClick={onClick} disabled={disabled} aria-label={label}>
      <span className="launch-entry-top">
        <span className="launch-entry-icon" aria-hidden="true">{icon}</span>
        <span>{label}</span>
        <span className="launch-arrow" aria-hidden="true">↗</span>
      </span>
      <span className="launch-hint" aria-hidden="true">{hint}</span>
    </Button>
  );
}
