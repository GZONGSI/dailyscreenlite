import { expect, test, type Page } from "@playwright/test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  classifyText,
  goModule,
  leaveLaunch,
  startServer,
  type ServerHandle,
} from "./helpers";

/**
 * 工单 03 的浏览器验收：真实 Chrome → 本机 HTTP → 临时真实数据库。
 *
 * 行情与每日状态都走与 AKShare 同一来源边界的夹具（不访问外网），
 * 固定北京时间便于复现 16:30 与周末边界。覆盖：沉浸启动页与四入口、
 * 顶部未补齐提醒、数据中心分项与逐股结果、全天停牌与状态待确认、
 * 部分失败保留旧数据与重试、证券库失败单独提示。
 */

const PORT = 8799;
let server: ServerHandle;
let quotesPath: string;
let statusPath: string;
let statusPayload: Record<string, unknown>;

function bar(day: string, close: number) {
  return {
    date: day,
    open: close,
    high: close,
    low: close,
    close,
    volume_lots: 1000,
    amount_yuan: close * 1e5,
  };
}

/** 目标交易日 09-18：000001 已更新、600519 落后、300750 全天停牌、920001 北交所未知。 */
function quotesFixture(): Record<string, unknown> {
  return {
    bars: {
      "000001.SZ": [bar("2026-09-17", 11.0), bar("2026-09-18", 11.2)],
      "600519.SH": [bar("2026-09-17", 1500.0)],
      "300750.SZ": [bar("2026-09-17", 200.0)],
      "920001.BJ": [bar("2026-09-17", 10.0)],
    },
  };
}

function statusFixture(): Record<string, unknown> {
  return {
    trade_dates: ["2026-09-17", "2026-09-18"],
    covered_markets: ["SH", "SZ"],
    uncovered_markets: ["BJ"],
    suspensions: [
      {
        code: "300750",
        exchange: "SZ",
        name: "宁德时代",
        kind: "continuous",
        start: "2026-09-18",
        end: null,
        market: "深交所创业板",
      },
    ],
  };
}

function writeFixtures(quotes: Record<string, unknown>, status: Record<string, unknown>) {
  writeFileSync(quotesPath, JSON.stringify(quotes, null, 2), "utf8");
  writeFileSync(statusPath, JSON.stringify(status, null, 2), "utf8");
}

test.beforeEach(async () => {
  const dir = mkdtempSync(join(tmpdir(), "dslite-data-"));
  quotesPath = join(dir, "quotes.json");
  statusPath = join(dir, "status.json");
  statusPayload = statusFixture();
  writeFixtures(quotesFixture(), statusPayload);
  server = await startServer(PORT, undefined, {
    DSLITE_QUOTES_FIXTURE: quotesPath,
    DSLITE_MARKET_STATUS_FIXTURE: statusPath,
    DSLITE_UPDATE_SCHEDULE: "off",
    // 固定北京时间：目标交易日为 09-18，提醒日期可核对
    DSLITE_NOW: "2026-09-18T17:00:00+08:00",
    DSLITE_WENCAI_BASE: "http://127.0.0.1:1",
  });
});

test.afterEach(async () => {
  await server?.stop();
});

/**
 * 触发整体更新，并等待其真正结束时再返回。
 *
 * 导入后的后台补取会短暂持有更新锁，此时手动更新会返回「进行中」；
 * 先等到没有更新在跑，点击才代表用户的真实操作，测试也因此不依赖时序。
 */
async function runUpdate(page: Page, center: ReturnType<Page["getByTestId"]>) {
  const runId = async () =>
    ((await (await page.request.get("/api/data")).json()).lastRun?.runId ?? null) as string | null;
  // 导入后的后台补取会短暂持有更新锁，此时手动更新会返回「进行中」；
  // 先等到没有更新在跑，点击才代表用户的真实操作，测试因此不依赖时序。
  await expect
    .poll(async () => (await (await page.request.get("/api/updates")).json()).running, {
      timeout: 15_000,
    })
    .toBe(false);
  const before = await runId();
  await center.getByRole("button", { name: "更新数据" }).click();
  await expect.poll(runId, { timeout: 30_000 }).not.toBe(before);
  await expect(center.getByText(/最近一次：/)).toBeVisible({ timeout: 15_000 });
}

