"""应用状态与健康检查路由。"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api", tags=["system"])


@router.get("/health")
def health(request: Request) -> dict:
    container = request.app.state.container
    status = container.cookies.status()
    return {
        "status": "ok",
        "importDate": container.clock.today().isoformat(),
        "securitiesLoaded": container.securities_loaded,
        "securitiesCount": container.securities_count,
        "securitiesMessage": container.securities_message,
        "wencaiAvailable": container.wencai_available,
        "wencaiMessage": container.wencai_message,
        "wencaiCookieConfigured": status.configured,
    }
