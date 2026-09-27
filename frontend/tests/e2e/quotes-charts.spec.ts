import { expect, test, type Page } from "@playwright/test";
import {
  goModule,
  leaveLaunch,
  startClassification,
  startServer,
  type ServerHandle,
} from "./helpers";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";


const REPO_ROOT = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "..",
);

/**
 * 工单 06 的浏览器验收：真实 Chrome → 本机 HTTP → 临时真实数据库。
 * 行情走与 AKShare 同一 QuotesSource 边界的夹具来源（不访问外网），验证
 * 卡片 K 线与成交量、前复权标识、最新行情日期、向前浏览与重启读回，
 * 以及无行情时的明确缺失提示。
 */

let server: ServerHandle;
let fixturePath: string;

/** 生成 300 个交易日的日线：价格 10 → 20 递增，便于核对坐标不是零值缩放。 */
function buildBars(count = 300): Array<Record<string, number | string>> {
  const bars: Array<Record<string, number | string>> = [];
  let day = new Date(
    count > 300 ? "2023-10-02T00:00:00Z" : "2025-01-01T00:00:00Z",
  );
  let price = 10;
  while (bars.length < count) {
    const weekday = day.getUTCDay();
    if (weekday !== 0 && weekday !== 6) {
      const close = Number(price.toFixed(2));
      bars.push({
        date: day.toISOString().slice(0, 10),
        open: Number((close - 0.05).toFixed(2)),
        high: Number((close + 0.1).toFixed(2)),
        low: Number((close - 0.12).toFixed(2)),
        close,
        volume_lots: 100000 + bars.length * 1000,
        amount_yuan: close * 100 * (100000 + bars.length * 1000),
      });
      price += 20 / 300; // 300 天后到 30 左右，保证明显上升
    }
    day = new Date(day.getTime() + 86_400_000);
  }
  return bars;
}

test.beforeEach(async () => {
  fixturePath = join(
    mkdtempSync(join(tmpdir(), "dslite-quotes-")),
    "quotes.json",
  );
  writeFileSync(
    fixturePath,
    JSON.stringify({ bars: { "000001.SZ": buildBars() } }),
    "utf8",
  );
  server = await startServer(8799, undefined, {
    DSLITE_NOW: "2026-09-21T17:00:00+08:00",
    DSLITE_QUOTES_FIXTURE: fixturePath,
    DSLITE_UPDATE_SCHEDULE: "off",
    DSLITE_WENCAI_BASE: "http://127.0.0.1:1", // 本用例不涉及问财
  });
});

test.afterEach(async () => {
  await server?.stop();
});

async function enterClassification(page: Page, text: string) {
  await goModule(page, "导入");
  await page.locator("#import-text").fill(text);
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText(/已提交 \d+ 个来源/)).toBeVisible();
  await startClassification(page);
}

test("导入后卡片显示 K 线、成交量、前复权与最新行情日期，重启后读回", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await enterClassification(page, "000001");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 后台补行情：面板出现图表（有界等待，不假设时长）
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByTestId("candle-chart")).toBeVisible({
    timeout: 20_000,
  });
  await expect(panel.getByText("前复权", { exact: true })).toBeVisible();
  // 最新行情日期来自数据，非零值伪造
  await expect(panel.getByText(/最新 \d{4}-\d{2}-\d{2}/)).toBeVisible();
  // 少于最大 500 根时，默认显示全部 300 根。
  await expect(panel.getByTestId("chart-visible-range")).toContainText(
    "300 根",
  );
  // 单位标注：元与手
  await expect(panel.getByText(/价格单位：元 · 成交量单位：手/)).toBeVisible();

  // 页面核对真实数值：默认显示全部日线，末根对应夹具最后一天。
  const lastTitle = await panel.getByTestId("chart-readout").textContent();
  for (const value of [
    "2026-02-24",
    "开 29.88",
    "高 30.03",
    "低 29.81",
    "收 29.93",
  ])
    expect(lastTitle).toContain(value);
  // 成交量 100000+299*1000=399000 手，formatLots 显示为 39.9 万
  expect(lastTitle).toContain("量 39.90 万 手");
  await expect(panel.getByText("最新 2026-02-24")).toBeVisible();

  await context.close();

  // 重启服务：图表数据从数据库读回
  await server.stop();
  server = await startServer(8799, server.dataDir, {
    DSLITE_UPDATE_SCHEDULE: "off",
  });
  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  await page2.goto("/");
  await goModule(page2, "候选归类");
  await expect(page2.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(
    page2.getByTestId("quote-panel").getByTestId("candle-chart"),
  ).toBeVisible({
    timeout: 20_000,
  });
  await expect(
    page2.getByTestId("quote-panel").getByTestId("chart-visible-range"),
  ).toContainText("300 根");
  await context2.close();
});

