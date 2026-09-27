import { expect, test, type Page } from "@playwright/test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  classifyText,
  goModule,
  startServer,
  submitText,
  type ServerHandle,
} from "./helpers";

/**
 * 工单 02 的观察组浏览器验收：真实 Chrome → 真实本机 HTTP → 临时真实数据库。
 *
 * 覆盖独立观察组工作区（组列表 / 观察股票表 / 同页详情）、按证券去重与切组、
 * 排序与工作位置恢复、详情的展开与返回、组与关系的增删改（含删除组、退出全部组）、
 * 归类与观察的单向联动，以及全局搜索的三个分支与重启读回。
 * 行情走与 AKShare 同一 QuotesSource 边界的夹具来源（不访问外网）。
 */

let server: ServerHandle;
let quotesFixture: string;

/** 三只股票的日线与涨跌幅：平安 +1%、招商 +3%、茅台 -2%，用于验证排序。 */
function buildFixture(): string {
  const dir = mkdtempSync(join(tmpdir(), "dslite-obs-quotes-"));
  const path = join(dir, "quotes.json");
  const barsFor = (closes: number[]) =>
    closes.map((close, index) => ({
      date: ["2026-09-17", "2026-09-18"][index],
      open: close,
      high: close,
      low: close,
      close,
      volume_lots: 100000,
      amount_yuan: close * 100 * 100000,
    }));
  writeFileSync(
    path,
    JSON.stringify({
      bars: {
        "000001.SZ": barsFor([10, 10.1]),
        "600036.SH": barsFor([20, 20.6]),
        "600519.SH": barsFor([100, 98]),
      },
    }),
    "utf8",
  );
  return path;
}

test.beforeAll(() => {
  quotesFixture = buildFixture();
});

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

/** 进入观察组模块（组列表所在工作区）。 */
async function manage(page: Page) {
  await goModule(page, "观察组");
  await expect(page.getByTestId("observation-groups")).toBeVisible();
}

async function createGroup(page: Page, name: string) {
  await page.getByLabel("新观察组名称").fill(name);
  await page.getByRole("button", { name: "新建", exact: true }).click();
  await expect(
    page.getByRole("list", { name: "观察组列表" }).getByText(name, { exact: true }),
  ).toBeVisible();
}

/** 打开归类卡片的「加入 / 保留观察」并等待已有关系读回。 */
async function picker(page: Page) {
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  const popover = page.getByRole("dialog", { name: "选择观察组" });
  await expect(popover.getByRole("checkbox").first()).toBeVisible();
  return popover;
}

/** 把当前卡片加入指定组（先清掉预选，再勾选目标组），完成本次归类。 */
async function observeCurrent(page: Page, names: string[]) {
  const popover = await picker(page);
  const boxes = popover.getByRole("checkbox");
  for (let index = 0; index < (await boxes.count()); index += 1) {
    const box = boxes.nth(index);
    if (await box.isChecked()) await box.uncheck();
  }
  for (const name of names) {
    await popover.getByRole("checkbox", { name: new RegExp(name) }).check();
  }
  await popover.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(page.getByRole("dialog", { name: "选择观察组" })).toHaveCount(0);
  // 归类成功会自动推进；这些观察关系用例继续在原股验证。
  await page.getByRole("button", { name: "上一个" }).click();
}

/** 打开归类卡片的「移出观察组」并等待已有关系读回。 */
async function removePicker(page: Page) {
  await page.getByRole("button", { name: "移出观察组" }).click();
  const popover = page.getByRole("dialog", { name: "选择要移出的观察组" });
  await expect(popover.getByRole("checkbox").first()).toBeVisible();
  return popover;
}

const box = (page: Page, name: string) =>
  page.getByRole("checkbox", { name: new RegExp(name) });

/** 观察股票表的行：按显示顺序取名称列文本。 */
async function rowNames(page: Page): Promise<string[]> {
  return page
    .getByRole("table", { name: "观察股票列表" })
    .locator("tbody tr")
    .evaluateAll((rows) =>
      rows.map((row) => row.querySelector("td button span")?.textContent ?? ""),
    );
}

async function memberships(page: Page, securityId = "000001.SZ") {
  return (
    await (await page.request.get(`/api/observations/memberships?securityId=${securityId}`)).json()
  ).groupIds as string[];
}

async function candidateState(page: Page, scope = "processed") {
  const payload = await (
    await page.request.get(`/api/classification/candidates?scope=${scope}`)
  ).json();
  return payload.candidates as { securityId: string; state: string }[];
}

// --- 组管理：新建、重命名、删除 ---

