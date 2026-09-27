import { expect, test, type Page } from "@playwright/test";

import { classifyText, goModule, startServer, type ServerHandle } from "./helpers";

/**
 * 工单 01 的浏览器验收：统一归类浏览结果与轻量候选列表。
 *
 * 覆盖：一次读取形成当前卡＋完整列表＋数量；普通切卡不重传整份列表；
 * 服务端变化与版本驱动列表更新（含版本不一致时读回完整结果）；
 * 列表只渲染可见行但保留完整可滚动、可点选体验；
 * 服务端重启后当前卡、列表与数量一致读回。
 */

let server: ServerHandle;

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

/** 记录工作区发出的归类浏览请求：方法、路径与响应大小。 */
function recordBrowseCalls(page: Page) {
  const calls: { method: string; path: string; body: string }[] = [];
  page.on("response", async (response) => {
    const url = response.url();
    if (!url.includes("/api/classification/")) return;
    let body = "";
    try {
      body = await response.text();
    } catch {
      body = "";
    }
    calls.push({
      method: response.request().method(),
      path: url.split("/api/classification")[1],
      body,
    });
  });
  return calls;
}

test("打开工作区一次读到一致的当前卡、列表与数量", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  const calls = recordBrowseCalls(page);
  await page.goto("/");

  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByText(/第 1 \/ 3 只/)).toBeVisible();

  // 列表与当前卡来自同一份结果：打开时读一次完整浏览结果即可
  const listboxes = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(listboxes.getByRole("option")).toHaveCount(3);
  await expect(listboxes.getByRole("option").nth(0)).toContainText("平安银行");

  const browseReads = calls.filter(
    (call) => call.method === "GET" && call.path.startsWith("/view"),
  );
  expect(browseReads.length).toBeLessThanOrEqual(2);
  await context.close();
});

test("普通切卡只更新卡片与列表变化，不重传整份列表", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  const calls = recordBrowseCalls(page);
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  // 等切卡请求全部落定再点数：两次点选各一次导航命令，没有多余的补发
  await expect
    .poll(() => calls.filter((call) => call.path.includes("/view/navigate")).length)
    .toBe(2);

  const navigations = calls.filter(
    (call) => call.method === "POST" && call.path.includes("/view/navigate"),
  );
  expect(navigations.length).toBe(2);
  // 切卡响应不含整份列表：没有 rows/pending 数组，只带变化的列表行；
  // order 只在这次写入改变队列顺序时非空，切卡一律为空
  for (const call of navigations) {
    const payload = JSON.parse(call.body);
    expect(payload.rows).toBeUndefined();
    expect(payload.pending).toBeUndefined();
    expect(payload.dates).toBeUndefined();
    expect(Object.keys(payload.changes).sort()).toEqual(["changed", "order", "removed"]);
    expect(payload.changes.order).toEqual([]);
  }
  // 切卡期间没有重读整份列表
  expect(
    calls.filter((call) => call.method === "GET" && call.path.startsWith("/view")).length,
  ).toBe(0);

  // 左侧列表仍与服务端一致：位置、状态与当前卡同步（窗口化只渲染可见行）
  const options = page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option");
  await expect(options.first()).toHaveAttribute("aria-setsize", "3");
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option", { selected: true }),
  ).toContainText("贵州茅台");
  await context.close();
});

test("服务端变化与版本驱动列表：稍后处理移尾后列表顺序与服务端一致", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "稍后处理" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();

  const options = page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option");
  await expect(options.nth(0)).toContainText("宁德时代");
  await expect(options.nth(2)).toContainText("平安银行");

  // 直接读接口核对：列表顺序与服务端一致，且列表行是轻量投影
  const fresh = await (await page.request.get(`${server.baseURL}/api/classification/view`)).json();
  expect(fresh.rows.map((row: { security: { name: string } }) => row.security.name)).toEqual([
    "宁德时代",
    "贵州茅台",
    "平安银行",
  ]);
  for (const row of fresh.rows) {
    expect(row.history).toBeUndefined();
    expect(row.sources).toBeUndefined();
    expect(row.selections).toBeUndefined();
    expect(row.noteCount).toBeUndefined();
    expect(row.sourceCount).toBe(1);
  }
  await context.close();
});

test("筛选与页签切换后当前卡仍属于左侧列表", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 切到已处理页签：当前卡与列表同时落到该范围，不出现空卡
  await page.getByRole("tab", { name: /已处理/ }).click();
  await expect(
    page.getByRole("listbox", { name: "已归类股票列表" }).getByRole("option"),
  ).toHaveCount(1);
  await expect(page.getByTestId("stock-card")).toBeVisible();
  await expect(
    page.getByTestId("stock-card").getByRole("heading", { name: "平安银行" }),
  ).toBeVisible();

  // 结果筛选进一步收窄范围：卡片跟随
  await page.getByLabel("处理结果筛选").selectOption("dismissed");
  await expect(page.getByTestId("stock-card")).toBeVisible();
  await page.getByLabel("处理结果筛选").selectOption("cleared");
  await expect(page.getByTestId("stock-card")).toHaveCount(0);
  await expect(page.getByText("当前筛选没有匹配股票，请调整或清除筛选。")).toBeVisible();
  await context.close();
});