/** 导入四只并跑一次整体更新，进入可核对的数据中心状态。 */
async function prepare(page: Page, codes = "000001\n600519\n300750\n920001") {
  await page.goto("/");
  await classifyText(page, codes);
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  await runUpdate(page, center);
  return center;
}

function row(center: ReturnType<Page["getByTestId"]>, code: string) {
  return center.locator("tbody tr", { hasText: code });
}

// --- 沉浸式启动页（视觉基线 01） ---

test("新打开应用先看到启动页：四个入口，无导航、搜索与数据提醒", async ({ page }) => {
  await page.goto("/");

  const launch = page.getByTestId("launch-page");
  await expect(launch).toBeVisible();
  await expect(launch.getByRole("button", { name: "导入候选" })).toBeVisible();
  await expect(launch.getByRole("button", { name: "开始归类" })).toBeVisible();
  await expect(launch.getByRole("button", { name: "观察组", exact: true })).toBeVisible();
  await expect(launch.getByRole("button")).toHaveCount(4);
  await expect(launch.getByRole("button", { name: "更新数据" })).toBeVisible();

  // 隐藏常驻顶部导航、全局搜索与数据未更新提醒，也不展示统计或日期
  await expect(page.getByRole("navigation", { name: "主导航" })).toHaveCount(0);
  await expect(page.getByLabel("搜索已导入股票")).toHaveCount(0);
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
  await expect(launch.getByText(/\d{2}-\d{2}/)).toHaveCount(0);
  await expect(launch.getByRole("heading", { name: /看见起伏，\s*守住判断/ })).toBeVisible();
  await expect(launch.getByText("留一份清醒", { exact: false })).toHaveCount(0);
  const artwork = launch.locator("img.launch-landscape");
  await expect(artwork).toBeVisible();
  await expect(artwork).toHaveAttribute("src", "/launch-landscape-v2.png");
  await expect.poll(() => artwork.evaluate((node: HTMLImageElement) => node.naturalWidth)).toBeGreaterThan(1000);
  for (const viewport of [{ width: 390, height: 844 }, { width: 1440, height: 900 }]) {
    await page.setViewportSize(viewport);
    for (const button of await launch.getByRole("button").all()) {
      const box = await button.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.x).toBeGreaterThanOrEqual(0);
      expect(box!.x + box!.width).toBeLessThanOrEqual(viewport.width);
      expect(box!.y + box!.height).toBeLessThanOrEqual(viewport.height);
    }
    await launch.screenshot({ path: test.info().outputPath(`launch-${viewport.width}.png`) });
  }
});

test("启动页背景：默认云雾流动，系统开启减少动态效果时静止", async ({ page }) => {
  const scenery = () => page.locator(".launch-scenery");
  await page.goto("/");
  await expect(page.getByTestId("launch-page")).toBeVisible();
  await expect.poll(() => page.locator("img.launch-landscape").evaluate((node: HTMLImageElement) => node.naturalWidth)).toBeGreaterThan(1000);
  const first = await scenery().screenshot();
  await page.waitForTimeout(600);
  const second = await scenery().screenshot();
  expect(first.equals(second)).toBe(false);

  // 同一页面即时响应减少动态效果，不依赖刷新。
  await page.emulateMedia({ reducedMotion: "reduce" });
  const still1 = await scenery().screenshot();
  await page.waitForTimeout(600);
  const still2 = await scenery().screenshot();
  expect(still1.equals(still2)).toBe(true);
});

