import { expect, test, type Page } from "@playwright/test";
import {
  closePanel,
  goModule,
  openSettings,
  startClassification,
  startServer,
  type ServerHandle,
} from "./helpers";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { leaveLaunch } from "./helpers";
import { startWencaiStub } from "./wencai-stub";

/**
 * 工单 07 集成验收：真实 Chrome → 本机 HTTP → 临时真实数据库 → 重启读回。
 *
 * 只在真实页面做一次贯穿旅程，不预写研究结果、不注入成功：混合导入
 * （文件 + 文本 + 问财链接）→ 权威识别 → 行情图表 → 观察组 → 笔记 →
 * 本轮结束；随后停止并重启服务，从页面读回同一结果与进度。
 * 另验证"证券库更新本身不自动重识别，只在用户触发时补卡且保留原导入日期"。
 *
 * 行情走与 AKShare 同一 QuotesSource 边界的夹具来源（不访问外网）；
 * 问财走本地协议桩（真实适配器链路）。实源范围与真实会话过期未验证项见工单 Comments。
 */

let server: ServerHandle;
let stub: Awaited<ReturnType<typeof startWencaiStub>>;
let quotesFixture: string;
let growthFixture: string;

/** 约 300 个交易日：价格 10 → 20 递增，便于核对图表与最新日期。 */
function buildBars(): Array<Record<string, number | string>> {
  const bars: Array<Record<string, number | string>> = [];
  let day = new Date("2025-01-01T00:00:00Z");
  let price = 10;
  while (bars.length < 300) {
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
      price += 20 / 300;
    }
    day = new Date(day.getTime() + 86_400_000);
  }
  return bars;
}

const WENCAI_URL =
  "https://www.iwencai.com/screener/result?w=%E5%88%9B%E6%96%B0%E9%AB%98&querytype=stock&sign=1";

test.beforeAll(() => {
  const dir = mkdtempSync(join(tmpdir(), "dslite-journey-"));
  // 000001.SZ 有真实日线；其余股票无行情，用于验证缺行情不阻塞处理
  quotesFixture = join(dir, "quotes.json");
  writeFileSync(
    quotesFixture,
    JSON.stringify({ bars: { "000001.SZ": buildBars() } }),
    "utf8",
  );
  // 证券库更新夹具：名单含 999001.SZ（初始快照不含该代码），用于验证重识别仅在用户触发时发生
  growthFixture = join(dir, "growth.json");
  writeFileSync(
    growthFixture,
    JSON.stringify({
      securities: [
        { code: "999001", exchange: "SZ", board: "main", name: "示例股票" },
      ],
      bars: {
        "999001.SZ": [
          {
            date: "2026-09-10",
            open: 7.51,
            high: 7.6,
            low: 7.4,
            close: 7.55,
            volume_lots: 512000,
            amount_yuan: 3.86e8,
          },
        ],
      },
    }),
    "utf8",
  );
});

test.beforeEach(async () => {
  stub = await startWencaiStub();
  server = await startServer(8799, undefined, {
    DSLITE_QUOTES_FIXTURE: quotesFixture,
    DSLITE_UPDATE_SCHEDULE: "off",
    DSLITE_WENCAI_BASE: stub.baseUrl,
  });
});

test.afterEach(async () => {
  await server?.stop();
  await stub?.stop();
});

async function setCookie(page: Page) {
  await openSettings(page);
  await page.getByLabel("问财 Cookie").fill("stub-cookie");
  await page.getByRole("button", { name: "保存 / 替换 Cookie" }).click();
  await expect(page.getByText("Cookie 已配置")).toBeVisible();
  await closePanel(page);
}

async function enterClassification(page: Page, text: string) {
  await goModule(page, "导入");
  await page.locator("#import-text").fill(text);
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText(/已提交 \d+ 个来源/)).toBeVisible();
  await startClassification(page);
}

