import { expect, test } from "@playwright/test";
import { spawnSync } from "node:child_process";
import { join, resolve } from "node:path";

import { classifyText, goModule, startServer, type ServerHandle } from "./helpers";

let server: ServerHandle;

test.beforeEach(async () => {
  server = await startServer(8799);
});

test.afterEach(async () => {
  await server?.stop();
});

test("处理后沿持久路径双向浏览，结束卡跨服务重启恢复", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");

  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();

  await page.getByRole("button", { name: "上一个" }).click();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute(
    "aria-selected",
    "true",
  );
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByTestId("end-card")).toBeVisible();
  await expect(page.getByTestId("round-remaining")).toHaveText("2");

  await context.close();
  await server.stop();
  server = await startServer(8799, server.dataDir);
  const reopened = await browser.newContext();
  const page2 = await reopened.newPage();
  await page2.goto("/");
  await goModule(page2, "候选归类");
  await expect(page2.getByTestId("end-card")).toBeVisible();
  await page2.getByRole("button", { name: "上一个" }).click();
  await expect(page2.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await reopened.close();
});

test("导航响应丢失后只重试导航，不重复保存归类或跨过股票", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");

  const initialView = await page.request.get(`${server.baseURL}/api/classification/view`);
  const firstId = (await initialView.json()).currentCandidateId as string;
  let intercepted = false;
  await page.route("**/api/classification/view/navigate", async (route) => {
    if (intercepted || route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    intercepted = true;
    // The real server commits the move, but the browser receives an error.
    await route.fetch();
    await route.fulfill({ status: 503, body: "response lost" });
  });

  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByText("已保存，切换失败")).toBeVisible();
  await page.getByRole("button", { name: "重试切换" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();

  const firstResponse = await page.request.get(
    `${server.baseURL}/api/classification/candidates/${firstId}`,
  );
  const first = await firstResponse.json();
  expect(first.state).toBe("dismissed");
  expect(first.history.filter((entry: { action: string }) => entry.action === "dismissed"))
    .toHaveLength(1);

  const currentResponse = await page.request.get(`${server.baseURL}/api/classification/view`);
  const current = await currentResponse.json();
  expect(current.path).toHaveLength(2);
  expect(current.cursor).toBe(1);
  expect(current.currentCandidate.security.name).toBe("宁德时代");
  await context.close();
});

test("保存成功但导航响应丢失时停在原卡，重试只恢复切换", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");

  const initial = await (await page.request.get(`${server.baseURL}/api/classification/view`)).json();
  // 服务端会提交这次移动，但浏览器拿不到响应：卡片必须显示已保存的真实结果，
  // 重试只重放同一游标，不重复归类也不跨过第二只。
  let lost = false;
  await page.route("**/api/classification/view/navigate", async (route) => {
    if (lost || route.request().method() !== "POST") {
      await route.continue();
      return;
    }
    lost = true;
    await route.fetch();
    await route.fulfill({ status: 503, body: "response lost" });
  });
  await page.getByRole("button", { name: "暂不关注" }).click();
  await expect(page.getByText("已保存，切换失败")).toBeVisible();
  await expect(page.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page.getByRole("tab", { name: /已处理/ })).toHaveAttribute("aria-selected", "true");
  await page.unrouteAll();

  await page.getByRole("button", { name: "重试切换" }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  const saved = await (
    await page.request.get(`${server.baseURL}/api/classification/candidates/${initial.currentCandidateId}`)
  ).json();
  expect(saved.state).toBe("dismissed");
  expect(saved.history.filter((entry: { action: string }) => entry.action === "dismissed"))
    .toHaveLength(1);
  await context.close();
});

test("重启后当前股票已不存在时跳过失效路径项", async ({ browser }) => {
  const context = await browser.newContext();
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519\n300750");
  await page.getByRole("button", { name: "下一个" }).click();
  await expect(page.getByRole("heading", { name: "宁德时代" })).toBeVisible();
  const before = await (await page.request.get(`${server.baseURL}/api/classification/view`)).json();
  const deletedId = before.currentCandidateId as string;
  await context.close();
  await server.stop();

  // 删除是运维/旧数据变化夹具；浏览和恢复仍走真实服务与页面。
  const python = process.platform === "win32"
    ? resolve("..", "backend", ".venv", "Scripts", "python.exe")
    : resolve("..", "backend", ".venv", "bin", "python");
  const db = join(server.dataDir, "runtime", "dailyscreen_lite.sqlite3");
  const removed = spawnSync(python, [
    "-c",
    "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute('delete from candidates where candidate_id = ?', (sys.argv[2],)); db.commit(); db.close()",
    db,
    deletedId,
  ]);
  expect(removed.status, String(removed.stderr)).toBe(0);

  server = await startServer(8799, server.dataDir);
  const reopened = await browser.newContext();
  const page2 = await reopened.newPage();
  await page2.goto("/");
  await goModule(page2, "候选归类");
  // 失效步骤被跳过，当前位置按模块规则落到范围内可用位置（队首），不卡在无效卡片
  await expect(page2.getByRole("heading", { name: "平安银行" })).toBeVisible();
  await expect(page2.getByRole("button", { name: "上一个" })).toBeDisabled();
  // 退出失效的那一步后，沿路径前进回到仍在的第二只
  await page2.getByRole("button", { name: "下一个" }).click();
  await expect(page2.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await reopened.close();
});

test("窄屏双向控件保持点击区域和键盘焦点", async ({ browser }) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  await page.goto("/");
  await classifyText(page, "000001\n600519");

  const previous = page.getByRole("button", { name: "上一个" });
  const next = page.getByRole("button", { name: "下一个" });
  await expect(previous).toBeDisabled();
  for (const button of [previous, next]) {
    const box = await button.boundingBox();
    expect(box).not.toBeNull();
    expect(box!.width).toBeGreaterThanOrEqual(44);
    expect(box!.height).toBeGreaterThanOrEqual(44);
  }
  await next.focus();
  await expect(next).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await expect(previous).toBeEnabled();
  await context.close();
});