test("图表坐标使用真实价格区间（非零值缩放），并可向前浏览更早历史", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await enterClassification(page, "000001");
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByTestId("candle-chart")).toBeVisible({
    timeout: 20_000,
  });

  await expect(panel.getByTestId("chart-readout")).toContainText("收 29.93");
  await expect(panel.getByText("已加载 300/300 日")).toBeVisible();
  await panel.getByRole("button", { name: "周线", exact: true }).click();
  await expect(panel.getByRole("img", { name: /周线 K 线/ })).toBeVisible();
  await panel.getByRole("button", { name: "月线", exact: true }).click();
  await expect(panel.getByRole("img", { name: /月线 K 线/ })).toBeVisible();
  await expect(panel.getByRole("button", { name: "看更早" })).toHaveCount(0);

  // 详情入口共用同一行情面板
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(
    page.getByTestId("stock-card").getByTestId("candle-chart"),
  ).toBeVisible();
  await expect(
    page.getByTestId("stock-card").getByText("前复权", { exact: true }),
  ).toBeVisible();
  await context.close();
});

test("无行情时明确提示缺失，不用零值伪造，也不影响研究处理", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 600519 不在夹具中：无行情
  await enterClassification(page, "600519");
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByText(/行情暂未取得/)).toBeVisible({
    timeout: 20_000,
  });
  await expect(panel.getByTestId("candle-chart")).toHaveCount(0);
  // 研究处理不受影响
  await expect(page.getByRole("button", { name: "稍后处理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "暂不关注" })).toBeVisible();
  await context.close();
});

test("手动更新展示成功与数据日期，并刷新卡片行情", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await enterClassification(page, "000001");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "数据中心" }).click();
  const center = page.getByTestId("data-center");
  await expect(center).toBeVisible();
  await center.getByRole("button", { name: "更新数据" }).click();

  // 更新结果如实展示：分项状态与逐股结果
  await expect(center.getByText(/最近一次：/)).toBeVisible({ timeout: 20_000 });
  await expect(center.getByTestId("data-item-securities")).toBeVisible();
  await context.close();
});

test("A25：授权历史库回放样本经同一行情边界在页面按真实数值呈现", async ({
  browser,
}) => {
  // 用 tools/build_quote_fixture.py 生成的真实历史样本（000001.SZ，2019 起，通达信原始日线）
  const replay = join(
    REPO_ROOT,
    "backend",
    "tests",
    "fixtures",
    "quote_replay_sample.json",
  );
  test.skip(
    !existsSync(replay),
    "未生成历史回放夹具（tools/build_quote_fixture.py）",
  );
  await server.stop();
  server = await startServer(8799, undefined, {
    DSLITE_NOW: "2026-09-21T17:00:00+08:00",
    DSLITE_QUOTES_FIXTURE: replay,
    DSLITE_UPDATE_SCHEDULE: "off",
  });

  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await enterClassification(page, "000001");
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByTestId("candle-chart")).toBeVisible({
    timeout: 20_000,
  });

  // 来源口径如实标注为「不复权」，不冒充前复权
  await expect(panel.getByText("不复权", { exact: true })).toBeVisible();
  // 最新数据日期为真实样本末行
  await expect(panel.getByText("最新 2026-09-03")).toBeVisible();

  // 页面核对真实收盘与成交量：末根 11.88 元、110.5 万手
  const lastTitle = await panel.getByTestId("chart-readout").textContent();
  expect(lastTitle).toContain("2026-09-03");
  expect(lastTitle).toContain("收 11.88");
  expect(lastTitle).toContain("量 110.51 万 手");

  await context.close();
});

