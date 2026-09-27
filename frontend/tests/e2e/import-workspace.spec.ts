import { expect, test, type Locator, type Page } from "@playwright/test";
import { writeFileSync } from "node:fs";
import { join } from "node:path";

import {
  classifyText,
  closePanel,
  goModule,
  leaveLaunch,
  openSettings,
  startServer,
  startClassification,
  submitFile,
  submitText,
  type ServerHandle,
} from "./helpers";

/**
 * 工单 01 的导入工作区浏览器验收：真实 Chrome → 真实本机 HTTP → 临时真实数据库。
 * 覆盖综合输入（文件/文本/链接）、歧义选择、条件确认、重新识别、
 * Cookie 设置、失败与真实零结果、同日重复与重启读回。
 */

let server: ServerHandle;

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

function writeSource(name: string, content: string): string {
  const path = join(server.dataDir, name);
  writeFileSync(path, content, "utf-8");
  return path;
}

/** 结果区当前展示的批次卡（一次只展示选中的那一批）。 */
function resultCard(page: Page): Locator {
  return page.locator('[data-testid^="batch-card-"]').first();
}

/** 从「最近导入记录」里选中某个来源，结果区切换到它。 */
async function openBatch(page: Page, name: string): Promise<Locator> {
  await page
    .getByRole("list", { name: "导入记录列表" })
    .getByRole("button", { name: new RegExp(name) })
    .first()
    .click();
  return resultCard(page);
}

test("综合输入：上传 CSV → 结果表 → 开始归类 → 暂不关注 → 重启读回", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 新打开应用先看到启动页；从入口进入模块后才有顶部导航
  await expect(page.getByTestId("launch-page")).toBeVisible();
  await leaveLaunch(page);
  await expect(page.getByRole("button", { name: "返回启动页" })).toBeVisible();
  await goModule(page, "导入");
  await expect(
    page.getByRole("heading", { name: "选择导入方式" }),
  ).toBeVisible();

  const file = writeSource(
    "candidates.csv",
    [
      "代码,名称,最新价",
      "000001,平安银行,11.50",
      "600519,贵州茅台,1500.00",
      "999999,不存在的股票,1.00",
    ].join("\n") + "\n",
  );
  await submitFile(page, file);

  const card = resultCard(page);
  await expect(card.getByText("已导入 2 只，跳过 1 只")).toBeVisible();
  await expect(card.getByTestId("stat-unique")).toHaveText("3");
  await expect(card.getByTestId("stat-recognized")).toHaveText("2");
  await expect(card.getByTestId("stat-skipped")).toHaveText("1");
  await expect(card.getByTestId("stat-new")).toHaveText("2");
  // 明细逐行给出名称、代码与导入结果
  await expect(card.getByText("平安银行")).toBeVisible();
  await expect(card.getByText("未识别", { exact: true })).toBeVisible();
  await card.getByText("查看 1 只未识别股票").click();
  await expect(card.getByText("999999").first()).toBeVisible();

  // 导入成功停留在结果页；由用户点击「开始归类」
  await startClassification(page);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByText("行情暂未取得")).toBeVisible();
  await expect(page.getByText("11.50")).toHaveCount(0); // 来源附带价格不进入工作台
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(
    page.getByText("已暂不关注，已结束本次归类；导入事实保留。"),
  ).toBeVisible();
  // 回看已处理股票，左栏跟随真实状态；待归类池仍只剩另一只。
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
  const remaining = await (await page.request.get("/api/classification/candidates?scope=unprocessed")).json();
  expect(remaining.candidates.map((item: { securityId: string }) => item.securityId)).toEqual(["600519.SH"]);

  await context.close();

  // 停止并重启服务后，从页面读回导入事实、统计与处理状态
  await server.stop();
  server = await startServer(8799, server.dataDir);

  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  await page2.goto("/");
  await goModule(page2, "导入");
  const restored = await openBatch(page2, "candidates.csv");
  await expect(restored.getByText("已导入 2 只，跳过 1 只")).toBeVisible();
  await expect(restored.getByTestId("stat-recognized")).toHaveText("2");
  await restored.getByText("查看 1 只未识别股票").click();
  await expect(restored.getByText("999999").first()).toBeVisible();

  await goModule(page2, "候选归类");
  // 模块恢复上次工作位置：已处理的平安银行仍作为当前股票卡呈现（可重新归类）
  await expect(page2.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page2.getByTestId("stock-card").getByRole("button", { name: "重新归类", exact: true })).toBeVisible();
  const processed = page2
    .getByRole("listbox", { name: "已归类股票列表" })
    .getByRole("option");
  await expect(processed).toHaveCount(1);
  await expect(processed.first()).toContainText("平安银行");
  await context2.close();
});

