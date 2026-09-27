import { expect, test, type Page } from "@playwright/test";
import {
  closePanel,
  goModule,
  startClassification,
  startServer,
  type ServerHandle,
} from "./helpers";
import { writeFileSync } from "node:fs";
import { join } from "node:path";
let server: ServerHandle;
const bars: {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume_lots: number;
  amount_yuan: number;
}[] = [];
let day = new Date("2023-10-02T00:00:00Z");
while (bars.length < 700) {
  if (day.getUTCDay() !== 0 && day.getUTCDay() !== 6) {
    const close = bars.length + 10;
    bars.push({
      date: day.toISOString().slice(0, 10),
      open: close + (bars.length % 3 === 0 ? 1 : -1),
      high: close + 2,
      low: close - 2,
      close,
      volume_lots: 1000 + bars.length,
      amount_yuan: close * 100000,
    });
  }
  day = new Date(day.getTime() + 86400000);
}
test.beforeEach(async () => {
  server = await startServer(8799);
  await server.stop();
  const path = join(server.dataDir, "quotes.json");
  writeFileSync(
    path,
    JSON.stringify({
      bars: {
        "000001.SZ": bars,
        "600519.SH": bars.map((b) => ({
          ...b,
          open: b.open + 1000,
          high: b.high + 1000,
          low: b.low + 1000,
          close: b.close + 1000,
        })),
      },
    }),
  );
  server = await startServer(8799, server.dataDir, {
    DSLITE_QUOTES_FIXTURE: path,
    DSLITE_NOW: "2026-09-21T17:00:00+08:00",
  });
});
test.afterEach(async () => {
  await server?.stop();
});
async function enter(page: Page) {
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001\n600519");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await startClassification(page);
  await expect(page.getByTestId("candle-chart")).toBeVisible({
    timeout: 20000,
  });
}
test("各周期打开默认显示最多K线，历史补齐扩展默认范围，手动缩放后往返保留", async ({ page }) => {
  await enter(page);
  const visible = page.getByTestId("chart-visible-range");
  await expect(visible).toContainText("500 根");
  await expect(visible).toContainText("2024-07-08 — 2026-06-05");
  await expect(page.getByRole("button", { name: "缩小图表" })).toBeDisabled();
  for (const [label, count] of [["周线", 140], ["月线", 33], ["日线", 500]] as const) {
    await page.getByRole("button", { name: label, exact: true }).click();
    await expect(visible).toContainText(`${count} 根`);
    await expect(visible).toContainText("2026-06-05");
    await expect(page.getByRole("button", { name: "缩小图表" })).toBeDisabled();
  }
  await page.getByRole("button", { name: "放大图表" }).click();
  await expect(visible).toContainText("400 根");
  const chosen = await visible.textContent();
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(visible).toHaveText(chosen!);
  await closePanel(page);
  await expect(visible).toHaveText(chosen!);
  await page.getByRole("button", { name: "下一个", exact: true }).click();
  await expect(visible).toContainText("500 根");
  await page.reload();
  await expect(visible).toContainText("500 根");
});