test("组管理：新建、重命名、删除；删除组不影响其他组与处理记录", async ({ page }) => {
  await page.goto("/");
  await manage(page);

  await expect(
    page.getByTestId("observation-group-default").getByText("默认观察组"),
  ).toBeVisible();
  await createGroup(page, "核心跟踪");
  await page.getByRole("button", { name: "重命名 核心跟踪" }).click();
  await page.getByLabel("重命名观察组").fill("长期核心");
  await page.getByRole("button", { name: "保存组名 核心跟踪" }).click();
  await expect(
    page.getByRole("list", { name: "观察组列表" }).getByText("长期核心", { exact: true }),
  ).toBeVisible();

  await createGroup(page, "旧分组");
  await page.getByRole("button", { name: "删除 旧分组" }).click();
  await page.getByRole("button", { name: "删除这个组" }).click();
  await expect(
    page.getByRole("list", { name: "观察组列表" }).getByText("旧分组"),
  ).toHaveCount(0);
  // 同名组删除后名字重新可用
  await createGroup(page, "旧分组");

  // 归类：把平安银行加入「长期核心」，写一条笔记留作删除组后的证据
  await classifyText(page, "000001");
  await page.getByRole("button", { name: "新增笔记", exact: true }).click();
  await page
    .getByRole("dialog", { name: /新增笔记/ })
    .getByLabel("新增笔记")
    .fill("删组不该影响这条笔记");
  await page
    .getByRole("dialog", { name: /新增笔记/ })
    .getByRole("button", { name: "保存笔记" })
    .click();
  await expect(page.getByRole("dialog", { name: /新增笔记/ })).toHaveCount(0);
  await observeCurrent(page, ["长期核心"]);

  // 观察列表读回：一只股票、一个组、笔记仍在
  await manage(page);
  await expect(page.getByRole("table", { name: "观察股票列表" }).locator("tbody tr")).toHaveCount(1);
  expect(await rowNames(page)).toEqual(["平安银行"]);
  await expect(
    page.getByRole("list", { name: "观察组列表" }).getByText("长期核心", { exact: true }),
  ).toBeVisible();

  // 删除该组：列表清空，但来源、笔记与处理状态都保留
  await page.getByRole("button", { name: "删除 长期核心" }).click();
  await page.getByRole("button", { name: "删除这个组" }).click();
  await expect(page.getByRole("table", { name: "观察股票列表" }).locator("tbody tr")).toHaveCount(0);

  await goModule(page, "候选归类");
  await page.getByRole("tab", { name: /已处理/ }).click();
  const processed = await candidateState(page);
  expect(processed[0].state).toBe("observed");
  await page.getByRole("button", { name: /查看全部笔记/ }).click();
  await expect(page.getByTestId("note-all-panel").getByText("删组不该影响这条笔记")).toBeVisible();
});

test("默认组可删：删至无组并重建；重启不重建已删除的默认组", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  await manage(page);
  await page.getByRole("button", { name: "删除 默认观察组" }).click();
  await page.getByRole("button", { name: "删除这个组" }).click();
  await expect(
    page.getByText("还没有观察组。在归类时加入观察，或先在这里新建一个组。"),
  ).toBeVisible();

  // 删除关系不改变候选处理状态
  const processed = await candidateState(page);
  expect(processed[0].state).toBe("observed");

  // 重启：已删除的默认组不再被重建
  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await manage(page);
  const groups = await (await page.request.get("/api/observations/groups")).json();
  expect(groups.groups).toEqual([]);
  const stocks = await (await page.request.get("/api/observations/securities")).json();
  expect(stocks.stocks).toEqual([]);

  // 允许在观察组模块重新建组，默认名重新可用
  await createGroup(page, "默认观察组");
  expect((await (await page.request.get("/api/observations/groups")).json()).groups).toHaveLength(1);
});

// --- 观察列表：去重、所属组、切组、缺行情 ---