test("启动页业务入口可用；模块切换不重入，点标识才返回启动页", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "导入候选" }).click();
  await expect(page.getByTestId("launch-page")).toHaveCount(0);

  // 模块切换不重入启动页
  await goModule(page, "候选归类");
  await expect(page.getByTestId("launch-page")).toHaveCount(0);
  await goModule(page, "观察组");
  await expect(page.getByTestId("launch-page")).toHaveCount(0);

  // 点击左上角标识返回启动页
  await page.getByRole("button", { name: "返回启动页" }).click();
  await expect(page.getByTestId("launch-page")).toBeVisible();

  // 再次进入「开始归类」直达候选归类（而不是导入）
  await page.getByRole("button", { name: "开始归类" }).click();
  await expect(page.getByTestId("main-column")).toBeVisible();
});

test("刷新保留当前模块，只有 Logo 返回启动页且刷新后仍停在启动页", async ({ page, context }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "导入候选" }).click();
  for (const label of ["导入", "候选归类", "观察组", "数据中心"] as const) {
    await goModule(page, label);
    await page.reload();
    await expect(page.getByTestId("launch-page")).toHaveCount(0);
    await expect(page.getByRole("button", { name: label, exact: true })).toBeVisible();
    if (label === "数据中心") {
      await expect(page.getByTestId("data-center")).toBeVisible();
    } else {
      await expect(page.locator(".shell-module")).toHaveAttribute("data-module",
        label === "导入" ? "import" : label === "候选归类" ? "classification" : "observations");
    }
  }
  const newTab = await context.newPage();
  await newTab.goto("/");
  await expect(newTab.getByTestId("launch-page")).toBeVisible();
  await newTab.close();
  await page.getByRole("button", { name: "返回启动页" }).click();
  await expect(page.getByTestId("launch-page")).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("launch-page")).toBeVisible();
  await expect(page.getByRole("navigation", { name: "主导航" })).toHaveCount(0);
});

test("启动页不展示历史更新结果，主动更新后才在该入口给出反馈", async ({ page }) => {
  // 导入已触发后台补取：服务端因此存在一条历史更新结果
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect
    .poll(
      async () =>
        (await (await page.request.get("/api/updates")).json()).lastRun?.runId ?? null,
      { timeout: 15_000 },
    )
    .not.toBeNull();

  // 回到启动页：上一次的结果属状态摘要，不在这里展示，只有四个入口
  await page.getByRole("button", { name: "返回启动页" }).click();
  const launch = page.getByTestId("launch-page");
  await expect(launch).toBeVisible();
  await expect(launch.getByTestId("launch-update-result")).toHaveCount(0);
  await expect(launch.getByTestId("launch-update-error")).toHaveCount(0);

  // 主动更新：留在本页，结果只在更新入口附近出现，且按实际完整性如实说
  await expect
    .poll(
      async () => (await (await page.request.get("/api/updates")).json()).running,
      { timeout: 15_000 },
    )
    .toBe(false);
  await launch.getByRole("button", { name: "更新数据" }).click();
  const result = launch.getByTestId("launch-update-result");
  await expect(result).toBeVisible({ timeout: 30_000 });
  await expect(result).toContainText("部分完成");
  await expect(result).toContainText("1 只未补齐");
});

test("启动页主动更新留在本页显示进展，期间仍可进入导入与归类", async ({ page }) => {
  // 协议拦截：模拟后台更新进行中
  await page.route("**/api/updates", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        running: true,
        lastRun: null,
        progress: { total: 4, done: 1, attempt: 1 },
      }),
    });
  });
  await page.goto("/");

  await expect(page.getByTestId("launch-update-progress")).toBeVisible();
  // 更新在后台继续：仍可进入导入与归类
  await page.getByRole("button", { name: "导入候选" }).click();
  await expect(page.getByRole("heading", { name: "选择导入方式" })).toBeVisible();
  await goModule(page, "候选归类");
  await expect(page.getByTestId("main-column")).toBeVisible();
});

// --- 数据中心与顶部提醒（视觉基线 05） ---

