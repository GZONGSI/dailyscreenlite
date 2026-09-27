@echo off
REM DailyScreen Lite 本机启动脚本（双击运行）
setlocal

set "ROOT=%~dp0"
set "VENV=%ROOT%backend\.venv\Scripts\python.exe"

if not exist "%VENV%" (
  echo [DailyScreen Lite] 未找到后端虚拟环境：%VENV%
  echo 请先运行：python -m venv backend\.venv
  echo          backend\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
  pause
  exit /b 1
)

if not exist "%ROOT%frontend\dist\index.html" (
  echo [DailyScreen Lite] 前端尚未构建，正在构建...
  pushd "%ROOT%frontend"
  call pnpm install
  call pnpm run build
  popd
)

echo [DailyScreen Lite] 启动本机服务： http://127.0.0.1:8765
start "" "http://127.0.0.1:8765"
"%VENV%" -m dailyscreen_lite --host 127.0.0.1 --port 8765

endlocal
