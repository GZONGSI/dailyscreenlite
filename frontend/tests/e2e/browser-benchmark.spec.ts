import { expect, test } from "@playwright/test";
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { goModule, startServer, type ServerHandle } from "./helpers";

/**
 * 浏览器到画面的性能基准（开发工具，不是验收用例）。
 *
 * 与后端 `benchmark_classification_browse.py` 的分工：那边量服务层与 HTTP 边界，
 * 这里在**真实浏览器**上量同一份隔离数据里的首开、切卡与筛选的「浏览器到画面耗时」、
 * 滚动帧率与渲染进程内存峰值；两边都只报告实测数字，静态代码风险不当作已测性能缺陷。
 * 筛选一项由点页签触发，连同那次 `PUT /api/classification/view` 的接口耗时与响应字节
 * 一起记录。
 *
 * 内存一列按规格要求记录**峰值**：首屏渲染后、连续切卡中、滚动过程中、长列表滚动
 * 到底部后各采一次 JS 堆与 DOM 节点数，报告取其中的最大值（`JS堆峰值_MB` /
 * `DOM节点数峰值`），另外保留一次 `HeapProfiler.collectGarbage` 之后的读数作对照
 * （`GC后_JS堆_MB`），后者不是峰值。浏览路径步数由播种脚本按 `min(--path, 证券数)`
 * 实际写入，报告参数块里的 `path_steps` 是回读它的值。
 *
 * 默认跳过，避免混进常规端到端验收（数据集大、耗时长）：
 *
 *     cd frontend
 *     $env:DSLITE_BROWSER_BENCH="1"
 *     $env:DSLITE_BENCH_SECURITIES="all"   # 或数字；缺省按工单量级 600
 *     $env:DSLITE_BENCH_DAYS="750"         # 多年每日入选；缺省 750
 *     $env:DSLITE_BENCH_OUT="..\.scratch\classification-browse-core\evidence\browser-benchmark.json"
 *     pnpm exec playwright test tests/e2e/browser-benchmark.spec.ts
 *
 * 数据由后端播种脚本生成（同一份快照与同形的入选/来源/处理历史），因此这里的
 * 数字与后端报告对应同一种数据规模。
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = resolve(HERE, "..", "..");
const REPO_ROOT = resolve(FRONTEND_ROOT, "..");
const PYTHON =
  process.platform === "win32"
    ? resolve(REPO_ROOT, "backend", ".venv", "Scripts", "python.exe")
    : resolve(REPO_ROOT, "backend", ".venv", "bin", "python");
const SEEDER = resolve(REPO_ROOT, "backend", "tools", "benchmark_classification_browse.py");

const ENABLED = process.env.DSLITE_BROWSER_BENCH === "1";
const SECURITIES = process.env.DSLITE_BENCH_SECURITIES ?? "600";
const DAYS = process.env.DSLITE_BENCH_DAYS ?? "750";
const HISTORY = 20;
/**
 * 浏览路径步数：播种脚本按 `min(PATH_STEPS, 证券数)` 如实写入 `path_json`，
 * 参数块里的 `path_steps` 回读它的实际值，因此报告数字与库里的步数一致。
 */
const PATH_STEPS = 200;
const PORT = 8799;
const OUT = process.env.DSLITE_BENCH_OUT;

interface PaintSample {
  label: string;
  ms: number;
}

/** 渲染进程内存的一个采样点：峰值取这些点的最大值。 */
interface MemorySample {
  label: string;
  jsHeapMB: number;
  domNodes: number;
  jsEventListeners: number;
}

interface SeedResult {
  dataDir: string;
  /** 播种脚本实际写入浏览路径的步数；解析不到记 null，报告里照实记。 */
  pathSteps: number | null;
}

/** 一次筛选切换里统一浏览读取（PUT /api/classification/view）的接口采样。 */
interface ApiSample {
  method: string;
  path: string;
  status: number;
  ms: number;
  bytes: number;
}

