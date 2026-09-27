import { expect, test } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { classifyText, goModule, startServer, submitText, type ServerHandle } from "./helpers";

/**
 * 工单 04 的浏览器验收：更新轮询与刷新传播。
 *
 * 三处轮询各自一个资源（更新状态 / 顶部提醒 / 数据中心）与各自的频率；
 * 应用级终态观察者只在出现**新的完整终态**时通知一次刷新；
 * 页面隐藏时的政策、读取失败时的展示差异与 409 后的恢复按迁移前保留；
 * 刷新接线（新终态、手动更新、手动重试、导入落地）逐条钉住。
 * 用例多是协议层夹具（拦截对应端点），不改后端也不需要行情来源。
 */

const REMINDER = "截至最近交易日（09-18），数据尚未更新完整！";
const READ_FAILURE = { detail: { message: "注入的读取失败" } };

let server: ServerHandle;
test.beforeEach(async () => {
  server = await startServer(8799, mkdtempSync(join(tmpdir(), "dslite-poll-")));
});
test.afterEach(async () => {
  await server?.stop();
});

function runRecord(status: "running" | "success") {
  return {
    runId: "r1",
    kind: "manual",
    status,
    startedAt: "2026-09-18T16:30:00+08:00",
    finishedAt: status === "running" ? null : "2026-09-18T16:30:20+08:00",
    securitiesStatus: null,
    securitiesMessage: null,
    securitiesCount: 0,
    quotesOk: 1,
    quotesFailed: 0,
    quotesSkipped: 0,
    quotesPending: 0,
    failedSecurities: [],
  };
}

/** 轻量数据状态（顶部提醒的端点）：只有完整性结论，没有逐股明细。 */
function statusBody({
  updating,
  reminder,
}: {
  updating: boolean;
  reminder: string | null;
}) {
  return {
    reminder,
    complete: reminder === null,
    updating,
    targetTradeDate: "2026-09-18",
    targetSource: "akshare",
    calendarAvailable: true,
    incompleteCount: reminder === null ? 0 : 1,
    items: [],
  };
}

/** 数据中心（逐股明细的端点）：默认一只待补齐的股票，进度为空即闲置不轮询。 */
function centerBody({
  retryIds = [],
  progress = null,
}: {
  retryIds?: string[];
  progress?: { total: number; done: number; attempt: number } | null;
} = {}) {
  return {
    reminder: null,
    complete: false,
    targetTradeDate: "2026-09-18",
    targetSource: "akshare",
    calendarAvailable: true,
    incompleteCount: retryIds.length,
    items: [],
    scopeCount: 1,
    scopeLabel: "待归类股票与观察组股票",
    securitiesStatus: null,
    securitiesMessage: null,
    securitiesCount: 0,
    stocks: [
      {
        securityId: "000001.SZ",
        name: "平安银行",
        state: "pending",
        marketStatus: "normal",
        latestDate: null,
        reason: null,
        lastError: null,
        source: null,
      },
    ],
    retryIds,
    progress,
    quoteDiagnostics: [],
    lastRun: null,
    history: [],
    automaticRounds: [],
  };
}

test("两次轮询之间跑完的短轮次只通知一次刷新", async ({ page }) => {
  await page.clock.install();
  let statusReads = 0;
  await page.route("**/api/updates", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    statusReads += 1;
    const running = statusReads === 1;
    // 第三次读回的是同一个终态，但内容仍有别的字段变化：结构化共享因此不会跳过
    // 观察者，「只通知一次」必须由终态标识判断，而不是靠对象没变
    const later = statusReads >= 3;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        running,
        lastRun: runRecord(running ? "running" : "success"),
        progress: running ? { total: 1, done: 0, attempt: 1 } : null,
        automaticRounds: later
          ? [
              {
                run_date: "2026-09-18",
                slot: 1,
                run_id: "r1",
                started_at: "2026-09-18T16:30:00+08:00",
              },
            ]
          : [],
        history: later ? [runRecord("success")] : [],
      }),
    });
  });
  // 未迁移的工作表读取仍由刷新令牌桥接：以它的读取次数看「通知了几次」
  let browseReads = 0;
  await page.route("**/api/classification/**", async (route) => {
    if (route.request().method() === "GET") browseReads += 1;
    await route.continue();
  });
  await page.goto("/");
  await classifyText(page, "000001");
  await expect(page.getByTestId("stock-card")).toBeVisible();
  const before = browseReads;

  // 运行中按 2 秒轮询：这一轮读到的是两次轮询之间跑完的短轮次 → 通知一次刷新
  await page.clock.runFor(2_001);
  await expect.poll(() => browseReads).toBeGreaterThan(before);
  await page.waitForTimeout(300);
  const afterTerminal = browseReads;

  // 闲置按 60 秒轮询：再次读到同一个终态，不能重复通知
  await page.clock.runFor(61_000);
  await expect.poll(() => statusReads).toBeGreaterThanOrEqual(3);
  await page.waitForTimeout(300);
  expect(browseReads).toBe(afterTerminal);
});

