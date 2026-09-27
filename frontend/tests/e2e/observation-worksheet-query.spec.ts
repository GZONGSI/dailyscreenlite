import { expect, test, type Page } from "@playwright/test";

import { classifyText, goModule, startServer, type ServerHandle } from "./helpers";

/**
 * 工单 06 的观察工作表读取接线：真实 Chrome → 真实本机 HTTP → 临时真实数据库。
 *
 * 只钉新增接缝：工作表与详情的读取时机（进入模块、跨模块打开、数据更新）、
 * 写入与读取的先后（写入前的在途读取、写入期间到来的失效、卸载后落地的旧响应）、
 * 以及重读失败的展示政策。
 * 组管理、排序、关系编辑与重启恢复在 `observation-groups.spec.ts` 里复跑。
 */

let server: ServerHandle;

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

/** 在应用自己的 fetch 上数请求：不受请求是否到达网络层影响。 */
async function countReads(page: Page) {
  await page.addInitScript(() => {
    const calls: string[] = [];
    (window as unknown as { __reads: string[] }).__reads = calls;
    const original = window.fetch;
    window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      const url =
        typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      const method = (init?.method ?? "GET").toUpperCase();
      if (url.includes("/api/observations/")) calls.push(`${method} ${url}`);
      return original(input, init);
    };
  });
}

const reads = (page: Page) =>
  page.evaluate(() => (window as unknown as { __reads: string[] }).__reads);

const clearReads = (page: Page) =>
  page.evaluate(() => {
    (window as unknown as { __reads: string[] }).__reads.length = 0;
  });

const viewReads = async (page: Page) =>
  (await reads(page)).filter((call) => call.startsWith("GET") && call.includes("/view"));

const detailReads = async (page: Page) =>
  (await reads(page)).filter(
    (call) => call.startsWith("GET") && call.includes("/securities/"),
  );

async function manage(page: Page) {
  await goModule(page, "观察组");
  await expect(page.getByTestId("observation-groups")).toBeVisible();
}

/** 把当前卡片加入默认观察组（预选即默认组），完成后回到同一张卡。 */
async function observeCurrent(page: Page) {
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  const popover = page.getByRole("dialog", { name: "选择观察组" });
  await expect(popover.getByRole("checkbox").first()).toBeVisible();
  const boxes = popover.getByRole("checkbox");
  for (let index = 0; index < (await boxes.count()); index += 1) {
    const box = boxes.nth(index);
    if (await box.isChecked()) await box.uncheck();
  }
  await popover.getByRole("checkbox", { name: /默认观察组/ }).check();
  await popover.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(page.getByRole("dialog", { name: "选择观察组" })).toHaveCount(0);
  await page.getByRole("button", { name: "上一个" }).click();
}

/** 导入两只股票并加入观察，最后停在观察模块。 */
async function seed(page: Page) {
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await observeCurrent(page);
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await observeCurrent(page);
  await manage(page);
  await expect(page.getByTestId("security-detail")).toBeVisible();
}

/**
 * 更新状态夹具：先报运行中，用例把 `terminal` 打开后，下一次轮询读到完整终态。
 *
 * 终态是「通知工作台重读」的那一刻，因此 `terminalDelivered` 比轮询次数更可靠
 * （闲置轮询也可能让次数增长）。
 */
interface UpdateStatusStub {
  /** 打开后，下一次轮询读到完整终态。 */
  terminal: boolean;
  /** 已投递的轮询次数。 */
  reads: number;
  /** 是否已经投递过完整终态。 */
  terminalDelivered: boolean;
}

async function stubUpdateStatus(page: Page): Promise<UpdateStatusStub> {
  const stub: UpdateStatusStub = { terminal: false, reads: 0, terminalDelivered: false };
  await page.route("**/api/updates", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    stub.reads += 1;
    const running = !stub.terminal;
    stub.terminalDelivered = stub.terminalDelivered || stub.terminal;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        running,
        lastRun: {
          runId: running ? "run-1" : "run-2",
          kind: "manual",
          status: running ? "running" : "success",
          startedAt: "2026-09-18T16:30:00+08:00",
          finishedAt: running ? null : "2026-09-18T16:31:00+08:00",
          securitiesStatus: null,
          securitiesMessage: null,
          securitiesCount: 0,
          quotesOk: 2,
          quotesFailed: 0,
          quotesSkipped: 0,
          quotesPending: 0,
          failedSecurities: [],
        },
        progress: running ? { total: 2, done: 0, attempt: 1 } : null,
      }),
    });
  });
  return stub;
}