test("观察列表：按证券去重、显示所属全部组、可切单组、缺行情仍可浏览", async ({ page }) => {
  await page.goto("/");
  await manage(page);
  await createGroup(page, "组A");
  await createGroup(page, "组B");

  await classifyText(page, "000001\n600519");
  await observeCurrent(page, ["组A", "组B"]);
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await observeCurrent(page, ["组A"]);

  await manage(page);
  const table = page.getByRole("table", { name: "观察股票列表" });
  // 全部观察股票：两只股票各一行，不因多组关系重复
  await expect(table.locator("tbody tr")).toHaveCount(2);
  const firstRow = table.locator("tbody tr").filter({ hasText: "平安银行" });
  await expect(firstRow.getByText("组A")).toBeVisible();
  await expect(firstRow.getByText("组B")).toBeVisible();

  // 缺行情不阻塞浏览：价格与行情日留空占位
  await expect(firstRow.locator("td").nth(1)).toHaveText("—");
  await expect(firstRow.locator("td").nth(2)).toHaveText("—");
  await expect(firstRow.locator("td").nth(3)).toHaveText("—");
  // 同页详情照常呈现
  await expect(page.getByTestId("security-detail")).toBeVisible();
  await expect(page.getByTestId("quote-panel").getByText(/行情暂未取得/)).toBeVisible();

  // 切单组
  await page.getByRole("button", { name: /^组A/ }).click();
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await page.getByRole("button", { name: /^组B/ }).click();
  await expect(table.locator("tbody tr")).toHaveCount(1);
  expect(await rowNames(page)).toEqual(["平安银行"]);
  // 切单组只改变筛选：所属组列仍列出该股票的全部现存组
  await expect(table.locator("tbody tr").getByText("组A")).toBeVisible();
  await expect(table.locator("tbody tr").getByText("组B")).toBeVisible();
  // 回到汇总视图
  await page.getByTestId("observation-group-all").click();
  await expect(table.locator("tbody tr")).toHaveCount(2);
});

// --- 排序与工作位置恢复 ---

test("列表按加入时间倒序，可按名称或涨跌幅排序，重启恢复同一排序", async ({ page }) => {
  await server.stop();
  server = await startServer(8799, undefined, { DSLITE_QUOTES_FIXTURE: quotesFixture });
  await page.goto("/");

  // 同批导入按证券身份稳定排序：平安 → 招商 → 茅台，逐只加入观察
  await classifyText(page, "600519\n600036\n000001");
  for (const name of ["平安银行", "招商银行"]) {
    await expect(page.getByRole("heading", { name })).toBeVisible();
    await observeCurrent(page, ["默认观察组"]);
    await page.getByRole("button", { name: "下一个" }).click();
  }
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await observeCurrent(page, ["默认观察组"]);

  // 等行情补取完成，再进观察组模块
  await expect
    .poll(
      async () => {
        const summary = await (
          await page.request.get(
            "/api/quotes/summary?securityIds=000001.SZ,600036.SH,600519.SH",
          )
        ).json();
        return summary.quotes.length;
      },
      { timeout: 30_000 },
    )
    .toBe(3);

  await manage(page);
  const table = page.getByRole("table", { name: "观察股票列表" });
  await expect(table.locator("tbody tr")).toHaveCount(3);
  // 默认按加入时间倒序：最后加入的茅台在最前
  await expect
    .poll(async () => rowNames(page), { timeout: 15_000 })
    .toEqual(["贵州茅台", "招商银行", "平安银行"]);
  // 行情日与实际涨跌幅如实展示
  const pingan = table.locator("tbody tr").filter({ hasText: "平安银行" });
  await expect(pingan.locator("td").nth(1)).toHaveText("10.10");
  await expect(pingan.locator("td").nth(2)).toHaveText("+1.00%");
  await expect(pingan.locator("td").nth(3)).toHaveText("2026-09-18");

  const sort = page.getByLabel("观察列表排序");
  await sort.selectOption("name");
  await expect.poll(async () => rowNames(page)).toEqual(["贵州茅台", "平安银行", "招商银行"]);
  await sort.selectOption("change");
  await expect.poll(async () => rowNames(page)).toEqual(["招商银行", "平安银行", "贵州茅台"]);

  // 重启后排序与当前股票一起读回
  await server.stop();
  server = await startServer(8799, server.dataDir, { DSLITE_QUOTES_FIXTURE: quotesFixture });
  await page.reload();
  await manage(page);
  await expect(page.getByLabel("观察列表排序")).toHaveValue("change");
  await expect.poll(async () => rowNames(page), { timeout: 15_000 }).toEqual([
    "招商银行",
    "平安银行",
    "贵州茅台",
  ]);
});

// --- 同页详情：展开与返回、共用来源与处理记录 ---