test("完整旅程：混合导入 → 识别 → 行情 → 观察 → 笔记 → 结束，重启后页面读回", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 新打开应用先看到启动页；从入口进入模块后才有顶部导航
  await expect(page.getByTestId("launch-page")).toBeVisible();
  await leaveLaunch(page);
  await expect(page.getByRole("button", { name: "返回启动页" })).toBeVisible();
  await setCookie(page);

  // 1) 一次混合提交：文件（含未识别项）+ 纯代码文本块 + 问财链接，各自成批次
  const csv = join(server.dataDir, "journey.csv");
  writeFileSync(
    csv,
    [
      "代码,名称,最新价",
      "000001,平安银行,11.50",
      "999999,不存在的股票,1.00",
    ].join("\n") + "\n",
    "utf-8",
  );
  await goModule(page, "导入");
  await page.setInputFiles('input[type="file"]', csv);
  await expect(page.getByText("journey.csv")).toBeVisible();
  await page
    .getByLabel("股票代码、表格文本或问财链接")
    .fill(`300750\n${WENCAI_URL}`);
  await page.getByRole("button", { name: "提交", exact: true }).click();

  // 三个来源各成批次：记录里各一条，结果区展示选中的那一条
  const history = page.getByRole("list", { name: "导入记录列表" });
  await expect(history.getByRole("button")).toHaveCount(3);
  const openBatch = (name: string) =>
    history.getByRole("button", { name: new RegExp(name) }).first().click();

  // 文件批次：部分未识别，可追溯到原始位置；来源附带价格不进工作台
  await openBatch("journey.csv");
  const fileBatch = page.locator('[data-testid^="batch-card-"]').first();
  await expect(fileBatch.getByText("已导入 1 只，跳过 1 只")).toBeVisible();
  await fileBatch.getByText("查看 1 只未识别股票").click();
  await expect(fileBatch.getByText("999999").first()).toBeVisible();
  // 文本块批次单独成批
  await expect(history.getByRole("button", { name: /粘贴文本/ })).toHaveCount(1);
  // 链接批次：首次要确认实际解析条件
  await openBatch("问财链接");
  const linkBatch = page.locator('[data-testid^="batch-card-"]').first();
  await expect(linkBatch.getByText("待确认条件")).toBeVisible();
  await expect(linkBatch.getByText("创新高").first()).toBeVisible();
  await linkBatch.getByTestId("confirm-condition").click();
  await expect(linkBatch.getByText(/已导入 3 只/)).toBeVisible();
  await expect(page.getByText("11.50")).toHaveCount(0);

  await startClassification(page);
  // 2) 卡片出现，行情后台补取后在页面按真实数值呈现
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByTestId("candle-chart")).toBeVisible({
    timeout: 20_000,
  });
  await expect(panel.getByText("前复权", { exact: true })).toBeVisible();
  await expect(panel.getByText("最新 2026-02-24")).toBeVisible();
  // 默认视口最多 500 根；此夹具不足上限，应显示全部 300 根及完整日期范围。
  await expect(panel.getByTestId("chart-visible-range")).toHaveText(
    "可见 2025-01-01 — 2026-02-24 · 300 根",
  );
  const lastTitle = await panel.getByTestId("chart-readout").textContent();
  expect(lastTitle).toContain("2026-02-24");
  expect(lastTitle).toContain("收 29.93");

  // 3) 观察组：新建并加入；写笔记不完成研究，加入观察才完成
  await page.getByRole("button", { name: "观察组" }).click();
  await expect(page.getByTestId("observation-groups")).toBeVisible();
  await page.getByLabel("新观察组名称").fill("集成跟踪");
  await page.getByRole("button", { name: "新建" }).click();
  await expect(
    page.getByRole("list", { name: "观察组列表" }).getByText("集成跟踪"),
  ).toBeVisible();

  // 回到候选归类继续当前股票（各模块独立保留位置）
  await goModule(page, "候选归类");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "新增笔记", exact: true }).click();
  await page
    .getByRole("dialog", { name: /新增笔记/ })
    .getByLabel("新增笔记")
    .fill("集成旅程笔记：关注资产质量");
  await page
    .getByRole("dialog", { name: /新增笔记/ })
    .getByRole("button", { name: "保存笔记" })
    .click();
  await expect(page.getByRole("dialog", { name: /新增笔记/ })).toHaveCount(0);
  await expect(page.getByTestId("stock-card").getByText("1 条")).toBeVisible();
  // 写笔记后仍停留在当前卡且未处理
  await expect(page.getByRole("button", { name: "暂不关注" })).toBeVisible();

  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  await page.getByRole("checkbox", { name: /集成跟踪/ }).check();
  await page.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(
    page.getByTestId("stock-card").getByText("已观察"),
  ).toBeVisible();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 已观察股票的新来源：同日再次导入同一只，不新建研究项、不重置已观察，
  // 只把来源追加到原研究项（观察关系与处理状态保留）
  await enterClassification(page, "000001");
  // 待处理批次优先、其余按接收时间倒序：最新提交的这一批在最前
  await goModule(page, "导入");
  const extraSource = page.locator('[data-testid^="batch-card-"]').first();
  // 同一天已入选过：既不新建也不再次触发归类，只追加来源
  await expect(extraSource.getByTestId("stat-new")).toHaveText("0");
  await expect(extraSource.getByTestId("stat-existing")).toHaveText("0");
  await expect(extraSource.getByText("仅追加来源")).toBeVisible();
  await goModule(page, "候选归类");
  await expect(
    page.getByTestId("stock-card").getByText("已观察"),
  ).toBeVisible();
  await expect(
    page.getByTestId("stock-card").getByText(/来源与处理记录（3）/),
  ).toBeVisible();
  await expect(
    page
      .getByTestId("stock-card")
      .getByText("已加入观察组，已完成本次归类。"),
  ).toBeVisible();

  // 4) 详情与卡片共用同一笔记与行情面板
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  const detail = page.getByTestId("note-all-panel");
  await expect(detail.getByText("集成旅程笔记：关注资产质量")).toBeVisible();
  await expect(
    page.getByTestId("stock-card").getByTestId("candle-chart"),
  ).toBeVisible();
  await closePanel(page);
  await expect(page.getByTestId("stock-card")).toBeVisible();

  // 5) 浏览剩余两只：缺行情不阻塞处理，稍后处理留在未处理池
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await expect(
    page.getByTestId("quote-panel").getByText(/行情暂未取得/),
  ).toBeVisible({
    timeout: 20_000,
  });
  await page.getByRole("button", { name: "稍后处理" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  // 三只都看过一轮：再前进进入结束卡，稍后处理的宁德时代留在待归类池。
  await page.getByRole("button", { name: "下一个" }).click();

  const end = page.getByTestId("end-card");
  await expect(end).toBeVisible();
  // 本轮真实统计：浏览 3 只、处理 1 只（加入观察）、仍有 2 只未处理
  // （贵州茅台只浏览未决策，宁德时代稍后处理仍属未处理池）
  await expect(end.getByTestId("round-viewed")).toHaveText("3");
  await expect(end.getByTestId("round-processed")).toHaveText("1");
  await expect(end.getByTestId("round-remaining")).toHaveText("2");
  await context.close();

  // 6) 停止并重启服务：从真实页面读回同一结果与进度
  await server.stop();
  server = await startServer(8799, server.dataDir, {
    DSLITE_QUOTES_FIXTURE: quotesFixture,
    DSLITE_UPDATE_SCHEDULE: "off",
  });

  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  await page2.goto("/");

  // 导入事实与统计读回：从导入记录里选回 journey.csv
  await goModule(page2, "导入");
  await page2
    .getByRole("list", { name: "导入记录列表" })
    .getByRole("button", { name: /journey.csv/ })
    .first()
    .click();
  const restored = page2.locator('[data-testid^="batch-card-"]').first();
  await expect(restored.getByText("已导入 1 只，跳过 1 只")).toBeVisible();
  await restored.getByText("查看 1 只未识别股票").click();
  await expect(restored.getByText("999999").first()).toBeVisible();

  // 结束卡及未处理项跨重启保留。
  await goModule(page2, "候选归类");
  await expect(page2.getByTestId("end-card")).toBeVisible();
  await page2.getByRole("button", { name: "上一个" }).click();
  await expect(page2.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(
    page2.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);
  // 观察组与成员读回
  await goModule(page2, "观察组");
  await expect(
    page2.getByRole("list", { name: "观察组列表" }).getByText(/集成跟踪/),
  ).toBeVisible();
  // 已处理范围：平安银行已观察；笔记与图表读回
  await goModule(page2, "候选归类");
  await page2.getByRole("tab", { name: /已处理/ }).click();
  await expect(
    page2.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
  await page2
    .getByRole("button", { name: /查看全部笔记/ })
    .first()
    .click();
  const detail2 = page2.getByTestId("note-all-panel");
  await expect(
    page2.getByTestId("stock-card").getByTestId("candle-chart"),
  ).toBeVisible({
    timeout: 20_000,
  });
  await expect(detail2.getByText("集成旅程笔记：关注资产质量")).toBeVisible();
  await context2.close();
});

test("证券库更新本身不重识别；用户触发后补入并保留原导入日期", async ({
  browser,
}) => {
  // 换用含 000002.SZ 的证券库夹具，模拟"证券库在下一次更新中才拿到该股票"
  await server.stop();
  server = await startServer(8799, undefined, {
    DSLITE_QUOTES_FIXTURE: growthFixture,
    DSLITE_UPDATE_SCHEDULE: "off",
  });

  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 当前证券库不含 999001：全部未识别，不建卡
  const csv = join(server.dataDir, "unknown.csv");
  writeFileSync(csv, "代码\n999001\n", "utf-8");
  await goModule(page, "导入");
  await page.setInputFiles('input[type="file"]', csv);
  await page.getByRole("button", { name: "提交", exact: true }).click();
  const batch = page
    .locator('[data-testid^="batch-card-"]')
    .filter({ hasText: "unknown.csv" });
  await expect(batch.getByText("未导入任何股票：全部未识别")).toBeVisible();
  const importDate = await batch.getByTestId("stat-date").textContent();
  await goModule(page, "候选归类");
  await expect(
    page.getByText("没有待归类的股票，先在导入页提交候选。"),
  ).toBeVisible();

  // 用户手动更新：证券库改为含 000002；更新本身不得自动补入研究项
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  await center.getByRole("button", { name: "更新数据" }).click();
  await expect(center.getByText(/最近一次：/)).toBeVisible({ timeout: 20_000 });
  await expect(center.getByTestId("data-item-securities")).toContainText("已更新 1 只");
  await goModule(page, "导入");
  await expect(batch.getByText("未导入任何股票：全部未识别")).toBeVisible();
  await goModule(page, "候选归类");
  await expect(
    page.getByText("没有待归类的股票，先在导入页提交候选。"),
  ).toBeVisible();

  // 用户触发重新识别：按原导入日期补入，并立即补行情
  await goModule(page, "导入");
  await batch.getByRole("button", { name: "重新识别跳过项" }).click();
  await expect(batch.getByText("已导入 1 只，跳过 0 只")).toBeVisible();
  await expect(batch.getByTestId("stat-date")).toHaveText(importDate ?? "");
  await goModule(page, "候选归类");
  await expect(page.getByRole("heading", { name: "示例股票" })).toBeVisible();
  await expect(
    page.getByTestId("quote-panel").getByTestId("candle-chart"),
  ).toBeVisible({
    timeout: 20_000,
  });
  await context.close();
});