test("长队列虚拟滚动不出现空白：上下滑动覆盖可见区域，底部候选可点选", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 1092, height: 871 } });
  const page = await context.newPage();
  // 记录列表里同时出现过的最大行数：逐次追加与整份挂载都会被记下，
  // 因此「首次打开渲染全部行又缩回窗口」这种瞬时超标同样会被抓住。
  await page.addInitScript(() => {
    const peaks: number[] = [];
    (window as unknown as { __rowPeaks: number[] }).__rowPeaks = peaks;
    const watch = () => {
      const list = document.querySelector('[role="listbox"]');
      if (!list) {
        requestAnimationFrame(watch);
        return;
      }
      const sample = () => peaks.push(list.querySelectorAll('[role="option"]').length);
      new MutationObserver(sample).observe(list, { childList: true });
      sample();
    };
    requestAnimationFrame(watch);
  });
  await page.goto("/");
  // 个人使用上限内的长队列：全部为已识别股票
  await classifyText(
    page,
    [
      "000001", "000002", "000006", "000007", "000008", "000009", "000010",
      "000011", "000012", "000014", "000016", "000017", "000019", "000020",
      "000021", "000025", "000026", "000027", "000028", "000029", "000030",
      "000031", "000032", "000034", "000035", "000036", "000037", "000039",
      "000042", "000045", "000048", "000049", "000050", "000055", "000056",
      "000058", "000059", "000060", "000061", "000062",
    ].join("\n"),
  );
  await expect(page.getByTestId("stock-card")).toBeVisible();

  const list = page.getByRole("listbox", { name: "待归类股票列表" });
  const total = Number(await list.getAttribute("data-virtual-total"));
  expect(total).toBeGreaterThanOrEqual(35);

  // 只渲染可见窗口内的行，而不是全部行
  const rendered = await list.getByRole("option").count();
  expect(rendered).toBeLessThan(total / 2);
  expect(Number(await list.getAttribute("data-virtual-start"))).toBe(0);

  // 挂载期间也从未渲染过全部行：渲染窗口由测量决定，首帧不挂载整份列表
  const peaks = await page.evaluate(
    () => (window as unknown as { __rowPeaks: number[] }).__rowPeaks,
  );
  expect(Math.max(...peaks, 0)).toBeLessThan(total / 2);

  // 真实滚轮连续滚动：渲染行必须覆盖列表在滚动容器内的可见部分，不能只挂在屏幕下方。
  const pane = page.locator(".classification-queue-pane");
  const assertCovered = async () => {
    await expect.poll(async () => list.evaluate((node) => {
      const scroller = node.closest(".classification-queue-pane")!;
      const viewport = scroller.getBoundingClientRect();
      const bounds = node.getBoundingClientRect();
      const rows = Array.from(node.querySelectorAll(".classification-queue-row"));
      if (!rows.length) return false;
      const first = rows[0].getBoundingClientRect();
      const last = rows[rows.length - 1].getBoundingClientRect();
      return first.top <= Math.max(viewport.top, bounds.top) + 1
        && last.bottom >= Math.min(viewport.bottom, bounds.bottom) - 1;
    })).toBe(true);
  };
  await pane.hover();
  for (const delta of [480, 480, 480, -480, -480]) {
    const beforeScroll = await pane.evaluate((node) => node.scrollTop);
    await page.mouse.wheel(0, delta);
    await expect.poll(() => pane.evaluate((node) => node.scrollTop)).not.toBe(beforeScroll);
    await assertCovered();
  }
  await page.screenshot({ path: test.info().outputPath("queue-scrolled.png") });

  // 滚动到底部：窗口整体平移，末项进入可见范围并可点开
  await page.locator(".classification-queue-pane").evaluate((node) => {
    node.scrollTop = node.scrollHeight;
  });
  await expect
    .poll(async () => Number(await list.getAttribute("data-virtual-start")))
    .toBeGreaterThan(0);
  await assertCovered();
  const last = list.getByRole("option").last();
  await expect(last).toHaveAttribute("aria-posinset", String(total));
  await last.click();
  await expect(last).toHaveAttribute("aria-selected", "true");
  await expect(page.getByTestId("stock-card").getByRole("heading", { name: "深圳华强", exact: true })).toBeVisible();
  await context.close();
});

test("重启后当前卡、列表与数量读回一致", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  const before = await (await page.request.get(`${server.baseURL}/api/classification/view`)).json();
  await page.reload();
  await expect(page.getByTestId("launch-page")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await expect(page.getByText(/第 2 \/ 3 只/)).toBeVisible();
  expect((await (await page.request.get(`${server.baseURL}/api/classification/view`)).json()).currentCandidateId)
    .toBe(before.currentCandidateId);
  await context.close();

  await server.stop();
  server = await startServer(8799, server.dataDir);
  const reopened = await browser.newContext();
  const page2 = await reopened.newPage();
  await page2.goto("/");
  await goModule(page2, "候选归类");
  await expect(page2.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await expect(page2.getByText(/第 2 \/ 3 只/)).toBeVisible();
  await expect(
    page2.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(3);

  const after = await (await page2.request.get(`${server.baseURL}/api/classification/view`)).json();
  expect(after.currentCandidateId).toBe(before.currentCandidateId);
  expect(after.rows.map((row: { candidateId: string }) => row.candidateId)).toEqual(
    before.rows.map((row: { candidateId: string }) => row.candidateId),
  );
  expect(after.summary).toEqual(before.summary);
  await reopened.close();
});