test("纯代码文本与全部未识别、真实零结果分别显示", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await goModule(page, "导入");

  // 纯代码文本：每行一个代码
  await submitText(page, "000001\n600519\n300750");
  await expect(
    resultCard(page).getByText("已导入 3 只，跳过 0 只"),
  ).toBeVisible();

  // 全部未识别：不建卡并说明原因
  await submitText(page, "999998\n999999");
  await expect(
    resultCard(page).getByText("未导入任何股票：全部未识别"),
  ).toBeVisible();

  // 空文本提交：明确提示，而不是当成零结果
  await page.locator("#import-text").fill("   ");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: "请选择文件" })).toBeVisible();

  const empty = writeSource("empty.csv", "代码,名称\n");
  await submitFile(page, empty);
  await expect(resultCard(page).getByText(/本次为真实零结果/)).toBeVisible();
  await context.close();
});

test("歧义输入：多候选代码列才要求选择，选定后继续", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  const ambiguous = writeSource(
    "amb.csv",
    "a,b\n000001,600000\n000002,600519\n",
  );
  await submitFile(page, ambiguous);

  const card = resultCard(page);
  await expect(card.getByText("待选择")).toBeVisible();
  await expect(
    card.getByText("存在多个候选股票代码列，需要选择"),
  ).toBeVisible();
  await expect(
    card.getByRole("group", { name: "代码列或工作表选择" }),
  ).toBeVisible();

  await card.getByTestId("selection-option-0").click();
  await expect(card.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await expect(card.getByTestId("stat-recognized")).toHaveText("2");

  // 成交额列即使内容命中证券库，也不被擅自导入：要求用户显式选择
  const pricey = writeSource(
    "pricey.csv",
    ["名称,成交额,序号", "alpha,600000,1", "beta,600519,2"].join("\n") + "\n",
  );
  await submitFile(page, pricey);
  const priceyCard = await openBatch(page, "pricey.csv");
  await expect(priceyCard.getByText("待选择")).toBeVisible();
  await expect(
    priceyCard.getByText("列名与内容不一致，需要选择股票代码列"),
  ).toBeVisible();
  // 未擅自导入：待归类列表仍是上一步的 2 只
  await goModule(page, "候选归类");
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);
  await context.close();
});

test("多来源独立：一个失败不阻止其他批次", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  const rejected = writeSource(
    "rejected.csv",
    "name,amount\nalpha,11.5\nbeta,13.2\n",
  );
  await submitFile(page, rejected);
  // 同一个文本框再放一个有效来源
  await submitText(page, "000001");

  // 两个来源各自成批次：记录里各有一条，失败的那条如实标注未发布
  const history = page.getByRole("list", { name: "导入记录列表" });
  await expect(history.getByRole("button")).toHaveCount(2);
  const failed = await openBatch(page, "rejected.csv");
  await expect(failed.getByText("未发布")).toBeVisible();

  await goModule(page, "候选归类");
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
  await context.close();
});