test("详情可展开为完整页面并返回；来源与处理记录默认收起", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  await manage(page);
  const detail = page.getByTestId("security-detail");
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(detail.getByTestId("quote-panel")).toBeVisible();
  // 来源与处理记录默认收起：展开前没有入选日期与处理记录列表
  await expect(page.getByRole("dialog", { name: "来源与处理记录" })).toHaveCount(0);
  await detail.getByRole("button", { name: /来源与处理记录/ }).click();
  const records = page.getByRole("dialog", { name: "来源与处理记录" });
  await expect(records.getByText("入选日期")).toBeVisible();
  await expect(records.getByText("处理记录")).toBeVisible();
  await page.keyboard.press("Escape");

  // 展开为完整页面：组与列表让位；返回后仍是同一只股票
  const groups = page.getByTestId("observation-groups");
  await page.getByRole("button", { name: "展开" }).click();
  await expect(groups).toBeHidden();
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "返回列表" }).click();
  await expect(groups).toBeVisible();
  await expect(
    page.getByRole("table", { name: "观察股票列表" }).locator('tr[aria-selected="true"]'),
  ).toHaveCount(1);

  // 在详情里写笔记：只动笔记，不改变处理状态
  await detail.getByRole("button", { name: "新增笔记", exact: true }).click();
  await page
    .getByRole("dialog", { name: /新增笔记/ })
    .getByLabel("新增笔记")
    .fill("在观察页写的笔记");
  await page
    .getByRole("dialog", { name: /新增笔记/ })
    .getByRole("button", { name: "保存笔记" })
    .click();
  await expect(page.getByRole("dialog", { name: /新增笔记/ })).toHaveCount(0);
  const processed = await candidateState(page);
  expect(processed[0].state).toBe("observed");
});

// --- 转组、多组归属、退出全部组 ---

test("观察页可转组、多组归属与退出全部组，均不改变处理状态", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  await manage(page);
  const editor = page.getByRole("dialog", { name: "调整观察关系" });
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("checkbox").first()).toBeVisible();
  // 归类时可直接新建组，观察页同样可以
  await editor.getByLabel("新建观察组名称").fill("趋势跟踪");
  await editor.getByRole("button", { name: "新建" }).click();
  await editor.getByRole("button", { name: "保存观察关系" }).click();
  await expect(page.getByRole("dialog", { name: "调整观察关系" })).toHaveCount(0);
  expect(await memberships(page)).toHaveLength(2);

  // 转组：取消默认组，只保留趋势跟踪
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await editor.getByRole("checkbox", { name: /默认观察组/ }).uncheck();
  // 保存已完成但页面读回仍在途时，入口必须明确禁用，不能显示可点却吞掉点击。
  let releaseDetails!: () => void;
  const detailsGate = new Promise<void>((resolve) => { releaseDetails = resolve; });
  await page.route("**/api/observations/securities/000001.SZ", async (route) => {
    const response = await route.fetch();
    await detailsGate;
    await route.fulfill({ response });
  });
  await editor.getByRole("button", { name: "保存观察关系" }).click();
  await expect(page.getByRole("dialog", { name: "调整观察关系" })).toHaveCount(0);
  expect(await memberships(page)).toHaveLength(1);
  try {
    await expect(page.getByRole("button", { name: "调整观察关系" })).toBeDisabled();
  } finally {
    releaseDetails();
  }
  await expect(page.getByRole("button", { name: "调整观察关系" })).toBeEnabled();
  await page.unroute("**/api/observations/securities/000001.SZ");

  // 退出全部组
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await editor.getByRole("button", { name: "退出全部组" }).click();
  await expect(page.getByRole("dialog", { name: "调整观察关系" })).toHaveCount(0);
  expect(await memberships(page)).toEqual([]);
  await expect(page.getByRole("table", { name: "观察股票列表" }).locator("tbody tr")).toHaveCount(0);
  // 详情不残留已失效的旧股票：它已不在观察范围内，不能继续显示「已观察」
  await expect(page.getByTestId("security-detail")).toHaveCount(0);
  await expect(page.getByTestId("stock-card")).toHaveCount(0);
  await expect(page.getByText("已观察", { exact: true })).toHaveCount(0);
  await expect(
    page.getByText("还没有观察股票，在候选归类里加入观察后就会出现在这里。"),
  ).toBeVisible();
  // 关系被清空，但处理状态仍是已观察
  const processed = await candidateState(page);
  expect(processed[0].state).toBe("observed");
});

test("读取观察关系失败时禁止保存，不误清空已有关系", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  await manage(page);
  await page.route("**/api/observations/memberships*", (route) => route.abort());
  await page.getByRole("button", { name: "调整观察关系" }).click();
  const editor = page.getByRole("dialog", { name: "调整观察关系" });
  // 读不到已有关系就不能保存：空的勾选会被当成「移出全部组」写回库
  await expect(editor.getByRole("alert")).toContainText("未能读取已有观察关系");
  await expect(
    editor.getByRole("button", { name: "保存观察关系" }),
  ).toBeDisabled();
  await expect(editor.getByRole("button", { name: "退出全部组" })).toBeDisabled();

  await page.unrouteAll();
  await editor.getByRole("button", { name: "重试读取" }).click();
  await expect(editor.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await expect(
    editor.getByRole("button", { name: "保存观察关系" }),
  ).toBeEnabled();
  await editor.getByRole("button", { name: "保存观察关系" }).click();
  await expect(page.getByRole("dialog", { name: "调整观察关系" })).toHaveCount(0);
  expect(await memberships(page)).toHaveLength(1);
});

