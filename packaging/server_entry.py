#!/usr/bin/env python3
"""PyInstaller 打包入口：以「sidecar」方式启动 FastAPI 服务。

桌面外壳（Tauri）会这样调用本程序：

    resume-server.exe --port 0

本程序做的事：
  1. 选一个空闲端口（``--port 0`` = 自动选；用 **bind 后取实际端口** 的方式，
     不用 connect 探测——connect 在对方 backlog 满时会误判端口空闲）；
  2. 把端口写进 ``APP_PORT`` 环境变量（让应用日志打印**真实**端口）；
  3. 在子线程里跑 uvicorn，主线程轮询 ``server.started``；
  4. 就绪后向 stdout 打印一行 ``APP_READY <port>``（这是与外壳之间的协议）；
  5. 同时把端口写到 ``<DATA_DIR>/port.txt``，作为外壳读 stdout 失败时的兜底；
  6. 阻塞等待「stdin EOF（父进程已退出）」或 ``server.should_exit``，然后优雅退出。

为什么必须 ``console=True`` 打包：windowed（无控制台）模式下 ``sys.stdout``
是 ``None``，外壳读不到 ``APP_READY``，窗口会永远停在占位页。Tauri spawn 子进程
时会带 ``CREATE_NO_WINDOW``，用户并不会看到黑框。

注意：这里**不做**虚拟环境 / 依赖安装（那是源码版 ``start.py`` 的职责）。
"""
from __future__ import annotations

import argparse
import multiprocessing
import os
import socket
import sys
import threading
import time
from pathlib import Path

#: 与 backend/app/main.py 的 APP_ID 保持一致（跨进程无法 import，故各存一份）。
APP_ID = "ai-resume-generator"

#: 与外壳约定的就绪协议前缀，格式：``APP_READY <port>``。
READY_PREFIX = "APP_READY"

#: 等待 uvicorn 完成启动的最长秒数。
STARTUP_TIMEOUT = 60.0

#: 优雅关闭时等待 uvicorn 线程收尾的秒数。
SHUTDOWN_TIMEOUT = 10.0


def _log(msg: str) -> None:
    """错误/诊断信息一律走 stderr，避免污染 stdout 上的 APP_READY 协议。"""
    print(msg, file=sys.stderr, flush=True)


def _ensure_sys_path() -> None:
    """保证 ``import app.main`` 可用。

    源码运行：调用方需设 ``PYTHONPATH=backend``，这里再兜一层 ``../backend``；
    打包运行：spec 把 ``backend`` 收进了 ``_MEIPASS``，故把 ``_MEIPASS`` 与
    ``_MEIPASS/backend`` 都加入 ``sys.path``。
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        candidates.extend([base, base / "backend"])
    else:
        candidates.append(Path(__file__).resolve().parent.parent / "backend")

    for candidate in candidates:
        if not candidate.is_dir():
            continue
        entry = str(candidate)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def _harden_local_networking() -> None:
    """本地回环访问必须绕过系统代理。

    本机若设了 ``http_proxy``，代理会拦截 ``127.0.0.1`` 请求（表现为 502），
    外壳的健康检查就会误判为「不是本应用」。同时统一 UTF-8，避免 Windows
    默认 GBK 让中文日志/文件名出问题。
    """
    no_proxy = "localhost,127.0.0.1,::1"
    existing = (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").strip()
    if no_proxy not in existing:
        merged = f"{existing},{no_proxy}".strip(",") if existing else no_proxy
        os.environ["NO_PROXY"] = merged
        os.environ["no_proxy"] = merged
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def pick_free_port(preferred: int = 0) -> int:
    """返回一个可用的本地端口。

    ``preferred > 0`` 时直接用它；否则 bind 到 ``("127.0.0.1", 0)`` 让内核分配，
    再读 ``getsockname()`` 拿到**实际**端口后立刻 close。

    为什么用 bind 而不是 connect 探测：本项目刚踩过 connect 的坑——对方
    「listen 但 backlog 已满」时 connect 会被 RST 拒绝，从而把**已占用**的端口
    误判为空闲。bind 才是「我能不能起来」的真判据。
    """
    if preferred > 0:
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_port_file(data_dir: Path, port: int) -> None:
    """把端口写到 ``<DATA_DIR>/port.txt``，供外壳在 stdout 解析失败时兜底。

    写失败（磁盘只读 / 目录被删）只告警，不影响服务本身。
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "port.txt").write_text(str(port), encoding="utf-8")
    except OSError as exc:
        _log(f"[warn] 无法写入端口文件 {data_dir / 'port.txt'}：{exc}")


