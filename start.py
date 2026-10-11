#!/usr/bin/env python3
"""AI Resume Generator —— 一键启动。

做四件事，让你双击就能用：
  1. 准备 Python 虚拟环境（不存在就建）
  2. 安装 / 更新依赖（requirements.txt 变了才重装）
  3. 挑一个可用端口，起服务
  4. 等健康检查通过后自动打开浏览器

另外顺手处理了一个真实踩过的坑：本机若设了 HTTP_PROXY，代理会拦截
localhost 请求（表现为 502）。脚本会为子进程设置 NO_PROXY，保证本地
LLM（如 Ollama）与本地服务都能直连。

用法：
    python start.py                  # 默认 8000 端口，自动开浏览器
    python start.py --port 9000      # 换端口
    python start.py --no-browser     # 不自动开浏览器
    python start.py --reload         # 开发模式（改代码自动重启）
    python start.py --reinstall      # 强制重装依赖
"""
from __future__ import annotations

import argparse
import hashlib
import os
import platform
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".deps-stamp"

IS_WINDOWS = platform.system() == "Windows"
MIN_PY = (3, 11)

# 这些包能 import 成功，就认为依赖已就绪
REQUIRED_MODULES = ["fastapi", "uvicorn", "sqlmodel", "httpx", "jinja2", "nh3"]


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def say(msg: str) -> None:
    print(f"  {msg}", flush=True)


def title(msg: str) -> None:
    print(f"\n=== {msg} ===", flush=True)


def die(msg: str, code: int = 1):
    print(f"\n[错误] {msg}\n", flush=True)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# 虚拟环境
# --------------------------------------------------------------------------- #
def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")


def running_in_venv() -> bool:
    """判断当前解释器是不是项目自己的 .venv。"""
    try:
        # normcase 兼容 Windows 的盘符/路径大小写差异
        return os.path.normcase(str(Path(sys.executable).resolve())) == os.path.normcase(
            str(venv_python().resolve())
        )
    except OSError:
        return False


#: 防止「重执行」无限递归（万一解释器比较判断出错）
_REEXEC_FLAG = "_RESUME_GENERATOR_REEXEC"