test("问财链接未配置 Cookie 时明确提示，而不是零结果", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await goModule(page, "导入");

  await page
    .locator("#import-text")
    .fill(
      "https://www.iwencai.com/screener/result?w=%E5%88%9B%E6%96%B0%E9%AB%98&querytype=stock&sign=1",
    );
  await expect(
    page.getByText("识别为问财链接，将按链接获取完整结果"),
  ).toBeVisible();
  await page.getByRole("button", { name: "提交", exact: true }).click();

  const card = resultCard(page);
  await expect(card.getByText("未发布")).toBeVisible();
  await expect(card.getByText(/未配置问财 Cookie/)).toBeVisible();
  await expect(card.getByText("真实零结果")).toHaveCount(0);
  await context.close();
});

test("设置页本地保存 Cookie 且不回显内容", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await openSettings(page);
  await expect(page.getByText("Cookie 未配置")).toBeVisible();

  await page.getByLabel("问财 Cookie").fill("secret-cookie-value");
  await page.getByRole("button", { name: "保存 / 替换 Cookie" }).click();
  await expect(page.getByText(/已本地保存/)).toBeVisible();
  await expect(page.getByText("Cookie 已配置")).toBeVisible();
  // 不回显 Cookie 内容
  await expect(page.getByText("secret-cookie-value")).toHaveCount(0);

  // 重启后 Cookie 仍在（本机保存）
  await context.close();
  await server.stop();
  server = await startServer(8799, server.dataDir);
  const context2 = await browser.newContext();
  const page2 = await context2.newPage();
  await page2.goto("/");
  await openSettings(page2);
  await expect(page2.getByText("Cookie 已配置")).toBeVisible();
  await context2.close();
});

test("组合输入期间 Enter 不误提交，Ctrl+Enter 才提交", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await goModule(page, "导入");

  const box = page.locator("#import-text");
  await box.click();
  await box.fill("000001\n600519");

  // 组合输入中按 Enter 不提交
  await page.evaluate(() => {
    const el = document.getElementById("import-text") as HTMLTextAreaElement;
    el.dispatchEvent(
      new CompositionEvent("compositionstart", { bubbles: true }),
    );
    el.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Enter",
        bubbles: true,
        cancelable: true,
      }),
    );
    el.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true }));
  });
  await expect(page.locator('[data-testid^="batch-card-"]')).toHaveCount(0);

  // 普通 Enter 只在文本框内换行
  await box.press("Enter");
  await expect(page.locator('[data-testid^="batch-card-"]')).toHaveCount(0);

  // Ctrl+Enter 提交
  await box.press("Control+Enter");
  await expect(
    resultCard(page).getByText("已导入 2 只，跳过 0 只"),
  ).toBeVisible();
  await context.close();
});

test("重新识别跳过项：补入成功项，仍未知明细直接清除", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  const file = writeSource("reid.csv", "代码\n000001\n999999\n");
  await submitFile(page, file);

  const card = resultCard(page);
  await expect(card.getByText("已导入 1 只，跳过 1 只")).toBeVisible();
  await card.getByTestId("reidentify").click();

  // 名单未变：仍未知明细被删除，跳过数归零，汇总与导入事实保留
  await expect(card.getByText("已导入 1 只，跳过 0 只")).toBeVisible();
  await expect(card.getByText(/重新识别/)).toBeVisible();
  await expect(card.getByText(/删除仍未知明细 1 条/)).toBeVisible();
  await context.close();
});

test("失败批次保留并可重试，且不新建批次", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 不支持的文件格式 → 未发布批次
  const bad = writeSource("bad.bin", "not,a,table\n");
  await submitFile(page, bad);

  const card = resultCard(page);
  await expect(card.getByText("未发布")).toBeVisible();
  const batchTestId = await card.getAttribute("data-testid");

  // 重试入口存在；重试后仍是同一批次（保留批次身份与导入日期）
  const retry = card.getByTestId("retry");
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(page.getByTestId(batchTestId!)).toBeVisible();
  await expect(
    page.getByTestId(batchTestId!).getByText("未发布"),
  ).toBeVisible();
  await context.close();
});

