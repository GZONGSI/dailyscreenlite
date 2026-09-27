import { test, expect } from "@playwright/test";
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
test("选组默认预选、取消不保存、稍后仍可回看并决定", async ({ page }) => {
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001\n600519");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await startClassification(page);
  await page.getByRole("button", { name: "稍后处理", exact: true }).click();
  await expect(page.getByRole("heading", { name: "贵州茅台" })).toBeVisible();
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(
    page.getByRole("button", { name: "暂不关注", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  const picker = page.getByRole("dialog", { name: "选择观察组" });
  await expect(picker.getByRole("checkbox").first()).toBeChecked();
  await picker.getByRole("checkbox").first().uncheck();
  await expect(
    picker.getByRole("button", { name: "确认并完成归类" }),
  ).toBeDisabled();
  await picker.getByRole("button", { name: "取消", exact: true }).click();
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  await expect(picker.getByRole("checkbox").first()).toBeChecked();
  await picker.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(picker).toHaveCount(0);
  await page.getByRole("button", { name: "上一个" }).click();
  await expect(
    page.getByRole("heading", { name: "平安银行", exact: true }),
  ).toBeVisible();
  await expect(page.getByTestId("stock-card").getByRole("button", { name: "重新归类", exact: true })).toBeVisible();
});

test("新增笔记弹窗保存关闭、脏输入确认，图表保留在原卡", async ({ page }) => {
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 1 只，跳过 0 只")).toBeVisible();
  await startClassification(page);
  await page.getByRole("button", { name: "新增笔记", exact: true }).click();
  const editor = page.getByRole("dialog", { name: /新增笔记/ });
  await editor
    .getByLabel("新增笔记", { exact: true })
    .fill("等待突破\n关注成交量");
  await page.keyboard.press("Escape");
  const confirm = page.getByRole("alertdialog", { name: "放弃未保存的修改？" });
  await expect(confirm).toBeVisible();
  await confirm.getByRole("button", { name: "继续编辑" }).click();
  await expect(editor.getByLabel("新增笔记", { exact: true })).toBeFocused();
  await editor.getByRole("button", { name: "保存笔记" }).click();
  await expect(editor).toHaveCount(0);
  await expect(page.getByTestId("note-summary")).toContainText("等待突破");
  await expect(
    page.getByRole("button", { name: "新增笔记", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByRole("button", { name: "稍后处理", exact: true }),
  ).toBeVisible();
});

test("完整K线与笔记选组组合：三尺寸双主题、键盘及减少动画", async ({
  page,
}, info) => {
  test.setTimeout(180000);
  const { writeFileSync } = await import("node:fs");
  const { join } = await import("node:path");
  await server.stop();
  const path = join(server.dataDir, "quotes.json");
  let day = new Date("2023-01-02T00:00:00Z");
  const bars = [];
  while (bars.length < 700) {
    if (day.getUTCDay() !== 0 && day.getUTCDay() !== 6) {
      const close: number = 10 + bars.length * 0.03;
      bars.push({
        date: day.toISOString().slice(0, 10),
        open: close + (bars.length % 3 === 0 ? 0.08 : -0.05),
        high: close + 0.12,
        low: close - 0.12,
        close,
        volume_lots: 10000 + bars.length * 20,
        amount_yuan: 1000000,
      });
    }
    day = new Date(day.getTime() + 86400000);
  }
  writeFileSync(path, JSON.stringify({ bars: { "000001.SZ": bars } }));
  server = await startServer(8799, server.dataDir, {
    DSLITE_QUOTES_FIXTURE: path,
  });
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/");
  await goModule(page, "导入");
  await page.locator("#import-text").fill("000001\n600519");
  await page.getByRole("button", { name: "提交", exact: true }).click();
  await expect(page.getByText("已导入 2 只，跳过 0 只")).toBeVisible();
  await startClassification(page);
  await expect(page.getByTestId("candle-chart")).toBeVisible({
    timeout: 20000,
  });
  await page.getByRole("button", { name: "MA60", exact: true }).click();
  const visible = page.getByTestId("chart-visible-range");
  for (let i = 0; i < 6; i++) {
    const old = await visible.textContent();
    await page.getByRole("button", { name: "放大图表" }).click();
    await expect(visible).not.toHaveText(old!);
  }
  await page.getByRole("button", { name: "放大图表" }).click();
  await expect(visible).toContainText("105 根");
  const before = await visible.textContent();
  const canvas = await page
    .getByTestId("candle-chart")
    .locator("canvas")
    .first()
    .elementHandle();
  for (const theme of ["浅色", "深色"]) {
    await page.getByRole("button", { name: "主题" }).click();
    await page.getByRole("menuitemradio", { name: theme, exact: true }).click();
    for (const [width, height] of [
      [1920, 1080],
      [1366, 768],
      [390, 844],
    ]) {
      await page.setViewportSize({ width, height });
      await expect(visible).toHaveText(before!);
      if (width === 1920) {
        const summary = await page.getByTestId("note-summary").boundingBox();
        expect(summary!.y + summary!.height).toBeLessThanOrEqual(height);
        const chart = await page.getByTestId("candle-chart").boundingBox();
        expect(chart!.y).toBeGreaterThan(0);
        expect(chart!.y + chart!.height).toBeLessThanOrEqual(height);
      }
      await page.screenshot({
        path: info.outputPath(`${theme}-${width}-workbench.png`),
      });
      await page.getByRole("button", { name: "加入 / 保留观察" }).click();
      const pick = page.getByRole("dialog", { name: "选择观察组" });
      await expect(pick.getByRole("checkbox").first()).toBeVisible();
      const bounds = await pick.boundingBox();
      expect(bounds!.x).toBeGreaterThanOrEqual(0);
      expect(bounds!.y).toBeGreaterThanOrEqual(0);
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(height);
      await pick.screenshot({
        path: info.outputPath(`${theme}-${width}-picker.png`),
      });
      await page.keyboard.press("Escape");
      await expect(
        page.getByRole("button", { name: "加入 / 保留观察" }),
      ).toBeFocused();
      await page.getByRole("button", { name: "新增笔记", exact: true }).click();
      const dialog = page.getByRole("dialog", { name: /新增笔记/ });
      await expect(
        dialog.getByLabel("新增笔记", { exact: true }),
      ).toBeFocused();
      await page.keyboard.press("Shift+Tab");
      expect(
        await dialog.evaluate((el) => el.contains(document.activeElement)),
      ).toBe(true);
      await dialog.screenshot({
        path: info.outputPath(`${theme}-${width}-dialog.png`),
      });
      await page.keyboard.press("Escape");
      await expect(dialog).toHaveCount(0);
      await page.getByRole("button", { name: "查看全部笔记" }).click();
      const drawer = page.getByRole("dialog", { name: /全部笔记/ });
      await expect(drawer).toBeVisible();
      await drawer.screenshot({
        path: info.outputPath(`${theme}-${width}-sheet.png`),
      });
      await page.keyboard.press("Escape");
      await expect(drawer).toHaveCount(0);
      expect(await canvas!.evaluate((el) => el.isConnected)).toBe(true);
      await expect(visible).toHaveText(before!);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
    }
  }
  // 放大字体用例在桌面宽度下进行：窄屏下粘性动作栏会盖住笔记区
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "200%";
  });
  await page.getByRole("button", { name: "新增笔记", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: /新增笔记/ });
  await dialog
    .getByLabel("新增笔记", { exact: true })
    .fill("放大字体下仍可记录");
  await dialog.getByRole("button", { name: "保存笔记" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("note-summary")).toContainText(
    "放大字体下仍可记录",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => {
    document.documentElement.style.fontSize = "";
  });
  for (let i = 0; i < 3; i++) {
    await page.getByRole("button", { name: "查看全部笔记" }).click();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
  }
  await page.getByRole("button", { name: "加入 / 保留观察" }).click();
  await page.getByRole("button", { name: "确认并完成归类" }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  // 观察完成本次归类后照常自动前进（保存与导航是两个确认步骤）：下一张卡是另一只
  // 没有行情夹具的股票，因此这里断言的是卡片切换本身，而不是上一只的图表范围。
  await expect(
    page.getByRole("heading", { name: "贵州茅台", exact: true }),
  ).toBeVisible();
});