test("启动页直接进入观察组并恢复组、排序与股票，重启后同样恢复", async ({ page }) => {
  await page.setViewportSize({ width: 1092, height: 871 });
  await seed(page);
  await page.getByRole("button", { name: /^默认观察组/ }).click();
  await page.getByRole("combobox", { name: "观察列表排序" }).selectOption("name");
  await page.getByRole("button", { name: "贵州茅台 600519" }).click();
  const detail = page.getByTestId("security-detail");
  await expect(detail.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  const saved = (await (await page.request.get("/api/observations/view")).json()).state;
  expect(saved).toMatchObject({ sort: "name", currentSecurityId: "600519.SH" });
  expect(saved.groupId).not.toBeNull();
  await page.getByRole("button", { name: "返回启动页" }).click();
  await page.screenshot({ path: test.info().outputPath("launch-observations.png") });
  await page.getByTestId("launch-page").getByRole("button", { name: "观察组", exact: true }).click();
  await expect(page.getByTestId("launch-page")).toHaveCount(0);
  await expect(detail.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "观察列表排序" })).toHaveValue("name");
  await expect(page.getByRole("button", { name: /^默认观察组/ })).toHaveAttribute("aria-current", "true");
  expect((await (await page.request.get("/api/observations/view")).json()).state).toEqual(saved);
  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await expect(page.getByTestId("launch-page")).toHaveCount(0);
  await expect(detail.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(page.getByRole("combobox", { name: "观察列表排序" })).toHaveValue("name");
  await expect(page.getByRole("button", { name: /^默认观察组/ })).toHaveAttribute("aria-current", "true");
  expect((await (await page.request.get("/api/observations/view")).json()).state).toEqual(saved);
});

test("点击观察股票行的行情或日期也能切股，名称按钮与重启读回保持一致", async ({ page }) => {
  await page.setViewportSize({ width: 1092, height: 871 });
  await seed(page);
  const table = page.getByRole("table", { name: "观察股票列表" });
  const bank = table.getByRole("row", { name: /平安银行/ });
  const moutai = table.getByRole("row", { name: /贵州茅台/ });
  const detail = page.getByTestId("security-detail");
  let positionWrites = 0;
  page.on("request", (request) => {
    if (request.method() === "PUT" && request.url().endsWith("/api/observations/view")) {
      positionWrites += 1;
    }
  });
  await expect(detail.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 行情尚缺失时，用户仍可点击该行的收盘价位置打开个股。
  await bank.getByRole("cell").nth(1).click();
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(bank).toHaveAttribute("aria-selected", "true");
  expect((await (await page.request.get("/api/observations/view")).json()).state.currentSecurityId)
    .toBe("000001.SZ");

  await moutai.getByRole("cell").nth(3).click();
  await expect(detail.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await bank.getByRole("button", { name: /平安银行/ }).click();
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
  // 名称按钮不能再冒泡到整行，三次点击只提交三次位置写入。
  expect(positionWrites).toBe(3);
  await page.screenshot({ path: test.info().outputPath("selected-after-row-click.png") });

  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await manage(page);
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(bank).toHaveAttribute("aria-selected", "true");
});

test("加入日期按北京时间与当前组显示，保留关系不改日期，重新加入更新日期", async ({ browser }) => {
  await server.stop();
  server = await startServer(8799, server.dataDir, { DSLITE_NOW: "2026-09-11T16:30:00Z" });
  const context = await browser.newContext({ timezoneId: "America/Los_Angeles", viewport: { width: 1092, height: 871 } });
  const page = await context.newPage();
  await seed(page);
  const table = page.getByRole("table", { name: "观察股票列表" });
  await expect(table.getByRole("columnheader", { name: "加入日期" })).toBeVisible();
  await expect(table.getByRole("columnheader")).toHaveText(["名称 / 代码", "收盘价", "涨跌幅", "行情日", "所属组", "加入日期"]);
  const bankDate = table.getByRole("row", { name: /平安银行/ }).locator("time");
  await expect(bankDate).toHaveText("2026-09-12");
  const defaultId = (await (await page.request.get("/api/observations/groups")).json()).groups[0].groupId;

  await server.stop();
  server = await startServer(8799, server.dataDir, { DSLITE_NOW: "2026-09-13T10:00:00+08:00" });
  const addedGroup = (await (await page.request.post("/api/observations/groups", { data: { name: "新组" } })).json()).groupId;
  await page.request.put("/api/observations/memberships", { data: { securityId: "000001.SZ", groupIds: [defaultId, addedGroup] } });
  await page.reload();
  await manage(page);
  await expect(bankDate).toHaveText("2026-09-13");
  await page.getByRole("button", { name: /^默认观察组/ }).click();
  await expect(bankDate).toHaveText("2026-09-12");
  await page.getByRole("button", { name: /^新组 / }).click();
  await expect(bankDate).toHaveText("2026-09-13");

  await server.stop();
  server = await startServer(8799, server.dataDir, { DSLITE_NOW: "2026-09-14T10:00:00+08:00" });
  await page.request.put("/api/observations/memberships", { data: { securityId: "000001.SZ", groupIds: [defaultId] } });
  await page.request.put("/api/observations/memberships", { data: { securityId: "000001.SZ", groupIds: [defaultId, addedGroup] } });
  await page.reload();
  await manage(page);
  await expect(bankDate).toHaveText("2026-09-14");
  await page.getByRole("button", { name: /^默认观察组/ }).click();
  await expect(bankDate).toHaveText("2026-09-12");
  await bankDate.scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath("join-date.png") });
  await context.close();
});

