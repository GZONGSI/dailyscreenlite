"""hexin-v 令牌生成（外部 Node 生成器）。

问财接口要求随请求变化的 hexin-v 头，其算法以 JS 下发，无法用纯 Python 复刻。
这里通过可配置的 Node 生成器脚本获取令牌，并在 TTL 内复用；生成器缺失或执行
失败时明确报错，不静默降级成伪造令牌（伪造令牌必定 401）。
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from dailyscreen_lite.domain.errors import WencaiTokenUnavailable

TOKEN_TTL_SECONDS = 300


class NodeTokenProvider:
    def __init__(self, bundle: Path, *, node: str | None = None, ttl_seconds: int = TOKEN_TTL_SECONDS) -> None:
        self._bundle = bundle
        self._node = node or "node"
        self._ttl = ttl_seconds
        self._value: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        now = time.time()
        if self._value and now < self._expires_at:
            return self._value
        if not self._bundle.exists():
            raise WencaiTokenUnavailable(
                "缺少问财令牌生成器，无法发起链接获取",
                detail=f"未找到生成器：{self._bundle}",
            )
        if shutil.which(self._node) is None:
            raise WencaiTokenUnavailable(
                "未找到 Node.js，无法生成问财令牌",
                detail="请安装 Node.js 或配置 DSLITE_WENCAI_BUNDLE 指向可用的生成器",
            )
        try:
            completed = subprocess.run(
                [self._node, str(self._bundle)],
                capture_output=True,
                encoding="utf-8",
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WencaiTokenUnavailable(
                "问财令牌生成器执行失败",
                detail=type(exc).__name__,
            ) from exc
        value = (completed.stdout or "").strip()
        if completed.returncode != 0 or not value:
            raise WencaiTokenUnavailable(
                "问财令牌生成器未返回有效令牌",
                detail=(completed.stderr or "").strip()[:120] or f"退出码 {completed.returncode}",
            )
        self._value = value
        self._expires_at = now + self._ttl
        return value