test("同日重复提交同一股票：只追加来源，不重复建项", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await submitText(page, "300750");
  const first = resultCard(page);
  await expect(first.getByText("已导入 1 只，跳过 0 只")).toBeVisible();
  await expect(first.getByTestId("stat-new")).toHaveText("1");
  await expect(first.getByTestId("stat-date")).toHaveText(
    /^\d{4}-\d{2}-\d{2}$/,
  );

  await submitText(page, "300750");
  const second = resultCard(page);
  // 同一天已入选：既不新建也不再次触发归类，只追加来源
  await expect(second.getByTestId("stat-new")).toHaveText("0");
  await expect(second.getByTestId("stat-existing")).toHaveText("0");
  await expect(second.getByTestId("stat-reopened")).toHaveText("0");
  await expect(second.getByText("仅追加来源")).toBeVisible();

  await goModule(page, "候选归类");
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
  // 卡片上来源与处理记录能看到两个来源、一条入选日期
  await page.getByRole("button", { name: /来源与处理记录/ }).click();
  await expect(page.getByRole("heading", { name: "入选日期" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "来源（2）" })).toBeVisible();
  await closePanel(page);
  await context.close();
});

test("搜索命中已处理股票：打开卡片保留原状态，主动重新归类才恢复", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();

  // 搜索命中已处理股票：结果里展示处理状态，打开卡片不把它拉回待归类
  await page.getByLabel("搜索已导入股票").fill("平安");
  const row = page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("listitem")
    .first();
  await expect(row.getByText("暂不关注")).toBeVisible();
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(queue.getByRole("option")).toHaveCount(1);

  await row.getByRole("button", { name: "查看卡片" }).click();
  await expect(page.getByTestId("stock-card")).toBeVisible();
  await expect(page.getByTestId("stock-card").getByText("暂不关注", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "加入 / 保留观察" })).toHaveCount(0);
  // 展示页签跟随当前卡的真实状态，未处理池仍是原来的一只
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute("aria-selected", "true");
  const unprocessed = await page.request.get(
    `${server.baseURL}/api/classification/view?scope=unprocessed`,
  );
  expect(((await unprocessed.json()).rows as unknown[]).length).toBe(1);

  // 主动重新归类才恢复待处理并留在该股票上
  await page.getByTestId("stock-card").getByRole("button", { name: "重新归类" }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByRole("button", { name: "加入 / 保留观察" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /待归类/ })).toHaveAttribute("aria-selected", "true");
  await expect(queue.getByRole("option")).toHaveCount(2);
  await context.close();
});

test("搜不到的股票不进队列：全市场名单不成为可搜索集合", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await leaveLaunch(page);

  await page.getByLabel("搜索已导入股票").fill("贵州茅台");
  await expect(page.getByText("没有匹配的已导入股票")).toBeVisible();
  await context.close();
});

test("全局搜索命中待归类股票：打开卡片但不重排队列", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByTestId("stock-card")).toBeVisible();
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(queue.getByRole("option").first()).toContainText("平安银行");
  const orderBefore = await Promise.all(
    [0, 1, 2].map(async (index) => (await queue.getByRole("option").nth(index).innerText())),
  );

  await page.getByLabel("搜索已导入股票").fill("300750");
  await page.getByRole("list", { name: "搜索结果列表" }).getByRole("button").first().click();

  await expect(page.getByTestId("stock-card")).toBeVisible();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  // 打开候选只改变当前卡与访问历史，未处理池顺序不变
  const orderAfter = await Promise.all(
    [0, 1, 2].map(async (index) => (await queue.getByRole("option").nth(index).innerText())),
  );
  expect(orderAfter).toEqual(orderBefore);
  await context.close();
});
