#!/usr/bin/env python3
"""端口守卫自检（port_guard_check）——验证 start.py 的端口占用行为。

背景（真实事故提炼）：某项目 vite 端口被占后静默跳到别的端口，而客户端 devUrl
写死原端口，导致界面连到别的项目。本脚本锁定修复后的契约：

  a. IPv4 占用        → 明确报错：退出码非 0，报错含端口号、占用者（PID）、
                         「不会静默换端口」的理由与两个出路
  b. IPv6-only 占用   → 同样被检出并报错（修复前只探 IPv4 会漏检：本服务能在
                         127.0.0.1 起起来，但用户按 localhost 访问优先落 ::1，
                         连到那个别的服务——Forest Gap 事故同源）
  c. --auto-port      → 显式开启才顺延，顺延提示醒目（本服务不在原端口上）
  d. 本应用已占用     → 复用已有服务：退出码 0、提示「已在运行」、不重复起服务
  e. start.bat 路径   → 失败时 [ERROR] 可见、退出码非 0（仅 Windows）
  f. 伪装服务         → 只返回 {"ok": true}、无 app 标识的服务不被误判为本应用
  g. bat 顺延         → start.bat 透传 --auto-port，占住原端口时顺延并真正就绪
  h. 竞态兜底         → 已退出的子进程使 wait_for_health 在 1 秒内失败、不开浏览器

用法（项目根目录，用项目虚拟环境的 Python 跑）：
    .venv/Scripts/python.exe backend/tests/port_guard_check.py

隔离约定（与其它套件一致）：
- 端口只用 8231+，绝不碰 8000（用户开发服务端口）；
- 隔离库只落在 data/_port_guard*.db / data/_ui_port_guard*.db（下划线前缀），
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


def child_env() -> dict:
    """子进程环境：强制 UTF-8，避免管道解码把中文断言打歪。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
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
# 场景
# --------------------------------------------------------------------------- #
def scenario_ipv4_busy() -> bool:
    """a. IPv4 占用 8231 → 明确报错（退出码非 0 + 端口号 + 占用者 + 两个出路）。"""
    port = 8231
    dummy = DummyListener(port)
    try:
        # 单元级：占用者定位应能拿到本测试进程自己的 PID（dummy 就在本进程里）
        owners = start_mod.find_port_owner(port)
        assert any(o.get("pid") == os.getpid() for o in owners), \
            f"find_port_owner 未定位到本进程 PID: {owners}"
        print(f"[单元] find_port_owner({port}) → {owners}")

        result = run_start(["--port", str(port), "--no-browser"])
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert result.returncode != 0, "端口被别的程序占用时应非 0 退出"
        assert str(port) in out, "报错应包含端口号"
        assert "占用" in out, "报错应说明端口被占用"
        assert "不会静默" in out, "报错应包含「不会静默换端口」的理由"
        assert "--port" in out, "报错应给出 --port 这个出路"
        print(f"[通过] 退出码 {result.returncode}，报错要素齐全")
        return True
    finally:
        dummy.close()


def scenario_ipv6_busy() -> bool:
    """b. IPv6-only 占用 8232 → 同样被检出并报错（修复前只探 IPv4 会漏检）。"""
    port = 8232
    dummy = DummyListener(port, ipv6_only=True)
    try:
        # 前置确认场景成立：IPv4 上确实空闲、只有 [::1] 在监听——
        # 这正是修复前「只探 IPv4 会漏检」的复现条件（Forest Gap 同源）
        import start as _s
        assert _s._connect_ok("127.0.0.1", port, socket.AF_INET) is False, \
            "前置不成立：127.0.0.1 上不应有监听"
        assert _s._connect_ok("::1", port, socket.AF_INET6) is True, \
            "前置不成立：[::1] 上应有监听"
        owners = start_mod.find_port_owner(port)
        assert any(o.get("pid") == os.getpid() for o in owners), \
            f"IPv6-only 占用未被 netstat 定位到: {owners}"
        print(f"[单元] find_port_owner({port}) → {owners}")

        result = run_start(["--port", str(port), "--no-browser"])
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert result.returncode != 0, "IPv6-only 占用也必须报错（Forest Gap 教训）"
        assert str(port) in out, "报错应包含端口号"
        assert "占用" in out, "报错应说明端口被占用"
        print(f"[通过] IPv6-only 占用被双栈探测检出，退出码 {result.returncode}")
        return True
    finally:
        dummy.close()


