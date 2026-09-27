"""设置路由：问财 Cookie 的本地保存与状态查询。

响应只暴露"是否已配置"与脱敏长度，不返回 Cookie 内容；Cookie 不进入日志或批次记录。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _container(request: Request):
    return request.app.state.container


@router.get("")
def get_settings(request: Request) -> dict:
    container = _container(request)
    status = container.cookies.status()
    return {
        "wencaiAvailable": container.wencai_available,
        "wencaiMessage": container.wencai_message,
        "wencaiCookie": {"configured": status.configured, "length": status.length},
    }


@router.put("/wencai-cookie")
def save_cookie(request: Request, payload: dict) -> dict:
    cookie = str(payload.get("cookie") or "").strip()
    if not cookie:
        raise HTTPException(
            status_code=400,
            detail={"code": "empty_cookie", "message": "Cookie 不能为空"},
        )
    status = _container(request).cookies.save(cookie)
    return {"wencaiCookie": {"configured": status.configured, "length": status.length}}


@router.delete("/wencai-cookie")
def clear_cookie(request: Request) -> dict:
    _container(request).cookies.clear()
    return {"wencaiCookie": {"configured": False, "length": 0}}