test("进入观察模块每次都重读工作表，跨模块打开只发一次写入", async ({ page }) => {
  await countReads(page);
  await seed(page);

  // 切走再回来：进入模块显式失效，工作表重新读一次
  await goModule(page, "候选归类");
  await clearReads(page);
  await manage(page);
  await expect(page.getByTestId("security-detail")).toBeVisible();
  expect(await viewReads(page)).toHaveLength(1);

  // 跨模块打开详情（全局搜索命中已观察股票）：整页重载后工作表是冷缓存，
  // 这次打开只发一次焦点写入，不再额外读一遍整份工作表，也不因焦点落地再补一次
  await page.reload();
  await goModule(page, "候选归类");
  await clearReads(page);
  await page.getByLabel("搜索已导入股票").fill("贵州茅台");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: /贵州茅台/ })
    .click();
  await expect(
    page.getByTestId("security-detail").getByRole("heading", { name: "贵州茅台" }),
  ).toBeVisible();
  const calls = await reads(page);
  expect(calls.filter((call) => call.includes("/focus"))).toHaveLength(1);
  expect(await viewReads(page)).toHaveLength(0);
});

test("迟到的整份工作表读取不覆盖刚保存的切组", async ({ page }) => {
  await seed(page);
  await page.getByLabel("新观察组名称").fill("组B");
  await page.getByRole("button", { name: "新建", exact: true }).click();
  await expect(
    page.getByRole("list", { name: "观察组列表" }).getByText("组B", { exact: true }),
  ).toBeVisible();

  // 压住一次进入模块时的整份读取，让它在切组之后才落地
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/observations/view", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    await gate;
    await route.fulfill({ response }).catch(() => undefined);
  });
  await goModule(page, "候选归类");
  await manage(page);

  const table = page.getByRole("table", { name: "观察股票列表" });
  // 这次进入的读取还在路上：切到空组并等它落地
  await page.getByRole("button", { name: /^组B/ }).click();
  await expect(page.getByText("当前组：组B")).toBeVisible();

  // 放行迟到响应：它带着切组之前的整份工作表，不能把当前组改回汇总视图
  release();
  await page.waitForTimeout(500);
  await expect(table.locator("tbody tr")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^组B/ })).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(page.getByText("当前组：组B")).toBeVisible();
});

test("数据更新的完整终态让工作表与详情按查询失效重读", async ({ page }) => {
  await countReads(page);
  const status = await stubUpdateStatus(page);
  await seed(page);

  await clearReads(page);
  status.terminal = true;
  await expect.poll(() => status.reads, { timeout: 20_000 }).toBeGreaterThan(1);
  // 终态通知让工作表与详情按查询失效重读（不再由观察刷新令牌驱动）
  await expect.poll(async () => (await viewReads(page)).length, { timeout: 20_000 }).toBeGreaterThan(0);
  await expect.poll(async () => (await detailReads(page)).length, { timeout: 20_000 }).toBeGreaterThan(0);
});

