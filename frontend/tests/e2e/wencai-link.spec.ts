import { expect, test, type Page } from "@playwright/test";
import {
  closePanel,
  goModule,
  openSettings,
  startClassification,
  startServer,
  type ServerHandle,
} from "./helpers";

import { startWencaiStub } from "./wencai-stub";

/**
 * 问财链路浏览器验收：真实 Chrome → 本机 HTTP → 真实适配器 → 本地协议桩 → 临时真实数据库。
 * 覆盖"首次确认后发布 / 同查询自动发布 / 登录失效保留批次并重试成功"。
 * 桩只替代外部站点，获取完整性、条件确认、身份判定与落库都用真实实现。
 */

let server: ServerHandle;
let stub: Awaited<ReturnType<typeof startWencaiStub>>;

const URL_TEXT =
  "https://www.iwencai.com/screener/result?w=%E5%88%9B%E6%96%B0%E9%AB%98&querytype=stock&sign=1";

test.beforeEach(async () => {
  stub = await startWencaiStub();
  server = await startServer(8799, undefined, {
    DSLITE_WENCAI_BASE: stub.baseUrl,
  });
});

test.afterEach(async () => {
  await server?.stop();
  await stub?.stop();
});

async function submitLink(page: Page) {
  await goModule(page, "导入");
  await page.locator("#import-text").fill(URL_TEXT);
  await page.getByRole("button", { name: "提交", exact: true }).click();
}

function firstBatch(page: Page) {
  return page.locator('[data-testid^="batch-card-"]').first();
}

test("问财链接：首次确认后发布，同查询再次导入自动发布", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  // 先配置 Cookie（桩不校验内容，只为满足"已配置"）
  await openSettings(page);
  await page.getByLabel("问财 Cookie").fill("stub-cookie");
  await page.getByRole("button", { name: "保存 / 替换 Cookie" }).click();
  await expect(page.getByText("Cookie 已配置")).toBeVisible();
  await closePanel(page);

  await submitLink(page);
  const card = firstBatch(page);
  // 首次：待确认，展示原句与实际条件
  await expect(card.getByText("待确认条件")).toBeVisible();
  await expect(card.getByText("创新高").first()).toBeVisible();
  await expect(page.locator('[role="listbox"] [role="option"]')).toHaveCount(0);

  await card.getByTestId("confirm-condition").click();
  await expect(card.getByText(/已导入 3 只/)).toBeVisible();
  await startClassification(page);
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(3);

  // 同一查询、同一口径再次导入 → 无需确认，自动发布且不重复建项
  await submitLink(page);
  const again = firstBatch(page);
  await expect(again.getByText(/已导入 3 只/)).toBeVisible();
  // 同一查询同一天再次导入：同日已入选，既不新建也不再次触发归类，只追加来源
  await expect(again.getByTestId("stat-new")).toHaveText("0");
  await expect(again.getByTestId("stat-existing")).toHaveText("0");
  await expect(again.getByText("仅追加来源").first()).toBeVisible();
  await context.close();
});

test("多个问财链接各占一行分别成批次", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");

  await openSettings(page);
  await page.getByLabel("问财 Cookie").fill("stub-cookie");
  await page.getByRole("button", { name: "保存 / 替换 Cookie" }).click();
  await closePanel(page);

  await goModule(page, "导入");
  // 两行链接：应拆成两个独立链接批次，而不是一个文本块
  await page
    .locator("#import-text")
    .fill(`${URL_TEXT}\n${URL_TEXT.replace("sign=1", "sign=2")}`);
  await page.getByRole("button", { name: "提交", exact: true }).click();

  await expect(
    page.getByRole("list", { name: "导入记录列表" }).getByRole("button"),
  ).toHaveCount(2);
  await expect(page.getByText("待确认条件").first()).toBeVisible();
  await context.close();
});

test("问财链接：登录失效保留批次，更新 Cookie 后重试成功", async ({
  browser,
}) => {
  // 桩在 Cookie 更新前返回登录标记，更新后返回正常结果
  let expired = true;
  await stub.stop();
  stub = await startWencaiStub({ loginExpired: () => expired });
  await server.stop();
  server = await startServer(8799, server.dataDir, {
    DSLITE_WENCAI_BASE: stub.baseUrl,
  });

  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await openSettings(page);
  await page.getByLabel("问财 Cookie").fill("old-cookie");
  await page.getByRole("button", { name: "保存 / 替换 Cookie" }).click();
  await closePanel(page);

  await submitLink(page);
  const card = firstBatch(page);
  await expect(card.getByText("未发布")).toBeVisible();
  // 真实站点会话失效返回 HTTP 401 与"未登陆"提示：按鉴权异常显示，不解读为零结果
  await expect(card.getByText(/鉴权|未登陆|请登录/)).toBeVisible();
  await expect(page.locator('[role="listbox"] [role="option"]')).toHaveCount(0);
  const batchTestId = await card.getAttribute("data-testid");

  // 更新 Cookie 并重试 → 待确认（不新建批次）
  expired = false;
  await openSettings(page);
  await page.getByLabel("问财 Cookie").fill("new-cookie");
  await page.getByRole("button", { name: "保存 / 替换 Cookie" }).click();
  await expect(page.getByText(/已本地保存/)).toBeVisible();
  await closePanel(page);
  await goModule(page, "导入");
  await page.getByTestId(batchTestId!).getByTestId("retry").click();
  await expect(
    page.getByTestId(batchTestId!).getByText("待确认条件"),
  ).toBeVisible();

  await page.getByTestId(batchTestId!).getByTestId("confirm-condition").click();
  await expect(
    page.getByTestId(batchTestId!).getByText(/已导入 3 只/),
  ).toBeVisible();
  await context.close();
});
