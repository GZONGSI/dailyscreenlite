"""FastAPI 应用工厂：装配依赖、注册路由、托管前端构建产物。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dailyscreen_lite.app.container import Container, build_container
from dailyscreen_lite.domain.clock import Clock
from dailyscreen_lite.domain.errors import ImportRejected
from dailyscreen_lite.settings import Settings, load_settings


def create_app(settings: Settings | None = None, clock: Clock | None = None) -> FastAPI:
    resolved = settings or load_settings()
    container = build_container(resolved, clock)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 启动补更与每日 16:30 尝试更新都在应用内轻量线程完成
        container.start_background()
        try:
            yield
        finally:
            container.stop_background()

    app = FastAPI(title="DailyScreen Lite", version="0.1.0", lifespan=lifespan)
    app.state.container = container

    from dailyscreen_lite.app.routes import (
        classification,
        imports,
        notes,
        observations,
        quotes,
        settings as settings_routes,
        system,
    )

    app.include_router(system.router)
    app.include_router(imports.router)
    app.include_router(classification.router)
    app.include_router(observations.router)
    app.include_router(notes.router)
    app.include_router(quotes.router)
    app.include_router(settings_routes.router)

    @app.exception_handler(ImportRejected)
    async def _import_rejected(_: Request, exc: ImportRejected) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"code": exc.code, "message": exc.message, "detail": exc.detail},
        )

    _mount_frontend(app, resolved.frontend_dist)
    return app


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if not index.exists():
        @app.get("/")
        def _no_frontend() -> dict:
            return {
                "message": "前端尚未构建，请运行 frontend 构建命令或使用开发服务器。",
                "api": "/api/health",
            }

        return

    assets = dist / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    def _index() -> FileResponse:
        return FileResponse(index)

    @app.get("/{path:path}")
    def _spa(path: str) -> FileResponse:
        candidate = dist / path
        if candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index)