test("共享缓存不能代替本次确认：本次打开读取失败仍禁止保存", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  await manage(page);
  const editor = page.getByRole("dialog", { name: "调整观察关系" });

  // 先成功打开一次：组列表与该股票关系的读取结果进入共享查询缓存
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await editor.getByRole("button", { name: "取消", exact: true }).click();
  await expect(editor).toHaveCount(0);

  // 缓存里仍有关系，但这次打开的读取失败：不得据此初始化勾选或开放保存
  await page.route("**/api/observations/memberships*", (route) => route.abort());
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("alert")).toContainText("未能读取已有观察关系");
  await expect(editor.getByRole("checkbox")).toHaveCount(0);
  await expect(editor.getByRole("button", { name: "保存观察关系" })).toBeDisabled();
  await expect(editor.getByRole("button", { name: "退出全部组" })).toBeDisabled();
  expect(await memberships(page)).toHaveLength(1);

  // 手动重试读取成功后才初始化勾选并开放保存
  await page.unrouteAll();
  await editor.getByRole("button", { name: "重试读取" }).click();
  await expect(editor.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await expect(editor.getByRole("button", { name: "保存观察关系" })).toBeEnabled();
});

test("浏览器报告离线时本机读取照常进行，不因框架默认值暂停", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);
  await manage(page);

  // 外网断开但本机服务仍可用：onlineManager 只认 window 的 online/offline 事件，
  // 用事件把查询库判成离线，本机 /api 请求本身不受影响。
  await page.evaluate(() => window.dispatchEvent(new Event("offline")));

  const editor = page.getByRole("dialog", { name: "调整观察关系" });
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await expect(editor.getByRole("button", { name: "保存观察关系" })).toBeEnabled();
  await editor.getByRole("button", { name: "保存观察关系" }).click();
  await expect(editor).toHaveCount(0);
  expect(await memberships(page)).toHaveLength(1);
});

test("归类卡片的两个关系入口读取失败时都禁止提交", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");

  // 先成功打开一次，让关系读取进入缓存：缓存命中时最容易被误当成本次确认
  const firstPicker = await picker(page);
  await firstPicker.getByRole("button", { name: "取消", exact: true }).click();
  await expect(firstPicker).toHaveCount(0);

  // 加入 / 保留观察：本次打开读不到已有关系就不预选、不提交
  await page.route("**/api/observations/memberships*", (route) => route.abort());
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  const pickerDialog = page.getByRole("dialog", { name: "选择观察组" });
  await expect(pickerDialog.getByRole("alert")).toContainText("未能读取已有观察关系");
  await expect(pickerDialog.getByRole("checkbox")).toHaveCount(0);
  await expect(pickerDialog.getByRole("button", { name: "确认并完成归类" })).toBeDisabled();
  await page.unrouteAll();
  await pickerDialog.getByRole("button", { name: "重试读取" }).click();
  await expect(pickerDialog.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await pickerDialog.getByRole("button", { name: "取消", exact: true }).click();
  await expect(pickerDialog).toHaveCount(0);

  // 完成一次归类，让当前卡片出现「移出观察组」
  await observeCurrent(page, ["默认观察组"]);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 移出观察组：先把关系读进缓存，再让本次打开读取失败，同样不预选、不提交
  const firstRemover = await removePicker(page);
  await firstRemover.getByRole("button", { name: "取消", exact: true }).click();
  await expect(firstRemover).toHaveCount(0);
  await page.route("**/api/observations/memberships*", (route) => route.abort());
  await page.getByRole("button", { name: "移出观察组" }).click();
  const removerDialog = page.getByRole("dialog", { name: "选择要移出的观察组" });
  await expect(removerDialog.getByRole("alert")).toContainText("未能读取已有观察关系");
  await expect(removerDialog.getByRole("checkbox")).toHaveCount(0);
  await expect(removerDialog.getByRole("button", { name: "确认移出" })).toBeDisabled();
  await page.unrouteAll();
  expect(await memberships(page)).toHaveLength(1);
});