def ensure_venv() -> None:
    """确保 .venv 存在；若当前不在其中，则用它的解释器重新执行本脚本。"""
    if not venv_python().exists():
        title("首次运行：创建虚拟环境")
        say(f"位置：{VENV}")
        if sys.version_info < MIN_PY:
            die(f"需要 Python {MIN_PY[0]}.{MIN_PY[1]} 或更高版本，当前是 {sys.version.split()[0]}")
        try:
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        except subprocess.CalledProcessError as exc:
            die(f"创建虚拟环境失败：{exc}")
        say("虚拟环境已创建")

    if running_in_venv() or os.environ.get(_REEXEC_FLAG):
        return

    # 用 .venv 的解释器重新执行本脚本，保证依赖都装在隔离环境里。
    #
    # 这里**不能用 os.execv**：Windows 上它不会正确转义含空格的路径参数，
    # 会把 "...\2. 智\计算机\..." 这类路径拆坏，报 "can't open file"。
    # subprocess 传参数列表时会用 list2cmdline 正确加引号，跨平台都安全。
    env = os.environ.copy()
    env[_REEXEC_FLAG] = "1"
    try:
        result = subprocess.run(
            [str(venv_python()), str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=str(ROOT),
            env=env,
        )
    except KeyboardInterrupt:
        sys.exit(130)
    sys.exit(result.returncode)


def ensure_deps(force: bool = False) -> None:
    """依赖缺失或 requirements.txt 变化时安装。"""
    if not REQUIREMENTS.exists():
        die(f"找不到 {REQUIREMENTS}")

    digest = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    stamp = STAMP.read_text(encoding="utf-8").strip() if STAMP.exists() else ""

    if not force and stamp == digest:
        return

    title("安装依赖")
    say("首次安装或依赖有更新，正在安装（可能需要 1-2 分钟）…")
    cmd = [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q", "--disable-pip-version-check"]
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        die("依赖安装失败，请检查网络后重试。\n  提示：若在代理环境下，可先设置 NO_PROXY 或检查 pip 源。")

    # 校验关键包真的能 import
    probe = "import " + ", ".join(REQUIRED_MODULES)
    check = subprocess.run([sys.executable, "-c", probe], capture_output=True)
    if check.returncode != 0:
        die("依赖安装后仍无法导入，请手动执行：\n  " + " ".join(cmd[:4]))

    STAMP.write_text(digest, encoding="utf-8")
    say("依赖已就绪")


# --------------------------------------------------------------------------- #
# 端口
# --------------------------------------------------------------------------- #
def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


def find_port(preferred: int, host: str = "127.0.0.1", tries: int = 20) -> int:
    """从 preferred 开始找一个空闲端口。"""
    for offset in range(tries):
        candidate = preferred + offset
        if not port_in_use(candidate, host):
            return candidate
    die(f"{preferred} 起的 {tries} 个端口都被占用了，请用 --port 指定一个空闲端口")


# --------------------------------------------------------------------------- #
# 健康检查 / 浏览器
# --------------------------------------------------------------------------- #
def is_our_app(port: int, host: str = "127.0.0.1") -> bool:
    """判断该端口上跑的是不是本应用（而不是别的服务）。"""
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/health", timeout=2) as resp:
            return resp.status == 200 and b'"ok"' in resp.read()
    except Exception:
        return False


def wait_for_health(port: int, timeout: float = 40.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_our_app(port):
            return True
        time.sleep(0.4)
    return False


def open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def build_env(port: int) -> dict:
    """构造子进程环境。

    - 本地地址不走代理：系统若设了 HTTP_PROXY，代理会拦截 localhost 请求
      （表现为 502），会让 Ollama 等本地 LLM 与本地服务都不可用。
    - 注入 APP_PORT：让应用日志打印真实监听端口，而不是配置里的默认端口。
    """
    env = os.environ.copy()
    no_proxy = "localhost,127.0.0.1,::1,0.0.0.0"
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    if no_proxy not in existing:
        merged = f"{existing},{no_proxy}".strip(",") if existing else no_proxy
        env["NO_PROXY"] = merged
        env["no_proxy"] = merged
    env["APP_PORT"] = str(port)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Resume Generator 一键启动")
    parser.add_argument("--port", type=int, default=8000, help="监听端口（默认 8000，被占用时自动顺延）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")
    parser.add_argument("--reinstall", action="store_true", help="强制重新安装依赖")
    args = parser.parse_args()

    # 横幅放在 ensure_venv() 之后：重执行时父进程会直接退出、由子进程打印，
    # 否则会出现两次横幅。
    ensure_venv()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║   AI RESUME GENERATOR  ·  岗位定制简历生成器          ║")
    print("╚══════════════════════════════════════════════════════╝")

    ensure_deps(force=args.reinstall)

    title("启动服务")

    # 端口已被本应用占用 → 直接打开浏览器，不重复起服务
    if port_in_use(args.port, args.host) and is_our_app(args.port, args.host):
        url = f"http://{args.host}:{args.port}"
        say(f"检测到服务已在运行：{url}")
        if not args.no_browser:
            open_browser(url)
        return 0

    port = find_port(args.port, args.host)
    if port != args.port:
        say(f"{args.port} 被占用，改用 {port}")

    url = f"http://{args.host}:{port}"
    say(f"地址：{url}")
    say("（按 Ctrl+C 停止服务）")

    cmd = [
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--app-dir", str(BACKEND),
        "--host", args.host,
        "--port", str(port),
    ]
    if args.reload:
        cmd.append("--reload")
        say("模式：开发（代码改动会自动重启）")

    env = build_env(port)
    if env.get("NO_PROXY"):
        say("已设置 NO_PROXY，本地地址将绕过系统代理")

    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)

    # 后台等健康检查，通过后开浏览器；不阻塞主进程
    if not args.no_browser:
        def _open_when_ready() -> None:
            if wait_for_health(port):
                say("服务已就绪，正在打开浏览器…")
                open_browser(url)
            else:
                say(f"等待超时，请手动访问 {url}")

        import threading

        threading.Thread(target=_open_when_ready, daemon=True).start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n\n收到停止信号，正在关闭服务…", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print("已停止。\n", flush=True)
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