test("数据中心展示分项状态、范围与逐股结果，并用实际目标交易日提醒", async ({ page }) => {
  await prepare(page);
  const center = page.getByTestId("data-center");

  // 分项状态：证券库、股票状态、日线，以及更新范围
  await expect(center.getByTestId("data-item-securities")).toContainText("证券库");
  await expect(center.getByTestId("data-item-market_status")).toContainText("09-18");
  await expect(center.getByTestId("data-item-market_status")).toContainText("BJ 状态未知");
  await expect(center.getByText(/更新范围：待归类股票与观察组股票（4 只）/)).toBeVisible();

  // 逐股结果：已更新 / 全天停牌 / 待补齐 / 状态待确认
  await expect(row(center, "000001")).toContainText("2026-09-18");
  await expect(row(center, "000001")).toContainText("已更新");
  await expect(row(center, "300750")).toContainText("全天停牌");
  await expect(row(center, "300750")).toContainText("最后行情 2026-09-17");
  await expect(row(center, "600519")).toContainText("待补齐");
  await expect(row(center, "920001")).toContainText("状态待确认");

  // 未补齐：顶部红色提醒用实际目标交易日；可重试未完成（待补齐 + 状态待确认）
  const reminder = page.getByTestId("data-reminder");
  await expect(reminder).toHaveText("截至最近交易日（09-18），数据尚未更新完整！");
  await expect(
    center.getByRole("button", { name: "重试未完成（2）" }),
  ).toBeVisible();
  await expect(center.getByRole("progressbar", { name: "更新进度" })).toBeVisible();

  // 抓取成功但目标交易日行情未到：本次更新只能记「部分成功」，不许说成更新完成
  await expect(center.getByText(/最近一次：部分成功/)).toBeVisible();
  await expect(center.getByText(/未补齐 2 只/)).toBeVisible();
  await expect(center.getByText(/用时 \d+/)).toBeVisible();
  await center.getByText("本次最慢的股票").click();
  await expect(center.getByTestId("quote-diagnostics").locator("li").first()).toBeVisible();
});

test("停牌与状态未知都不算未补齐之外的失败；补齐后提醒解除并重启读回", async ({
  page,
  browser,
}) => {
  await prepare(page);
  const center = page.getByTestId("data-center");
  await expect(page.getByTestId("data-reminder")).toBeVisible();

  // 600519 补上目标日行情；北交所在夹具中补上，但状态仍未知 → 仍提示待确认
  writeFixtures(
    {
      bars: {
        "000001.SZ": [bar("2026-09-18", 11.2)],
        "600519.SH": [bar("2026-09-17", 1500.0), bar("2026-09-18", 1510.0)],
        "300750.SZ": [bar("2026-09-17", 200.0)],
        "920001.BJ": [bar("2026-09-17", 10.0), bar("2026-09-18", 10.5)],
      },
    },
    statusPayload,
  );
  await runUpdate(page, center);
  // 北交所行情已到目标日 → 该股已更新；停牌股豁免；提醒解除
  await expect(row(center, "920001")).toContainText("已更新");
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);

  // 重启后分项与逐股结果仍可读回
  const dataDir = server.dataDir;
  await server.stop();
  server = await startServer(PORT, dataDir, {
    DSLITE_QUOTES_FIXTURE: quotesPath,
    DSLITE_MARKET_STATUS_FIXTURE: statusPath,
    DSLITE_UPDATE_SCHEDULE: "off",
    DSLITE_NOW: "2026-09-18T18:00:00+08:00",
    DSLITE_WENCAI_BASE: "http://127.0.0.1:1",
  });
  const reopened = await browser.newContext();
  const next = await reopened.newPage();
  await next.goto("/");
  await leaveLaunch(next);
  await goModule(next, "数据中心");
  const center2 = next.getByTestId("data-center");
  await expect(row(center2, "000001")).toContainText("已更新");
  await expect(row(center2, "300750")).toContainText("全天停牌");
  await expect(next.getByTestId("data-reminder")).toHaveCount(0);
  await reopened.close();
});