test("重开不吃上一轮还在路上的旧读取结果", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");

  // 观察关系直接用接口建立，再整页重载：这只证券在本次页面里还没有被读过，
  // 冷缓存正是框架会复用旧请求的条件
  const groups = await (await page.request.get("/api/observations/groups")).json();
  const defaultGroup = groups.groups.find((group: { isDefault: boolean }) => group.isDefault);
  const lateGroup = await (
    await page.request.post("/api/observations/groups", { data: { name: "迟到的组" } })
  ).json();
  const pending = await (
    await page.request.get("/api/classification/candidates?scope=unprocessed")
  ).json();
  await page.request.post(
    `/api/observations/candidates/${pending.candidates[0].candidateId}/observe`,
    { data: { groupIds: [defaultGroup.groupId] } },
  );
  await page.reload();

  // 首次打开：关系读取取到当时的快照后压住响应（此刻服务端还没有「迟到的组」关系）
  let releaseFirst!: () => void;
  const firstGate = new Promise<void>((resolve) => {
    releaseFirst = resolve;
  });
  let calls = 0;
  await page.route("**/api/observations/memberships*", async (route) => {
    calls += 1;
    if (calls > 1) {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    await firstGate;
    await route.fulfill({ response }).catch(() => undefined);
  });

  await manage(page);
  const editor = page.getByRole("dialog", { name: "调整观察关系" });
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("status")).toContainText("正在读取已有观察关系");
  await page.keyboard.press("Escape");
  await expect(editor).toHaveCount(0);

  // 本次打开期间，外部把「迟到的组」加进这只股票的关系
  await page.request.put("/api/observations/memberships", {
    data: { securityId: "000001.SZ", groupIds: [defaultGroup.groupId, lateGroup.groupId] },
  });

  // 重开：本次确认必须来自本次发起的读取
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("checkbox", { name: /迟到的组/ })).toBeChecked();

  // 放行上一轮旧响应：它不能把勾选改回旧快照，保存也不能丢掉新关系
  releaseFirst();
  await page.waitForTimeout(300);
  await expect(editor.getByRole("checkbox", { name: /迟到的组/ })).toBeChecked();
  await editor.getByRole("button", { name: "保存观察关系" }).click();
  await expect(editor).toHaveCount(0);
  expect(await memberships(page)).toHaveLength(2);
});

test("一次打开只发一轮读取，不因框架自动读取多发请求", async ({ page }) => {
  // 包住 window.fetch：无论请求有没有真的发到网络层，都能数清应用发起了几次读取
  await page.addInitScript(() => {
    const calls: string[] = [];
    (window as unknown as { __observationReads: string[] }).__observationReads = calls;
    const original = window.fetch;
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (url.includes("/api/observations/")) calls.push(url);
      return original(input, init);
    };
  });

  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  // 先成功打开一次让缓存有值：缓存命中的重开最容易触发框架自己再读一轮
  await manage(page);
  const editor = page.getByRole("dialog", { name: "调整观察关系" });
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await editor.getByRole("button", { name: "取消", exact: true }).click();
  await expect(editor).toHaveCount(0);

  await page.evaluate(() => {
    (window as unknown as { __observationReads: string[] }).__observationReads.length = 0;
  });
  await page.getByRole("button", { name: "调整观察关系" }).click();
  await expect(editor.getByRole("checkbox", { name: /默认观察组/ })).toBeChecked();
  await page.waitForTimeout(500);
  const reads = await page.evaluate(
    () => (window as unknown as { __observationReads: string[] }).__observationReads,
  );
  expect(reads.filter((url) => url.includes("memberships"))).toHaveLength(1);
  expect(reads.filter((url) => url.includes("/groups"))).toHaveLength(1);
  expect(reads).toHaveLength(2);
});

test("切组后行情摘要按当前组重新加载，不留空", async ({ page }) => {
  await server.stop();
  server = await startServer(8799, undefined, { DSLITE_QUOTES_FIXTURE: quotesFixture });
  await page.goto("/");
  await manage(page);
  await createGroup(page, "组A");
  await createGroup(page, "组B");

  // 同批按证券身份排序：平安 → 招商，分别加入两个组
  await classifyText(page, "600036\n000001");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await observeCurrent(page, ["组A"]);
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "招商银行" })).toBeVisible();
  await observeCurrent(page, ["组B"]);

  // 先等后台补取把两只股票的行情落到库里，再验证列表读取
  await expect
    .poll(
      async () => {
        const summary = await (
          await page.request.get(
            "/api/quotes/summary?securityIds=000001.SZ,600036.SH",
          )
        ).json();
        return summary.quotes.length;
      },
      { timeout: 30_000 },
    )
    .toBe(2);

  // 把单组视图存下来后重新打开页面：此时只取到该组成员的行情，
  // 再切到另一组就能暴露「行情不跟着切组重取」
  await manage(page);
  await page.getByRole("button", { name: /^组A/ }).click();
  await page.reload();
  await manage(page);
  const table = page.getByRole("table", { name: "观察股票列表" });
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(
    table.locator("tbody tr").filter({ hasText: "平安银行" }).locator("td").nth(1),
  ).toHaveText("10.10");

  // 切到另一组：行情要跟着这批股票重新取，而不是继续显示上一组的空值
  await page.getByRole("button", { name: /^组B/ }).click();
  await expect(table.locator("tbody tr")).toHaveCount(1);
  const zhaoshang = table.locator("tbody tr").filter({ hasText: "招商银行" });
  await expect(zhaoshang.locator("td").nth(1)).toHaveText("20.60");
  await expect(zhaoshang.locator("td").nth(2)).toHaveText("+3.00%");
  await expect(zhaoshang.locator("td").nth(3)).toHaveText("2026-09-18");
  // 详情里的当前股票同步落到该组成员
  await expect(
    page.getByTestId("security-detail").getByRole("heading", { name: "招商银行" }),
  ).toBeVisible();

  // 按涨跌幅排序用的是重新取到的行情，而不是缺失值
  await page.getByLabel("观察列表排序").selectOption("change");
  await page.getByTestId("observation-group-all").click();
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect
    .poll(async () => rowNames(page))
    .toEqual(["招商银行", "平安银行"]);
});

