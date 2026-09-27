"""DailyScreen Lite 开发启动器（对比完整版 dailyscreen dev.py）。

在后端（uvicorn :8765）之外再起一个 Vite 开发服务器（:5173，热更新，/api 代理到 8765），
浏览器打开 5173 即开发入口。两个进程后台运行并把日志写入 data/runtime/dev/，
因此 ``start`` 后可以拿回终端，用 ``status`` 查看、``stop`` 停止。

用法::

    python dev.py                 # 等价于 start
    python dev.py start           # 启动后端 + 前端开发服务器
    python dev.py status          # 查看运行状态与访问地址
    python dev.py stop            # 停止两个进程
    python dev.py restart
    python dev.py build           # 构建前端产物（生产模式用）
    python dev.py test [pytest 参数...]

仅本机开发使用；生产/日常使用走 start.bat（后端直接托管前端 dist）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
BACKEND_PORT = 8765
FRONTEND_PORT = 5173
RUNTIME_DIR = PROJECT_ROOT / "data" / "runtime" / "dev"
STATE_PATH = RUNTIME_DIR / "services.json"

BACKEND_HEALTH = f"http://127.0.0.1:{BACKEND_PORT}/api/health"
FRONTEND_HOME = f"http://127.0.0.1:{FRONTEND_PORT}/"

# Windows 下新开进程组，便于整组结束（前端会派生 node 子进程）
_NEW_GROUP = (
    {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
)


def _python() -> Path:
    if os.name == "nt":
        candidate = PROJECT_ROOT / "backend" / ".venv" / "Scripts" / "python.exe"
    else:
        candidate = PROJECT_ROOT / "backend" / ".venv" / "bin" / "python"
    if not candidate.is_file():
        raise SystemExit(
            f"未找到后端虚拟环境：{candidate}\n"
            "请先：python -m venv backend\\.venv && "
            "backend\\.venv\\Scripts\\python.exe -m pip install -r backend\\requirements.txt && "
            "backend\\.venv\\Scripts\\python.exe -m pip install -e backend"
        )
    return candidate


def _pnpm() -> str:
    found = shutil.which("pnpm")
    if not found:
        raise SystemExit("未找到 pnpm；请先安装 pnpm 并在 frontend 执行 pnpm install。")
    return found


def _health(url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 400
    except (urllib.error.URLError, OSError):
        return False


def _wait_until(label: str, probe, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if probe():
            return True
        time.sleep(0.4)
    print(f"[dev] 等待 {label} 就绪超时", file=sys.stderr)
    return False


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _write_state(state: dict) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _spawn(name: str, command: list[str], cwd: Path, log_path: Path) -> int:
    """后台启动一个进程，stdout/stderr 落日志文件，返回 pid。"""
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    log = log_path.open("a", encoding="utf-8")
    log.write(f"\n===== {name} 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n")
    log.flush()
    process = subprocess.Popen(
        command,
        cwd=str(cwd),
        stdout=log,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT / "backend" / "src")},
        **_NEW_GROUP,
    )
    return process.pid


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        completed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        )
        return str(pid) in (completed.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def _start() -> int:
    state = _read_state()
    if _health(BACKEND_HEALTH) or _health(FRONTEND_HOME):
        print("[dev] 已有进程在运行；如需重启请用 `python dev.py restart`，或先 stop。")
        return _status()

    python = _python()
    pnpm = _pnpm()
    if not (PROJECT_ROOT / "frontend" / "node_modules").is_dir():
        raise SystemExit("frontend/node_modules 不存在；请先在 frontend 执行 pnpm install。")

    backend_pid = _spawn(
        "backend",
        [str(python), "-m", "dailyscreen_lite", "--host", "127.0.0.1", "--port", str(BACKEND_PORT)],
        PROJECT_ROOT,
        RUNTIME_DIR / "backend.log",
    )
    frontend_pid = _spawn(
        "frontend",
        [pnpm, "run", "dev"],
        PROJECT_ROOT / "frontend",
        RUNTIME_DIR / "frontend.log",
    )
    _write_state(
        {"backend": {"pid": backend_pid, "port": BACKEND_PORT}, "frontend": {"pid": frontend_pid, "port": FRONTEND_PORT}}
    )

    ok_backend = _wait_until("后端", lambda: _health(BACKEND_HEALTH))
    ok_frontend = _wait_until("前端开发服务器", lambda: _health(FRONTEND_HOME))
    if not (ok_backend and ok_frontend):
        print("[dev] 启动未完全就绪，日志见 data/runtime/dev/*.log", file=sys.stderr)
        return 1
    print(f"[dev] 开发入口：{FRONTEND_HOME.rstrip('/')}（热更新，/api 代理到 127.0.0.1:{BACKEND_PORT}）")
    print("[dev] 后端直连：http://127.0.0.1:%d" % BACKEND_PORT)
    print("[dev] 停止：python dev.py stop    查看：python dev.py status")
    return 0


def _status() -> int:
    state = _read_state()
    backend = state.get("backend", {})
    frontend = state.get("frontend", {})
    backend_up = _health(BACKEND_HEALTH)
    frontend_up = _health(FRONTEND_HOME)
    print(f"[dev] 后端 :{BACKEND_PORT}  {'运行中' if backend_up else '未运行'}"
          f"  pid={backend.get('pid', '-')}  日志=data/runtime/dev/backend.log")
    print(f"[dev] 前端 :{FRONTEND_PORT}  {'运行中' if frontend_up else '未运行'}"
          f"  pid={frontend.get('pid', '-')}  日志=data/runtime/dev/frontend.log")
    if backend_up and frontend_up:
        print(f"[dev] 开发入口：{FRONTEND_HOME.rstrip('/')}")
        return 0
    return 1


def _stop() -> int:
    state = _read_state()
    for name in ("frontend", "backend"):
        pid = int(state.get(name, {}).get("pid") or 0)
        if pid:
            _terminate(pid)
            print(f"[dev] 已停止 {name}（pid={pid}）")
    _write_state({})
    return 0


def _restart() -> int:
    _stop()
    time.sleep(1.0)
    return _start()


def _build() -> int:
    pnpm = _pnpm()
    return subprocess.run([pnpm, "run", "build"], cwd=str(PROJECT_ROOT / "frontend"), check=False).returncode


def _test(extra: list[str]) -> int:
    python = _python()
    return subprocess.run(
        [str(python), "-m", "pytest", *extra],
        cwd=str(PROJECT_ROOT / "backend"),
        check=False,
    ).returncode


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)

    # `test` 之后的参数原样透传给 pytest：交给 argparse 会把 -q/--lf 等选项
    # 当成未知选项拒绝（REMAINDER 不覆盖首个可选参数），因此在这里提前分流。
    if arguments and arguments[0] == "test":
        return _test(arguments[1:])

    parser = argparse.ArgumentParser(
        prog="dev.py",
        description="DailyScreen Lite 开发启动器（后端 + Vite 开发服务器）",
    )
    sub = parser.add_subparsers(dest="action")
    for action in ("start", "stop", "status", "restart", "build"):
        sub.add_parser(action)
    sub.add_parser("test", help="运行后端测试；`test` 之后的参数原样透传 pytest")
    args = parser.parse_args(arguments)

    action = args.action or "start"
    if action == "start":
        return _start()
    if action == "stop":
        return _stop()
    if action == "status":
        return _status()
    if action == "restart":
        return _restart()
    if action == "build":
        return _build()
    if action == "test":
        return _test([])
    parser.error(f"未知命令：{action}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
