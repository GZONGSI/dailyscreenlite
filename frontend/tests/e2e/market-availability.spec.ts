import { expect, test, type Page } from "@playwright/test";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { goModule, startServer, submitText, startClassification, type ServerHandle } from "./helpers";

let server: ServerHandle;
test.afterEach(async () => { await server?.stop(); });

async function update(page: Page) {
  await expect.poll(async () => (await (await page.request.get("/api/updates")).json()).running).toBe(false);
  await goModule(page, "数据中心");
  const completed = page.waitForResponse(r => r.url().endsWith("/api/updates") && r.request().method() === "POST");
  await page.getByTestId("data-center").getByRole("button", { name: "更新数据", exact: true }).click();
  expect((await completed).ok()).toBe(true);
}

test("分市场保旧、北交所尽力获取与日历失败不阻止归类，重启结果一致", async ({ page }) => {
  const dir = mkdtempSync(join(tmpdir(), "dslite-markets-"));
  const quotesPath = join(dir, "quotes.json");
  const statusPath = join(dir, "status.json");
  const snapshot = JSON.parse(readFileSync("../data/securities/initial_snapshot.json", "utf8"));
  const sz = snapshot.securities.filter((s: { exchange: string }) => s.exchange === "SZ");
  sz.find((s: { code: string }) => s.code === "000001").name = "深市更新名称";
  const bar = (date: string, close: number) => ({ date, open: close, high: close, low: close, close, volume_lots: 1000, amount_yuan: null });
  const quotes = {
    security_markets: { SH: { error: "沪市名单超时" }, SZ: { securities: sz }, BJ: { securities: [] } },
    bars: { "000001.SZ": [bar("2026-09-22", 12)], "920001.BJ": [bar("2026-09-21", 10)], "600519.SH": [bar("2026-09-21", 1500)] },
    fail_bars: [] as string[],
  };
  writeFileSync(quotesPath, JSON.stringify(quotes));
  const month = JSON.parse(readFileSync("../backend/tests/fixtures/szse-calendar-2026-09.json", "utf8"));
  writeFileSync(statusPath, JSON.stringify({ calendar_months: { "2026-09": month }, covered_markets: ["SH", "SZ"], uncovered_markets: ["BJ"] }));
  const env = { DSLITE_QUOTES_FIXTURE: quotesPath, DSLITE_MARKET_STATUS_FIXTURE: statusPath, DSLITE_NOW: "2026-09-22T17:00:00+08:00" };
  server = await startServer(8799, undefined, env);
  await page.goto("/");
  await update(page);
  const center = page.getByTestId("data-center");
  await expect(center.getByText("深市名单", { exact: true })).toBeVisible();
  await expect(center.getByText(/沪市名单超时/).first()).toBeVisible();
  await submitText(page, "000001\n920001\n999999");
  await expect(page.getByTestId("stat-skipped")).toContainText("1");
  await expect(page.getByText(/999999/).first()).toBeVisible();
  await startClassification(page);
  await expect(page.getByRole("heading", { name: "深市更新名称", exact: true })).toBeVisible();
  await expect.poll(async () => (await (await page.request.get("/api/data/status")).json()).complete).toBe(true);
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  await page.getByRole("checkbox").first().check();
  await page.getByRole("button", { name: "确认并完成归类" }).click();
  await goModule(page, "观察组");
  await expect(page.getByTestId("observed-stocks").getByRole("cell", { name: "深市更新名称 000001", exact: true })).toBeVisible();
  // 日历/状态刷新与北交所日线失败仍保留旧值，数据中心如实显示。
  quotes.fail_bars = ["920001.BJ"];
  writeFileSync(quotesPath, JSON.stringify(quotes));
  writeFileSync(statusPath, JSON.stringify({ calendar_months: {}, error: "停牌来源离线" }));
  await update(page);
  const bj = center.locator("tbody tr", { hasText: "920001" });
  await expect(bj).toContainText("2026-09-21");
  await expect(bj).toContainText("日线获取失败");
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
  const before = await (await page.request.get("/api/data")).json();
  expect(before.targetTradeDate).toBe("2026-09-22");
  expect(before.lastRun.status).not.toBe("success");
  await server.stop();
  server = await startServer(8799, server.dataDir, env);
  await page.reload();
  await goModule(page, "数据中心");
  const after = await (await page.request.get("/api/data")).json();
  expect(after.items).toEqual(before.items);
  expect(after.stocks).toEqual(before.stocks);
  await expect(bj).toContainText("2026-09-21");
  // 旧沪市身份仍可导入；沪市缺行情继续红，补齐才解除。
  await submitText(page, "600519");
  await startClassification(page);
  await expect(page.getByTestId("data-reminder")).toContainText("09-22");
  // 完整重抓夹具须包含已保存的旧日期，否则保旧规则会拒绝替换。
  quotes.bars["600519.SH"] = [bar("2026-09-21", 1500), bar("2026-09-22", 1501)];
  writeFileSync(quotesPath, JSON.stringify(quotes));
  await update(page);
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
  expect((await (await page.request.get("/api/quotes/600519.SH")).json()).latestDate).toBe("2026-09-22");
});
