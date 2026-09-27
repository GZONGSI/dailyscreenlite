"""HTTP 传输契约的公共约定：线格式别名、契约模型基类与业务失败信封。

DTO 仍按功能拥有（见各路由模块），这里只放跨功能相同的线格式规则，避免每条规则
各写一份：Python 侧写 snake_case，线格式保持既有 camelCase；业务失败的
`{"detail": {"message": ...}}` 信封由笔记、归类与观察三处共用。前端类型由这些模型经
`backend/tools/export_openapi.py` → `pnpm run gen:api` 生成。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


def to_camel(name: str) -> str:
    """DTO 字段别名：Python 侧写 snake_case，线格式保持既有的 camelCase。

    只做下划线分段的首字母大写，不动段内大小写（`security_ID` → `securityID`），
    避免把缩写或驼峰写成全小写。
    """
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


class WireContract(BaseModel):
    """线格式契约的公共配置：构造时仍可写 snake_case，序列化按 camelCase。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class FailureDetail(BaseModel):
    """业务失败的 detail：各路由统一用 `{"message": ...}` 包装。"""

    message: str


class FailurePayload(BaseModel):
    """业务失败响应：哪个状态码适用仍由各端点自己声明（`responses`）。"""

    detail: FailureDetail