def scenario_auto_port() -> bool:
    """c. --auto-port：8233 被占 → 顺延 8234，提示醒目；真起服务几秒后杀掉。"""
    busy_port, next_port = 8233, 8234
    dummy = DummyListener(busy_port)
    db = ROOT / "data" / "_port_guard.db"
    env = child_env()
    env["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"  # 隔离库，绝不碰 app.db
    proc = popen_start(["--port", str(busy_port), "--auto-port", "--no-browser"], env)
    lines: list[str] = []
    try:
        deadline = time.time() + 180
        bumped = False
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.rstrip()
            lines.append(line)
            print(line)
            if "顺延" in line and str(next_port) in line:
                bumped = True
                # 顺延提示后再抓几行（「地址：」行）确认三者一致
                for _ in range(4):
                    extra = proc.stdout.readline()
                    if not extra:
                        break
                    lines.append(extra.rstrip())
                    print(extra.rstrip())
                break
        assert bumped, "应打印顺延提示（含新端口号）"
        text = "\n".join(lines)
        assert f"不在 {busy_port} 上" in text, "顺延提示必须醒目：本服务不在原端口上"
        assert f"http://127.0.0.1:{next_port}" in text, "地址应为顺延后的端口"
        # 顺延后的服务应真的就绪（可选强校验，拿不到不算失败）
        print(f"[通过] 已顺延到 {next_port}，提示醒目")
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
        assert wait_port_free(next_port), f"顺延端口 {next_port} 未释放"
        cleanup_db_files()


def scenario_reuse() -> bool:
    """d. 本应用已占用 8235 → 复用：退出码 0、提示「已在运行」、不起第二个服务。"""
    port = 8235
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


def scenario_bat() -> bool:
    """e. start.bat 路径（Windows）：端口被占 → [ERROR] 可见、退出码非 0。

    bat 失败路径有 pause；用 stdin=DEVNULL 喂空输入使其立即继续，不会卡死。
    """
    port = 8236
    dummy = DummyListener(port)
    try:
        result = subprocess.run(
            ["cmd", "/c", "start.bat", "--port", str(port), "--no-browser"],
            cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300,
            stdin=subprocess.DEVNULL, env=child_env(),
        )
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert "[ERROR]" in out, "start.bat 失败路径应打印 [ERROR]"
        assert result.returncode != 0, "退出码应非 0"
        assert str(port) in out, "输出应能看到端口号（经 %* 透传）"
        print(f"[通过] start.bat 路径 [ERROR] 可见，退出码 {result.returncode}")
        return True
    finally:
        dummy.close()


# --------------------------------------------------------------------------- #
# 身份校验 / 竞态兜底 / bat 顺延（A/B/C 增量）
# --------------------------------------------------------------------------- #
class FakeHealthServer:
    """在指定端口起一个假 HTTP 服务，`/api/health` 返回固定 JSON。

    用于验证「伪装服务」：它可能恰好返回 `{"ok": true}`，但**没有 app 标识**。
    修复后启动器必须据此判定「不是本应用」，绝不能把它当成自己的启动成功。
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


def scenario_fake_service() -> bool:
    """f. 伪装服务：8263 返回 {"ok": true} 但无 app 标识 → 不得误判为本应用，退出码非 0。"""
    port = 8263
    srv = FakeHealthServer(port, {"ok": True})
    try:
        # 前置确认：该伪装服务确实在监听，且强校验 is_our_app 判为「不是本应用」
        assert start_mod.port_in_use(port), "前置不成立：8263 应有假服务监听"
        assert start_mod.is_our_app(port) is False, \
            "强身份校验失败：只有 ok、没有 app 标识的伪装服务被误判为本应用"
        result = run_start(["--port", str(port), "--no-browser"])
        out = result.stdout + result.stderr
        print(result.stdout.rstrip())
        assert result.returncode != 0, "伪装服务不应被当成本应用，应非 0 退出"
        assert "已在运行" not in out, "绝不能把伪装服务误判为本应用（不得提示「已在运行」）"
        assert "占用" in out or "不是本应用" in out, "应明确报「占用 / 不是本应用」"
        print(f"[通过] 伪装服务（无 app 标识）未被误判，退出码 {result.returncode}")
        return True
    finally:
        srv.close()


def scenario_race_guard() -> bool:
    """h. 竞态兜底：已退出的子进程 → wait_for_health 在 1 秒内返回失败，且不打开浏览器。"""
    port = 8265
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


def _bat_auto_port() -> bool:
    """g. bat 层顺延：占住 8261 → start.bat --port 8261 --auto-port → 顺延到 8262 并就绪。"""
    busy, nxt = 8261, 8262
    dummy = DummyListener(busy)
    db = ROOT / "data" / "_pg_bat.db"
    env = child_env()
    env["DATABASE_URL"] = f"sqlite:///{db.as_posix()}"  # 隔离库
    env["NO_PROXY"] = "localhost,127.0.0.1"
    env["no_proxy"] = "localhost,127.0.0.1"
    creationflags = 0x00000200 if IS_WINDOWS else 0  # CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        ["cmd", "/c", "start.bat", "--port", str(busy), "--auto-port"],
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
        assert bumped, "start.bat 应透传 --auto-port 并打印顺延提示（含 8262）"
        assert up, "顺延后的服务应在 8262 就绪（health 200 且 app 标识匹配）"
        text = "\n".join(lines)
        assert f"不在 {busy} 上" in text, "顺延提示必须醒目：本服务不在原端口上"
        print(f"[通过] bat 层已顺延到 {nxt}，health 200 且 app 标识匹配")
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


def scenario_bat_auto_port() -> bool:
    return _bat_auto_port()


def scenario_bat_reuse() -> bool:
    """bat 退出码 0 透传：复用已有本应用 → start.bat 应退出 0。

    说明：`--auto-port` 顺延后服务会常驻，强制停止（taskkill / CTRL_BREAK）会让
    `cmd` 自身以 STATUS_CONTROL_C_EXIT(0xC000013A) 退出，无法得到 0。故「退出码 0」
    的透传用本场景**确定性**验证（复用路径 start.py 立即返回 0）。
    """
    port = 8264
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
    print("端口守卫自检 port_guard_check（隔离端口 8231+，不碰 8000 / data/app.db）")
    print("=" * 68)

    scenarios = [scenario_ipv4_busy, scenario_ipv6_busy,
                 scenario_auto_port, scenario_reuse,
                 scenario_fake_service, scenario_race_guard]
    if IS_WINDOWS:
        scenarios += [scenario_bat, scenario_bat_auto_port, scenario_bat_reuse]
    else:
        print("[提示] 非 Windows：跳过 start.bat 相关场景（e / bat 顺延 / bat 退出码透传）")

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
    all_ok = all(results.values()) and len(results) >= 6
    print(f"\n结果：{'全部通过' if all_ok else '存在失败'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