test("更新已在运行时给出提示、重读状态并可再次触发", async ({ page }) => {
  let posts = 0;
  let statusReads = 0;
  await page.route("**/api/updates", async (route) => {
    if (route.request().method() !== "POST") {
      statusReads += 1;
      await route.continue();
      return;
    }
    posts += 1;
    if (posts === 1) {
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({ detail: { message: "更新正在进行中" } }),
      });
      return;
    }
    await route.continue();
  });
  await page.goto("/");
  await expect.poll(() => statusReads).toBeGreaterThan(0);
  const before = statusReads;

  await page.getByRole("button", { name: "更新数据" }).click();
  await expect(page.getByTestId("launch-update-error")).toContainText(
    "更新正在进行中，请稍候",
  );
  // 恢复：重读一次状态回到真实进度，入口可再次触发并拿到真实结果
  await expect.poll(() => statusReads).toBeGreaterThan(before);
  await page.getByRole("button", { name: "更新数据" }).click();
  await expect(page.getByTestId("launch-update-result")).toBeVisible({
    timeout: 30_000,
  });
  expect(posts).toBe(2);
});

test("页面隐藏时仍按原间隔重判，不因框架默认值暂停", async ({ page }) => {
  await page.clock.install();
  let reads = 0;
  await page.route("**/api/data/status", async (route) => {
    reads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(statusBody({ updating: false, reminder: REMINDER })),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "导入候选" }).click();
  // 等首次读取真的落地（提醒渲染出来）再推进时钟：否则这一轮的定时读取会与
  // 还在途的首次请求合并，看起来像「没有轮询」
  await expect(page.getByTestId("data-reminder")).toBeVisible();

  // 页面被隐藏：迁移前用 setInterval，照常到点重判（只是被浏览器节流）
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      configurable: true,
      get: () => "hidden",
    });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  const before = reads;
  await page.clock.runFor(61_000);
  await expect.poll(() => reads).toBeGreaterThan(before);
});

test("顶部状态读取失败时不显示可能过期的提醒", async ({ page }) => {
  let reads = 0;
  await page.route("**/api/data/status", async (route) => {
    reads += 1;
    if (reads === 1) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(statusBody({ updating: true, reminder: REMINDER })),
      });
      return;
    }
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify(READ_FAILURE),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "导入候选" }).click();
  await expect(page.getByTestId("data-reminder")).toBeVisible();

  // 之后读取失败：顶部当前隐藏，不拿过期提醒充数
  await expect(page.getByTestId("data-reminder")).toHaveCount(0, {
    timeout: 15_000,
  });
});

test("数据中心轮询失败保留已读回的资料", async ({ page }) => {
  let reads = 0;
  let failures = 0;
  await page.route("**/api/data", async (route) => {
    reads += 1;
    if (reads === 1) {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          centerBody({ retryIds: ["000001.SZ"], progress: { total: 2, done: 1, attempt: 1 } }),
        ),
      });
      return;
    }
    failures += 1;
    await route.fulfill({
      status: 500,
      contentType: "application/json",
      body: JSON.stringify(READ_FAILURE),
    });
  });
  await page.goto("/");
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  const row = center.locator("tbody tr", { hasText: "平安银行" });
  await expect(row).toBeVisible();

  // 更新进行中按 1.5 秒轮询：失败确实发生了，但已读回的资料仍在，也不打扰用户
  await expect.poll(() => failures).toBeGreaterThan(0);
  await expect(row).toBeVisible();
  await expect(center.getByRole("alert")).toHaveCount(0);
});

test("重新进入数据中心时读取失败给出错误，已读回的资料仍在", async ({ page }) => {
  let fail = false;
  await page.route("**/api/data", async (route) => {
    if (fail) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify(READ_FAILURE),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(centerBody({ retryIds: ["000001.SZ"] })),
    });
  });
  await page.goto("/");
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  const row = center.locator("tbody tr", { hasText: "平安银行" });
  await expect(row).toBeVisible();

  // 离开再进入：这次读取失败没有后续自动重读（闲置不轮询），必须说出来
  await goModule(page, "导入");
  fail = true;
  await goModule(page, "数据中心");
  await expect(center.getByRole("alert")).toContainText("注入的读取失败");
  await expect(row).toBeVisible();
});

