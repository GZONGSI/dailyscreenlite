import { expect, test } from "@playwright/test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { classifyText, goModule, startServer, type ServerHandle } from "./helpers";

let server: ServerHandle;
test.afterEach(async () => { await server?.stop(); });

test("切源后显示真实来源和空成交额，重启仍沿用备用源", async ({ page }) => {
  const fixture = join(mkdtempSync(join(tmpdir(), "dslite-fallback-")), "quotes.json");
  const bar = { date: "2026-09-11", open: 12, high: 12, low: 12, close: 12, volume_lots: 1234.56, amount_yuan: null };
  const providers = [
    { source: "primary", fail_bars: ["000001.SZ"], bars: { "000001.SZ": [{ ...bar, close: 99, open: 99, high: 99, low: 99 }] } },
    { source: "backup", fail_bars: [] as string[], bars: { "000001.SZ": [bar] } },
  ];
  writeFileSync(fixture, JSON.stringify({ providers }));
  const env = { DSLITE_QUOTES_FIXTURE: fixture, DSLITE_NOW: "2026-09-11T17:00:00+08:00" };
  server = await startServer(8799, undefined, env);
  await page.goto("/");
  await classifyText(page, "000001");
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByTestId("candle-chart")).toBeVisible({ timeout: 20000 });
  await expect(panel.getByTestId("chart-readout")).toContainText("收 12.00");
  const quote = await (await page.request.get("/api/quotes/000001.SZ")).json();
  expect(quote.bars[0].amountYuan).toBeNull();
  expect(quote.source).toBe("backup");
  expect((await (await page.request.get("/api/data/status")).json()).complete).toBe(true);
  await goModule(page, "数据中心");
  const row = page.getByTestId("data-center").locator("tbody tr", { hasText: "000001" });
  await expect(row).toContainText("backup");
  await expect(row).toContainText("primary");
  await server.stop();
  providers[0].fail_bars = [];
  writeFileSync(fixture, JSON.stringify({ providers }));
  server = await startServer(8799, server.dataDir, env);
  expect((await page.request.post("/api/securities/000001.SZ/refresh")).ok()).toBe(true);
  await page.reload();
  await goModule(page, "候选归类");
  await expect(panel.getByTestId("chart-readout")).toContainText("收 12.00");
  expect((await (await page.request.get("/api/quotes/000001.SZ")).json()).source).toBe("backup");
});

test("实源 BaoStock 经 HTTP 和图表显示，重启读回", async ({ page }) => {
  test.skip(process.env.DSLITE_LIVE_BAOSTOCK !== "1", "独立实源验收，不进入普通离线测试");
  test.setTimeout(120000);
  const env = { DSLITE_QUOTES_FIXTURE: "", DSLITE_NOW: "2026-09-21T23:00:00+08:00" };
  server = await startServer(8799, undefined, env);
  await page.goto("/");
  await classifyText(page, "000001");
  const panel = page.getByTestId("quote-panel");
  await expect(panel.getByTestId("candle-chart")).toBeVisible({ timeout: 60000 });
  const quote = await (await page.request.get("/api/quotes/000001.SZ")).json();
  expect(quote.source).toBe("baostock");
  expect(quote.latestDate).toBe("2026-09-21");
  expect(quote.bars.at(-1).close).toBe(11.73);
  await expect(panel.getByTestId("chart-readout")).toContainText("收 11.73");
  await expect(panel).toContainText("baostock");
  console.log("LIVE_BAOSTOCK", JSON.stringify({ source: quote.source, total: quote.total, latest: quote.latestDate, last: quote.bars.at(-1) }));
  await server.stop();
  server = await startServer(8799, server.dataDir); // Empty offline source: only stored data can appear.
  await page.reload();
  await goModule(page, "候选归类");
  await expect(panel.getByTestId("chart-readout")).toContainText("收 11.73");
  expect((await (await page.request.get("/api/quotes/000001.SZ")).json()).source).toBe("baostock");
});
