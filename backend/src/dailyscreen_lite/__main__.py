"""启动入口：python -m dailyscreen_lite 或 uvicorn 直接调用。"""

from __future__ import annotations

import argparse

import uvicorn

from dailyscreen_lite.app.main import create_app
from dailyscreen_lite.settings import load_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="启动 DailyScreen Lite 本机服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    args = parser.parse_args()

    if args.reload:
        uvicorn.run(
            "dailyscreen_lite.app.main:create_app",
            host=args.host,
            port=args.port,
            reload=True,
            factory=True,
        )
        return

    app = create_app(load_settings())
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