// --- 全局搜索的三个分支 ---

test("搜索：待归类打开卡片不改队列；已观察开详情；仅暂不关注保留原状态", async ({ page }) => {
  await page.goto("/");
  // 同批导入按证券身份稳定排序：平安 → 宁德 → 茅台
  await classifyText(page, "000001\n600519\n300750");

  // 平安银行加入观察（已处理）
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await observeCurrent(page, ["默认观察组"]);
  // 宁德时代留作待归类
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  // 贵州茅台暂不关注（已处理，但未观察）
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();

  const search = page.getByLabel("搜索已导入股票");

  // 分支一：已观察且没有待归类对象 → 打开个股详情，不创建待归类对象
  await search.fill("平安银行");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: /平安银行/ })
    .click();
  await expect(page.getByTestId("security-detail").getByRole("heading", { name: "平安银行" })).toBeVisible();
  expect((await candidateState(page, "unprocessed")).map((c) => c.securityId)).not.toContain(
    "000001.SZ",
  );

  // 分支二：仅暂不关注记录 → 保留原状态的即时卡片，主动重新归类才恢复
  await search.fill("贵州茅台");
  const instant = page.getByRole("list", { name: "搜索结果列表" });
  await expect(instant.getByText("暂不关注")).toBeVisible();
  // 打开即时卡片：仍停在原处理状态，没有被拉进待归类队列
  await instant.getByRole("button", { name: "查看卡片" }).click();
  await expect(
    page.getByTestId("stock-card").getByText("暂不关注", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByTestId("stock-card").getByRole("button", { name: "重新归类" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "加入 / 保留观察" })).toHaveCount(0);
  expect((await candidateState(page, "unprocessed")).map((c) => c.securityId)).toEqual([
    "300750.SZ",
  ]);

  // 主动重新归类才恢复待处理
  await search.fill("贵州茅台");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: "重新归类" })
    .click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(page.getByRole("button", { name: "加入 / 保留观察" })).toBeVisible();

  // 分支三：待归类股票 → 打开候选卡作为访问步骤，不重排未处理池
  const searchQueue = page.getByRole("listbox", { name: "待归类股票列表" });
  const orderBefore = await Promise.all(
    [0, 1].map(async (index) => (await searchQueue.getByRole("option").nth(index).innerText())),
  );
  await search.fill("宁德时代");
  await page.getByRole("list", { name: "搜索结果列表" }).getByRole("button", { name: /宁德时代/ }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  const orderAfter = await Promise.all(
    [0, 1].map(async (index) => (await searchQueue.getByRole("option").nth(index).innerText())),
  );
  expect(orderAfter).toEqual(orderBefore);
});

// --- 跨模块与重启恢复 ---

