"""前端走查用的「自起隔离服务」辅助。

为什么需要这个模块：
`ui_check` / `e2e_verify` 早期默认打 `http://127.0.0.1:8000`，而 8000 是开发服务、
用的是**用户真实库** `data/app.db`。跑一次走查就会往用户库里写几十上百条测试对话
（实测单日污染 200+ 条，清理起来很麻烦）。

现在统一改为：**默认自起一个隔离 DB 的独立端口服务**；只有显式设置 `BASE_URL`
时才使用外部服务（并打印提示，让使用者知道自己在打哪个库）。
"""
from __future__ import annotations

import contextlib
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx

ROOT = Path(__file__).resolve().parent.parent.parent   # 项目根目录


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _wait_ready(base: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{base}/api/health", timeout=2.0, trust_env=False)
            if r.status_code == 200:
                return True
        except Exception:
            time.sleep(0.4)
    return False


def spawn_isolated_server(name: str, port: int) -> tuple[subprocess.Popen, str, Path]:
    """启动隔离服务，返回 (进程, base_url, 数据库文件)。

    供「会在退出前调用 os._exit()、无法依赖 with 清理」的脚本使用；
    这类脚本必须在退出前自行调用 stop_isolated_server()。
    """
    db_file = ROOT / "data" / f"_ui_{name}.db"
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_file) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass

    if not _port_free(port):
        raise RuntimeError(f"端口 {port} 已被占用，无法启动隔离服务")

    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_file.as_posix()}"
    # 本机设了 http_proxy 时会拦截 localhost 请求，必须放行
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--app-dir", "backend",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    if not _wait_ready(base):
        stop_isolated_server(proc, db_file)
        raise RuntimeError(f"隔离服务未在预期时间内就绪：{base}")
    print(f"[隔离服务] {base}  DB={db_file.name}（不会写入用户库）")
    return proc, base, db_file


def stop_isolated_server(proc: subprocess.Popen, db_file: Path) -> None:
    """终止隔离服务并清理其数据库文件。可重复调用。"""
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        f = Path(str(db_file) + suffix)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass


@contextlib.contextmanager
def isolated_server(name: str, port: int) -> Iterator[str]:
    """with 用法：启动隔离服务并 yield base_url，退出时自动清理。"""
    proc, base, db_file = spawn_isolated_server(name, port)
    try:
        yield base
    finally:
        stop_isolated_server(proc, db_file)
