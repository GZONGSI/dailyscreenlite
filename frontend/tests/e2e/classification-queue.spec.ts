import { expect, test } from "@playwright/test";

import {
  classifyText,
  goModule,
  startServer,
  submitText,
  type ServerHandle,
} from "./helpers";

/**
 * 工单 01 的候选归类浏览器验收：真实 Chrome → 真实本机 HTTP → 临时真实数据库。
 * 覆盖队列浏览（下一个只浏览/当前范围结束）、逐个稍后处理后的自动推进、
 * 暂不关注与主动重新归类（含已处理筛选下的卡片入口）、队列筛选、
 * 主动清理、跨日合并与重新归类、重启读回归类上下文。
 */

let server: ServerHandle;

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

test("手动浏览：下一个只浏览，末项再前进进入结束卡并返回队列", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByText(/第 1 \/ 3 只/)).toBeVisible();

  // 未决策点击下一个只浏览：不标完成、队列数量不变
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await expect(page.getByText(/第 2 \/ 3 只/)).toBeVisible();
  await expect(page.getByText(/共 3 只待归类/)).toBeVisible();

  // 键盘可达：聚焦下一个并用 Enter 切换
  await expect(page.getByRole("button", { name: "下一个" })).toBeEnabled();
  await page.getByRole("button", { name: "下一个" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(page.getByText(/第 3 \/ 3 只/)).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  const end = page.getByTestId("end-card");
  await expect(end).toBeVisible();
  await expect(page.getByText("当前范围看完了")).toBeVisible();
  await expect(end.getByTestId("round-viewed")).toHaveText("3");
  await expect(end.getByTestId("round-processed")).toHaveText("0");
  await expect(end.getByTestId("round-remaining")).toHaveText("3");

  await page.getByRole("button", { name: "返回队列" }).click();
  await expect(page.getByTestId("end-card")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await context.close();
});

test("稍后处理移到队尾并自动前进，仍可回看原股票", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "稍后处理" }).click();

  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(
    page.getByText("已移到队尾稍后处理，仍留在待归类池，可随时回来。"),
  ).toBeVisible();

  // 移队尾：平安银行排到最后（列表顺序由服务端队列决定，等待重读完成）
  const options = page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option");
  await expect(options).toHaveCount(3);
  await expect(options.nth(0)).toContainText("宁德时代");
  await expect(options.nth(2)).toContainText("平安银行");
  await context.close();
});