/** 一次筛选切换：点击到列表画面更新的耗时 + 这次操作的接口耗时与响应字节。 */
interface FilterSample {
  label: string;
  /** 左侧队列的行集合变化后的第一帧（筛选的主口径：新列表画到屏幕上）。 */
  listPaintMs: number | null;
  /** 现有的 `__paint` 探针（当前卡/主列区域）作对照，见报告说明。 */
  paintMs: number | null;
  apiCount: number;
  apiMs: number | null;
  apiBytes: number | null;
  apiMethod: string | null;
  apiPath: string | null;
  apiStatus: number | null;
}

function percentile(values: number[], fraction: number): number {
  if (!values.length) return 0;
  const ordered = [...values].sort((a, b) => a - b);
  const index = Math.min(
    ordered.length - 1,
    Math.max(0, Math.round(fraction * (ordered.length - 1))),
  );
  return ordered[index];
}

function summary(values: number[]): Record<string, number> {
  return {
    次数: values.length,
    p50_ms: Math.round(percentile(values, 0.5) * 100) / 100,
    p95_ms: Math.round(percentile(values, 0.95) * 100) / 100,
    max_ms: Math.round(Math.max(...values, 0) * 100) / 100,
  };
}

/** 用后端播种脚本在隔离目录里构造满量数据，返回数据目录与实际写入的路径步数。 */
async function seedData(): Promise<SeedResult> {
  const dataDir = mkdtempSync(resolve(tmpdir(), "dslite-bench-browser-"));
  let output = "";
  await new Promise<void>((resolveSeed, rejectSeed) => {
    const child = spawn(
      PYTHON,
      [
        // `-X utf8`：播种脚本的摘要是 UTF-8，子进程按系统代码页输出会读成乱码
        "-X",
        "utf8",
        SEEDER,
        "--data-dir",
        dataDir,
        "--securities",
        SECURITIES,
        "--days",
        DAYS,
        "--history",
        String(HISTORY),
        "--path",
        String(PATH_STEPS),
      ],
      { cwd: REPO_ROOT, stdio: ["ignore", "pipe", "pipe"] },
    );
    child.stdout?.on("data", (chunk) => {
      output += String(chunk);
    });
    child.stderr?.on("data", (chunk) => {
      output += String(chunk);
    });
    child.once("exit", (code) => {
      if (code === 0) resolveSeed();
      else rejectSeed(new Error(`播种失败（code=${code}）：\n${output}`));
    });
  });
  return { dataDir, pathSteps: parseSeededPathSteps(output) };
}

/** 从播种脚本输出里取回实际写入的浏览路径步数，供报告的参数块如实记录。 */
function parseSeededPathSteps(output: string): number | null {
  for (const line of output.split(/\r?\n/).reverse()) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("{")) continue;
    try {
      const parsed = JSON.parse(trimmed) as { path_steps?: unknown };
      if (typeof parsed.path_steps === "number") return parsed.path_steps;
    } catch {
      // 不是播种摘要行，继续往前找
    }
  }
  return null;
}

/**
 * 给基于 rAF 的页面求值加上限时。
 *
 * 渲染线程被别的重负载进程饿死时，`requestAnimationFrame` 可能长时间不回调，
 * 那种情况下宁可明确失败，也不要挂到测试超时（满量档一挂就是一小时）。
 */
async function withTimeout<T>(promise: Promise<T>, ms: number, what: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([
      promise,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () =>
            reject(
              new Error(
                `${what} 超过 ${ms} ms：页面渲染线程可能被别的重负载进程饿死，请确认没有其他 Playwright/基准在跑`,
              ),
            ),
          ms,
        );
      }),
    ]);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

/**
 * 重置画面探针：等一次绘制落下后把 `__paint.ms` 置空，供下一次操作计时。
 *
 * 先等两帧是为了不把上一次渲染的收尾算进本次操作。
 */
async function armPaintProbe(page: import("@playwright/test").Page): Promise<void> {
  await withTimeout(
    page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => {
            requestAnimationFrame(() => {
              (window as unknown as { __paint: { ms: number | null } }).__paint = {
                ms: null,
              };
              resolve();
            });
          });
        }),
    ),
    15_000,
    "重置画面探针",
  );
}