test("各模块独立保存工作位置，重启与切模块都恢复自己的当前股票", async ({ page }) => {
  await page.goto("/");
  await manage(page);
  await createGroup(page, "长期核心");

  await classifyText(page, "000001\n600519");
  await observeCurrent(page, ["长期核心"]);
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 观察组：切到单组并把平安银行设为当前股票
  await manage(page);
  await page.getByRole("button", { name: /^长期核心/ }).click();
  await page.getByRole("table", { name: "观察股票列表" }).getByRole("button", { name: /平安银行/ }).click();
  await expect(page.getByTestId("security-detail").getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 切回候选归类：仍是贵州茅台，不被观察模块的当前股票带走
  await goModule(page, "候选归类");
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 重启后两个模块各自恢复
  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await goModule(page, "候选归类");
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await manage(page);
  await expect(page.getByTestId("security-detail").getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByLabel("观察列表排序")).toHaveValue("joined");
});

// --- 归类与观察的单向联动 ---

test("加入或保留观察只增加关系，保留原有组", async ({ page }) => {
  await page.goto("/");
  await manage(page);
  await createGroup(page, "组A");
  await createGroup(page, "组B");

  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组", "组A"]);

  // 主动重新归类后再次加入：原有关系保留，只增加组B
  await page.getByTestId("stock-card").getByRole("button", { name: "重新归类", exact: true }).click();
  await expect(page.getByRole("button", { name: "加入 / 保留观察" })).toBeVisible();
  const popover = await picker(page);
  await expect(box(page, "组A")).toBeChecked();
  await popover.getByRole("checkbox", { name: /组B/ }).check();
  await popover.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(page.getByRole("dialog", { name: "选择观察组" })).toHaveCount(0);

  const groups = await (await page.request.get("/api/observations/groups")).json();
  const byId = new Map(
    groups.groups.map((group: { groupId: string; name: string }) => [group.groupId, group.name]),
  );
  const kept = new Set((await memberships(page)).map((id) => byId.get(id)));
  expect(kept).toEqual(new Set(["默认观察组", "组A", "组B"]));
});

test("移出观察组：只移除所选关系并同时记为暂不关注", async ({ page }) => {
  await page.goto("/");
  await manage(page);
  await createGroup(page, "保留组");
  await createGroup(page, "移出组");

  await classifyText(page, "000001");
  await observeCurrent(page, ["保留组", "移出组"]);

  const remover = await removePicker(page);
  await expect(box(page, "保留组")).toBeChecked();
  await box(page, "移出组").uncheck();
  await remover.getByRole("button", { name: "确认移出" }).click();

  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByText("已暂不关注，已结束本次归类；导入事实保留。")).toBeVisible();
  expect(await memberships(page)).toHaveLength(1);
  // 已处理视图里仍是暂不关注，保留的那一组没被清掉
  await page.getByRole("tab", { name: /已处理/ }).click();
  await page.getByLabel("处理结果筛选").selectOption("dismissed");
  await expect(
    page.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
});

test("取消移出与移出失败都不改变关系与处理状态", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  const remover = await removePicker(page);
  await box(page, "默认观察组").uncheck();
  await remover.getByRole("button", { name: "取消", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "选择要移出的观察组" })).toHaveCount(0);
  expect(await memberships(page)).toHaveLength(1);

  await page.route("**/api/observations/candidates/**/remove-from-groups", async (route) => {
    const body = JSON.parse(route.request().postData()!);
    body.groupIds.push("group-does-not-exist");
    await route.continue({ postData: JSON.stringify(body) });
  });
  const failing = await removePicker(page);
  await failing.getByRole("button", { name: "确认移出" }).click();
  await expect(page.getByRole("alert")).toContainText("观察组不存在");
  expect(await memberships(page)).toHaveLength(1);
  await page.unrouteAll();

  // 原地重试成功：关系移除，状态变为暂不关注
  await failing.getByRole("button", { name: "确认移出" }).click();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByText("已暂不关注，已结束本次归类；导入事实保留。")).toBeVisible();
  expect(await memberships(page)).toEqual([]);
});

test("观察关系的增删不改变候选处理状态", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);
  const first = await memberships(page);

  // 暂不关注与主动重新归类都不删除观察关系
  await page.getByRole("button", { name: "重新归类" }).click();
  await page.getByRole("button", { name: "暂不关注" }).click();
  expect(await memberships(page)).toEqual(first);

  // 重启后关系与状态一起读回
  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await goModule(page, "候选归类");
  await page.getByRole("tab", { name: /已处理/ }).click();
  await expect(page.getByTestId("stock-card").getByText("已观察")).toBeVisible();
  expect(await memberships(page)).toEqual(first);
});

test("已观察股票当日新增来源不重置处理状态与关系", async ({ page }) => {
  await page.goto("/");
  await classifyText(page, "000001");
  await observeCurrent(page, ["默认观察组"]);

  await submitText(page, "000001\n600519");
  await goModule(page, "候选归类");

  // 已观察股票当天已入选过：不重新打开归类；模块恢复上次工作位置
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute("aria-selected", "true");
  await expect(
    page.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
  const pending = await (await page.request.get("/api/classification/candidates?scope=unprocessed")).json();
  expect(pending.candidates.map((item: { securityId: string }) => item.securityId)).toEqual(["600519.SH"]);
  expect(await memberships(page)).toHaveLength(1);
});
