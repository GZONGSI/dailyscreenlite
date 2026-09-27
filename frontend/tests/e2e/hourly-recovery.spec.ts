import { expect, test } from "@playwright/test";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { classifyText, goModule, startServer, submitText, type ServerHandle } from "./helpers";

let server: ServerHandle;
test.afterEach(async () => { await server?.stop(); });

test("自动补取失败保旧、同轮重启去重、下一轮恢复且观察关系和来源保留", async ({ page }) => {
  test.setTimeout(90000);
  const dir = mkdtempSync(join(tmpdir(), "dslite-hourly-"));
  const quotesPath = join(dir, "quotes.json");
  const statusPath = join(dir, "status.json");
  const snapshot = JSON.parse(readFileSync("../data/securities/initial_snapshot.json", "utf8"));
  const bar = (date: string, close: number) => ({ date, open: close, high: close, low: close, close, volume_lots: 1000, amount_yuan: null });
  const providers = [
    { source: "primary", fail_bars: ["000001.SZ"], bars: { "000001.SZ": [bar("2026-09-22", 99)] } },
    { source: "backup", fail_bars: [] as string[], bars: { "000001.SZ": [bar("2026-09-21", 12)] } },
  ];
  const quotes = { providers, security_markets: {
    SH: { error: "沪市名单超时" },
    SZ: { securities: snapshot.securities.filter((s: { exchange: string }) => s.exchange === "SZ") },
    BJ: { error: "北交所不可用" },
  } };
  writeFileSync(quotesPath, JSON.stringify(quotes));
  const month = JSON.parse(readFileSync("../backend/tests/fixtures/szse-calendar-2026-09.json", "utf8"));
  writeFileSync(statusPath, JSON.stringify({ calendar_months: { "2026-09": month }, covered_markets: ["SH", "SZ"], uncovered_markets: ["BJ"] }));
  const env = { DSLITE_QUOTES_FIXTURE: quotesPath, DSLITE_MARKET_STATUS_FIXTURE: statusPath, DSLITE_NOW: "2026-09-22T16:29:00+08:00", DSLITE_UPDATE_SCHEDULE: "off" };
  server = await startServer(8799, undefined, env);
  await page.clock.install();
  await page.goto("/");
  await classifyText(page, "000001");
  await expect(page.getByTestId("chart-readout")).toContainText("收 12.00");
  providers[1].fail_bars = ["000001.SZ"];
  writeFileSync(quotesPath, JSON.stringify(quotes));
  const restart = async (time: string, reload = true) => {
    await server.stop();
    server = await startServer(8799, server.dataDir, { ...env, DSLITE_NOW: `2026-09-22T${time}:00+08:00`, DSLITE_UPDATE_SCHEDULE: "on" });
    if (reload) await page.reload();
  };
  const rounds = async () => (await (await page.request.get("/api/updates")).json()).automaticRounds;
  await restart("16:30");
  await expect.poll(async () => (await rounds()).length).toBe(1);
  await goModule(page, "候选归类");
  await expect(page.getByTestId("data-reminder")).toContainText("09-22");
  await expect(page.getByTestId("chart-readout")).toContainText("收 12.00");
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  await page.getByRole("checkbox").first().check();
  await page.getByRole("button", { name: "确认并完成归类" }).click();
  await submitText(page, "920001");
  await goModule(page, "数据中心");
  await expect(page.getByTestId("automatic-recovery")).toContainText("1 轮");
  await expect(page.getByTestId("data-center").locator("tbody tr", { hasText: "000001" })).toContainText("2026-09-21");
  const first = await rounds();
  await restart("16:30");
  expect(await rounds()).toEqual(first);
  await goModule(page, "数据中心");
  await expect(page.getByTestId("automatic-recovery")).toContainText("1 轮");
  providers[0].fail_bars = [];
  providers[1].fail_bars = [];
  providers[1].bars["000001.SZ"].push(bar("2026-09-22", 13));
  writeFileSync(quotesPath, JSON.stringify(quotes));
  await restart("17:30", false); // 保持页面打开，不能靠页面重载才能看见后台恢复
  await expect.poll(async () => (await rounds()).length).toBe(2);
  await expect.poll(async () => (await (await page.request.get("/api/data/status")).json()).complete).toBe(true);
  await page.clock.runFor(60001);
  await expect(page.getByTestId("automatic-recovery")).toContainText("2 轮");
  await expect(page.getByTestId("data-center").locator("tbody tr", { hasText: "000001" })).toContainText("2026-09-22");
  await goModule(page, "观察组");
  await expect(page.getByTestId("data-reminder")).toHaveCount(0);
  await expect(page.getByTestId("observed-stocks").getByRole("cell", { name: /000001/ })).toBeVisible();
  const quote = await (await page.request.get("/api/quotes/000001.SZ")).json();
  expect(quote.source).toBe("backup");
  expect(quote.latestDate).toBe("2026-09-22");
  expect(quote.bars).toHaveLength(2);
  expect(quote.bars.at(-1).amountYuan).toBeNull();
  const beforeCutoff = await rounds();
  await restart("23:00");
  expect(await rounds()).toEqual(beforeCutoff);
  await goModule(page, "数据中心");
  const finished = page.waitForResponse(r => r.url().endsWith("/api/updates") && r.request().method() === "POST");
  await page.getByTestId("data-center").getByRole("button", { name: "更新数据", exact: true }).click();
  expect((await finished).ok()).toBe(true);
  expect(await rounds()).toEqual(beforeCutoff);
  expect((await (await page.request.get("/api/quotes/000001.SZ")).json()).source).toBe("backup");
});