/** 等探针落下并返回它记录的「画面已更新」时刻（`performance.now()`）；超时返回 null。 */
async function waitForPaintMark(
  page: import("@playwright/test").Page,
): Promise<number | null> {
  try {
    await page.waitForFunction(
      () =>
        (window as unknown as { __paint?: { ms: number | null } }).__paint?.ms != null,
      undefined,
      { timeout: 30_000 },
    );
  } catch {
    return null;
  }
  return page.evaluate(
    () => (window as unknown as { __paint: { ms: number } }).__paint.ms,
  );
}

/** 长跑基准的阶段日志：满量档一次要几分钟，日志里看得出停在哪儿。 */
function mark(label: string): void {
  console.log(`[浏览器基准] ${label}`);
}

/**
 * 在页面里量「浏览器到画面」：点击导航按钮到新卡片完成一次绘制。
 *
 * 起点取点击那一刻（浏览器开始处理这次用户操作），终点取当前卡所在区域下一次
 * DOM 变化后的第一帧，因此包含请求、响应、React 渲染与浏览器绘制；与只在服务端
 * 计时的指标互补。
 */
async function measureClick(
  page: import("@playwright/test").Page,
  label: string,
  action: () => Promise<void>,
  samples: number,
  /** 每次采样之后调用：用来在连续切卡过程中取内存采样点，不影响上面的计时。 */
  onSampled?: (index: number) => Promise<void>,
): Promise<PaintSample[]> {
  const results: PaintSample[] = [];
  for (let index = 0; index < samples; index += 1) {
    await armIdlePaintProbe(page);
    const started = await page.evaluate(() => performance.now());
    await action();
    const finished = await waitForPaintMark(page);
    if (finished == null) {
      throw new Error(`切卡第 ${index + 1} 次采样没有等到画面更新`);
    }
    results.push({ label, ms: finished - started });
    if (onSampled) await onSampled(index);
  }
  return results;
}

/**
 * 武装画面探针并确认此刻页面确实静默。
 *
 * 上一步操作遗留的渲染（例如切卡后的行情补全）会在点击之前就把探针标记掉，
 * 那样量到的不是这次点击；因此武装后探针还有值就再武装一次（最多 10 次）。
 */
async function armIdlePaintProbe(page: import("@playwright/test").Page): Promise<void> {
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await armPaintProbe(page);
    const idle = await page.evaluate(
      () => (window as unknown as { __paint: { ms: number | null } }).__paint.ms === null,
    );
    if (idle) return;
  }
}

/**
 * 武装「左侧队列行集合变化」探针：记录当前行集合签名，之后每帧比一次。
 *
 * 筛选切换时应用会先显示「正在更新筛选，暂时保留上次结果…」并保留旧行，新列表到位
 * 才替换行集合，因此用行集合变化判定「新列表画到屏幕上」。签名取「已挂载行数 +
 * 首行的 aria-setsize（该列表的完整只数）」，**不含行情价格等文本**：否则切卡后的
 * 行情补全也会改首行文本，把一次无关渲染当成列表更新。
 */