test("工作表重读失败保留已读回的内容并如实提示", async ({ page }) => {
  await seed(page);
  const table = page.getByRole("table", { name: "观察股票列表" });
  await expect(table.locator("tbody tr")).toHaveCount(2);

  // 之后的每次整份读取都失败：进入模块的显式重读因此拿不到新结果
  let fail = false;
  await page.route("**/api/observations/view", async (route) => {
    if (route.request().method() === "GET" && fail) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ detail: { message: "服务暂时不可用" } }),
      });
      return;
    }
    await route.continue();
  });
  fail = true;
  await goModule(page, "候选归类");
  await manage(page);

  // 已读回的表与详情仍在，并在同一处给出读取失败提示
  await expect(table.locator("tbody tr")).toHaveCount(2);
  await expect(
    page.getByRole("alert").filter({ hasText: "服务暂时不可用" }),
  ).toBeVisible();
});

test("离开模块后落地的旧实例写入响应不改写新工作区的位置", async ({ page }) => {
  await seed(page);

  // 扣住「切到默认观察组」这次写入的**响应**：请求照常到达服务端（服务端先落到默认组），
  // 投递被压到新工作区改完自己的位置之后
  let releaseOldWrite!: () => void;
  const oldWriteGate = new Promise<void>((resolve) => {
    releaseOldWrite = resolve;
  });
  let puts = 0;
  await page.route("**/api/observations/view", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    puts += 1;
    if (puts > 1) {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    await oldWriteGate;
    await route.fulfill({ response }).catch(() => undefined);
  });

  // 旧实例：切到默认观察组，响应被扣住
  await page.getByRole("button", { name: /^默认观察组/ }).click();

  // 离开模块：旧工作区卸载，它的这次写入仍在路上
  await goModule(page, "候选归类");

  // 跨模块打开（全局搜索命中已观察股票）挂载新工作区；这条路径没有普通读取，
  // 新位置能不能留住，只看共享缓存里留下的结论
  await page.getByLabel("搜索已导入股票").fill("贵州茅台");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: /贵州茅台/ })
    .click();
  const detail = page.getByTestId("security-detail");
  await expect(detail.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 新工作区改自己的位置：点选平安银行
  await page
    .getByRole("table", { name: "观察股票列表" })
    .getByRole("button", { name: /平安银行/ })
    .click();
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 放行旧实例的响应：它带着切组时的当前股票（贵州茅台），不能把新位置改回去
  releaseOldWrite();
  await page.waitForTimeout(500);
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 服务端保存的也是新位置：页面与持久化位置一致
  const state = await page.request.get(`${server.baseURL}/api/observations/view`);
  expect((await state.json()).state.currentSecurityId).toBe("000001.SZ");
});

test("搜索焦点被点选取代后，补读等待旧写入也不阻塞当前详情", async ({ page }) => {
  await seed(page);
  let releaseOld!: () => void;
  const oldGate = new Promise<void>((resolve) => { releaseOld = resolve; });
  let releaseFocus!: () => void;
  const focusGate = new Promise<void>((resolve) => { releaseFocus = resolve; });
  let oldSaved = false;
  let focusSaved = false;
  let positionSaved = false;
  let puts = 0;
  await page.route("**/api/observations/view", async (route) => {
    if (route.request().method() !== "PUT") return route.continue();
    puts += 1;
    const response = await route.fetch();
    if (puts === 1) {
      oldSaved = true;
      await oldGate;
    } else {
      positionSaved = true;
    }
    await route.fulfill({ response }).catch(() => undefined);
  });
  await page.route("**/api/observations/securities/600519.SH/focus", async (route) => {
    const response = await route.fetch();
    focusSaved = true;
    await focusGate;
    await route.fulfill({ response }).catch(() => undefined);
  });
  try {
    await page.getByRole("button", { name: /^默认观察组/ }).click();
    await expect.poll(() => oldSaved).toBe(true);
    await goModule(page, "候选归类");
    await page.getByLabel("搜索已导入股票").fill("贵州茅台");
    await page.getByRole("list", { name: "搜索结果列表" })
      .getByRole("button", { name: /贵州茅台/ }).click();
    await expect.poll(() => focusSaved).toBe(true);
    await page.getByRole("table", { name: "观察股票列表" })
      .getByRole("button", { name: /平安银行/ }).click();
    await expect.poll(() => positionSaved).toBe(true);
    releaseFocus();
    // 旧实例写入仍未返回：补读需要等它，但本次打开已被消费，详情应按新证券读取。
    const detail = page.getByTestId("security-detail");
    await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
    const reconciled = page.waitForResponse(
      (response) => response.request().method() === "GET" &&
        new URL(response.url()).pathname === "/api/observations/view",
    );
    releaseOld();
    expect((await reconciled).ok()).toBe(true);
    await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();
    const view = await (await page.request.get(`${server.baseURL}/api/observations/view`)).json();
    expect(view.state.currentSecurityId).toBe("000001.SZ");
  } finally {
    releaseFocus();
    releaseOld();
  }
});

