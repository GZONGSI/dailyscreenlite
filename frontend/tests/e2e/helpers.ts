import { expect, type Page } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, "..", "..");
const REPO_ROOT = resolve(FRONTEND_ROOT, "..");
const PYTHON =
  process.platform === "win32"
    ? resolve(REPO_ROOT, "backend", ".venv", "Scripts", "python.exe")
    : resolve(REPO_ROOT, "backend", ".venv", "bin", "python");

/**
 * 默认无行情的夹具来源：让端到端测试不访问 AKShare 外网、也不启动后台更新。
 * 需要真实图表数据的用例通过 extraEnv 覆盖 DSLITE_QUOTES_FIXTURE。
 * 每日状态来源默认关闭（状态记未知），数据可靠性用例通过 extraEnv 注入状态夹具。
 */
const EMPTY_QUOTES_FIXTURE = join(
  mkdtempSync(join(tmpdir(), "dslite-empty-quotes-")),
  "empty.json",
);
writeFileSync(EMPTY_QUOTES_FIXTURE, JSON.stringify({ bars: {} }), "utf8");

export interface ServerHandle {
  port: number;
  dataDir: string;
  baseURL: string;
  stop: () => Promise<void>;
}

/**
 * 启动真实本机服务，数据目录隔离到临时目录。
 * 证券库快照仍来自交付的 data/securities（settings 的兜底路径）。
 * extraEnv 可覆盖/追加环境变量（如指向本地问财桩的 DSLITE_WENCAI_BASE）。
 */
export async function startServer(
  port: number,
  dataDir?: string,
  extraEnv: Record<string, string> = {},
): Promise<ServerHandle> {
  const resolvedDataDir = dataDir ?? mkdtempSync(join(tmpdir(), "dslite-e2e-"));

  const child: ChildProcess = spawn(
    PYTHON,
    ["-m", "dailyscreen_lite", "--host", "127.0.0.1", "--port", String(port)],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        DSLITE_DATA_DIR: resolvedDataDir,
        // 端到端默认不启用定时更新，也不访问外网行情；用例可覆盖
        DSLITE_UPDATE_SCHEDULE: "off",
        DSLITE_QUOTES_FIXTURE: EMPTY_QUOTES_FIXTURE,
        // 每日状态来源默认关闭：状态记未知，不确定性用例自行注入状态夹具
        DSLITE_MARKET_STATUS: "off",
        ...extraEnv,
      },
      stdio: ["ignore", "pipe", "pipe"],
    },
  );

  const baseURL = `http://127.0.0.1:${port}`;
  await waitForHealth(baseURL, child);

  return {
    port,
    dataDir: resolvedDataDir,
    baseURL,
    stop: () =>
      new Promise<void>((resolveStop) => {
        if (child.exitCode !== null || child.killed) {
          resolveStop();
          return;
        }
        child.once("exit", () => resolveStop());
        child.kill("SIGTERM");
        // Windows 上确保进程结束
        setTimeout(() => {
          if (child.exitCode === null) {
            child.kill("SIGKILL");
          }
        }, 3000);
      }),
  };
}

async function waitForHealth(
  baseURL: string,
  child: ChildProcess,
): Promise<void> {
  let output = "";
  child.stdout?.on("data", (chunk) => {
    output += String(chunk);
  });
  child.stderr?.on("data", (chunk) => {
    output += String(chunk);
  });

  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null) {
      throw new Error(`服务提前退出（code=${child.exitCode}）：\n${output}`);
    }
    try {
      const response = await fetch(`${baseURL}/api/health`);
      if (response.ok) {
        return;
      }
    } catch {
      // 服务尚未就绪
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`等待服务超时：\n${output}`);
}

/** Switch actual modal tools through the same controls used by people. */
export async function closePanel(page: Page) {
  const close = page.getByRole("button", { name: "关闭面板" });
  if (await close.isVisible()) await close.click();
}

export type ModuleLabel = "导入" | "候选归类" | "观察组" | "数据中心";

/**
 * 每次新打开应用先看到沉浸启动页，顶部导航只在模块内出现。
 * 本函数从启动页进入一个模块（默认导入），使后续导航操作可用；
 * 已在模块内时不做任何事。
 */
export async function leaveLaunch(page: Page) {
  const launch = page.getByTestId("launch-page");
  if (await launch.isVisible().catch(() => false)) {
    await page.getByRole("button", { name: "导入候选", exact: true }).click();
    await expect(launch).toHaveCount(0);
  }
}

/** 顶部主导航：三个业务模块与数据中心各占完整工作区。 */
export async function goModule(page: Page, label: ModuleLabel) {
  await leaveLaunch(page);
  await page.getByRole("button", { name: label, exact: true }).click();
}

/** 打开设置面板（问财 Cookie 等连接配置）。 */
export async function openSettings(page: Page) {
  await closePanel(page);
  await leaveLaunch(page);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "设置：问财登录" }),
  ).toBeVisible();
}

/** 在导入页提交一段文本（代码列表、表格文本或问财链接）。 */
export async function submitText(page: Page, text: string) {
  await goModule(page, "导入");
  await leaveLaunch(page);
  await page.locator("#import-text").fill(text);
  await page.getByRole("button", { name: "提交", exact: true }).click();
}

/** 在导入页提交文件。 */
export async function submitFile(page: Page, path: string) {
  await goModule(page, "导入");
  await leaveLaunch(page);
  await page.setInputFiles('input[type="file"]', path);
  await page.getByRole("button", { name: "提交", exact: true }).click();
}

/** 提交文本并点击「开始归类」进入候选归类工作区。 */
export async function classifyText(page: Page, text: string) {
  await submitText(page, text);
  await startClassification(page);
}

/** 导入结果页的「开始归类」：不自动跳转，由用户主动进入候选归类。 */
export async function startClassification(page: Page) {
  await page.getByRole("button", { name: "开始归类" }).click();
  await expect(page.getByTestId("main-column")).toBeVisible();
}

/** 当前股票卡可见（候选归类的工作对象）。 */
export async function expectStockCard(page: Page) {
  await expect(page.getByTestId("stock-card")).toBeVisible();
}