test("后台更新结束后，更新面板自动同步终态并可再次触发", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 用协议拦截模拟「面板首次读取时更新仍在运行，随后后台结束」：
  // 首次 GET /api/updates 返回 running，之后返回终态。面板必须轮询到终态，
  // 否则按钮永久禁用，用户无法再次触发更新。
  let finished = false;
  await page.route("**/api/updates", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    const running = !finished;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        running,
        lastRun: running
          ? null
          : {
              runId: "update-simulated",
              kind: "startup",
              status: "success",
              startedAt: "2026-09-12T16:30:00",
              finishedAt: "2026-09-12T16:31:00",
              securitiesStatus: "ok",
              securitiesMessage: "证券库已更新为 5551 只",
              securitiesCount: 5551,
              quotesOk: 3,
              quotesFailed: 0,
              quotesSkipped: 0,
              quotesPending: 0,
              failedSecurities: [],
            },
      }),
    });
  });

  await page.goto("/");
  await leaveLaunch(page);
  await page.getByRole("button", { name: "数据中心" }).click();
  const center = page.getByTestId("data-center");
  const button = center.getByRole("button", { name: "更新数据" });
  await expect(button).toBeDisabled(); // 首次读取为「进行中」
  finished = true;

  // 背景轮询到终态：按钮恢复可用，更新面板与数据中心仍可继续使用
  await expect(button).toBeEnabled({ timeout: 10_000 });
  await expect(center.getByRole("progressbar", { name: "更新进度" })).toBeVisible();
  await context.close();
});

test("翻页回包若属于旧数据版本则不合并，图表不混用新旧价格", async ({
  browser,
}) => {
  await server.stop();
  writeFileSync(
    fixturePath,
    JSON.stringify({ bars: { "000001.SZ": buildBars(700) } }),
  );
  server = await startServer(8799, server.dataDir, {
    DSLITE_NOW: "2026-09-21T17:00:00+08:00",
    DSLITE_QUOTES_FIXTURE: fixturePath,
  });
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 拦截「看更早」的翻页请求，返回一个不同数据版本（fetchedAt 不同）的旧价回包：
  // 若面板只按证券+日期合并，会把早期旧价画进当前图表（混用不同前复权版本）。
  await page.route("**/api/quotes/000001.SZ*", async (route) => {
    const url = new URL(route.request().url());
    if (!url.searchParams.get("end")) {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        securityId: "000001.SZ",
        available: true,
        reason: null,
        adjust: "qfq",
        adjustLabel: "前复权",
        source: "fixture",
        latestDate: "2024-01-02",
        earliestDate: "2023-01-02",
        total: 300,
        fetchedAt: "2019-01-01T00:00:00",
        bars: [
          {
            date: "2023-01-02",
            open: 1.23,
            high: 1.23,
            low: 1.23,
            close: 1.23,
            volumeLots: 1,
            amountYuan: 1,
          },
        ],
      }),
    });
  });

  await enterClassification(page, "000001");
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByRole("button", { name: "重试历史" })).toBeVisible();
  await expect(panel.getByTestId("chart-visible-range")).toContainText("309 根");
  await panel.getByRole("button", { name: "看更早" }).click();
  // 版本不一致：不合并旧回包，图表仍是当前版本的一页，绝不含旧版本的 1.23
  await expect(panel.getByTestId("chart-visible-range")).toContainText(
    "309 根",
  );
  await expect(panel.getByTestId("chart-readout")).not.toContainText("收 1.23");
  await expect(panel.getByText("已加载 309/700 日")).toBeVisible();
  await context.close();
});

test("同股刷新失败保留已有K线并可见提示", async ({ page }) => {
  await page.goto("/");
  await enterClassification(page, "000001");
  const chart = page.getByTestId("candle-chart");
  await expect(chart).toBeVisible({ timeout: 20_000 });
  const original = await page.getByTestId("chart-readout").textContent();
  await page.route("**/api/quotes/**", (route) => route.abort());
  await page.getByRole("button", { name: "数据中心" }).click();
  const center = page.getByTestId("data-center");
  await center.getByRole("button", { name: "更新数据" }).click();
  await expect(center.getByText(/最近一次：/)).toBeVisible({
    timeout: 20_000,
  });
  await goModule(page, "候选归类");
  await expect(
    page.getByTestId("quote-panel").getByRole("alert"),
  ).toContainText("已保留上次行情");
  await expect(chart).toBeVisible();
  await expect(page.getByTestId("chart-readout")).toHaveText(original!);
});
