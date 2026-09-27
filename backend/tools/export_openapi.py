"""导出 OpenAPI 契约：后端 DTO → 确定性 schema → 前端生成类型。

生成链路：
    后端 DTO（`app/routes/*.py` 的请求/响应模型）
      → 本工具（隔离导出确定性 OpenAPI，写 `frontend/src/api/generated/openapi.json`）
      → `pnpm run gen:api`（openapi-typescript，写同目录 `schema.d.ts`）
      → 前端按功能引用生成类型（当前：`frontend/src/api/notes.ts`）

隔离要求（不能碰运行数据、不能依赖供应商现场可用）：
- 数据目录用系统临时目录，证券库只读交付快照，行情用空夹具，不装配问财令牌；
- 不启用调度与每日状态来源；只构造应用并取 `openapi()`，不进入 lifespan（不启后台线程）；
- 只导出 `/api` 路径：本机是否存在 `frontend/dist` 会额外注册 SPA 路由，
  不切开就会让生成结果随构建产物变化；
- 输出排序、不含生成时间戳，重复运行必须逐字一致。

用法：
    python backend/tools/export_openapi.py                # 写入默认输出
    python backend/tools/export_openapi.py --out PATH     # 写入指定文件
    python backend/tools/export_openapi.py --check        # 只检查生成物是否与当前 DTO 一致
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT / "src"))

from dailyscreen_lite.app.main import create_app  # noqa: E402
from dailyscreen_lite.settings import REPO_ROOT, Settings  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "frontend" / "src" / "api" / "generated" / "openapi.json"
DELIVERED_SNAPSHOT = REPO_ROOT / "data" / "securities" / "initial_snapshot.json"


def isolated_settings(workspace: Path) -> Settings:
    """隔离配置：临时数据目录 + 交付证券库 + 空行情夹具，禁用调度、状态来源与问财。"""
    workspace.mkdir(parents=True, exist_ok=True)
    fixture = workspace / "empty_quotes.json"
    fixture.write_text("{}", encoding="utf-8")
    return Settings(
        data_dir=workspace / "data",
        securities_snapshot=DELIVERED_SNAPSHOT,
        quotes_fixture=fixture,
        # 问财令牌装配发生在容器构造时：这里不装，避免依赖本机生成器是否存在
        wencai_token_bundle=None,
        market_status_fixture=None,
        market_status_enabled=False,
        update_schedule_enabled=False,
    )


def build_schema(workspace: Path) -> dict:
    """在隔离目录里装配应用并取 schema；只保留 /api 路径。"""
    app = create_app(isolated_settings(workspace))
    schema = app.openapi()
    paths = {path: item for path, item in sorted(schema["paths"].items()) if path.startswith("/api")}
    return {
        "openapi": schema["openapi"],
        "info": schema["info"],
        "paths": paths,
        "components": schema["components"],
    }


def render(schema: dict) -> str:
    """确定性文本：键排序、固定缩进、LF 换行、无生成时间戳。"""
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _read_text(path: Path) -> str:
    """按 LF 比较：仓库检出可能把换行转成 CRLF，那不是生成差异。"""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def matches_current(path: Path, text: str) -> bool:
    """生成物是否与当前 DTO 导出的文本一致（缺失也算不一致）。"""
    return path.exists() and _read_text(path) == text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出笔记 DTO 所在应用的 OpenAPI 契约")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"输出文件（默认 {DEFAULT_OUT}）")
    parser.add_argument("--check", action="store_true", help="只检查生成物是否与当前 DTO 一致")
    args = parser.parse_args(argv)
    out = args.out.resolve()

    with tempfile.TemporaryDirectory(prefix="dslite-openapi-") as tmp:
        text = render(build_schema(Path(tmp)))

    if args.check:
        if not out.exists():
            print(f"缺少生成物：{out}\n请运行 python backend/tools/export_openapi.py 后重试。")
            return 1
        if not matches_current(out, text):
            print(f"生成物已过期：{out}\n请运行 python backend/tools/export_openapi.py 后重试。")
            return 1
        print(f"契约与生成物一致：{out}")
        return 0

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    print(f"已写入 {out}（{len(json.loads(text)['paths'])} 个 /api 路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
