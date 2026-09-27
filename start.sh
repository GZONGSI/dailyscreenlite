#!/usr/bin/env bash
# DailyScreen Lite 本机启动脚本（macOS/Linux 或 Git Bash）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/backend/.venv/bin/python"
[ -x "$VENV" ] || VENV="$ROOT/backend/.venv/Scripts/python.exe"
if [ ! -x "$VENV" ]; then
  echo "[DailyScreen Lite] 未找到后端虚拟环境，请先创建 backend/.venv 并安装 requirements.txt"; exit 1
fi
if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  echo "[DailyScreen Lite] 前端尚未构建..."
  (cd "$ROOT/frontend" && pnpm install && pnpm run build)
fi
echo "[DailyScreen Lite] 启动本机服务： http://127.0.0.1:8765"
exec "$VENV" -m dailyscreen_lite --host 127.0.0.1 --port 8765
