import { defineConfig } from "@playwright/test";

/**
 * 端到端验收：真实浏览器 → 本机 HTTP 服务 → 临时真实数据库。
 *
 * 使用系统 Chrome（channel: "chrome"），不下载 Playwright 自带浏览器。
 * 服务由测试的 webServer 启动，数据目录指向隔离的 tests/e2e/.data。
 */
export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 60_000,
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:8799",
    channel: "chrome",
    headless: true,
    trace: "off",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium-system",
      use: { browserName: "chromium", channel: "chrome" },
    },
  ],
});
