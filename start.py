#!/usr/bin/env python3
"""AI Resume Generator —— 一键启动。

做四件事，让你双击就能用：
  1. 准备 Python 虚拟环境（不存在就建）
  2. 安装 / 更新依赖（requirements.txt 变了才重装）
  3. 检查端口：空闲直接用；被**别的程序**占用时**自动顺延**到下一个空闲端口并醒目告知实际地址
     （--strict-port 可改为直接报错退出；--auto-port 为兼容别名，自动顺延已是默认行为）
  4. 等健康检查通过后自动打开浏览器

另外顺手处理了一个真实踩过的坑：本机若设了 HTTP_PROXY，代理会拦截
localhost 请求（表现为 502）。脚本会为子进程设置 NO_PROXY，保证本地
LLM（如 Ollama）与本地服务都能直连。

安全底座：启动成功前会校验 `/api/health` 的 `app` 标识「确实是本应用」，
不是本应用绝不打开浏览器；端口被别的程序占用时会打印占用者信息。

用法：
    python start.py                  # 默认 8000 端口；被占用时自动顺延并告知实际端口
    python start.py --port 9000      # 指定端口（被占用时同样自动顺延）
    python start.py --strict-port    # 端口被占用时直接报错退出，不自动顺延
    python start.py --auto-port      # 兼容别名（自动顺延已是默认行为，保留以兼容旧脚本/习惯）
    python start.py --no-browser     # 不自动开浏览器
    python start.py --reload         # 开发模式（改代码自动重启）
    python start.py --reinstall      # 强制重装依赖
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
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

#: 应用标识（与 backend/app/main.py 的 APP_ID 必须一致）。启动器用它校验
#: 「端口上应答的确实本应用」，而不是只看 HTTP 200 / "ok" 子串。
APP_ID = "ai-resume-generator"

#: 启动后等待健康检查的总时长（秒）。
STARTUP_TIMEOUT = 40.0

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
#: 回环语义的 host（本机地址）。对这些 host，端口探测必须**双栈**进行：
#: 只探测 127.0.0.1 会漏掉「只监听 [::1] 的服务」——本服务能在 127.0.0.1 上
#: 正常起来，但用户按 localhost 访问时（localhost 在 Windows / WebView 下优先
#: 解析到 ::1）会连到那个别的服务，表现为「浏览器打开的是别人的界面」。
_LOOPBACK_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})

#: 「IPv6 探测不可用」是否已提示过（只提示一次，见 _note_ipv6_unavailable）。
_IPV6_PROBE_WARNED = False


def _is_loopback_host(host: str) -> bool:
    """是否是本机 / 回环语义的地址。"""
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _connect_ok(addr: str, port: int, family: int) -> bool | None:
    """尝试 TCP 连接 addr:port。

    Returns:
        True: 连接成功（端口被监听）。
        False: 连接被拒（端口空闲）。
        None: 探测本身不可用（如系统没有 IPv6、端口越界抛
            OverflowError、地址非法抛 ValueError），调用方需降级处理，
            不当成占用，避免误报。
    """
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex((addr, port)) == 0
    except (OSError, ValueError, OverflowError):
        return None


def _note_ipv6_unavailable() -> None:
    """调试模式下说明「无法探测 IPv6 回环」。主流程不因此中断或崩溃。"""
    global _IPV6_PROBE_WARNED
    if _IPV6_PROBE_WARNED:
        return
    _IPV6_PROBE_WARNED = True
    if os.environ.get("RESUME_DEBUG"):
        print("[调试] 本环境无法进行 IPv6 回环探测，端口占用仅按 IPv4 结果判断",
              file=sys.stderr, flush=True)


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """端口是否已被监听（双栈探测）。

    - 回环语义的 host（localhost / 127.x.x.x / ::1 / 0.0.0.0 / 缺省）：
      **同时**探测 127.0.0.1 与 [::1]，任一被监听即视为占用。
      只探 IPv4 会把「只听 IPv6 回环的服务」误判为空闲（真实事故模式）。
    - 显式指定的其他地址：只探测对应地址（含 ':' 的按 IPv6 探测）。
    """
    if _is_loopback_host(host):
        used_v4 = _connect_ok("127.0.0.1", port, socket.AF_INET)
        used_v6 = _connect_ok("::1", port, socket.AF_INET6)
        if used_v6 is None:
            _note_ipv6_unavailable()
        return used_v4 is True or used_v6 is True

    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    used = _connect_ok(host, port, family)
    if used is None and family == socket.AF_INET6:
        _note_ipv6_unavailable()
    return used is True


def find_port(preferred: int, host: str = "127.0.0.1", tries: int = 20) -> int:
    """从 preferred 开始顺延找空闲端口（默认行为；--strict-port 时不顺延）。"""
    for offset in range(tries):
        candidate = preferred + offset
        if not port_in_use(candidate, host):
            return candidate
    die(f"{preferred} 起的 {tries} 个端口都被占用了，请先释放端口，或换一个端口段（--port 8300 起）")


def _health_probe_host(host: str) -> str:
    """健康检查用的探测地址：与 uvicorn 实际 bind 的协议栈保持一致，避免误判。

    - localhost / 127.x.x.x / 0.0.0.0 / 缺省 → 127.0.0.1（服务 bind 在 IPv4 回环）
    - ::1 → ::1（服务 bind 在 IPv6 回环）
    - 其他显式地址 → 原样
    """
    if not host or host == "localhost" or host == "0.0.0.0" or host.startswith("127."):
        return "127.0.0.1"
    return host


# --------------------------------------------------------------------------- #
# 端口占用者定位（跨平台；拿不到就优雅降级，绝不影响主流程）
# --------------------------------------------------------------------------- #
def _parse_netstat_line(line: str, port: int, strict: bool = True) -> tuple[int, str] | None:
    """解析一行 netstat -ano 输出，返回 (pid, 本地地址) 或 None。

    端口必须是**整数精确匹配**：禁止 `findstr ":8000"` 这类子串匹配，
    否则 :18000 / :80001 会被误判为 8000 的占用者（另一个项目的实测教训）。
    strict=True 时只认 LISTENING 行；strict=False 为兜底（兼容本地化状态词）。
    """
    parts = line.split()
    if len(parts) < 5 or parts[0].upper() != "TCP":
        return None
    if strict and parts[3].upper() != "LISTENING":
        return None
    local = parts[1]
    try:
        if int(local.rsplit(":", 1)[-1]) != port:
            return None
        return int(parts[-1]), local
    except ValueError:
        return None


def _listening_pids_windows(port: int) -> list[tuple[int, str]]:
    """解析 netstat -ano，返回 [(pid, 本地地址如 '[::1]:8000')]。"""
    result = subprocess.run(
        ["netstat", "-ano"], capture_output=True, timeout=15,
        encoding="utf-8", errors="replace",
    )
    strict = [h for line in result.stdout.splitlines()
              if (h := _parse_netstat_line(line, port, strict=True))]
    if strict:
        return strict
    return [h for line in result.stdout.splitlines()
            if (h := _parse_netstat_line(line, port, strict=False))]


def _process_info_windows(pid: int, local: str = "") -> dict:
    """取 Windows 进程的映像名与命令行。拿不到就返回空字段，绝不抛异常。"""
    info: dict = {"pid": pid, "local": local, "image": "", "cmdline": ""}
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=15, encoding="utf-8", errors="replace",
        ).stdout
        for row in csv.reader(io.StringIO(out)):
            if row and row[0].strip().lower().endswith(".exe"):
                info["image"] = row[0].strip()
                break
    except Exception:
        pass
    # 命令行最能「一眼认出是谁」（Forest Gap 事故的实践经验）
    try:
        ps = (f'Get-CimInstance Win32_Process -Filter "ProcessId={pid}" '
              f"| Select-Object -ExpandProperty CommandLine")
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=20, encoding="utf-8", errors="replace",
        ).stdout
        info["cmdline"] = out.strip()
    except Exception:
        pass
    return info


def _listening_pids_posix(port: int) -> list[tuple[int, str]]:
    """lsof → ss -ltnp → /proc/net/tcp{,6} 兜底。返回 [(pid, 本地地址)]。"""
    # 1) lsof：最直接（-t 只输出 PID）
    try:
        out = subprocess.run(["lsof", "-ti", f":{port}"],
                             capture_output=True, timeout=10,
                             encoding="utf-8", errors="replace").stdout
        pids = [int(t) for t in out.split() if t.isdigit()]
        if pids:
            return [(p, "") for p in pids]
    except Exception:
        pass
    # 2) ss -ltnp
    hits: list[tuple[int, str]] = []
    try:
        out = subprocess.run(["ss", "-ltnp"],
                             capture_output=True, timeout=10,
                             encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or not parts[0].upper().startswith("LISTEN"):
                continue
            local = parts[3]
            try:
                if int(local.rsplit(":", 1)[-1]) != port:
                    continue
            except ValueError:
                continue
            m = re.search(r"pid=(\d+)", line)
            if m:
                hits.append((int(m.group(1)), local))
        if hits:
            return hits
    except Exception:
        pass
    # 3) /proc/net/tcp{,6}（十六进制端口）兜底；无法映射 inode→pid 时 pid 记 -1
    for proc_file in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(proc_file, "r", encoding="ascii") as f:
                next(f)  # 表头
                for line in f:
                    cols = line.split()
                    if len(cols) < 4 or cols[3] != "0A":  # 0A = LISTEN
                        continue
                    local_hex = cols[1]
                    try:
                        if int(local_hex.rsplit(":", 1)[-1], 16) != port:
                            continue
                    except ValueError:
                        continue
                    hits.append((-1, local_hex))
        except Exception:
            continue
    return hits


def _process_info_posix(pid: int, local: str = "") -> dict:
    """取 POSIX 进程的命令行与映像名。拿不到就返回空字段，绝不抛异常。"""
    info: dict = {"pid": pid, "local": local, "image": "", "cmdline": ""}
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        info["cmdline"] = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        info["image"] = os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except Exception:
        pass
    if not info["cmdline"]:
        try:
            out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                                 capture_output=True, timeout=10,
                                 encoding="utf-8", errors="replace").stdout
            info["cmdline"] = out.strip()
        except Exception:
            pass
    return info


def find_port_owner(port: int) -> list[dict]:
    """跨平台定位谁在监听 port，返回 [{'pid','image','cmdline','local'}, ...]。

    全部 try/except 包住；定位不到时返回 []，**绝不抛异常、绝不影响启动流程**。
    """
    try:
        if IS_WINDOWS:
            return [_process_info_windows(pid, local)
                    for pid, local in _listening_pids_windows(port)]
        return [_process_info_posix(pid, local)
                for pid, local in _listening_pids_posix(port)]
    except Exception:
        return []


def describe_port_owner(port: int) -> str:
    """「占用者是谁」的可读描述；定位不到时给出手动排查命令。"""
    owners = find_port_owner(port)
    if not owners:
        if IS_WINDOWS:
            manual = f"netstat -ano | findstr :{port}"
        else:
            manual = f"ss -ltnp | grep :{port}（或 lsof -i :{port}）"
        return f"端口 {port} 被占用（未能定位占用者，可手动执行 `{manual}` 查看）"

    lines: list[str] = []
    for owner in owners[:5]:
        pid = owner.get("pid", -1)
        image = owner.get("image") or "(未知进程)"
        local = owner.get("local") or ""
        head = image
        if isinstance(pid, int) and pid > 0:
            head += f"（PID {pid}）"
        if local:
            head += f" 监听 {local}"
        lines.append(f"  - {head}")
        cmdline = (owner.get("cmdline") or "").strip()
        if cmdline:
            if len(cmdline) > 200:
                cmdline = cmdline[:200] + " …"
            lines.append(f"      命令行：{cmdline}")
    return "\n".join(lines)


def port_busy_error(port: int, host: str) -> None:
    """严格模式（`--strict-port`）下端口被别的程序占用时的明确报错。"""
    owner_text = describe_port_owner(port)
    die(
        f"端口 {port} 已被别的程序占用，启动中止。\n"
        f"\n"
        f"占用者：\n{owner_text}\n"
        f"\n"
        f"你显式加了 --strict-port，因此这里直接报错（默认会自动改用空闲端口）。\n"
        f"不会静默更换端口——静默换端口曾导致别的项目界面错乱：\n"
        f"若这里悄悄改到别的端口，而客户端/文档仍以 {port} 为约定地址，\n"
        f"用户打开的就会是占用 {port} 的那个服务，而不是本应用。\n"
        f"\n"
        f"三个出路：\n"
        f"  1. 去掉 --strict-port，让启动器自动改用空闲端口\n"
        f"  2. 换一个空闲端口启动：  python start.py --port <空闲端口>\n"
        f"  3. 结束上面占用 {port} 的进程，然后重新启动\n"
    )


# --------------------------------------------------------------------------- #
# 健康检查 / 浏览器
# --------------------------------------------------------------------------- #
def is_our_app(port: int, host: str = "127.0.0.1") -> bool:
    """判断该端口上跑的是不是本应用（而不是别的服务）。

    强校验：解析 `/api/health` 的 JSON，要求 `app == "ai-resume-generator"`
    且 `ok is True`。**只看 HTTP 200 或响应体含 `"ok"` 太弱**——别的服务恰好
    在 `/api/health` 返回 `{"ok": true}` 就会被误判为「本应用」，进而把「别人的
    页面」当成自己的启动成功打开（参考项目点名的头号风险）。

    本地健康检查**绕过系统代理**（本机可能设了 http_proxy，否则会误判成
    「不是本应用」而走报错/顺延路径）。
    """
    import urllib.request

    host_part = f"[{host}]" if ":" in host else host
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host_part}:{port}/api/health", timeout=2) as resp:
            if resp.status != 200:
                return False
            raw = resp.read()
    except Exception:
        return False
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return False
    return isinstance(data, dict) and data.get("app") == APP_ID and data.get("ok") is True


def wait_for_health(
    port: int,
    timeout: float = STARTUP_TIMEOUT,
    proc: subprocess.Popen | None = None,
    host: str = "127.0.0.1",
) -> bool:
    """轮询 `/api/health` 直到确认**本应用**就绪。

    - 只有 `is_our_app()` 为真才返回 True（强身份校验，绝不把别人的服务当自己）；
    - 一旦 `proc` 已退出（uvicorn bind 失败 / 端口竞态抢先被占）**立即**返回
      False，不再傻等到超时（毫秒级竞态窗口的关键兜底）。
    """
    probe_host = _health_probe_host(host)
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 先判子进程是否已退出：已退出就不可能再就绪，立即失败，
        # 且**不**去做网络探测（探测在个别环境可能耗时 1-2s，会拖慢失败判定）。
        if proc is not None and proc.poll() is not None:
            return False
        if is_our_app(port, probe_host):
            return True
        time.sleep(0.4)
    return False


def _startup_failure_message(port: int, host: str, proc: subprocess.Popen | None) -> int:
    """启动失败后的三态诊断，打印可读错误并返回退出码。

    1) 端口上有服务且确是本应用 → 视为成功（竞态下自己其实起来了），返回 0；
    2) 端口上有服务但**不是**本应用 → 明确警告「不是本应用，请勿直接打开」，
       返回 1（这正是「打开别人的页面」最危险的场景）；
    3) 端口上没有任何服务 → 子进程已退出则报「服务未能启动（退出码 N）」，
       沿用其退出码（为 0 时用 1）；否则报超时，返回 1。
    """
    probe_host = _health_probe_host(host)
    if port_in_use(port, probe_host):
        if is_our_app(port, probe_host):
            say("服务已就绪。")
            return 0
        print(
            f"\n[错误] 端口 {port} 上应答的不是本应用（可能是别的程序抢先占用），"
            f"请勿直接打开该地址。\n"
            f"  请查看上方日志排查，或换一个端口重试：python start.py --port <空闲端口>\n",
            flush=True,
        )
        return 1
    code = proc.poll() if proc is not None else None
    if code is not None:
        print(f"\n[错误] 服务未能启动（退出码 {code}），请查看上方日志。\n", flush=True)
        return code if code not in (0, None) else 1
    print(
        f"\n[错误] 服务启动超时（{port} 上没有任何服务应答）。"
        f"请查看上方日志，或换端口重试。\n",
        flush=True,
    )
    return 1


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


def _port_type(value: str) -> int:
    """argparse 的 --port 类型校验：必须是 1-65535 的整数。

    没有这层校验时，越界端口（如 99999 / -1）会在 socket.connect_ex 里
    抛 OverflowError，用户看到的是裸 traceback 而不是可读的错误提示。
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("端口必须是 1-65535 之间的整数") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须是 1-65535 之间的整数")
    return port


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Resume Generator 一键启动")
    parser.add_argument("--port", type=_port_type, default=8000,
                        help="监听端口（默认 8000；被别的程序占用时自动顺延到下一个空闲端口）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--no-browser", action="store_true", help="不要自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式：改代码自动重启")
    parser.add_argument("--reinstall", action="store_true", help="强制重新安装依赖")
    parser.add_argument("--strict-port", action="store_true",
                        help="端口被占用时直接报错退出，不自动顺延")
    parser.add_argument("--auto-port", action="store_true",
                        help="兼容别名：自动顺延已是默认行为，保留以兼容旧脚本/习惯")
    args = parser.parse_args()

    # 横幅放在 ensure_venv() 之后：重执行时父进程会直接退出、由子进程打印，
    # 否则会出现两次横幅。
    ensure_venv()

    print("\n╔══════════════════════════════════════════════════════╗")
    print("║   AI RESUME GENERATOR  ·  岗位定制简历生成器          ║")
    print("╚══════════════════════════════════════════════════════╝")

    ensure_deps(force=args.reinstall)

    title("启动服务")

    # 端口已被本应用占用 → 直接打开浏览器，不重复起服务（保留的好行为）。
    # 探测地址与 uvicorn 实际 bind 的协议栈保持一致，避免误判。
    probe_host = _health_probe_host(args.host)
    busy = port_in_use(args.port, args.host)
    if busy and is_our_app(args.port, probe_host):
        url = f"http://{args.host}:{args.port}"
        say(f"检测到服务已在运行：{url}")
        if not args.no_browser:
            open_browser(url)
        return 0

    if busy:
        if args.strict_port:
            # 严格模式（--strict-port）：指出占用者、影响说明与两个出路（die 退出码 1）。
            port_busy_error(args.port, args.host)
        else:
            # 默认（含 --auto-port 兼容别名）：自动顺延，但必须**醒目告知**实际端口——
            # 「问题不在换端口本身，而在静默」：用户若仍按原端口访问会连到占用者，
            # 所以这里把新地址、原端口的占用情况、以及如何改回严格模式都说清楚。
            port = find_port(args.port, args.host)
            say(f"[注意] {args.port} 被占用，已顺延到 {port}；本服务不在 {args.port} 上！")
            say(f"        将打开 http://{args.host}:{port}；如需在 {args.port} 上运行，请先关闭占用它的程序。")
            say(f"        想改为「被占用即报错」：加 --strict-port 重新启动。")
    else:
        port = args.port

    # 打印地址、浏览器 URL、uvicorn bind 三者共用同一 host/port，天然一致。
    url = f"http://{args.host}:{port}"
    say(f"地址：{url}")
    if _is_loopback_host(args.host) and args.host != "::1":
        say(f"（提示：若 http://localhost:{port} 打不开、而 http://127.0.0.1:{port} 能打开，")
        say(f" 说明 {port} 的 IPv6 [::1] 监听被别的程序占用——请处理该占用后重试。）")
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

    # 后台等待就绪：**只有确认是本应用**才打印就绪并打开浏览器；
    # 失败分支绝不调用 open_browser（避免把别人的页面当成自己的启动成功）。
    state = {"ready": False}

    def _open_when_ready() -> None:
        state["ready"] = wait_for_health(
            port, timeout=STARTUP_TIMEOUT, proc=proc, host=args.host
        )
        if state["ready"] and not args.no_browser:
            say("服务已就绪，正在打开浏览器…")
            open_browser(url)

    watcher = threading.Thread(target=_open_when_ready, daemon=True)
    watcher.start()

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
        return proc.returncode if proc.returncode is not None else 0

    # 子进程已退出：等后台判定落定（proc 已退出时 wait_for_health 会立即返回）。
    watcher.join(timeout=2.0)
    # 非正常退出且从未就绪 → 启动失败，走三态诊断并返回非 0。
    if proc.returncode not in (0, None) and not state["ready"]:
        return _startup_failure_message(port, args.host, proc)
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