test("部分股票失败保留旧数据、逐股可见原因，重试补齐后提醒解除", async ({ page }) => {
  // 先成功更新一次，让 600519 有 09-17 的旧行情
  await prepare(page);
  const center = page.getByTestId("data-center");
  await expect(row(center, "600519")).toContainText("2026-09-17");

  // 来源随后对该股持续失败：旧数据必须保留，其他股票继续
  writeFixtures({ ...quotesFixture(), fail_bars: ["600519.SH"] }, statusPayload);
  await runUpdate(page, center);

  const failed = row(center, "600519");
  await expect(failed).toContainText("2026-09-17"); // 旧行情仍在
  await expect(failed).toContainText("待补齐");
  await expect(failed).toContainText("夹具注入"); // 逐股失败原因可查
  await expect(row(center, "000001")).toContainText("已更新");

  // 来源恢复后手动重试未完成部分：只重取待补齐/待确认的部分
  writeFixtures(
    {
      bars: {
        "000001.SZ": [bar("2026-09-18", 11.2)],
        "600519.SH": [bar("2026-09-17", 1500.0), bar("2026-09-18", 1510.0)],
        "300750.SZ": [bar("2026-09-17", 200.0)],
        "920001.BJ": [bar("2026-09-17", 10.0)],
      },
    },
    statusPayload,
  );
  await center.getByRole("button", { name: /重试未完成/ }).click();
  await expect(row(center, "600519")).toContainText("已更新", { timeout: 30_000 });
  await expect(row(center, "600519")).toContainText("2026-09-18");

  // 更新记录仍可查
  await center.getByRole("button", { name: "更新记录" }).click();
  await expect(center.getByRole("list", { name: "更新记录" })).toBeVisible();
});

test("证券库失败单独提示，完整行情不被判为未更新", async ({ page }) => {
  writeFixtures(
    {
      bars: {
        "000001.SZ": [bar("2026-09-18", 11.2)],
        "300750.SZ": [bar("2026-09-17", 200.0)],
      },
      securities_error: "交易所名单获取失败（夹具注入）",
    },
    statusPayload,
  );
  await pull(page, "000001\n300750");

  const center = page.getByTestId("data-center");
  await expect(center.getByTestId("data-item-securities")).toContainText(
    "证券库更新失败，当前使用上次数据",
  );
  await expect(page.getByTestId("securities-notice")).toBeVisible();
  // 日线完整（停牌股豁免）→ 不把完整行情判为未更新
  await expect(center.getByTestId("data-item-quotes")).toContainText("已更新");
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
});

test("详情页可单股更新历史行情，且不加入持续更新范围", async ({ page }) => {
  // 300750 先加入观察组，再从归类卡片的详情单股更新
  await page.goto("/");
  await classifyText(page, "300750");
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  const picker = page.getByRole("dialog", { name: "选择观察组" });
  await expect(picker.getByRole("checkbox").first()).toBeVisible();
  await picker.getByRole("checkbox").first().check();
  await picker.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(page.getByRole("dialog", { name: "选择观察组" })).toHaveCount(0);

  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  await runUpdate(page, center);

  // 该股目标日为全天停牌（豁免日线缺失），详情仍提供单股更新入口
  await goModule(page, "观察组");
  const detail = page.getByTestId("security-detail");
  const single = detail.getByTestId("single-quote-update");
  await expect(single).toBeVisible();
  await single.click();
  await expect(detail.getByText(/单股更新完成/)).toBeVisible({ timeout: 30_000 });

  // 单股更新不改变持续更新范围：数据中心逐股行数仍为 1
  await goModule(page, "数据中心");
  await expect(page.getByTestId("data-center").locator("tbody tr")).toHaveCount(1);
});

/** 导入并进入数据中心（不触发整体更新，用于只关心证券库步骤的用例）。 */
async function pull(page: Page, codes: string) {
  await page.goto("/");
  await classifyText(page, codes);
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  await runUpdate(page, center);
  return center;
}