async function armListRowsProbe(page: import("@playwright/test").Page): Promise<void> {
  await page.evaluate(() => {
    const state = window as unknown as {
      __listRows: { ms: number | null; armed: string };
    };
    const signature = () => {
      const pane = document.querySelector(".classification-queue-pane");
      const options = pane ? pane.querySelectorAll('[role="option"]') : [];
      const first = options[0];
      return `${options.length}|${first ? first.getAttribute("aria-setsize") : ""}`;
    };
    // 循环每帧读当前探针对象，不闭包捕获：上一次的循环因此不会把这次的采样提前落定。
    state.__listRows = { ms: null, armed: signature() };
    const tick = () => {
      const probe = state.__listRows;
      if (probe.ms !== null) return;
      if (probe.armed !== signature()) {
        requestAnimationFrame(() => {
          const current = state.__listRows;
          if (current === probe && current.ms === null) {
            current.ms = performance.now();
          }
        });
        return;
      }
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  });
}

/** 等行集合探针落下；一直没变化时返回 null，报告里照实记（不把它当成 0）。 */
async function waitForListRowsMark(
  page: import("@playwright/test").Page,
): Promise<number | null> {
  try {
    await page.waitForFunction(
      () =>
        (window as unknown as { __listRows?: { ms: number | null } }).__listRows?.ms !=
        null,
      undefined,
      { timeout: 20_000 },
    );
  } catch {
    return null;
  }
  return page.evaluate(
    () => (window as unknown as { __listRows: { ms: number } }).__listRows.ms,
  );
}

/**
 * 量一次真实的筛选切换：点击那一刻到左侧列表行集合更新后的第一帧（主口径），
 * 加上现有的 `__paint` 探针（当前卡/主列区域）作对照，以及这次操作里统一浏览读取的
 * 接口耗时与响应字节。
 *
 * 筛选切换由前端发一次 `PUT /api/classification/view`（同一读取视图里返回当前卡、
 * 整份轻量列表与数量），因此接口列与只回传卡片与变化的切卡列正好形成对照。
 *
 * 接口耗时按浏览器侧墙钟量：请求发出（`request`）到请求完成（`requestfinished`，
 * 正文已收完），与后端报告量「一次操作到响应体到手」的口径一致；不用
 * `request.timing()`，它在 Chromium 下 `startTime` 是 epoch 而 `responseEnd` 是相对值，
 * 相减会得到无意义的负数。响应字节取解码后的正文长度，与后端 `len(response.content)` 同口径。
 */
async function measureFilter(
  page: import("@playwright/test").Page,
  label: string,
  action: () => Promise<void>,
): Promise<FilterSample> {
  await armIdlePaintProbe(page);
  await armListRowsProbe(page);
  const api: ApiSample[] = [];
  const pending: Promise<void>[] = [];
  const sentAt = new Map<import("@playwright/test").Request, number>();
  const isViewRead = (url: string) => url.includes("/api/classification/view");
  const onRequest = (request: import("@playwright/test").Request) => {
    if (isViewRead(request.url())) sentAt.set(request, Date.now());
  };
  const onRequestFinished = (request: import("@playwright/test").Request) => {
    if (!isViewRead(request.url())) return;
    const began = sentAt.get(request) ?? null;
    const finished = Date.now();
    pending.push(
      request
        .response()
        .then(async (response) => {
          if (!response) return;
          const buffer = await response.body();
          api.push({
            method: request.method(),
            path: new URL(request.url()).pathname,
            status: response.status(),
            ms: began == null ? 0 : finished - began,
            bytes: buffer.length,
          });
        })
        .catch(() => undefined),
    );
  };
  page.on("request", onRequest);
  page.on("requestfinished", onRequestFinished);
  const started = await page.evaluate(() => performance.now());
  try {
    await action();
    const finished = await waitForPaintMark(page);
    const rowsFinished = await waitForListRowsMark(page);
    await Promise.all(pending);
    // 只认这次筛选自己发的写命令；万一窗口里还夹着别的读取，不拿它顶替。
    const sample = api.find((entry) => entry.method === "PUT") ?? api[0] ?? null;
    // 探针值早于点击起点时是残留渲染的标记（负值），如实记 null 而不是留下坏数字。
    const listPaint = rowsFinished == null ? null : rowsFinished - started;
    const paint = finished == null ? null : finished - started;
    return {
      label,
      listPaintMs:
        listPaint == null || listPaint < 0 ? null : Math.round(listPaint * 100) / 100,
      paintMs: paint == null || paint < 0 ? null : Math.round(paint * 100) / 100,
      apiCount: api.length,
      apiMs: sample?.ms ?? null,
      apiBytes: sample?.bytes ?? null,
      apiMethod: sample?.method ?? null,
      apiPath: sample?.path ?? null,
      apiStatus: sample?.status ?? null,
    };
  } finally {
    page.off("request", onRequest);
    page.off("requestfinished", onRequestFinished);
  }
}

/** 等到渲染循环已经记录了至少 `count` 帧。 */
async function waitForFrames(
  page: import("@playwright/test").Page,
  count: number,
): Promise<void> {
  await page.waitForFunction(
    (expected) =>
      (window as unknown as { __frames: number[] }).__frames.length >= expected,
    count,
    { timeout: 30_000 },
  );
}

/**
 * 在队列容器里连续滚动 `frames` 帧，返回每帧间隔（毫秒）。
 *
 * `hooks` 的采样点按已完成的帧数触发。内存采样另起一次滚动来插点，不插进帧率
 * 测量的那一次：CDP 调用可能让渲染线程停顿一帧，落进帧率统计就会变成假丢帧。
 */
async function scrollQueue(
  page: import("@playwright/test").Page,
  frames: number,
  stepPx: number,
  hooks: { atFrame: number; run: () => Promise<unknown> }[] = [],
): Promise<number[]> {
  await page.evaluate(
    (options) => {
      (window as unknown as { __frames: number[] }).__frames = [];
      const pane = document.querySelector(".classification-queue-pane");
      if (!pane) return;
      let last = performance.now();
      let remaining = options.frames;
      const step = (now: number) => {
        (window as unknown as { __frames: number[] }).__frames.push(now - last);
        last = now;
        pane.scrollTop += options.stepPx;
        remaining -= 1;
        if (remaining > 0) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    { frames, stepPx },
  );
  for (const hook of hooks) {
    await waitForFrames(page, hook.atFrame);
    await hook.run();
  }
  await waitForFrames(page, frames);
  return page.evaluate(
    () => (window as unknown as { __frames: number[] }).__frames,
  );
}

/** 等到页面又画过两帧（虚拟列表在滚动后的重新挂载已完成）。 */
async function waitForPaint(page: import("@playwright/test").Page): Promise<void> {
  await withTimeout(
    page.evaluate(
      () =>
        new Promise<void>((resolve) => {
          requestAnimationFrame(() => requestAnimationFrame(() => resolve()));
        }),
    ),
    15_000,
    "等待两帧",
  );
}

test.describe("浏览器到画面的性能基准", () => {
  test.skip(!ENABLED, "设置 DSLITE_BROWSER_BENCH=1 才运行浏览器基准");

  test("首开、切卡、筛选的画面耗时与滚动帧率、峰值内存", async ({ browser }) => {
    test.setTimeout(60 * 60 * 1000);
    mark(`开始播种（securities=${SECURITIES} days=${DAYS}）`);
    const seed = await seedData();
    mark(`播种完成：数据目录 ${seed.dataDir}，path_steps=${String(seed.pathSteps)}`);
    let server: ServerHandle | null = null;
    try {
      server = await startServer(PORT, seed.dataDir);
      mark("服务已就绪");
      const context = await browser.newContext();
      const page = await context.newPage();
      const client = await context.newCDPSession(page);
      await client.send("Performance.enable");

      // 内存采样点：流程里每个关键时刻各读一次，报告取其中的峰值；
      // 只有最后那次「GC 后」是强制回收之后的对照读数。
      const memorySamples: MemorySample[] = [];
      const sampleMemory = async (label: string): Promise<MemorySample> => {
        const metrics = await client.send("Performance.getMetrics");
        const counters = await client.send("Memory.getDOMCounters");
        const heapBytes =
          metrics.metrics.find((entry) => entry.name === "JSHeapUsedSize")?.value ?? 0;
        const sample: MemorySample = {
          label,
          jsHeapMB: Math.round((heapBytes / 1024 / 1024) * 100) / 100,
          domNodes: counters.nodes,
          jsEventListeners: counters.jsEventListeners,
        };
        memorySamples.push(sample);
        return sample;
      };

      // 画面计时探针：当前卡标题变化后的第一帧即为「已画到屏幕上」。
      await page.addInitScript(() => {
        const state = window as unknown as {
          __paint: { ms: number | null };
          __frames: number[];
        };
        state.__paint = { ms: null };
        state.__frames = [];
        const observe = () => {
          const main = document.querySelector('[data-testid="main-column"]');
          if (!main) {
            requestAnimationFrame(observe);
            return;
          }
          const mark = () => {
            requestAnimationFrame(() => {
              if (state.__paint.ms === null) state.__paint.ms = performance.now();
            });
          };
          new MutationObserver(mark).observe(main, {
            childList: true,
            subtree: true,
            characterData: true,
          });
        };
        requestAnimationFrame(observe);
      });

      await page.goto("/");
      // 播种数据已经把浏览上下文落在满量数据的起点：打开工作区就是真实规模的队列
      const firstOpenStart = await page.evaluate(() => performance.now());
      await goModule(page, "候选归类");
      await page.waitForFunction(
        () => (window as unknown as { __paint?: { ms: number | null } }).__paint?.ms != null,
      );
      const firstOpenMs =
        (await page.evaluate(
          () => (window as unknown as { __paint: { ms: number } }).__paint.ms,
        )) - firstOpenStart;
      await expect(page.getByTestId("stock-card")).toBeVisible();
      await sampleMemory("首屏渲染后");
      mark(`首开完成：${Math.round(firstOpenMs)} ms`);

      const total = Number(
        await page
          .getByRole("listbox", { name: "待归类股票列表" })
          .getAttribute("data-virtual-total"),
      );
      const renderedRows = await page
        .getByRole("listbox", { name: "待归类股票列表" })
        .getByRole("option")
        .count();
      mark(`列表读数完成：总行数 ${total}，同时渲染 ${renderedRows}`);

      const switches = await measureClick(
        page,
        "切卡",
        async () => {
          await page.getByRole("button", { name: "下一个" }).click();
        },
        10,
        // 连续切卡过程中的内存采样：切卡耗时已经记下，采样不落进那一列
        async (index) => {
          await sampleMemory(`连续切卡中_第${index + 1}次`);
        },
      );
      mark("连续切卡完成");

      // 筛选：点页签触发真实的筛选切换（先切到已处理，再切回待归类恢复后面的工作范围）
      const filters: FilterSample[] = [];
      filters.push(
        await measureFilter(page, "切到已处理", async () => {
          await page.getByRole("tab", { name: "已处理" }).click();
        }),
      );
      mark("筛选（切到已处理）完成");
      filters.push(
        await measureFilter(page, "回到待归类", async () => {
          await page.getByRole("tab", { name: "待归类" }).click();
        }),
      );
      mark("筛选（回到待归类）完成");

      // 滚动帧率：在队列容器里连续滚动，记录每帧间隔。这一列不插采样点。
      const scrollFrames = (await scrollQueue(page, 120, 160)).slice(1);
      mark(`滚动帧率测量完成：${scrollFrames.length} 帧`);

      // 滚动过程中与长列表滚动到底部后的内存采样：另起一次滚动，采样点落在中段
      await scrollQueue(page, 60, 160, [
        { atFrame: 30, run: () => sampleMemory("滚动过程中") },
      ]);
      await page.evaluate(() => {
        const pane = document.querySelector(".classification-queue-pane");
        if (pane) pane.scrollTop = pane.scrollHeight;
      });
      await waitForPaint(page);
      await sampleMemory("长列表滚动到底部后");

      // GC 后对照：强制回收一次再读，只作对照，不代表峰值
      await client.send("HeapProfiler.enable");
      await client.send("HeapProfiler.collectGarbage");
      const afterGc = await sampleMemory("GC 后");
      mark(`内存采样完成：${memorySamples.length} 个采样点`);

      const peakHeap = memorySamples.reduce((best, sample) =>
        sample.jsHeapMB > best.jsHeapMB ? sample : best,
      );
      const peakDom = memorySamples.reduce((best, sample) =>
        sample.domNodes > best.domNodes ? sample : best,
      );

      const report = {
        隔离数据: {
          参数: {
            securities: SECURITIES,
            days: DAYS,
            history: HISTORY,
            // `path` 是请求值，`path_steps` 是播种脚本实际写入浏览路径的步数：
            // 报告不能写一个库里没有的步数。
            path: PATH_STEPS,
            path_steps: seed.pathSteps,
          },
        },
        画面: {
          首开_ms: Math.round(firstOpenMs * 100) / 100,
          切卡: summary(switches.map((sample) => sample.ms)),
          筛选: {
            次数: filters.length,
            画面_ms: summary(
              filters
                .map((sample) => sample.listPaintMs)
                .filter((value): value is number => value != null),
            ),
            主列画面_ms: summary(
              filters
                .map((sample) => sample.paintMs)
                .filter((value): value is number => value != null),
            ),
            接口耗时_ms: summary(
              filters
                .map((sample) => sample.apiMs)
                .filter((value): value is number => value != null),
            ),
            响应字节_p50: Math.round(
              percentile(
                filters
                  .map((sample) => sample.apiBytes)
                  .filter((value): value is number => value != null),
                0.5,
              ),
            ),
            响应字节_max: Math.max(
              ...filters.map((sample) => sample.apiBytes ?? 0),
              0,
            ),
            样本: filters,
            说明:
              "点「待归类／已处理」页签触发真实的筛选切换。画面_ms 是点击到左侧队列行集合更新后的第一帧" +
              "（应用在加载期间保留旧列表并显示提示，因此以行集合变化判定「新列表画到屏幕上」）；" +
              "主列画面_ms 是同一次点击里原 `__paint` 探针（当前卡/主列区域）的值，仅作对照。" +
              "探针先确认页面静默再武装；某个采样点记 null 表示那次探针被残留渲染提前标记或未变化。" +
              "接口列是同一次操作里 PUT /api/classification/view 统一浏览读取的浏览器侧墙钟耗时" +
              "（请求发出到正文收完）与解码后正文长度，可与只回传卡片与变化的切卡列对照。",
          },
        },
        列表: {
          总行数: total,
          同时渲染行数: renderedRows,
          渲染比例: total ? Math.round((renderedRows / total) * 10000) / 10000 : 0,
        },
        滚动: {
          采样帧数: scrollFrames.length,
          帧间隔: summary(scrollFrames),
          丢帧数_超过33ms: scrollFrames.filter((value) => value > 33).length,
          说明: "无头 Chrome 的 requestAnimationFrame 不跟随显示器垂直同步，帧间隔只反映本机渲染线程能否跟上滚动",
        },
        内存: {
          采样点数: memorySamples.length,
          采样点: memorySamples,
          JS堆峰值_MB: peakHeap.jsHeapMB,
          JS堆峰值_采样点: peakHeap.label,
          DOM节点数峰值: peakDom.domNodes,
          DOM节点数峰值_采样点: peakDom.label,
          JS事件监听数峰值: Math.max(
            ...memorySamples.map((sample) => sample.jsEventListeners),
          ),
          GC后_JS堆_MB: afterGc.jsHeapMB,
          GC后_DOM节点数: afterGc.domNodes,
          GC后_JS事件监听数: afterGc.jsEventListeners,
          说明:
            "峰值取流程多个时刻（首屏渲染后、连续切卡中、滚动过程中、长列表滚动到底部后）的最大值；" +
            "GC后_* 是 HeapProfiler.collectGarbage 之后的一次对照读数，不是峰值。" +
            "Chrome 的 DOM 节点计数包含尚未回收的分离节点，因此 DOM节点数峰值 高于 GC 后的存活数。",
        },
      };

      const payload = JSON.stringify(report, null, 2);
      if (OUT) {
        const { mkdirSync, writeFileSync } = await import("node:fs");
        mkdirSync(dirname(resolve(REPO_ROOT, OUT)), { recursive: true });
        writeFileSync(resolve(REPO_ROOT, OUT), `${payload}\n`, "utf8");
        console.log(`浏览器基准报告已写入 ${OUT}`);
      } else {
        console.log(payload);
      }
      await context.close();
    } finally {
      await server?.stop();
    }
  });
});
