import { useCallback, useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { BarChart3, Settings as SettingsIcon } from "lucide-react";

import { api, type HealthInfo } from "../../api/client";
import { Button } from "../../components/ui/Button";
import { Logo } from "../../components/ui/Logo";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "../../components/ui/Sheet";
import { ClassificationWorkspace } from "../classification/ClassificationWorkspace";
import { DataStatusBanner } from "../data/DataStatusBanner";
import { DataWorkspace } from "../data/DataWorkspace";
import { refreshDataViews } from "../data/queries";
import { ImportWorkspace } from "../import/ImportWorkspace";
import { LaunchPage, type LaunchTarget } from "../launch/LaunchPage";
import { ObservationWorkspace } from "../observations/ObservationWorkspace";
import { refreshObservationView } from "../observations/queries";
import { useUpdateController } from "../data/useUpdateController";
import { SettingsPanel } from "../settings/SettingsPanel";
import { ThemeMenu } from "../settings/ThemeMenu";
import { GlobalSearch } from "./GlobalSearch";

type ModuleKey = "import" | "classification" | "observations" | "data";

const MODULES: { key: ModuleKey; label: string }[] = [
  { key: "import", label: "导入" },
  { key: "classification", label: "候选归类" },
  { key: "observations", label: "观察组" },
];

/** 当前浏览记录只保存模块入口；股票、组与筛选仍从服务端恢复。刷新保留，首次打开为空。 */
function readCurrentModule(): ModuleKey | null {
  const value = window.history.state?.dsliteModule;
  return value === "import" || value === "classification" || value === "observations" || value === "data"
    ? value
    : null;
}

/**
 * 应用外壳：启动页 → 顶部主导航（导入 / 候选归类 / 观察组）+ 全局搜索、数据中心、设置。
 *
 * 首次打开应用先看到沉浸启动页；刷新保留当前模块，点击左上角标识才返回启动页。
 * 各模块占完整工作区并各自保存工作位置（服务端），模块间不自动同步当前股票。
 * 顶部数据未补齐提醒与证券库失败提示由 DataStatusBanner 统一承担。
 */
export function AppShell() {
  const [module, setModule] = useState<ModuleKey>(() => readCurrentModule() ?? "import");
  const [launched, setLaunched] = useState(() => readCurrentModule() !== null);
  useEffect(() => {
    window.history.replaceState({ ...window.history.state, dsliteModule: launched ? module : null }, "");
  }, [module, launched]);
  // 搜索跳转与导入落地后要求候选归类模块重读
  const [classificationToken, setClassificationToken] = useState(0);
  // 导入工作区常驻（切换模块时只隐藏），草稿与待提交来源不因切换模块丢失；
  // 每次进入导入模块重读一次导入记录
  const [importToken, setImportToken] = useState(0);
  // 行情图表的数据更新桥接：行情分页尚未迁移，图表仍需要一个信号原位重读；
  // 观察工作表本身的读取由查询负责，不再由令牌驱动。
  const [quoteToken, setQuoteToken] = useState(0);
  const [focusSecurityId, setFocusSecurityId] = useState<string | null>(null);
  const [panel, setPanel] = useState<null | "settings">(null);
  // 关闭面板后把焦点还给触发按钮，键盘操作不丢位置
  const panelTrigger = useRef<HTMLButtonElement | null>(null);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const queryClient = useQueryClient();

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health());
    } catch {
      // 健康信息失败不阻塞业务页面
    }
  }, []);

  useEffect(() => {
    void refreshHealth();
  }, [refreshHealth]);

  const updateController = useUpdateController({
    onUpdated: () => {
      // 未迁移的工作表读取仍由刷新令牌桥接；数据视图与观察工作表改由查询失效重读，
      // 不再为它们保留第二个刷新令牌。行情图表按 quoteToken 原位重读（行情分页未迁移）。
      setClassificationToken((value) => value + 1);
      setQuoteToken((value) => value + 1);
      refreshDataViews(queryClient);
      void refreshObservationView(queryClient);
    },
  });

  const openClassification = useCallback(() => {
    setLaunched(true);
    setClassificationToken((value) => value + 1);
    setModule("classification");
  }, []);

  const openData = useCallback(() => {
    setLaunched(true);
    refreshDataViews(queryClient);
    setModule("data");
  }, [queryClient]);

  /** 启动页入口：进入模块后恢复该模块上次工作位置（各模块各自持久化）。 */
  const enterFromLaunch = useCallback(
    (target: LaunchTarget) => {
      setLaunched(true);
      if (target === "import") {
        setImportToken((value) => value + 1);
        setModule("import");
        return;
      }
      if (target === "observations") {
        setFocusSecurityId(null);
        void refreshObservationView(queryClient);
        setModule("observations");
        return;
      }
      setClassificationToken((value) => value + 1);
      setModule("classification");
    },
    [queryClient],
  );

  /** 跨模块打开详情只生效一次；之后的重读沿用观察组自己保存的工作位置。 */
  const clearFocus = useCallback(() => setFocusSecurityId(null), []);

  /** 全局搜索命中已观察股票：切到观察组并打开它的详情（打开写入即新的工作表）。 */
  const openObservationDetail = useCallback((securityId: string) => {
    setLaunched(true);
    setFocusSecurityId(securityId);
    setModule("observations");
  }, []);

  if (!launched) {
    return (
      <LaunchPage
        onEnter={enterFromLaunch}
        onOpenData={openData}
        controller={updateController}
      />
    );
  }

  return (
    <div className="app-shell">
      <header className="app-header">
        <button
          type="button"
          className="flex shrink-0 items-center gap-2"
          aria-label="返回启动页"
          onClick={() => setLaunched(false)}
        >
          <Logo className="h-8 w-8" />
          <span className="text-base font-semibold">DailyScreen Lite</span>
        </button>
        <nav className="flex items-center gap-1" aria-label="主导航">
          {MODULES.map((entry) => (
            <Button
              key={entry.key}
              size="sm"
              variant={module === entry.key ? "default" : "ghost"}
              aria-current={module === entry.key}
              onClick={() => {
                setModule(entry.key);
                if (entry.key === "import") setImportToken((value) => value + 1);
                if (entry.key === "observations") {
                  setFocusSecurityId(null);
                  // 进入模块显式重读工作表：读取时机由业务表达，不依赖框架的挂载判定
                  void refreshObservationView(queryClient);
                }
              }}
            >
              {entry.label}
            </Button>
          ))}
        </nav>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <GlobalSearch
            onJump={openClassification}
            onOpenDetail={openObservationDetail}
          />
          <Button
            size="sm"
            variant={module === "data" ? "default" : "ghost"}
            onClick={() => {
              setModule("data");
              refreshDataViews(queryClient);
              void updateController.reload();
            }}
          >
            <BarChart3 className="h-4 w-4" aria-hidden="true" />
            数据中心
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={(event) => {
              panelTrigger.current = event.currentTarget;
              setPanel("settings");
            }}
          >
            <SettingsIcon className="h-4 w-4" aria-hidden="true" />
            设置
          </Button>
          <ThemeMenu />
        </div>
      </header>

      {health && !health.securitiesLoaded ? (
        <p role="alert" className="px-4 text-sm text-danger">
          证券库未加载：{health.securitiesMessage}。导入的股票将全部未识别。
        </p>
      ) : null}
      {health && health.wencaiCookieConfigured && !health.wencaiAvailable ? (
        <p role="status" className="px-4 text-sm text-danger">
          问财链接获取暂不可用：{health.wencaiMessage}
        </p>
      ) : null}
      <DataStatusBanner />

      <div className="shell-module" data-module={module}>
        <div className="module-slot" hidden={module !== "import"}>
          <ImportWorkspace
            refreshToken={importToken}
            onStartClassification={openClassification}
            onImported={() => {
              setClassificationToken((value) => value + 1);
              // 导入会触发后台补取：顶部提醒要按新的完整性重判
              refreshDataViews(queryClient);
            }}
          />
        </div>
        {module === "classification" ? (
          <ClassificationWorkspace
            refreshToken={classificationToken}
            onManageGroups={() => {
              setFocusSecurityId(null);
              void refreshObservationView(queryClient);
              setModule("observations");
            }}
          />
        ) : null}
        {module === "observations" ? (
          <ObservationWorkspace
            quoteRefreshToken={quoteToken}
            focusSecurityId={focusSecurityId}
            onFocusHandled={clearFocus}
            onOpenClassification={openClassification}
          />
        ) : null}
        {module === "data" ? (
          <div className="module-slot">
            <DataWorkspace controller={updateController} />
          </div>
        ) : null}
      </div>

      <Sheet
        open={panel !== null}
        onOpenChange={(open) => {
          if (!open) setPanel(null);
        }}
      >
        <SheetContent
          onCloseAutoFocus={(event) => {
            event.preventDefault();
            panelTrigger.current?.focus();
          }}
        >
          <div className="border-b border-border p-5 pr-14">
            <SheetTitle className="font-semibold">设置</SheetTitle>
            <SheetDescription className="mt-1 text-xs text-foreground/60">
              关闭面板后继续当前工作区。
            </SheetDescription>
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto p-4 space-y-4">
            {panel === "settings" ? <SettingsPanel onSaved={refreshHealth} /> : null}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  );
}
