#!/usr/bin/env python3
"""端口守卫自检（port_guard_check）——验证 start.py 的端口占用行为。

产品语义（本轮演进）：**默认自动顺延 + 醒目告知**；`--strict-port` 才是「被占即报错」；
`--auto-port` 保留为兼容别名（自动顺延已是默认行为）。安全底座不变：就绪判定必须通过
`/api/health` 的 `app` 身份校验，绝不把别人的服务当成本应用。

锁定以下契约：

  a. 默认自动顺延   → 8261 被占，**不带任何端口参数**启动 → 顺延到 8262，health 200 且
                      JSON `app` 标识匹配；原端口 8261 上没有本应用；进程树杀干净、端口释放
  b. 严格模式       → 8263 被占 + `--strict-port` → 退出码非 0、含「占用」、**无**顺延措辞
  c. IPv4 占用      → `--strict-port` 时报错；含占用者(PID)、「不会静默」理由与出路
  d. IPv6-only 占用 → 默认应顺延、`--strict-port` 应报错（修复前只探 IPv4 会漏检）
  e. --auto-port    → 兼容别名仍被接受且生效（8264 → 8265）
  f. 本应用已占用   → 复用：退出码 0、提示「已在运行」、不重复起服务
  g. 伪装服务       → 只返回 {"ok": true}、无 app 标识的服务不被复用（默认顺延）；严格则报错
  h. 竞态兜底       → 已退出子进程使 wait_for_health 在 1 秒内失败、不开浏览器
  i. bat 严格路径   → `start.bat --strict-port` 失败时 [ERROR] 可见、退出码非 0（仅 Windows）
  j. bat 默认顺延   → `start.bat --port 8269`（不带参数）→ 顺延到 8270 并就绪（仅 Windows）
  k. bat 退出码透传 → 复用已有本应用 → `start.bat` 退出码 0（仅 Windows）

用法（项目根目录，用项目虚拟环境的 Python 跑）：
    .venv/Scripts/python.exe backend/tests/port_guard_check.py

隔离约定（与其它套件一致）：
- 端口只用 8231+ / 8261+，绝不碰 8000（用户开发服务端口）；
- 隔离库只落在 data/_port_guard*.db / data/_ui_port_guard*.db / data/_pg_bat*.db（下划线前缀），
  测完即删；绝不触碰 data/app.db。
"""
from __future__ import annotations

