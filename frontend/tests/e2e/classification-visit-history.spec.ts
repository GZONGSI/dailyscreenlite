import { expect, test } from "@playwright/test";

import { classifyText, goModule, startServer, submitText, type ServerHandle } from "./helpers";

/**
 * 工单 02 的浏览器验收：按访问步骤持久化归类浏览路径。
 *
 * 覆盖：手动点选与全局搜索形成新的访问步骤、历史中途跳转替换前进分支、
 * 再次访问同一候选形成新步骤、搜索与点选不改队列顺序与筛选、筛选外候选可打开
 * 并有明确提示、已处理页签可点开候选但历史末端不自动遍历、
 * 结束卡作为历史步骤跨重启恢复并在有前进历史时提供「下一个」、
 * 新导入不抢当前卡与结束卡、返回队列重置本轮。
 */

let server: ServerHandle;

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

/** 读取服务端当前浏览状态：位置断言以服务端保存的路径为准。 */
async function viewState(page: import("@playwright/test").Page) {
  const response = await page.request.get(`${server.baseURL}/api/classification/view`);
  expect(response.ok(), `浏览状态读取失败：${response.status()}`).toBe(true);
  return response.json();
}

test("列表点选形成访问步骤，退回后打开新候选替换前进分支", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750\n000006");
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(queue.getByRole("option")).toHaveCount(4);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 每次点选后从服务端读回真实路径：断言不依赖行内容与候选标识的对应关系
  const first = (await viewState(page)).currentCandidateId as string;
  await queue.getByRole("option").nth(1).click();
  await expect(queue.getByRole("option").nth(1)).toHaveAttribute("aria-selected", "true");
  let state = await viewState(page);
  const second = state.currentCandidateId as string;
  expect(second).not.toBe(first);
  expect(state.path).toEqual([first, second]);
  expect(state.cursor).toBe(1);

  await queue.getByRole("option").nth(2).click();
  await expect(queue.getByRole("option").nth(2)).toHaveAttribute("aria-selected", "true");
  state = await viewState(page);
  const third = state.currentCandidateId as string;
  expect(third).not.toBe(second);
  expect(state.path).toEqual([first, second, third]);
  expect(state.cursor).toBe(2);

  // 退回第二只
  await page.getByRole("button", { name: "上一个" }).click();
  await expect
    .poll(async () => (await viewState(page)).currentCandidateId)
    .toBe(second);

  // 中间跳转：打开第四只，历史变为 A→B→D，C 被替换
  await queue.getByRole("option").nth(3).click();
  await expect(queue.getByRole("option").nth(3)).toHaveAttribute("aria-selected", "true");
  state = await viewState(page);
  const fourth = state.currentCandidateId as string;
  expect(fourth).not.toBe(third);
  expect(state.path).toEqual([first, second, fourth]);
  expect(state.cursor).toBe(2);
  expect(state.path).not.toContain(third);

  // 上一个回到本次跳转前的位置（第二只），下一个再回到跳转目标
  await page.getByRole("button", { name: "上一个" }).click();
  await expect
    .poll(async () => (await viewState(page)).currentCandidateId)
    .toBe(second);
  await page.getByRole("button", { name: "下一个" }).click();
  await expect
    .poll(async () => (await viewState(page)).currentCandidateId)
    .toBe(fourth);
  await context.close();
});

test("再次访问同一候选形成新步骤：上一个回到本次跳转前的位置", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });

  // 先记下队列真实顺序，逐步点选形成 A→B→C
  const rowIds = await Promise.all(
    [0, 1, 2].map(async (index) => queue.getByRole("option").nth(index).innerText()),
  );
  await page.getByRole("button", { name: "下一个" }).click();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect.poll(async () => (await viewState(page)).path.length).toBe(3);
  let state = await viewState(page);
  const visitedA = state.path[0];

  // 退回上一只（B），再重新打开 A：A 成为新的一步
  await page.getByRole("button", { name: "上一个" }).click();
  await expect.poll(async () => (await viewState(page)).cursor).toBe(1);
  const beforeJump = (await viewState(page)).currentCandidateId;
  expect(beforeJump).not.toBe(visitedA);
  await queue.getByRole("option").nth(0).click();
  await expect.poll(async () => (await viewState(page)).cursor).toBe(2);
  state = await viewState(page);
  expect(state.path).toHaveLength(3);
  expect(state.path[2]).toBe(state.currentCandidateId);
  expect(state.currentCandidateId).toBe(visitedA);
  expect(rowIds[0]).toContain("平安银行");

  // 上一个回到本次跳转前的位置，而不是回到这一只更早的那次访问
  await page.getByRole("button", { name: "上一个" }).click();
  await expect
    .poll(async () => (await viewState(page)).currentCandidateId)
    .toBe(beforeJump);
  await context.close();
});