test("日周月切换按交易周期汇总价格与成交量，笔记往返保留周期", async ({ page }) => {
  const sample = bars.slice(0, -7); // 截至 2026-05-27：未结束的周与月长度不同。
  writeFileSync(join(server.dataDir, "quotes.json"), JSON.stringify({ bars: { "000001.SZ": sample } }));
  await enter(page);
  const panel = page.getByTestId("quote-panel");
  for (const removed of ["三个月", "半年", "一年"])
    await expect(panel.getByRole("button", { name: removed, exact: true })).toHaveCount(0);
  for (const [label, from] of [["周线", "2026-05-25"], ["月线", "2026-05-01"]] as const) {
    await panel.getByRole("button", { name: label, exact: true }).click();
    await expect(panel.getByRole("button", { name: label, exact: true })).toHaveAttribute("aria-pressed", "true");
    await expect(panel.getByText("已加载 693/693 日")).toBeVisible();
    await page.mouse.move(1, 1);
    const rows = sample.filter((b) => b.date >= from);
    const read = panel.getByTestId("chart-readout");
    await expect(read).toContainText(`开 ${rows[0].open.toFixed(2)}`);
    await expect(read).toContainText(`高 ${Math.max(...rows.map((b) => b.high)).toFixed(2)}`);
    await expect(read).toContainText(`低 ${Math.min(...rows.map((b) => b.low)).toFixed(2)}`);
    await expect(read).toContainText(`收 ${rows.at(-1)!.close.toFixed(2)}`);
    const volume = rows.reduce((sum, b) => sum + b.volume_lots, 0);
    await expect(read).toContainText(`量 ${volume >= 10000 ? `${(volume / 10000).toFixed(2)} 万` : volume.toLocaleString("zh-CN")} 手`);
    const ends = label === "周线"
      ? ["2026-03-27", "2026-04-03", "2026-04-10", "2026-04-17", "2026-04-24", "2026-05-01", "2026-05-08", "2026-05-15", "2026-05-22", "2026-05-27"]
      : ["2025-08-29", "2025-09-30", "2025-10-31", "2025-11-28", "2025-12-31", "2026-01-30", "2026-02-27", "2026-03-31", "2026-04-30", "2026-05-27"];
    if (label === "周线") await panel.getByRole("button", { name: "MA10", exact: true }).click();
    const average = ends.reduce((sum, date) => sum + sample.find((b) => b.date === date)!.close, 0) / 10;
    await expect(read).toContainText(`MA10 ${average.toFixed(2)}`);
    await expect(panel.getByRole("img", { name: new RegExp(`000001.SZ ${label}`) })).toBeVisible();
  }
  const monthlyRange = await panel.getByTestId("chart-visible-range").textContent();
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(page.getByRole("button", { name: "月线", exact: true, includeHidden: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByTestId("chart-visible-range")).toHaveText(monthlyRange!);
  await closePanel(page);
  await panel.getByRole("button", { name: "日线", exact: true }).click();
  await expect(panel.getByTestId("chart-readout")).toContainText(`量 1,692 手`);
  await expect(panel.getByRole("button", { name: "日线", exact: true })).toHaveAttribute("aria-pressed", "true");
});

test("周线跨年合并同一自然周，月线在年初分开，缺交易日不补零", async ({ page }) => {
  const sample = ["2025-12-26", "2025-12-29", "2025-12-30", "2025-12-31", "2026-01-02"].map((date, i) => ({
    date, open: 10 + i, high: 20 + i, low: 5 + i, close: 15 + i, volume_lots: 100 * (i + 1), amount_yuan: null,
  }));
  writeFileSync(join(server.dataDir, "quotes.json"), JSON.stringify({ bars: { "000001.SZ": sample } }));
  await enter(page);
  await expect(page.getByText("已加载 5/5 日")).toBeVisible();
  await page.getByRole("button", { name: "周线", exact: true }).click();
  const read = page.getByTestId("chart-readout");
  await expect(read).toContainText("日期 2026-01-02");
  await expect(read).toContainText("开 11.00高 24.00低 6.00收 19.00量 1,400 手");
  await expect(page.getByTestId("chart-visible-range")).toContainText("2 根");
  await expect(page.getByRole("button", { name: "放大图表" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "缩小图表" })).toBeDisabled();
  await page.getByRole("button", { name: "月线", exact: true }).click();
  await expect(read).toContainText("开 14.00高 24.00低 9.00收 19.00量 500 手");
});

test("Canvas首屏、均线、范围、真实滚轮拖动、主题与笔记往返", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  const requests: URL[] = [];
  page.on("request", (req) => {
    if (req.url().includes("/api/quotes/000001"))
      requests.push(new URL(req.url()));
  });
  await enter(page);
  const visible = page.getByTestId("chart-visible-range");
  await expect(visible).toContainText("500 根");
  expect(requests[0].searchParams.get("limit")).toBe("309");
  await expect(
    page.getByTestId("candle-chart").locator("canvas").first(),
  ).toBeVisible();
  const read = page.getByTestId("chart-readout");
  await expect(read).toContainText("收 709.00");
  await expect(
    page.getByRole("button", { name: "MA10", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
  for (const n of [10, 20, 60])
    await page.getByRole("button", { name: `MA${n}`, exact: true }).click();
  await expect(read).toContainText("MA10 704.50");
  await expect(read).toContainText("MA20 699.50");
  await expect(read).toContainText("MA60 679.50");
  for (let i = 0; i < 6; i++) {
    const old = await visible.textContent();
    await page.getByRole("button", { name: "放大图表" }).click();
    await expect(visible).not.toHaveText(old!);
  }
  await expect(visible).toContainText("131 根");
  await page.getByRole("button", { name: "放大图表" }).click();
  await expect(visible).toContainText("105 根");
  const before = await visible.textContent();
  await page.getByRole("button", { name: "主题" }).click();
  await page.getByRole("menuitemradio", { name: "深色", exact: true }).click();
  await expect(visible).toHaveText(before!);
  await page.getByRole("button", { name: "收起队列", exact: true }).click();
  await expect(visible).toHaveText(before!);
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(visible).toHaveText(before!);
  await closePanel(page);
  await expect(visible).toHaveText(before!);
  await expect(
    page.getByRole("button", { name: "日线", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const plot = page.getByRole("img", { name: /000001.SZ 日线/ });
  await plot.scrollIntoViewIfNeeded();
  const box = (await plot.boundingBox())!;
  await page.mouse.move(box.x + box.width * 0.5, box.y + 100);
  await page.mouse.wheel(0, -180);
  await expect(visible).not.toHaveText(before!);
  await page.getByRole("button", { name: "回到最新", exact: true }).click();
  await page.mouse.move(box.x + box.width * 0.35, box.y + 110);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width * 0.75, box.y + 110, { steps: 15 });
  await page.mouse.up();
  await expect
    .poll(() => requests.some((u) => u.searchParams.has("end")))
    .toBe(true);
  await page.mouse.move(10, 10);
  await page.screenshot({
    path: info.outputPath("desktop-dark.png"),
    animations: "disabled",
  });
  await page.getByRole("button", { name: "回到最新" }).click();
  await expect(read).toContainText("收 709.00");
  await page.getByRole("button", { name: "下一个", exact: true }).click();
  await expect(visible).toContainText("500 根");
  await expect(read).toContainText("收 1709.00");
  await expect(
    page.getByRole("button", { name: "MA60", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await goModule(page, "候选归类");
  await expect(
    page.getByRole("button", { name: "MA60", exact: true }),
  ).toHaveAttribute("aria-pressed", "false");
});

for (const boundary of ["最少", "最多"] as const) {
  test(`达到${boundary}根数后继续缩放不能移动日期范围`, async ({ page }) => {
    await page.setViewportSize({ width: 1092, height: 871 });
    await enter(page);
    const zoomIn = boundary === "最少";
    const button = page.getByRole("button", { name: zoomIn ? "放大图表" : "缩小图表" });
    const visible = page.getByTestId("chart-visible-range");
    await expect(visible).toContainText("500 根");
    for (let i = 0; i < (zoomIn ? 13 : 0); i++) {
      const old = await visible.textContent();
      await button.click();
      await expect(visible).not.toHaveText(old!);
    }
    await expect(visible).toContainText(zoomIn ? "30 根" : "500 根");
    const before = await visible.textContent();
    const plot = page.getByRole("img", { name: /000001.SZ 日线/ });
    await plot.scrollIntoViewIfNeeded();
    const box = (await plot.boundingBox())!;
    await page.mouse.move(box.x + box.width * 0.25, box.y + 100);
    for (let i = 0; i < 6; i++) {
      await page.mouse.wheel(0, zoomIn ? -180 : 180);
      await page.waitForTimeout(100);
    }
    await expect(visible).toHaveText(before!);
    await expect(button).toBeDisabled();
    // 反方向的缩放仍可用，不能把图表整个锁死。
    await page.mouse.wheel(0, zoomIn ? 180 : -180);
    await expect(visible).not.toHaveText(before!);
    await expect(button).toBeEnabled();
  });
}

test("默认历史加载期间手动缩放后图表稳定，加载后保留选定日期范围", async ({ page }) => {
  await page.setViewportSize({ width: 1092, height: 871 });
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let calls = 0;
  await page.route("**/api/quotes/000001.SZ*", async (route) => {
    if (!new URL(route.request().url()).searchParams.has("end")) return route.continue();
    calls++;
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  try {
    await enter(page);
    await expect(page.getByText(/已加载 309\//)).toBeVisible();
    await page.getByRole("button", { name: "放大图表" }).click();
    await expect(page.getByTestId("chart-visible-range")).toContainText("247 根");
    await page.getByRole("button", { name: "缩小图表" }).click();
    await expect(page.getByText("正在加载更早行情…")).toBeVisible();
    const plot = page.getByRole("img", { name: /000001.SZ 日线/ });
    await page.mouse.move(1, 1);
    await expect(page.getByTestId("chart-visible-range")).toContainText("309 根");
    const stable = await page.getByTestId("chart-visible-range").textContent();
    const first = await plot.screenshot();
    await page.waitForTimeout(700);
    const second = await plot.screenshot();
    expect(second.equals(first), "历史请求等待期间 Canvas 不应持续重绘抖动").toBe(true);
    expect(calls).toBe(1);
    release();
    await expect(page.getByText(/已加载 558\//)).toBeVisible();
    await expect(page.getByText("正在加载更早行情…")).toHaveCount(0);
    await expect(page.getByTestId("chart-visible-range")).toHaveText(stable!);
    const settled = await plot.screenshot();
    await page.waitForTimeout(700);
    expect((await plot.screenshot()).equals(settled), "历史加载完成后 Canvas 不应持续抖动").toBe(true);
    expect(calls).toBe(1);
  } finally { release(); }
});

test("自动历史页版本不同但当前版本未变化时暂停，不能反复加载抖动", async ({ page }) => {
  await page.setViewportSize({ width: 1092, height: 871 });
  let calls = 0;
  await page.route("**/api/quotes/000001.SZ*", async (route) => {
    if (!new URL(route.request().url()).searchParams.has("end")) return route.continue();
    calls++;
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, fetchedAt: "2026-09-20T17:00:00+08:00" } });
  });
  await enter(page);
  await expect(page.getByRole("button", { name: "重试历史" })).toBeVisible();
  await expect(page.getByText("正在加载更早行情…")).toHaveCount(0);
  expect(calls).toBe(1);
  const plot = page.getByRole("img", { name: /000001.SZ 日线/ });
  await page.mouse.move(1, 1);
  const before = await plot.screenshot();
  await page.waitForTimeout(700);
  expect((await plot.screenshot()).equals(before)).toBe(true);
  expect(calls).toBe(1);
  await expect(page.getByText("已加载 309/700 日")).toBeVisible();
});

test("历史失败暂停自动请求，重试去重后日期和均线不跳位", async ({ page }) => {
  await page.setViewportSize({ width: 1366, height: 768 });
  let calls = 0;
  let fail = true;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/quotes/000001.SZ*", async (route) => {
    if (!new URL(route.request().url()).searchParams.has("end"))
      return route.continue();
    calls++;
    if (fail) return route.abort();
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response });
  });
  await enter(page);
  const plot = page.getByRole("img", { name: /000001.SZ 日线/ });
  await plot.scrollIntoViewIfNeeded();
  const box = (await plot.boundingBox())!;
  const drag = async () => {
    await page.mouse.move(box.x + box.width * 0.25, box.y + 80);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.55, box.y + 80, { steps: 12 });
    await page.mouse.up();
    await page.mouse.move(1, 1);
  };
  await drag();
  await expect(
    page.getByRole("button", { name: "重试历史", includeHidden: true }),
  ).toBeVisible();
  expect(calls).toBe(1);
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(
    page.getByRole("button", { name: "重试历史", includeHidden: true }),
  ).toBeVisible();
  await closePanel(page);
  await expect(
    page.getByRole("button", { name: "重试历史", includeHidden: true }),
  ).toBeVisible();
  expect(calls).toBe(1);
  const before = await page.getByTestId("chart-visible-range").textContent();
  await page.getByRole("button", { name: "MA10", exact: true }).click();
  await expect(page.getByTestId("chart-readout")).toContainText("MA10");
  expect(calls).toBe(1);
  fail = false;
  await page
    .getByRole("button", { name: "重试历史", includeHidden: true })
    .click();
  await expect.poll(() => calls).toBe(2);
  await expect(page.getByText("正在加载更早行情…")).toBeVisible();
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(page.getByText("正在加载更早行情…")).toBeVisible();
  expect(calls).toBe(2);
  release();
  await expect(page.getByText("已加载 558/700 日")).toBeVisible();
  await closePanel(page);
  await expect(page.getByTestId("chart-visible-range")).toHaveText(before!);
  const read = await page.getByTestId("chart-readout").textContent();
  const date = read!.match(/日期 (\d{4}-\d{2}-\d{2})/)![1];
  const value = bars.find((b) => b.date === date)!.close;
  expect(read).toContain(`收 ${value.toFixed(2)}`);
  expect(read).toContain(`MA10 ${(value - 4.5).toFixed(2)}`);
});

test("旧股票的迟到历史不写入新股，切回恢复默认日线范围", async ({ page }) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started = false;
  await page.route("**/api/quotes/000001.SZ*", async (route) => {
    if (!new URL(route.request().url()).searchParams.has("end"))
      return route.continue();
    const response = await route.fetch();
    started = true;
    await gate;
    await route.fulfill({ response });
  });
  await enter(page);
  await expect.poll(() => started).toBe(true);
  await page.getByRole("button", { name: "下一个", exact: true }).click();
  await expect(page.getByTestId("chart-readout")).toContainText("收 1709.00");
  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(page.getByTestId("chart-readout")).toContainText("收 1709.00");
  await expect(page.getByTestId("chart-visible-range")).toContainText("500 根");
});

test("三尺寸双主题与触控：点击读数、横向拖动和纵向滚动", async ({
  browser,
}, info) => {
  const context = await browser.newContext({
    viewport: { width: 390, height: 844 },
    hasTouch: true,
    isMobile: true,
  });
  const page = await context.newPage();
  await page.goto("/");
  await enter(page);
  const cdp = await context.newCDPSession(page);
  const plot = page.getByRole("img", { name: /000001.SZ 日线/ });
  await plot.scrollIntoViewIfNeeded();
  const box = (await plot.boundingBox())!;
  const before = await page.getByTestId("chart-visible-range").textContent();
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x: box.x + 70, y: box.y + 100 }],
  });
  for (let x = 80; x <= 210; x += 10)
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: box.x + x, y: box.y + 100 }],
    });
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  await expect(page.getByTestId("chart-visible-range")).not.toHaveText(before!);
  await page.touchscreen.tap(box.x + 130, box.y + 70);
  const tapRead = page.getByTestId("chart-readout");
  await expect(tapRead).toContainText("日期");
  const lastVisible = (await page
    .getByTestId("chart-visible-range")
    .textContent())!.match(/— (\d{4}-\d{2}-\d{2})/)![1];
  await expect(tapRead).not.toContainText(`日期 ${lastVisible}`);
  // 窄屏滚动由模块外壳承担（.workbench-shell 固定不滚动）
  const top = await page
    .locator(".shell-module")
    .evaluate((el) => el.scrollTop);
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x: box.x + 140, y: box.y + 200 }],
  });
  for (let y = 190; y >= 70; y -= 10)
    await cdp.send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [{ x: box.x + 140, y: box.y + y }],
    });
  await cdp.send("Input.dispatchTouchEvent", {
    type: "touchEnd",
    touchPoints: [],
  });
  await expect
    .poll(() => page.locator(".shell-module").evaluate((el) => el.scrollTop))
    .toBeGreaterThan(top);
  await page.getByRole("button", { name: "日线", exact: true }).click();
  for (const n of [10, 20, 60])
    await page.getByRole("button", { name: `MA${n}`, exact: true }).click();
  for (const theme of ["浅色", "深色"]) {
    await page.getByRole("button", { name: "主题" }).click();
    await page.getByRole("menuitemradio", { name: theme, exact: true }).click();
    for (const [width, height] of [
      [1920, 1080],
      [1366, 768],
      [390, 844],
    ]) {
      const visibleBeforeResize = await page
        .getByTestId("chart-visible-range")
        .textContent();
      await page.setViewportSize({ width, height });
      await expect(page.getByTestId("chart-visible-range")).toHaveText(
        visibleBeforeResize!,
      );
      await page.getByTestId("quote-panel").scrollIntoViewIfNeeded();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.getByTestId("quote-panel").screenshot({
        path: info.outputPath(`${theme}-${width}.png`),
        animations: "disabled",
      });
    }
  }
  await context.close();
});

test("到达历史尽头后停止加载，笔记往返保留结束状态", async ({ page }) => {
  await enter(page);
  await expect(page.getByText("已加载 558/700 日")).toBeVisible();
  let requests = 0;
  page.on("request", (request) => {
    if (
      request.url().includes("/api/quotes/000001") &&
      new URL(request.url()).searchParams.has("end")
    )
      requests++;
  });
  await page.getByRole("button", { name: "看更早" }).click();
  await expect(page.getByText("已加载 700/700 日")).toBeVisible();
  await expect(page.getByRole("button", { name: "看更早" })).toHaveCount(0);
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await closePanel(page);
  await expect(page.getByText("已加载 700/700 日")).toBeVisible();
  await expect(page.getByRole("button", { name: "看更早" })).toHaveCount(0);
  expect(requests).toBe(1);
});
