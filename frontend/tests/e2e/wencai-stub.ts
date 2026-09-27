/**
 * 本地问财桩：实现当前网页使用的流式接口 `gateway/aime/stream-query`（SSE），
 * 让真实后端适配器（HttpWencaiSession + acquire + 条件确认）在不访问外网的情况下
 * 走完整链路。仅用于端到端验收，不参与任何生产路径。
 */
import { createServer, type Server } from "node:http";

const CODES = ["000001.SZ", "600519.SH", "300750.SZ"];
const ROW_COUNT = CODES.length;

/** 与当前接口一致的 model_sql 条件结构（窗口为相对表述，不含滚动日期）。 */
const MODEL_SQL = JSON.stringify({
  logic_relation: [
    {
      logic: "INTERSECT",
      requirements: [
        { text: "最高价创近120天新高", sql: "where record_high(最高价, recent('120d'))" },
        { text: "过去250个交易日区间涨跌幅<=100%", sql: "where 交易日期 = front('250t')" },
      ],
    },
  ],
});

function row(code: string) {
  const market = code.startsWith("6") || code.startsWith("9") ? "SH" : code.startsWith("92") ? "BJ" : "SZ";
  return { code: code.split(".")[0], 股票代码: `${code.split(".")[0]}.${market}`, 股票简称: "示例" };
}

/** 组织 SSE 响应：base_info + 结果组件（含 datas/code_count/model_sql）。 */
function streamPayload(query: string): string {
  const events = [
    { type: "base_info", base_info: { question: query } },
    {
      answer_path: "other/openAnswer",
      section: {
        result_page: {
          components: [
            {
              data: {
                chunks_info: JSON.stringify([`条件下命中 (${ROW_COUNT})`]),
                code_count: ROW_COUNT,
                row_count: ROW_COUNT,
                dataSize: ROW_COUNT,
                status_code: 0,
                status_msg: "请求正常",
                token: "stub-token",
                model_sql: MODEL_SQL,
                datas: CODES.map(row),
              },
            },
          ],
        },
      },
    },
  ];
  return events.map((e) => `data:${JSON.stringify(e)}\n\n`).join("");
}

/**
 * 启动桩服务。loginExpired=false 时首次 stream-query 返回登录失效（HTTP 401），
 * 随后（用户更新 Cookie 并重试）返回正常结果，用于验证"失败 → 重试成功"链路。
 */
export function startWencaiStub(options: { loginExpired?: () => boolean } = {}): Promise<{
  baseUrl: string;
  requests: string[];
  stop: () => Promise<void>;
}> {
  const requests: string[] = [];
  const server: Server = createServer((req, res) => {
    const path = req.url ?? "/";
    requests.push(`${req.method} ${path}`);
    if (path.startsWith("/gateway/aime/stream-query")) {
      if (options.loginExpired?.()) {
        const body = JSON.stringify({
          status: 401,
          error: "Unauthorized",
          status_code: -1935,
          status_msg: "未登陆,请登录后再试",
        });
        res.writeHead(401, { "content-type": "application/json" });
        res.end(body);
        return;
      }
      let query = "创新高";
      let raw = "";
      req.on("data", (chunk) => (raw += chunk));
      req.on("end", () => {
        try {
          const parsed = JSON.parse(raw || "{}");
          if (typeof parsed.question === "string" && parsed.question) {
            query = parsed.question;
          }
        } catch {
          // 保持默认问句
        }
        res.writeHead(200, { "content-type": "text/event-stream" });
        res.end(streamPayload(query));
      });
      return;
    }
    res.writeHead(404);
    res.end();
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      resolve({
        baseUrl: `http://127.0.0.1:${port}`,
        requests,
        stop: () =>
          new Promise<void>((done) => {
            server.close(() => done());
          }),
      });
    });
  });
}