test("搜索打开候选不改队列顺序与筛选，筛选外候选有明确提示", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(queue.getByRole("option").nth(0)).toContainText("平安银行");
  const order = await Promise.all(
    [0, 1, 2].map(async (index) => (await queue.getByRole("option").nth(index).innerText())),
  );

  // 从全局搜索打开第三只：不改变筛选，也不重排未处理池
  const first = await viewState(page);
  await page.getByLabel("搜索已导入股票").fill("宁德");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: "宁德时代" })
    .click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  const afterJump = await Promise.all(
    [0, 1, 2].map(async (index) => (await queue.getByRole("option").nth(index).innerText())),
  );
  expect(afterJump).toEqual(order);

  // 主动改队列查找（只留「平安」）：当前卡仍在，且属于新筛选结果
  await page.getByLabel("在队列中按股票代码或名称查找").fill("平安");
  await page.getByLabel("在队列中按股票代码或名称查找").press("Enter");
  await expect(queue.getByRole("option")).toHaveCount(1);
  const narrowed = await viewState(page);
  expect(narrowed.currentCandidateId).toBe(first.currentCandidateId);
  expect(narrowed.inFilter).toBe(true);

  // 筛选之外手动打开一只（全局搜索）：筛选不变，卡片明确提示它不在当前结果里
  await page.getByLabel("搜索已导入股票").fill("宁德");
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: "宁德时代" })
    .click();
  await expect(page.getByTestId("outside-filter-hint")).toBeVisible();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  const outside = await viewState(page);
  expect(outside.search).toBe("平安");
  expect(outside.inFilter).toBe(false);
  await expect(queue.getByRole("option")).toHaveCount(1);

  // 上一个回到本次跳转前的位置，原筛选条件保留
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByLabel("在队列中按股票代码或名称查找")).toHaveValue("平安");
  await context.close();
});

test("已处理页签可点开候选，历史末端不自动遍历已处理项", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  // 切回待归类并处理掉第三只：待归类范围空，卡片是刚处理的这只，列表与卡片一致
  await page.getByRole("tab", { name: /待归类/ }).click();
  await expect(page.getByTestId("stock-card")).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();
  const pending = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(pending.getByRole("option")).toHaveCount(0);
  const emptied = await viewState(page);
  expect(emptied.currentCandidateId).toBeNull();
  // 待归类池里的三只没有被当成「当前范围的首项」重新打开
  expect(emptied.summary.unprocessed).toBe(0);

  // 切到已处理页签：点开其中一只查看，不因此启动逐项自动遍历
  await page.getByRole("tab", { name: /已处理/ }).click();
  const processed = page.getByRole("listbox", { name: "已归类股票列表" });
  await expect(processed.getByRole("option")).toHaveCount(3);
  await processed.getByRole("option").nth(0).click();
  const processedIds = await Promise.all([
    processed.getByRole("option").nth(0).innerText(),
    processed.getByRole("option").nth(1).innerText(),
  ]);
  await page.getByTestId("stock-card").waitFor();

  const opened = await viewState(page);
  // 当前卡按真实处理状态展示，但筛选范围仍是「已处理」：不自动遍历其他已处理项
  expect(opened.scope).toBe("processed");
  expect(opened.path[opened.cursor]).toBe(opened.currentCandidateId);
  expect(opened.currentCandidate.state).toBe("dismissed");
  expect(opened.inFilter).toBe(true);
  expect(processedIds.join(" ")).toContain("宁德时代");
  expect(processedIds.join(" ")).toContain("平安银行");
  // 只打开了被点的那一只，没有自动把另一只已处理项加入路径
  expect(opened.path.filter((step: string) => step !== "~end")).toHaveLength(1);

  // 历史末端按「下一个」：已处理范围里没有待归类候选，进入结束卡而不是逐项遍历
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();
  await expect(page.getByText("当前范围看完了")).toBeVisible();
  // 其他范围的真实剩余数量照常显示（这里全部处理完了）
  await expect(page.getByTestId("round-remaining")).toHaveText("0");
  await context.close();
});