test("重试未完成成功后顶部提醒立即重判", async ({ page }) => {
  let statusReads = 0;
  // 重试前一直是未补齐，重试后才是完整：进入数据中心的刷新也会读一次，
  // 因此以「重试是否已经发生」而不是读取次数决定返回什么
  let retried = false;
  await page.route("**/api/data/status", async (route) => {
    statusReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        statusBody({ updating: false, reminder: retried ? null : REMINDER }),
      ),
    });
  });
  await page.route("**/api/data", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(centerBody({ retryIds: ["000001.SZ"] })),
    });
  });
  await page.goto("/");
  await goModule(page, "数据中心");
  await expect(page.getByTestId("data-reminder")).toBeVisible();
  const before = statusReads;

  retried = true;
  await page
    .getByTestId("data-center")
    .getByRole("button", { name: /重试未完成/ })
    .click();
  // 重试改的是数据事实：提醒按新的完整性重判，不等下一次 60 秒轮询
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
  expect(statusReads).toBeGreaterThan(before);
});

test("缓存里的旧进度不掩盖重新进入时的读取失败", async ({ page }) => {
  let failing = false;
  await page.route("**/api/data", async (route) => {
    if (failing) {
      await route.fulfill({
        status: 500,
        contentType: "application/json",
        body: JSON.stringify(READ_FAILURE),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        centerBody({ retryIds: ["000001.SZ"], progress: { total: 2, done: 1, attempt: 1 } }),
      ),
    });
  });
  await page.goto("/");
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  const row = center.locator("tbody tr", { hasText: "平安银行" });
  // 第一次进入读到「更新进行中」的资料：缓存里因此留下 progress
  await expect(row).toBeVisible();
  await expect(center.getByRole("button", { name: "更新数据" })).toBeDisabled();

  // 离开后更新已经结束（控制器也不在跑），再进入时读取失败：
  // 旧进度不能继续算「在跑」，失败要说出来，更新入口也不能一直被禁用
  await goModule(page, "导入");
  failing = true;
  await goModule(page, "数据中心");
  await expect(center.getByRole("alert")).toContainText("注入的读取失败");
  await expect(center.getByRole("button", { name: "更新数据" })).toBeEnabled();
  await expect(row).toBeVisible();
});

test("冷缓存下手动更新只通知一次，之后读到同一终态不再通知", async ({ page }) => {
  await page.clock.install();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let statusReads = 0;
  let holding = true;
  await page.route("**/api/updates", async (route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    statusReads += 1;
    if (holding) {
      holding = false;
      const response = await route.fetch(); // 写入之前的快照
      await gate;
      // 写后重读前会取消在途读取：请求已取消时回填不落地
      void route.fulfill({ response }).catch(() => {});
      return;
    }
    await route.continue();
  });
  let centerReads = 0;
  await page.route("**/api/data", async (route) => {
    centerReads += 1;
    await route.continue();
  });
  await page.goto("/");
  await goModule(page, "数据中心");
  const center = page.getByTestId("data-center");
  await expect(center).toBeVisible();

  // 首次状态读取还在途（冷缓存），此时手动更新：写后确认必须发起新的读取，
  // 不能沿用写前那次在途请求
  await center.getByRole("button", { name: "更新数据" }).click();
  await expect.poll(() => statusReads).toBeGreaterThan(1);
  release();
  await expect(center.getByText(/最近一次：/)).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(300);
  const afterUpdate = centerReads;

  // 之后按闲置频率再读到同一个终态：不能再通知一次
  await page.clock.runFor(61_000);
  await expect.poll(() => statusReads).toBeGreaterThan(2);
  await page.waitForTimeout(300);
  expect(centerReads).toBe(afterUpdate);
});

test("导入落地后顶部提醒立即按新完整性重判", async ({ page }) => {
  let statusReads = 0;
  await page.route("**/api/data/status", async (route) => {
    statusReads += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(
        statusBody({
          updating: false,
          reminder: statusReads === 1 ? null : REMINDER,
        }),
      ),
    });
  });
  await page.goto("/");
  await page.getByRole("button", { name: "导入候选" }).click();
  await expect.poll(() => statusReads).toBeGreaterThan(0);
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);

  await submitText(page, "000001");
  // 导入会触发后台补取：提醒按新的完整性重判，不等下一次 60 秒轮询
  await expect(page.getByTestId("data-reminder")).toBeVisible();
});