import http.server
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (str(ROOT), str(ROOT / "backend"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import start as start_mod  # noqa: E402  端口守卫实现（被测对象）
from _ui_server import isolated_server  # noqa: E402  自起隔离服务的辅助

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# 测试基建
# --------------------------------------------------------------------------- #
class DummyListener:
    """测试用的假占用者：只在指定协议栈上监听端口。"""

    def __init__(self, port: int, ipv6_only: bool = False) -> None:
        self.port = port
        self._stop = threading.Event()
        if ipv6_only:
            self.sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            self.sock.bind(("::1", port))
        else:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.bind(("127.0.0.1", port))
        self.sock.listen(8)
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
                conn.close()
            except socket.timeout:
                continue
            except OSError:
                break

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


class FakeHealthServer:
    """在指定端口起一个假 HTTP 服务，`/api/health` 返回固定 JSON。

    用于验证「伪装服务」：它可能恰好返回 `{"ok": true}`，但**没有 app 标识**。
    修复后启动器必须据此判定「不是本应用」，默认应顺延而不是复用、更不能打开别人的页面。
    """

    def __init__(self, port: int, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")
        body = self._body

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args) -> None:  # 静默
                pass

        self.httpd = socketserver.TCPServer(("127.0.0.1", port), _Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass


def child_env() -> dict:
    """子进程环境：强制 UTF-8，避免管道解码把中文断言打歪。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _isolated_env(db_name: str) -> dict:
    env = child_env()
    env["DATABASE_URL"] = f"sqlite:///{(ROOT / 'data' / db_name).as_posix()}"
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    return env


def run_start(args: list[str], timeout: float = 240.0) -> subprocess.CompletedProcess:
    """同步跑 start.py，返回 CompletedProcess（文本输出按 UTF-8 解码）。"""
    return subprocess.run(
        [sys.executable, str(ROOT / "start.py"), *args],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=child_env(),
    )


def popen_start(args: list[str], env: dict) -> subprocess.Popen:
    """后台跑 start.py（stdout 合并 stderr，按行读）。"""
    return subprocess.Popen(
        [sys.executable, str(ROOT / "start.py"), *args],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", env=env,
        stdin=subprocess.DEVNULL, start_new_session=True,
    )


def kill_tree(proc: subprocess.Popen) -> None:
    """杀掉进程树：start.py 若已起 uvicorn 子进程，也必须杀掉，否则端口不释放。"""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def wait_port_free(port: int, timeout: float = 20.0) -> bool:
    """等待端口彻底释放（双栈探测）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not start_mod.port_in_use(port):
            return True
        time.sleep(0.3)
    return False


def isolate_db_files() -> list[Path]:
    """本脚本约定产生的隔离库文件（下划线前缀）。"""
    data = ROOT / "data"
    return (list(data.glob("_port_guard*.db*"))
            + list(data.glob("_ui_port_guard*.db*"))
            + list(data.glob("_pg_bat*.db*")))


def cleanup_db_files() -> None:
    for f in isolate_db_files():
        try:
            f.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 通用执行器
# --------------------------------------------------------------------------- #
def _run_bump(busy_port: int, next_port: int, db_name: str,
              extra_args: tuple[str, ...] = ()) -> str:
    """在 busy_port **已被调用方占用**的前提下，启动 start.py（默认顺延），
    断言顺延到 next_port 并就绪（health 200 且 app 标识匹配），返回收集到的输出。

    内部负责杀进程树、删隔离库、等待 next_port 释放；不负责 busy_port 的监听器。
    """
    proc = popen_start(["--port", str(busy_port), *extra_args], _isolated_env(db_name))
    lines: list[str] = []
    try:
        deadline = time.time() + 120
        bumped = up = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            lines.append(line)
            print(line)
            if "顺延" in line and str(next_port) in line:
                bumped = True
            if bumped and start_mod.is_our_app(next_port):
                up = True
                break
        assert bumped, f"应打印顺延提示（含新端口 {next_port}）"
        assert up, f"顺延后的 {next_port} 应就绪（health 200 且 app 标识匹配）"
        text = "\n".join(lines)
        assert f"不在 {busy_port} 上" in text, "顺延提示必须醒目：本服务不在原端口上"
        assert "将打开 http://" in text, "应告知实际将打开的地址"
        assert start_mod.is_our_app(busy_port) is False, f"{busy_port} 上不应出现本应用"
        return text
    finally:
        kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        assert wait_port_free(next_port), f"顺延端口 {next_port} 未释放"
        cleanup_db_files()


def _run_strict(busy_port: int, db_name: str) -> str:
    """在 busy_port 已被占用时跑 `--strict-port`，断言报错且**无**顺延措辞。"""
    result = run_start(["--port", str(busy_port), "--strict-port", "--no-browser"])
    out = result.stdout + result.stderr
    print(result.stdout.rstrip())
    assert result.returncode != 0, "严格模式下端口被占应非 0 退出"
    assert "占用" in out, "应说明端口被占用"
    assert "顺延" not in out, "严格模式不应出现顺延措辞"
    return out


# --------------------------------------------------------------------------- #
# 场景
# --------------------------------------------------------------------------- #
def scenario_default_auto_port() -> bool:
    """a. 默认（不带任何端口参数）：8261 被占 → 自动顺延到 8262 并就绪。"""
    busy, nxt = 8261, 8262
    dummy = DummyListener(busy)
    try:
        text = _run_bump(busy, nxt, "_port_guard.db")
        assert f"顺延到 {nxt}" in text
        print(f"[通过] 默认自动顺延：{busy} → {nxt}，health 200 且 app 标识匹配")
        return True
    finally:
        dummy.close()
        assert wait_port_free(busy), f"原端口 {busy} 未释放"


def scenario_strict_port() -> bool:
    """b. 严格模式：8263 被占 + --strict-port → 报错，不顺延。"""
    busy = 8263
    dummy = DummyListener(busy)
    try:
        out = _run_strict(busy, "_port_guard_strict.db")
        assert str(busy) in out
        print(f"[通过] 严格模式：{busy} 被占即报错，未顺延")
        return True
    finally:
        dummy.close()


def scenario_ipv4_busy() -> bool:
    """c. IPv4 占用 + --strict-port → 报错（含占用者定位单元校验与报错要素）。"""
    port = 8231
    dummy = DummyListener(port)
    try:
        # 单元级：占用者定位应能拿到本测试进程自己的 PID（dummy 就在本进程里）
        owners = start_mod.find_port_owner(port)
        assert any(o.get("pid") == os.getpid() for o in owners), \
            f"find_port_owner 未定位到本进程 PID: {owners}"
        print(f"[单元] find_port_owner({port}) → {owners}")

        result = run_start(["--port", str(port), "--strict-port", "--no-browser"])
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert result.returncode != 0, "严格模式下端口被占应非 0 退出"
        assert str(port) in out, "报错应包含端口号"
        assert "占用" in out, "报错应说明端口被占用"
        assert "不会静默" in out, "报错应包含「不会静默换端口」的理由"
        assert "--port" in out, "报错应给出 --port 这个出路"
        assert "顺延" not in out, "严格模式不应出现顺延措辞"
        print(f"[通过] IPv4 占用 + --strict-port：退出码 {result.returncode}，报错要素齐全")
        return True
    finally:
        dummy.close()


def scenario_ipv6_busy() -> bool:
    """d. IPv6-only 占用：默认应顺延（8232→8233）、--strict-port 应报错。"""
    port = 8232
    # ---- 默认顺延 ----
    dummy = DummyListener(port, ipv6_only=True)
    try:
        # 前置确认场景成立：IPv4 上确实空闲、只有 [::1] 在监听（只探 IPv4 会漏检的复现条件）
        assert start_mod._connect_ok("127.0.0.1", port, socket.AF_INET) is False, \
            "前置不成立：127.0.0.1 上不应有监听"
        assert start_mod._connect_ok("::1", port, socket.AF_INET6) is True, \
            "前置不成立：[::1] 上应有监听"
        owners = start_mod.find_port_owner(port)
        assert any(o.get("pid") == os.getpid() for o in owners), \
            f"IPv6-only 占用未被 netstat 定位到: {owners}"
        print(f"[单元] find_port_owner({port}) → {owners}")
        _run_bump(port, 8233, "_port_guard_v6.db")
        print(f"[通过] IPv6-only 默认顺延：{port} → 8233")
    finally:
        dummy.close()
        assert wait_port_free(port), f"原端口 {port} 未释放"

    # ---- 严格报错 ----
    dummy2 = DummyListener(port, ipv6_only=True)
    try:
        _run_strict(port, "_port_guard_v6.db")
        print("[通过] IPv6-only + --strict-port：报错，未顺延")
        return True
    finally:
        dummy2.close()


def scenario_auto_port_compat() -> bool:
    """e. --auto-port 兼容别名：8264 被占 → 仍被接受并顺延到 8265。"""
    busy, nxt = 8264, 8265
    dummy = DummyListener(busy)
    try:
        _run_bump(busy, nxt, "_port_guard_compat.db", extra_args=("--auto-port",))
        print(f"[通过] --auto-port 兼容别名：{busy} → {nxt}")
        return True
    finally:
        dummy.close()
        assert wait_port_free(busy), f"原端口 {busy} 未释放"


def scenario_reuse() -> bool:
    """f. 本应用已占用 8266 → 复用：退出码 0、提示「已在运行」、不起第二个服务。"""
    port = 8266
    with isolated_server("port_guard_reuse", port) as base:
        before = start_mod.find_port_owner(port)
        assert before, "隔离服务应已在监听（场景前置不成立）"
        result = run_start(["--port", str(port), "--no-browser"])
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert result.returncode == 0, "复用已有服务应返回 0"
        assert "已在运行" in out, "应提示「检测到服务已在运行」"
        after = start_mod.find_port_owner(port)
        assert len(after) == len(before) >= 1, \
            f"不应再起第二个服务：before={before} after={after}"
        print(f"[通过] 复用已有服务 {base}，未重复起服务（listener 数不变）")
    return True


def scenario_fake_service() -> bool:
    """g. 伪装服务（{"ok": true} 无 app）：默认不顺延复用（应顺延）；严格应报错。"""
    port = 8267
    srv = FakeHealthServer(port, {"ok": True})
    try:
        assert start_mod.port_in_use(port), "前置不成立：8267 应有假服务监听"
        assert start_mod.is_our_app(port) is False, \
            "强身份校验失败：只有 ok、没有 app 标识的伪装服务被误判为本应用"
        # 默认：绝不能复用（无「已在运行」），应顺延到 8268
        text = _run_bump(port, 8268, "_port_guard_fake.db")
        assert "已在运行" not in text, "绝不能把伪装服务误判为本应用（不得提示「已在运行」）"
        print("[通过] 伪装服务默认未被复用，顺延到 8268")
        # 严格：报错
        _run_strict(port, "_port_guard_fake.db")
        print("[通过] 伪装服务 + --strict-port：报错，未顺延")
        return True
    finally:
        srv.close()


def scenario_race_guard() -> bool:
    """h. 竞态兜底：已退出的子进程 → wait_for_health 在 1 秒内返回失败，且不打开浏览器。"""
    port = 8270
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()  # 确保子进程已退出
    called = {"n": 0}
    orig_open = start_mod.open_browser
    start_mod.open_browser = lambda url: called.__setitem__("n", called["n"] + 1)
    try:
        t0 = time.time()
        ok = start_mod.wait_for_health(port, timeout=5, proc=p)
        dt = time.time() - t0
        print(f"[单元] wait_for_health(port={port}, timeout=5, proc=exited) -> ok={ok} 用时 {dt:.3f}s")
        assert ok is False, "已退出的子进程不应判定为就绪"
        assert dt < 1.0, f"应在 1 秒内返回失败（证明不等满超时），实际 {dt:.2f}s"
        assert called["n"] == 0, "失败分支不得打开浏览器"
        print(f"[通过] 竞态兜底：{dt:.3f}s 内返回失败，未打开浏览器")
        return True
    finally:
        start_mod.open_browser = orig_open
        try:
            p.kill()
        except Exception:
            pass


def scenario_bat() -> bool:
    """i. start.bat 严格路径（Windows）：--strict-port + 端口被占 → [ERROR]、退出码非 0。

    bat 失败路径有 pause；用 stdin=DEVNULL 喂空输入使其立即继续，不会卡死。
    """
    port = 8236
    dummy = DummyListener(port)
    try:
        result = subprocess.run(
            ["cmd", "/c", "start.bat", "--port", str(port), "--strict-port", "--no-browser"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
            stdin=subprocess.DEVNULL, env=child_env(),
        )
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert "[ERROR]" in out, "start.bat 失败路径应打印 [ERROR]"
        assert result.returncode != 0, "退出码应非 0"
        assert str(port) in out, "输出应能看到端口号（经 %* 透传）"
        assert "顺延" not in out, "严格模式不应出现顺延措辞"
        print(f"[通过] start.bat --strict-port 路径 [ERROR] 可见，退出码 {result.returncode}")
        return True
    finally:
        dummy.close()


def scenario_bat_auto_port() -> bool:
    """j. bat 默认顺延（Windows）：占住 8269 → start.bat（不带参数）→ 顺延到 8270 并就绪。"""
    busy, nxt = 8269, 8270
    dummy = DummyListener(busy)
    env = _isolated_env("_pg_bat.db")
    creationflags = 0x00000200 if IS_WINDOWS else 0  # CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        ["cmd", "/c", "start.bat", "--port", str(busy)],
        cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        env=env, creationflags=creationflags,
    )
    lines: list[str] = []
    try:
        deadline = time.time() + 120
        bumped = up = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            lines.append(line)
            print(line)
            if "顺延" in line and str(nxt) in line:
                bumped = True
            if bumped and start_mod.is_our_app(nxt):
                up = True
                break
        assert bumped, "start.bat 默认应自动顺延并打印提示（含 8270）"
        assert up, "顺延后的服务应在 8270 就绪（health 200 且 app 标识匹配）"
        text = "\n".join(lines)
        assert f"不在 {busy} 上" in text, "顺延提示必须醒目：本服务不在原端口上"
        print(f"[通过] bat 默认已顺延到 {nxt}，health 200 且 app 标识匹配")
        return True
    finally:
        kill_tree(proc)
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
        try:
            proc.stdout.close()
        except Exception:
            pass
        dummy.close()
        assert wait_port_free(nxt), f"顺延端口 {nxt} 未释放"
        cleanup_db_files()


def scenario_bat_reuse() -> bool:
    """k. bat 退出码 0 透传：复用已有本应用 → start.bat 应退出 0。"""
    port = 8235
    with isolated_server("port_guard_bat_reuse", port) as base:
        result = subprocess.run(
            ["cmd", "/c", "start.bat", "--port", str(port), "--no-browser"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
            stdin=subprocess.DEVNULL, env=child_env(),
        )
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert result.returncode == 0, f"复用已有本应用时 bat 应退出 0，实际 {result.returncode}"
        assert "已在运行" in out, "应提示「检测到服务已在运行」"
        print(f"[通过] bat 复用已有本应用 {base}，退出码 0（验证 bat 退出码 0 透传）")
    return True


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 68)
    print("端口守卫自检 port_guard_check（隔离端口 8231+/8261+，不碰 8000 / data/app.db）")
    print("=" * 68)

    scenarios = [scenario_default_auto_port, scenario_strict_port, scenario_ipv4_busy,
                 scenario_ipv6_busy, scenario_auto_port_compat, scenario_reuse,
                 scenario_fake_service, scenario_race_guard]
    if IS_WINDOWS:
        scenarios += [scenario_bat, scenario_bat_auto_port, scenario_bat_reuse]
    else:
        print("[提示] 非 Windows：跳过 start.bat 相关场景（严格 / 默认顺延 / 退出码透传）")

    results: dict[str, bool] = {}
    for fn in scenarios:
        print(f"\n----- 场景 {fn.__name__} -----", flush=True)
        try:
            results[fn.__name__] = fn()
        except Exception as exc:
            results[fn.__name__] = False
            print(f"[失败] {fn.__name__}: {exc}")

    # 兜底清理：dummy listener 均已在各自 finally 关闭；这里再扫一遍隔离库文件
    cleanup_db_files()

    print("\n" + "=" * 68)
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    all_ok = all(results.values()) and len(results) >= 8
    print(f"\n结果：{'全部通过' if all_ok else '存在失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