test("结束卡作为历史步骤跨重启恢复，有前进历史时提供下一个", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();

  let state = await viewState(page);
  expect(state.ended).toBe(true);
  expect(state.path[state.path.length - 1]).toBe("~end");
  // 结束卡在末端且没有前进历史：不显示没有去处的「下一个」
  await expect(page.getByTestId("end-card").getByRole("button", { name: "下一个" })).toHaveCount(0);

  // 重启后结束卡与路径恢复
  await context.close();
  await server.stop();
  server = await startServer(8799, server.dataDir);
  const reopened = await browser.newContext();
  const page2 = await reopened.newPage();
  await page2.goto("/");
  await goModule(page2, "候选归类");
  await expect(page2.getByTestId("end-card")).toBeVisible();

  // 从结束卡手动打开一只（列表点选）：结束卡成为可回看的一步
  const queue = page2.getByRole("listbox", { name: "待归类股票列表" });
  await queue.getByRole("option").nth(0).click();
  await expect(page2.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 上一个回结束卡；此时结束卡拥有前进历史，「下一个」可用并回到那只
  await page2.getByRole("button", { name: "上一个" }).click();
  await expect(page2.getByTestId("end-card")).toBeVisible();
  const endCard = page2.getByTestId("end-card");
  await expect(endCard.getByRole("button", { name: "下一个" })).toBeVisible();
  await endCard.getByRole("button", { name: "下一个" }).click();
  await expect(page2.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await reopened.close();
});

test("返回队列重置本轮：结束卡清空并从当前范围重新落位", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await page.getByRole("button", { name: "下一个" }).click();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();
  await page.getByTestId("end-card").getByRole("button", { name: "上一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();

  const before = await viewState(page);
  expect(before.path.length).toBeGreaterThan(1);

  // 结束卡的「返回队列」结束并重置本轮
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();
  await page.getByTestId("end-card").getByRole("button", { name: "返回队列" }).click();

  await expect(page.getByTestId("end-card")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  const after = await viewState(page);
  // 本轮路径重置：只剩新工作的第一步
  expect(after.ended).toBe(false);
  expect(after.path).toHaveLength(1);
  expect(after.cursor).toBe(0);
  await context.close();
});

test("返回工作区时新导入出现，当前卡与历史保持不变", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  const before = await viewState(page);

  // 从导入页再提交一只：新候选只进入列表，不抢当前卡
  await submitText(page, "300750");
  await expect(page.getByRole("button", { name: "开始归类" })).toBeVisible();
  await goModule(page, "候选归类");
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  const after = await viewState(page);
  expect(after.currentCandidateId).toBe(before.currentCandidateId);
  expect(after.path).toEqual(before.path);
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(3);
  await expect(page.getByText("共 3 只待归类")).toBeVisible();
  await context.close();
});

test("手动打开候选取消待重试的自动推进，迟到命令不覆盖选择", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 归类保存成功，但这次自动推进的响应丢失：本页留下一条待重试的导航
  let lost = false;
  await page.route("**/api/classification/view/navigate", async (route) => {
    if (lost || route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    lost = true;
    // 服务端已提交这次移动，浏览器只收到失败
    await route.fetch();
    await route.fulfill({ status: 503, body: "response lost" });
  });
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByText("已保存，切换失败")).toBeVisible();
  await page.unrouteAll();

  // 用户手动打开另一只（全局搜索打开候选卡）：取消待重试的自动推进
  const manual = "贵州茅台";
  await page.getByLabel("搜索已导入股票").fill(manual);
  await page
    .getByRole("list", { name: "搜索结果列表" })
    .getByRole("button", { name: new RegExp(manual) })
    .click();
  await expect(page.getByRole("heading", { name: manual })).toBeVisible();
  const chosen = await viewState(page);
  expect(chosen.currentCandidate.security.name).toBe(manual);

  // 迟到的旧导航（保存时的游标与修订号）只能读回当前位置，不覆盖用户的选择
  const late = await page.request.post(`${server.baseURL}/api/classification/view/navigate`, {
    data: {
      direction: "next",
      expectedCursor: 0,
      expectedRevision: 0,
    },
  });
  expect(late.ok()).toBe(true);
  const after = await viewState(page);
  expect(after.currentCandidate.security.name).toBe(manual);
  expect(after.path).toEqual(chosen.path);

  // 保存后的自动推进没有重复写入归类结果
  const firstId = (
    await (await page.request.get(`${server.baseURL}/api/classification/candidates/000001.SZ`)).json()
  ) as { state: string; history: { action: string }[] };
  expect(firstId.state).toBe("dismissed");
  expect(firstId.history.filter((entry) => entry.action === "dismissed")).toHaveLength(1);
  await context.close();
});

/**
 * 版本追赶：外部入口（另一个页签、脚本或定时任务）在两次读取之间改过列表时，
 * 一次归类动作的增量补不齐变化。前端必须读回完整结果，而不是把本地版本追到写入后
 * ——否则后续导航会误判「版本一致」，那只新候选会被永久漏掉。
 */
test("外部入口新增候选后归类：不追写列表版本，重读并补齐新候选", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  const queue = page.getByRole("listbox", { name: "待归类股票列表" });
  await expect(queue.getByRole("option")).toHaveCount(2);
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 另一个入口直接写入：新增一只候选。本页没有刷新，手上列表与版本都已落后。
  const imported = await page.request.post(`${server.baseURL}/api/imports`, {
    multipart: { texts: "300750" },
  });
  expect(imported.ok(), `外部入口导入失败：${imported.status()}`).toBe(true);
  await expect(queue.getByRole("option")).toHaveCount(2);

  // 本页归类：响应里的「写入前版本」与本页版本不同，必须重读完整结果再自动推进
  const refreshed = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      new URL(response.url()).pathname === "/api/classification/view",
    { timeout: 5000 },
  );
  await page.getByRole("button", { name: "暂不关注" }).click();
  expect((await refreshed).ok()).toBe(true);
  // 成功后自动推进会卸载旧卡，不等待旧卡上短暂出现的结果说明。
  await expect(page.getByRole("heading", { name: "贵州茅台", exact: true })).toBeVisible();
  const saved = await (
    await page.request.get(`${server.baseURL}/api/classification/candidates/000001.SZ`)
  ).json();
  expect(saved.state).toBe("dismissed");
  expect(saved.history.filter((entry: { action: string }) => entry.action === "dismissed"))
    .toHaveLength(1);
  // 重读后：被归类的平安银行离开列表，外部新增的宁德时代出现在同一份结果里
  await expect(queue.getByRole("option")).toHaveCount(2);
  await expect(queue.getByText("宁德时代")).toBeVisible();
  await expect(queue.getByText("贵州茅台")).toBeVisible();

  // 后续导航按同一份结果推进：自动推进到茅台后，下一个必须能打开新导入的宁德时代。
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代", exact: true })).toBeVisible();
  const advanced = await viewState(page);
  expect(advanced.currentCandidate.security.name).toBe("宁德时代");
  await context.close();
});