def _stdin_is_watchable() -> bool:
    """stdin 是否值得监听 EOF。

    父进程（Tauri）把 stdin 接成管道：父进程一死管道即 EOF，据此及时退出，
    避免成为孤儿进程。但若 stdin 被重定向到 NUL / 普通文件（脚本、计划任务、
    CI 常见），EOF 会**立刻**发生——那不代表父进程已死，此时绝不能退出。
    """
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, ValueError, OSError):
        return False

    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
            if handle in (0, -1):
                return False
            mode = ctypes.c_uint32(0)
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)) != 0:
                return True  # 控制台
            return kernel32.GetFileType(handle) == 3  # 3 = FILE_TYPE_PIPE
        except Exception:  # pragma: no cover - 极端环境下降级为「不监听」
            return False

    try:
        import stat

        st = os.fstat(fd)
        return os.isatty(fd) or stat.S_ISFIFO(st.st_mode)
    except OSError:  # pragma: no cover
        return False


def _watch_stdin(stop_event: threading.Event) -> None:
    """阻塞读 stdin，读到 EOF 就置位 stop_event。"""
    try:
        while True:
            chunk = sys.stdin.readline()
            if chunk == "":
                break
    except Exception:  # 管道被强制关闭 / 编码错误都不该让进程崩
        pass
    stop_event.set()


def _parse_port(value: str) -> int:
    """``--port`` 的类型校验：0（自动）或 1-65535。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("端口必须是 0（自动）或 1-65535 之间的整数") from None
    if port < 0 or port > 65535:
        raise argparse.ArgumentTypeError("端口必须是 0（自动）或 1-65535 之间的整数")
    return port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="resume-server",
        description="AI Resume Generator 后端 sidecar（供桌面外壳调用）",
    )
    parser.add_argument(
        "--port",
        type=_parse_port,
        default=0,
        help="监听端口；0 表示自动选择一个空闲端口（默认）",
    )
    args = parser.parse_args(argv)

    _harden_local_networking()
    _ensure_sys_path()

    port = pick_free_port(args.port)
    # 让应用日志打印真实端口，而不是配置里的默认 8000
    os.environ["APP_PORT"] = str(port)

    try:
        import uvicorn

        from app.config import DATA_DIR
        from app.main import app
    except Exception as exc:  # pragma: no cover - 打包漏收模块时的可读报错
        _log(f"[fatal] 无法加载后端应用：{exc}")
        _log("  提示：打包时可能漏收了模块，请检查 resume-server.spec 的 hiddenimports。")
        return 2

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,   # 访问日志会污染 stdout 上的 APP_READY 协议
        log_config=None,    # 不覆盖宿主进程的日志配置
    )
    server = uvicorn.Server(config)

    server_thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    server_thread.start()

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if server.started:
            break
        if not server_thread.is_alive():
            _log("[fatal] uvicorn 未能启动（端口可能刚被别的程序抢占），请重试。")
            return 3
        time.sleep(0.05)
    else:
        _log(f"[fatal] uvicorn 启动超时（{STARTUP_TIMEOUT:.0f}s），已放弃。")
        server.should_exit = True
        server_thread.join(timeout=SHUTDOWN_TIMEOUT)
        return 4

    # ---- 与外壳的就绪协议 ----
    write_port_file(DATA_DIR, port)
    print(f"{READY_PREFIX} {port}", flush=True)
    _log(f"[info] {APP_ID} 已就绪 -> http://127.0.0.1:{port}")

    stop_event = threading.Event()
    watch_stdin = _stdin_is_watchable()
    if watch_stdin:
        threading.Thread(target=_watch_stdin, args=(stop_event,), name="stdin", daemon=True).start()

    try:
        while not stop_event.is_set() and not server.should_exit:
            if not server_thread.is_alive():
                break
            stop_event.wait(0.25)
    except KeyboardInterrupt:
        _log("[info] 收到中断信号，正在关闭…")

    server.should_exit = True
    server_thread.join(timeout=SHUTDOWN_TIMEOUT)
    if server_thread.is_alive():  # pragma: no cover - 极端情况
        _log("[warn] uvicorn 未能在超时内退出，强制结束进程。")
        return 0
    _log("[info] 已停止。")
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