test("同一实例里较晚的搜索焦点不被旧位置响应改回去", async ({ page }) => {
  await seed(page);

  // 扣住「切到默认观察组」这次写入的响应（服务端已保存），并留在观察模块：
  // 旧响应属于同一个仍在页面上的实例，只有写入代际能判断出它已经没有资格
  let releaseOldWrite!: () => void;
  const oldWriteGate = new Promise<void>((resolve) => {
    releaseOldWrite = resolve;
  });
  let puts = 0;
  await page.route("**/api/observations/view", async (route) => {
    if (route.request().method() !== "PUT") {
      await route.continue();
      return;
    }
    puts += 1;
    if (puts > 1) {
      await route.continue();
      return;
    }
    const response = await route.fetch();
    await oldWriteGate;
    await route.fulfill({ response }).catch(() => undefined);
  });

  await page.getByRole("button", { name: /^默认观察组/ }).click();

  // 同一个实例里完成一次更晚的写入：全局搜索打开平安银行
  await page.getByLabel("搜索已导入股票").fill("平安银行");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: /平安银行/ })
    .click();
  const detail = page.getByTestId("security-detail");
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 放行旧的位置响应：它带着切组时的当前股票（贵州茅台），不能覆盖更晚的焦点写入
  releaseOldWrite();
  await page.waitForTimeout(500);
  await expect(detail.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 服务端保存的也是平安银行：页面与持久化位置一致
  const state = await page.request.get(`${server.baseURL}/api/observations/view`);
  expect((await state.json()).state.currentSecurityId).toBe("000001.SZ");
});

test("写入在途时的数据更新读取延后到写入落定，页面与服务端位置一致", async ({ page }) => {
  const status = await stubUpdateStatus(page);
  await seed(page);
  let writeOpen = false;
  let readsDuringWrite = 0;
  let settledReads = 0;
  let readsReleased = false;
  let releaseWrite!: () => void;
  const writeGate = new Promise<void>((resolve) => {
    releaseWrite = resolve;
  });
  let releaseReads!: () => void;
  const readGate = new Promise<void>((resolve) => {
    releaseReads = resolve;
  });
  await page.route("**/api/observations/view", async (route) => {
    if (route.request().method() !== "PUT") {
      if (writeOpen && !readsReleased) {
        readsDuringWrite += 1;
        const response = await route.fetch();
        await readGate;
        await route.fulfill({ response }).catch(() => undefined);
        return;
      }
      if (!writeOpen) settledReads += 1;
      await route.continue();
      return;
    }
    writeOpen = true;
    await writeGate;
    writeOpen = false;
    await route.continue();
  });

  const switchToDefault = page.getByRole("button", { name: /^默认观察组/ }).click();
  await expect.poll(() => writeOpen).toBe(true);

  // 写入还在途中：数据更新的完整终态要求工作表失效重读
  status.terminal = true;
  await expect.poll(() => status.terminalDelivered, { timeout: 20_000 }).toBe(true);
  // 终态已被应用（渲染与副作用）后，再给错误实现的「写入期间读取」留出到达时间
  await page.waitForTimeout(500);
  expect(readsDuringWrite).toBe(0);

  // 放行写入：响应带回切组后的工作表
  releaseWrite();
  await switchToDefault;
  await expect(page.getByText("当前组：默认观察组")).toBeVisible();

  // 放行被扣住的那次旧读取（正确实现里它根本不存在）
  readsReleased = true;
  releaseReads();
  await page.waitForTimeout(500);
  await expect(page.getByText("当前组：默认观察组")).toBeVisible();
  // 延后的那次刷新没有丢：写入落定后仍按刷新读了服务器事实
  await expect.poll(() => settledReads, { timeout: 20_000 }).toBeGreaterThan(0);

  const state = await page.request.get(`${server.baseURL}/api/observations/view`);
  expect((await state.json()).state.groupId).not.toBeNull();
  await expect.poll(() => status.reads).toBeGreaterThan(1);
});
