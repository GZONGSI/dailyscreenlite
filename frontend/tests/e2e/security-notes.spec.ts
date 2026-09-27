import { expect, test, type Page } from "@playwright/test";
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
async function enter(page: Page, text = "000001") {
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill(text);
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText(/已提交 \d+ 个来源/)).toBeVisible();
  await startClassification(page);
}
async function editor(page: Page) {
  await page.getByRole("button", { name: "新增笔记", exact: true }).click();
  return page.getByRole("dialog", { name: /新增笔记/ });
}
async function create(page: Page, text: string) {
  const form = await editor(page);
  await form.getByLabel("新增笔记", { exact: true }).fill(text);
  await form.getByRole("button", { name: "保存笔记" }).click();
  await expect(form).toHaveCount(0);
}
async function sheet(page: Page) {
  await page.getByRole("button", { name: "查看全部笔记", exact: true }).click();
  return page.getByRole("dialog", { name: /全部笔记/ });
}
/**
 * 扣住笔记读取不放：`firstOnly` 为真时只扣第一次（模拟迟到的旧读取），否则每次读取都扣。
 *
 * 返回放行函数与已扣住的读取次数；计数在响应取回之后才增加，因此测试看到计数就能确定
 * 夹具拿到的是这次操作之前的快照。写前取消会中止请求，回填失败只说明它已被取消。
 */