test("暂不关注自动前进，已处理可筛选并主动重新归类", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(
    page.getByText("已暂不关注，已结束本次归类；导入事实保留。"),
  ).toBeVisible();
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);

  // 已处理视图：按结果筛选
  await page.getByRole("tab", { name: /已处理/ }).click();
  await page.getByLabel("处理结果筛选").selectOption("dismissed");
  const processedList = page.getByRole("listbox", { name: "已归类股票列表" });
  await expect(processedList.getByRole("option")).toHaveCount(1);
  await expect(processedList.getByRole("option").first()).toContainText("平安银行");

  // 主动重新归类：复用同一候选项，回到待归类队列并停在该股票
  await page.getByRole("button", { name: "重新归类 平安银行" }).click();
  await expect(page.getByRole("tab", { name: /待归类/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await context.close();
});

test("队列筛选与重启后恢复归类上下文", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  // 切到列表视图并在队列内查找
  await page.getByRole("button", { name: "列表" }).click();
  await expect(page.getByRole("button", { name: "列表" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await page.getByLabel("在队列中按股票代码或名称查找").fill("茅台");
  await page.getByLabel("在队列中按股票代码或名称查找").press("Enter");

  await expect(
    page.getByRole("table", { name: "候选股票列表" }).getByRole("button", { name: "贵州茅台" }),
  ).toBeVisible();
  await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(1);
  await context.close();

  // 重启后从数据库读回视图、筛选与当前股票
  await server.stop();
  server = await startServer(8799, server.dataDir);
  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  await page2.goto("/");
  await goModule(page2, "候选归类");
  await expect(page2.getByRole("button", { name: "列表" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(page2.getByLabel("在队列中按股票代码或名称查找")).toHaveValue(
    "茅台",
  );
  await expect(
    page2.getByRole("table", { name: "候选股票列表" }).getByRole("button", { name: "贵州茅台" }),
  ).toBeVisible();
  await context2.close();
});

test("处理队首后仍可继续下一个，不会提前显示结束", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await expect(page.getByRole("button", { name: "下一个" })).toBeVisible();

  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 已查看未决策可见但不等于已处理；列表完整（窗口化只渲染可见行，用集合大小核对）
  await expect(page.getByTestId("stock-card").getByText("已查看")).toBeVisible();
  await expect(
    page
      .getByRole("listbox", { name: "待归类股票列表" })
      .getByRole("option")
      .first(),
  ).toHaveAttribute("aria-setsize", "2");
  await context.close();
});

test("浏览 A→B 后暂不关注 B，下一个前进到 C 而非回跳 A", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await context.close();
});

test("结束卡期间新增来源保留位置，回看后可继续到新股票", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();

  // 新增来源保留结束卡；沿原路径回看再前进时可遇到新股票。
  await submitText(page, "600519");
  await goModule(page, "候选归类");
  await expect(page.getByTestId("end-card")).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  // 前进与回看都停在结束卡上：先回到结束卡，再按一次才到新导入的那只
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await context.close();
});

test("多个独立文本块分别成批次", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await goModule(page, "导入");
  const box = page.locator("#import-text");
  await box.fill("000001");
  await page.getByRole("button", { name: "添加文本块" }).click();
  await box.fill("600519");
  await page.getByRole("button", { name: "添加文本块" }).click();
  await expect(
    page.getByRole("list", { name: "待提交来源" }).getByRole("listitem"),
  ).toHaveCount(2);

  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(
    page.getByRole("list", { name: "导入记录列表" }).getByRole("button"),
  ).toHaveCount(2);

  await goModule(page, "候选归类");
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);
  await context.close();
});

test("卡片与列表视图切换会改变主栏内容", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519");
  const main = page.getByTestId("main-column");
  await expect(main.getByTestId("stock-card")).toBeVisible();

  await page.getByRole("button", { name: "列表" }).click();
  await expect(page.getByRole("table", { name: "候选股票列表" })).toBeVisible();
  await expect(main.getByTestId("stock-card")).toHaveCount(0);
  await context.close();
});

test("主动清理待归类池不删导入事实", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "清理待归类池" }).click();
  await page.getByRole("button", { name: "确认清理" }).click();

  await expect(
    page.getByText("没有待归类的股票，先在导入页提交候选。"),
  ).toBeVisible();

  // 已处理视图可见已清理状态，导入批次事实仍在
  await page.getByRole("tab", { name: /已处理/ }).click();
  const processedList = page.getByRole("listbox", { name: "已归类股票列表" });
  await expect(processedList.getByRole("option")).toHaveCount(2);
  await expect(processedList.getByText("已清理").first()).toBeVisible();

  await goModule(page, "导入");
  await expect(
    page
      .locator('[data-testid^="batch-card-"]')
      .first()
      .getByText(/已导入 2 只/),
  ).toBeVisible();
  await context.close();
});

test("逐个稍后处理后依序推进，一圈看完显示结束而不是回跳", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, `000001
600519
300750`);
  const heading = () =>
    page.getByTestId("stock-card").getByRole("heading").first().textContent();

  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "稍后处理" }).click();

  // 逐只推进：每次都应走到尚未看过的那只，而不是回跳到队首
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  expect(await heading()).toContain("宁德时代");
  await page.getByRole("button", { name: "稍后处理" }).click();

  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  expect(await heading()).toContain("贵州茅台");
  await page.getByRole("button", { name: "稍后处理" }).click();

  // 三只都看过一轮：进入结束卡，不再有「下一个」。
  await expect(page.getByTestId("end-card")).toBeVisible();
  await expect(page.getByRole("button", { name: "下一个" })).toHaveCount(0);

  // 队列仍是三只（稍后处理属于未处理），顺序为移动后的 A/B/C
  const options = page
    .getByRole("listbox", { name: "待归类股票列表" })
    .getByRole("option");
  await expect(options).toHaveCount(3);
  await expect(options.nth(0)).toContainText("平安银行");
  await expect(options.nth(2)).toContainText("贵州茅台");
  await context.close();
});

