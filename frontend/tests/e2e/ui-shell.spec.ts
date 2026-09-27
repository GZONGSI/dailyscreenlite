import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";

import {
  goModule,
  startClassification,
  startServer,
  type ServerHandle,
} from "./helpers";

let server: ServerHandle;
test.beforeEach(async () => {
  server = await startServer(8799);
});
test.afterEach(async () => {
  await server?.stop();
});

test("导入草稿跨模块保留，主题刷新与列表往返", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/");
  await goModule(page, "导入");
  const input = page.locator("#import-text");
  await input.fill("000001\n600519");
  // 切到候选归类再回来：导入草稿仍在（各模块互不影响）
  await goModule(page, "候选归类");
  await expect(input).not.toBeVisible();
  await goModule(page, "导入");
  await expect(input).toHaveValue("000001\n600519");

  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await startClassification(page);
  await expect(
    page.getByRole("heading", { name: "平安银行", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "列表", exact: true }).click();
  await page
    .getByRole("table", { name: "候选股票列表" })
    .getByRole("button", { name: "贵州茅台" })
    .click();
  await expect(
    page.getByRole("heading", { name: "贵州茅台", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "返回列表" }).click();
  await expect(page.getByRole("table", { name: "候选股票列表" })).toBeVisible();
  await page.getByRole("button", { name: "主题" }).click();
  await page.getByRole("menuitemradio", { name: "深色", exact: true }).click();
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await goModule(page, "候选归类");
  await expect(page.getByRole("table", { name: "候选股票列表" })).toBeVisible();
});

test("在途导入切模块后草稿保留，且只提交一次；重启可读回", async ({ page }) => {
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let requests = 0;
  await page.route("**/api/imports", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    requests++;
    await gate;
    await route.continue();
  });
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await goModule(page, "候选归类");
  await goModule(page, "导入");
  // 提交在途：草稿仍在，不会被切模块清掉
  await expect(page.locator("#import-text")).toHaveValue("000001");
  release();
  await expect(page.getByText("已导入 1 只，跳过 0 只")).toBeVisible();
  expect(requests).toBe(1);

  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await goModule(page, "候选归类");
  await expect(
    page.getByRole("heading", { name: "平安银行", exact: true }),
  ).toBeVisible();
});

test("浅深主题与三个尺寸：固定工作区、面板焦点和无页面横向溢出", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001\n600519");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await startClassification(page);

  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByLabel("问财 Cookie")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByLabel("问财 Cookie")).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "设置", exact: true }),
  ).toBeFocused();

  for (const theme of ["浅色", "深色"]) {
    await page.getByRole("button", { name: "主题" }).click();
    await page.getByRole("menuitemradio", { name: theme, exact: true }).click();
    for (const [width, height] of [
      [1920, 1080],
      [1366, 768],
      [390, 844],
    ]) {
      await page.setViewportSize({ width, height });
      await expect(page.getByTestId("stock-card")).toBeVisible();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(`${theme}-${width}.png`),
        fullPage: true,
        animations: "disabled",
      });
      await page.getByRole("button", { name: "设置", exact: true }).click();
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: testInfo.outputPath(`${theme}-${width}-sheet.png`),
        fullPage: true,
        animations: "disabled",
      });
      await page.keyboard.press("Escape");
    }
  }
});

test("首次读取失败可重试，筛选在途禁用旧结果，筛选切换串行写入", async ({
  page,
}) => {
  await page.route("**/api/classification/view", (route) => route.abort());
  await page.goto("/");
  await goModule(page, "候选归类");
  await expect(
    page.getByRole("button", { name: "重新加载候选队列" }),
  ).toBeVisible();
  await expect(page.getByText("正在加载候选队列…")).toHaveCount(0);

  await page.unroute("**/api/classification/view");
  await page.getByRole("button", { name: "重新加载候选队列" }).click();
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001\n600519");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await startClassification(page);
  await page.getByRole("button", { name: "列表", exact: true }).click();
  const table = page.getByRole("table");
  await expect(table.locator("tbody tr")).toHaveCount(2);

  // 改筛选会读一次统一浏览结果；把这一次的响应扣住，验证在途时旧结果只读不清空，
  // 并且筛选切换是串行的：前一次还没确认时，后一次不会抢在它前面写。
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let gated = false;
  const pending: string[] = [];
  await page.route("**/api/classification/view", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    const body = route.request().postData() ?? "";
    if (!gated && body.includes("平安")) {
      gated = true;
      pending.push("平安");
      const response = await route.fetch();
      await gate;
      await route.fulfill({ response });
      return;
    }
    pending.push(body);
    await route.continue();
  });
  const search = page.getByLabel("在队列中按股票代码或名称查找");
  await search.fill("平安");
  await search.press("Enter");
  await expect(page.getByText("正在更新筛选，暂时保留上次结果…")).toBeVisible();
  await expect(table.getByRole("button", { name: "贵州茅台" })).toBeDisabled();

  await search.fill("茅台");
  await search.press("Enter");
  // 前一次筛选尚未确认：后一次写入仍在排队，不会先落库
  await expect.poll(() => pending.length).toBe(1);

  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByRole("button", { name: "贵州茅台" })).toBeEnabled();
  await page.reload();
  await goModule(page, "候选归类");
  await expect(search).toHaveValue("茅台");
  await expect(table.locator("tbody tr")).toHaveCount(1);
});