async function holdNoteReads(page: Page, firstOnly = false) {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let held = 0;
  await page.route("**/api/notes/securities/**", async (route) => {
    if (route.request().method() === "GET" && (!firstOnly || held === 0)) {
      const response = await route.fetch();
      held++;
      await gate;
      // 写前取消会中止这次读取：已取消请求的回填可能一直不返回，等它会把用例收尾挂住，
      // 所以只发起回填、不等待；读到的内容由断言轮询等待。
      void route.fulfill({ response }).catch(() => {});
      return;
    }
    await route.continue();
  });
  return { release: () => release(), held: () => held };
}
test("弹窗新增、抽屉新增编辑删除、摘要同步，重启后读回", async ({
  browser,
}) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await enter(page);
  await create(page, "关注不良率变化");
  await expect(page.getByTestId("note-summary")).toContainText("1 条");
  const panel = await sheet(page);
  await expect(panel.getByText("关注不良率变化")).toBeVisible();
  await panel.getByRole("button", { name: "新增笔记", exact: true }).click();
  await panel.getByLabel("新增笔记", { exact: true }).fill("第二天再核对一次");
  await panel.getByRole("button", { name: "保存笔记" }).click();
  await expect(panel.getByText("笔记 2", { exact: false })).toBeVisible();
  await panel
    .locator("li")
    .filter({ hasText: "关注不良率变化" })
    .getByRole("button", { name: "编辑这条笔记" })
    .click();
  await panel.getByLabel("编辑笔记").fill("关注不良率与拨备覆盖率");
  await panel.getByRole("button", { name: "保存修改" }).click();
  await expect(
    panel.getByRole("button", { name: "新增笔记", exact: true }),
  ).toBeFocused();
  await expect(
    panel.getByText("关注不良率与拨备覆盖率", { exact: true }),
  ).toBeVisible();
  await panel
    .locator("li")
    .filter({ hasText: "第二天再核对一次" })
    .getByRole("button", { name: "删除这条笔记" })
    .click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "确认删除这条笔记" })
    .click();
  await expect(
    panel.getByText("第二天再核对一次", { exact: true }),
  ).toHaveCount(0);
  await panel.getByRole("button", { name: "关闭面板" }).click();
  await expect(page.getByTestId("note-summary")).toContainText(
    "关注不良率与拨备覆盖率",
  );
  await expect(page.getByTestId("note-summary")).toContainText("1 条");
  await expect(page.getByRole("button", { name: "稍后处理" })).toBeVisible();
  await server.stop();
  server = await startServer(8799, server.dataDir);
  await page.reload();
  await goModule(page, "候选归类");
  await expect(page.getByTestId("note-summary")).toContainText(
    "关注不良率与拨备覆盖率",
  );
  const again = await sheet(page);
  await expect(again.getByRole("listitem")).toHaveCount(1);
  await context.close();
});
test("连续写笔记不完成研究也不切股", async ({ page }) => {
  await enter(page, "000001\n600519");
  await create(page, "第一条");
  await create(page, "第二条");
  await expect(page.getByTestId("note-summary")).toContainText("2 条");
  await expect(
    page.getByRole("heading", { name: "平安银行", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("listbox", { name: "待归类股票列表" }).getByRole("option"),
  ).toHaveCount(2);
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(
    page.getByText("已暂不关注，已结束本次归类；导入事实保留。"),
  ).toBeVisible();
});
test("中文组合输入不误提交，Enter换行、Ctrl/Cmd Enter保存", async ({
  page,
}) => {
  await enter(page);
  const form = await editor(page);
  const input = form.getByLabel("新增笔记", { exact: true });
  await input.fill("第一行");
  await input.press("Enter");
  await expect(input).toHaveValue("第一行\n");
  await input.dispatchEvent("compositionstart");
  await input.press("Control+Enter");
  await expect(form).toBeVisible();
  await input.dispatchEvent("compositionend");
  await input.press("Meta+Enter");
  await expect(form).toHaveCount(0);
  await expect(page.getByTestId("note-summary")).toContainText("第一行");
});
test("保存失败保留输入并允许原文重试", async ({ page }) => {
  await enter(page);
  await page.route("**/api/notes/securities/**", async (route) => {
    if (route.request().method() === "POST")
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ message: "注入的保存失败" }),
      });
    await route.continue();
  });
  const form = await editor(page);
  await form.getByLabel("新增笔记", { exact: true }).fill("失败也不能丢的内容");
  await form.getByRole("button", { name: "保存笔记" }).click();
  await expect(form.getByRole("alert")).toContainText("保存失败");
  await expect(form.getByLabel("新增笔记", { exact: true })).toHaveValue(
    "失败也不能丢的内容",
  );
  await expect(form.getByLabel("新增笔记", { exact: true })).toHaveAttribute(
    "aria-invalid",
    "true",
  );
  await page.unrouteAll();
  await form.getByRole("button", { name: "保存笔记" }).click();
  await expect(form).toHaveCount(0);
  await expect(page.getByTestId("note-summary")).toContainText(
    "失败也不能丢的内容",
  );
});
test("抽屉往返保持同一股票且切股清除旧笔记", async ({ page }) => {
  await enter(page, "000001\n600519");
  await create(page, "仅平安银行的判断");
  await page.getByRole("button", { name: "下一个" }).click();
  const panel = await sheet(page);
  await expect(panel.getByRole("heading")).toContainText("贵州茅台 600519");
  await expect(panel.getByText("暂无笔记", { exact: true })).toBeVisible();
  await expect(panel.getByText("仅平安银行的判断")).toHaveCount(0);
  await panel.getByRole("button", { name: "关闭面板" }).click();
  await expect(
    page.getByRole("heading", { name: "贵州茅台", exact: true }),
  ).toBeVisible();
});
test("保存期间锁正文和关闭，重复快捷键只创建一条", async ({ page }) => {
  await enter(page);
  let release!: () => void;
  const gate = new Promise<void>((r) => {
    release = r;
  });
  let posts = 0;
  await page.route("**/api/notes/securities/**", async (route) => {
    if (route.request().method() === "POST") {
      posts++;
      await gate;
    }
    await route.continue();
  });
  const form = await editor(page);
  const input = form.getByLabel("新增笔记", { exact: true });
  await input.fill("第一条内容");
  await input.press("Control+Enter");
  await expect.poll(() => posts).toBe(1);
  await expect(input).toBeDisabled();
  await page.keyboard.press("Control+Enter");
  await page.keyboard.press("Escape");
  await expect(form).toBeVisible();
  await expect(form.getByRole("button", { name: "关闭笔记" })).toBeDisabled();
  await page.mouse.click(5, 5);
  await expect(form).toBeVisible();
  release();
  await expect(form).toHaveCount(0);
  expect(posts).toBe(1);
  await expect(page.getByTestId("note-summary")).toContainText("1 条");
});
test("首次未取得完整列表时保存不伪装成完整列表", async ({ page }) => {
  const reads = await holdNoteReads(page);
  await enter(page);
  await expect.poll(reads.held).toBeGreaterThan(0);
  // 列表还没读到：保存成功也只有一个「这一条」的响应，不能当成完整列表展示
  await create(page, "未取得完整列表前保存的笔记");
  await expect(page.getByTestId("note-summary")).toContainText("正在读取笔记…");
  await expect(page.getByTestId("note-summary")).not.toContainText(
    "未取得完整列表前保存的笔记",
  );
  // 放开读取：保存的内容由重新读取的真实列表读回
  reads.release();
  await expect(page.getByTestId("note-summary")).toContainText(
    "未取得完整列表前保存的笔记",
  );
  await expect(page.getByTestId("note-summary")).toContainText("1 条");
  await page.unrouteAll({ behavior: "wait" });
});
test("首次读取失败后保存不伪造列表，重试读回保存的内容", async ({ page }) => {
  let fail = true;
  await page.route("**/api/notes/securities/**", async (route) => {
    if (route.request().method() === "GET" && fail)
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ message: "注入的读取失败" }),
      });
    await route.continue();
  });
  await enter(page);
  await expect(page.getByTestId("note-summary")).toContainText("笔记读取失败");
  await create(page, "读取失败期间保存的笔记");
  // 保存成功但列表仍没读到：只能显示读取失败，不能拿单条响应凑一个「完整列表」
  await expect(page.getByTestId("note-summary")).toContainText("笔记读取失败");
  const panel = await sheet(page);
  await expect(panel.getByRole("alert")).toContainText("笔记读取失败");
  await expect(panel.getByText("读取失败期间保存的笔记")).toHaveCount(0);
  await panel.getByRole("button", { name: "关闭面板" }).click();
  // 手动重试读取：保存的内容由真实列表读回
  fail = false;
  await page.getByRole("button", { name: "重试笔记" }).click();
  await expect(page.getByTestId("note-summary")).toContainText(
    "读取失败期间保存的笔记",
  );
  await expect(page.getByTestId("note-summary")).toContainText("1 条");
});
test("保存失败后恢复被取消的初次读取，列表不卡在读取中", async ({ page }) => {
  const reads = await holdNoteReads(page, true);
  await page.route("**/api/notes/securities/**", async (route) => {
    if (route.request().method() === "POST")
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ message: "注入的保存失败" }),
      });
    await route.fallback();
  });
  await enter(page);
  await expect.poll(reads.held).toBeGreaterThan(0);
  const form = await editor(page);
  await form.getByLabel("新增笔记", { exact: true }).fill("保存失败后的草稿");
  await form.getByRole("button", { name: "保存笔记" }).click();
  await expect(form.getByRole("alert")).toContainText("保存失败");
  await expect(form.getByLabel("新增笔记", { exact: true })).toHaveValue(
    "保存失败后的草稿",
  );
  // 写前取消的那次读取要补回来：摘要不能停在读取中，而是读到真实的空列表
  await expect(page.getByTestId("note-summary")).toContainText("暂无笔记");
  await expect(page.getByTestId("note-summary")).not.toContainText(
    "正在读取笔记…",
  );
  // 抽屉同样不能停在读取中
  await form.getByRole("button", { name: "取消编辑" }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "放弃草稿" })
    .click();
  const panel = await sheet(page);
  await expect(panel.getByText("暂无笔记", { exact: true })).toBeVisible();
  await expect(panel.getByText("正在读取笔记…")).toHaveCount(0);
  reads.release();
  await page.unrouteAll({ behavior: "wait" });
});
test("首次读取迟到不覆盖期间新增的笔记", async ({ page }) => {
  const reads = await holdNoteReads(page, true);
  await enter(page);
  await expect.poll(reads.held).toBeGreaterThan(0);
  await create(page, "在途新增的笔记");
  reads.release();
  await expect(page.getByTestId("note-summary")).toContainText(
    "在途新增的笔记",
  );
  await expect(page.getByTestId("note-summary")).toContainText("1 条");
  await page.unrouteAll({ behavior: "wait" });
});
test("抽屉脏输入Esc只关闭最上层，继续编辑恢复焦点，放弃不删保存内容", async ({
  page,
}) => {
  await enter(page);
  await create(page, "已保存内容");
  const panel = await sheet(page);
  await panel.getByRole("button", { name: "编辑这条笔记" }).click();
  const input = panel.getByLabel("编辑笔记");
  await input.fill("未保存修改");
  await page.keyboard.press("Escape");
  const confirm = page.getByRole("alertdialog");
  await expect(confirm).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(confirm).toHaveCount(0);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("未保存修改");
  await panel.getByRole("button", { name: "取消编辑" }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "放弃草稿" })
    .click();
  await expect(
    panel.getByRole("button", { name: "新增笔记", exact: true }),
  ).toBeFocused();
  await panel.getByRole("button", { name: "编辑这条笔记" }).click();
  await panel.getByRole("button", { name: "取消编辑" }).click();
  await expect(
    panel.getByRole("button", { name: "新增笔记", exact: true }),
  ).toBeFocused();
  await panel.getByRole("button", { name: "编辑这条笔记" }).click();
  await input.fill("未保存修改");

  await panel.getByRole("button", { name: "关闭面板" }).click();
  await page
    .getByRole("alertdialog")
    .getByRole("button", { name: "放弃草稿" })
    .click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByTestId("note-summary")).toContainText("已保存内容");
  await expect(
    page.getByRole("button", { name: "查看全部笔记" }),
  ).toBeFocused();
});
test("编辑保存期间不能切换另一个编辑对象，删除失败保留笔记", async ({
  page,
}) => {
  await enter(page);
  await create(page, "甲笔记");
  await create(page, "乙笔记");
  const panel = await sheet(page);
  let release!: () => void;
  const gate = new Promise<void>((r) => {
    release = r;
  });
  let pending = false;
  await page.route("**/api/notes/**", async (route) => {
    if (route.request().method() === "PATCH") {
      pending = true;
      await gate;
    }
    if (route.request().method() === "DELETE")
      return route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify({ message: "删除失败" }),
      });
    await route.continue();
  });
  await panel
    .locator("li")
    .filter({ hasText: "甲笔记" })
    .getByRole("button", { name: "编辑这条笔记" })
    .click();
  await panel.getByLabel("编辑笔记").fill("甲笔记改");
  await panel.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => pending).toBe(true);
  await expect(panel.getByLabel("编辑笔记")).toBeDisabled();
  await expect(
    panel
      .locator("li")
      .filter({ hasText: "乙笔记" })
      .getByRole("button", { name: "编辑这条笔记" }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(panel).toBeVisible();
  release();
  await expect(panel.getByText("甲笔记改", { exact: true })).toBeVisible();
  await panel
    .locator("li")
    .filter({ hasText: "乙笔记" })
    .getByRole("button", { name: "删除这条笔记" })
    .click();
  const confirm = page.getByRole("alertdialog");
  await confirm.getByRole("button", { name: "确认删除这条笔记" }).click();
  await expect(confirm.getByRole("alert")).toContainText("删除失败");
  await confirm.getByRole("button", { name: "取消", exact: true }).click();
  await expect(panel.getByText("乙笔记", { exact: true })).toBeVisible();
});