test("已处理筛选下卡片重新归类：保留浏览路径并显示待归类队列", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, `000001
600519`);
  await page.getByRole("button", { name: "暂不关注" }).click();

  // 进入"已处理 + 暂不关注"筛选，此时限制队列的筛选会挡住刚重新归类的股票
  await page.getByRole("tab", { name: /已处理/ }).click();
  await page.getByLabel("处理结果筛选").selectOption("dismissed");
  await expect(
    page.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);

  await page
    .getByTestId("stock-card")
    .getByRole("button", { name: "重新归类", exact: true })
    .click();

  // 回到待归类队列，该股票重新可选，并能继续推进到另一只
  await expect(page.getByRole("tab", { name: /待归类/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  const pending = page
    .getByRole("listbox", { name: "待归类股票列表" })
    .getByRole("option");
  await expect(pending).toHaveCount(2);
  await expect(page.getByTestId("stock-card").getByText(
    "已重新进入待归类，队列位置已更新。",
  )).toBeVisible();
  await expect(page.getByRole("button", { name: "下一个" })).toBeVisible();
  await context.close();
});

test("跨日入选：未处理合并、处理后重新归类（真实浏览器 + 固定日期）", async ({
  browser,
}) => {
  // 本用例自己管理三台服务（按日固定"今天"），先让出 beforeEach 起的 8799
  await server.stop();
  const context = await browser.newContext();
  const page = await context.newPage();

  // 第一天：导入 A/B 并处理掉 A
  const day1 = await startServer(8799, undefined, {
    DSLITE_NOW: "2026-09-11T10:00:00+08:00",
  });
  server = day1;
  await page.goto("/");
  await classifyText(page, `000001
600519`);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();
  await context.close();
  await day1.stop();

  // 第二天：同一批股票再次入选
  const day2 = await startServer(8799, day1.dataDir, {
    DSLITE_NOW: "2026-09-12T10:00:00+08:00",
  });
  server = day2;
  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  await page2.goto("/");
  await submitText(page2, `000001
600519`);

  // 结果表逐行给出本次影响：A 处理后重新归类，B 跨日合并
  const card = page2.locator('[data-testid^="batch-card-"]').first();
  await expect(card.getByTestId("stat-reopened")).toHaveText("1");
  await expect(card.getByTestId("stat-existing")).toHaveText("1");
  await expect(card.getByText("重新归类", { exact: true }).first()).toBeVisible();
  await expect(card.getByText("合并", { exact: true }).first()).toBeVisible();

  await goModule(page2, "候选归类");
  const pending = page2
    .getByRole("listbox", { name: "待归类股票列表" })
    .getByRole("option");
  await expect(pending).toHaveCount(2);

  // 卡片能看到两个入选日期，且重新归类保留历次处理记录
  await pending.filter({ hasText: "平安银行" }).click();
  await page2.getByTestId("stock-card").getByRole("heading", { name: "平安银行" }).waitFor();
  await page2.getByRole("button", { name: /来源与处理记录/ }).click();
  await expect(page2.getByRole("heading", { name: "入选日期" })).toBeVisible();
  await expect(
    page2.getByRole("list").getByText("2026-09-11", { exact: true }),
  ).toBeVisible();
  await expect(
    page2.getByRole("list").getByText("2026-09-12", { exact: true }),
  ).toBeVisible();
  await expect(page2.getByText("暂不关注", { exact: true })).toBeVisible();

  // 重启后两个入选日期与队列都读回
  await context2.close();
  await day2.stop();
  const day3 = await startServer(8799, day1.dataDir, {
    DSLITE_NOW: "2026-09-12T18:00:00+08:00",
  });
  server = day3;
  const context3 = await browser.newContext();
  const page3 = await context3.newPage();
  await page3.goto("/");
  await goModule(page3, "候选归类");
  await expect(
    page3.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);
  await page3.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option").filter({ hasText: "平安银行" }).click();
  await page3.getByRole("button", { name: /来源与处理记录/ }).click();
  await expect(
    page3.getByRole("list").getByText("2026-09-11", { exact: true }),
  ).toBeVisible();
  await context3.close();
  await day3.stop();
});