test("早于最近五条的异常批次仍能从导入记录里处理", async ({ page }) => {
  const failed = await page.request.post("/api/imports", {
    multipart: { texts: "https://www.iwencai.com/screener/result?w=test" },
  });
  expect(failed.ok()).toBeTruthy();
  const batch = (await failed.json()).batches[0];
  for (let i = 0; i < 6; i++) {
    const response = await page.request.post("/api/imports", {
      multipart: { texts: "000001" },
    });
    expect(response.ok()).toBeTruthy();
  }
  await page.goto("/");
  await goModule(page, "导入");
  // 默认只列最近五条：展开后仍能选中那条失败批次并重试
  await page.getByRole("button", { name: "查看更多" }).click();
  await page
    .getByRole("list", { name: "导入记录列表" })
    .getByRole("button", { name: /问财链接/ })
    .first()
    .click();
  await expect(page.getByTestId(`batch-card-${batch.batchId}`)).toBeVisible();
  await expect(
    page.getByTestId(`batch-card-${batch.batchId}`).getByTestId("retry"),
  ).toBeVisible();
});

test("长列表返回恢复位置，文件草稿保留，窄屏队列可用", async ({ page }) => {
  const snapshot = JSON.parse(
    readFileSync(
      new URL("../../../data/securities/initial_snapshot.json", import.meta.url),
      "utf8",
    ),
  ) as { securities: { code: string }[] };
  const response = await page.request.post("/api/imports", {
    multipart: {
      texts: snapshot.securities
        .slice(0, 60)
        .map((s) => s.code)
        .join("\n"),
    },
  });
  expect(response.ok()).toBeTruthy();
  await page.goto("/");
  await goModule(page, "候选归类");
  await page.getByRole("button", { name: "列表", exact: true }).click();
  const table = page.getByRole("table");
  const scroll = page.locator(".classification-table-scroll");
  await expect(table.locator("tbody tr")).toHaveCount(60);
  await scroll.evaluate((el) => {
    el.scrollTop = 880;
  });
  const target = table.locator("tbody tr").nth(21).getByRole("button");
  await target.click();
  await page.getByRole("button", { name: "返回列表" }).click();
  await expect
    .poll(() => scroll.evaluate((el) => el.scrollTop))
    .toBeGreaterThan(800);

  // 文件草稿跨模块保留
  await goModule(page, "导入");
  await page.setInputFiles('input[type="file"]', {
    name: "draft.csv",
    mimeType: "text/csv",
    buffer: Buffer.from("code\n000001\n"),
  });
  await goModule(page, "候选归类");
  await goModule(page, "导入");
  await expect(
    page.getByRole("list", { name: "待提交来源" }).getByText("draft.csv"),
  ).toBeVisible();

  // 窄屏：队列堆在卡片上方，可以收起后继续用卡片
  await goModule(page, "候选归类");
  await page.getByRole("button", { name: "卡片", exact: true }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "收起队列", exact: true }).click();
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }),
  ).not.toBeVisible();
  await page.getByRole("button", { name: "展开队列", exact: true }).click();
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(queue).toBeVisible();
  await queue.getByRole("option").first().click();
  await expect(
    page.getByRole("heading", { name: "平安银行" }),
  ).toBeVisible();

  await page.addStyleTag({ content: "html { font-size: 32px; }" });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
  ).toBe(true);
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByLabel("问财 Cookie")).toBeVisible();
  await page.keyboard.press("Escape");
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  await page.getByRole("button", { name: "主题" }).click();
  await page.getByRole("menuitemradio", { name: "跟随系统", exact: true }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.emulateMedia({ colorScheme: "light" });
  await expect(page.locator("html")).not.toHaveClass(/dark/);
});
