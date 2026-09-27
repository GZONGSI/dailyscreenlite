"""问财 Cookie 的本地保存与读取。

Cookie 只写入本机数据目录下的设置文件（不进版本控制），读取时仅供请求头使用；
对外暴露的状态只包含"是否已配置"与脱敏后的长度，不返回任何凭据内容。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CookieStatus:
    configured: bool
    length: int


class CookieStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, cookie: str) -> CookieStatus:
        value = (cookie or "").strip().replace("\r", " ").replace("\n", " ")
        import re

        value = re.sub(r"\s+", " ", value)
        if not value:
            raise ValueError("Cookie 不能为空")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps({"cookie": value}, ensure_ascii=False), encoding="utf-8")
        return CookieStatus(configured=True, length=len(value))

    def load(self) -> str | None:
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        value = str(data.get("cookie", "")).strip()
        return value or None

    def status(self) -> CookieStatus:
        value = self.load()
        return CookieStatus(configured=value is not None, length=len(value) if value else 0)

    def clear(self) -> None:
        if self._path.exists():
            self._path.unlink()