/**
 * 版本追赶的失败分支：读回完整结果失败时，保存已经成功，
 * 卡片必须显示已保存的真实结果，并留下一条可重试的导航——重试先重读再推进，
 * 而不是带着旧修订号发出去被服务端原地返回。
 */
test("版本追赶读取失败后再重试：卡片显示已保存结果并能推进", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();

  // 外部入口写入新候选：本页列表版本落后，归类后必须重读完整结果
  const imported = await page.request.post(`${server.baseURL}/api/imports`, {
    multipart: { texts: "300750" },
  });
  expect(imported.ok(), `外部入口导入失败：${imported.status()}`).toBe(true);

  // 让这次「读回完整结果」失败一次：只拦 GET（PUT 是写入、POST 是导航）
  let failed = false;
  await page.route("**/api/classification/view**", async (route) => {
    if (failed || route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    failed = true;
    await route.fulfill({ status: 503, body: "read lost" });
  });

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByText("已保存，切换失败")).toBeVisible();
  // 卡片显示已保存的真实结果：已归类的这一只不再提供「暂不关注」
  await expect(
    page.getByTestId("stock-card").getByRole("button", { name: "暂不关注" }),
  ).toHaveCount(0);
  const held = await viewState(page);
  await page.unrouteAll();

  // 重试：先重读再推进（旧实现会带旧修订号，服务端原地返回、位置不变）
  await page.getByRole("button", { name: "重试切换" }).click();
  await expect
    .poll(async () => (await viewState(page)).currentCandidateId)
    .not.toBe(held.currentCandidateId);
  await expect(page.getByTestId("stock-card")).toBeVisible();
  const resumed = await viewState(page);
  expect(["贵州茅台", "宁德时代"]).toContain(resumed.currentCandidate.security.name);
  await context.close();
});

/**
 * 恢复落位：当前范围为空时只能停在空状态。拿待归类池兜底会在「已处理」筛选下
 * 悄悄改选一只未处理的候选，用户看到的页签与卡片就不再是同一份结果。
 */
test("已处理筛选为空时恢复工作区：停在空状态，不改选未处理候选", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");

  // 切到已处理页签：此刻还没有任何已处理项，范围为空
  await page.getByRole("tab", { name: /已处理/ }).click();
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("heading", { name: "平安银行" })).toHaveCount(0);
  const emptied = await viewState(page);
  expect(emptied.scope).toBe("processed");
  expect(emptied.currentCandidateId).toBeNull();

  // 重新进入工作区（首次恢复）：仍停在空状态，不拿待归类池里的候选顶上
  await page.reload();
  await goModule(page, "候选归类");
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await expect(page.getByRole("heading", { name: "平安银行" })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toHaveCount(0);
  const restored = await viewState(page);
  expect(restored.currentCandidateId).toBeNull();

  // 待归类范围不受影响：切回去仍然能看到两只
  await page.getByRole("tab", { name: /待归类/ }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await context.close();
});
