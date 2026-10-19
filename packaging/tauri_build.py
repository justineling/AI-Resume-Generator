#!/usr/bin/env python3
"""运行 `npm run tauri build`，带**心跳输出**与真实退出码透传。

为什么需要一个 Python 脚本来跑一条 npm 命令：

  * Rust 首次编译要 10-20 分钟且长时间没有输出，批处理里看不出来是「在编译」
    还是「已经卡死」，用户会以为程序挂了；
  * 批处理本身做不到「后台跑 + 定时心跳 + 拿到真实退出码」三件事同时成立
    （`start /b` 拿不到退出码，`call` 又阻塞到没有任何心跳）；
  * 用 PowerShell 的 `Start-Process` 托管子进程在本机会直接失败：
    系统同时设了 `HTTPS_PROXY` 与 `https_proxy`，Windows 环境块大小写不敏感，
    PowerShell 重建环境字典时抛
    「Item has already been added. Key in dictionary: 'HTTPS_PROXY'」。
    Python 的 `subprocess` 不做这种重建，因此不受影响。

用法（由 构建安装包.bat 调用）：
    python packaging/tauri_build.py --cwd desktop [--log 路径] [--interval 15]

关于 CARGO_TARGET_DIR（实测踩过的大坑）：
    机器上可能**全局**设了 `CARGO_TARGET_DIR`（本机就是指向另一个项目的
    `D:\\forest-gap-tools\\rust\\target`）。那样 `tauri build` 会把产物写到
    **别的项目目录**里，既污染别人的 workspace，也让本项目的
    `desktop/src-tauri/target/release/bundle/nsis/*.exe` 凭空消失——
    脚本会误报「构建成功但没有产物」。所以这里默认把 `CARGO_TARGET_DIR`
    钉到本项目自己的 `desktop/src-tauri/target`（可用 --target-dir 覆盖）。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

#: 默认心跳间隔（秒）。15 秒既不会刷屏，也不至于让用户以为卡死。
DEFAULT_INTERVAL = 15.0

#: 心跳文案（用户可见）。
HEARTBEAT_TEXT = "    ... 仍在构建（Rust 首次编译较慢，请耐心等待）"


def _say(msg: str) -> None:
    print(msg, flush=True)


def _npm_executable() -> str:
    """定位 npm 可执行文件。

    Windows 上 npm 是 `npm.cmd`；直接 spawn `npm` 会因为不是可执行文件而失败。
    `shutil.which` 会按 PATHEXT 正确解析。
    """
    for name in ("npm.cmd", "npm.exe", "npm"):
        found = shutil.which(name)
        if found:
            return found
    raise SystemExit(
        "[错误] 找不到 npm。请安装 Node.js 18+ 并重新打开命令行窗口：\n"
        "        https://nodejs.org/zh-cn/download"
    )


def _find_rc_exe() -> str | None:
    """定位 Windows 资源编译器 rc.exe（tauri-winres 嵌图标/清单时必需）。

    为什么必须显式去找：`rc.exe` 属于 Windows SDK，**默认不在 PATH 里**
    （只有从「x64 Native Tools 命令提示符」启动时才是）。tauri-build 的
    build script 一旦重新运行（本项目每次构建都会重写 sidecar 二进制，
    从而触发 `cargo:rerun-if-changed`），就会去 PATH 里找 rc.exe，找不到直接
    panic：

        Failed("Are you sure you have RC.EXE in your $PATH or ...")

    这个坑很隐蔽：**首次构建可能成功**（build script 恰好是缓存命中），
    第二次构建才炸，看起来像「随机失败」。

    查找顺序：`RC` 环境变量 → PATH → Windows Kits 里版本号最大的 x64/arm64。
    """
    existing = (os.environ.get("RC") or "").strip()
    if existing and Path(existing).is_file():
        return existing

    found = shutil.which("rc.exe") or shutil.which("rc")
    if found:
        return found

    kits_root = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    if not kits_root.is_dir():
        return None

    arch = "arm64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() == "arm64" else "x64"
    candidates: list[tuple[tuple[int, ...], Path]] = []
    for version_dir in kits_root.iterdir():
        candidate = version_dir / arch / "rc.exe"
        if not candidate.is_file():
            continue
        # 版本号的各段转成整数用于排序（如 10.0.26100.0）
        try:
            key = tuple(int(part) for part in version_dir.name.split("."))
        except ValueError:
            continue
        candidates.append((key, candidate))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return str(candidates[0][1])


def _pump(stream, log_file, sink: list[float]) -> None:
    """把子进程输出逐行转发到控制台与日志文件，并记录最后一次输出的时间。"""
    try:
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\r\n")
            print(line, flush=True)
            if log_file is not None:
                log_file.write(line + "\n")
                log_file.flush()
            sink[0] = time.monotonic()
    finally:
        try:
            stream.close()
        except Exception:
            pass


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="带心跳地运行 npm run tauri build")
    parser.add_argument("--cwd", required=True, help="执行目录（通常是 desktop/）")
    parser.add_argument("--log", default="", help="把完整输出同时写入该日志文件")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL, help="心跳间隔（秒）")
    parser.add_argument(
        "--target-dir",
        default="",
        help="覆盖 CARGO_TARGET_DIR（默认 <cwd>/src-tauri/target，避免写到别的项目里）",
    )
    parser.add_argument(
        "args",
        nargs="*",
        default=[],
        help="传给 npm 的参数（默认 run tauri build）",
    )
    ns = parser.parse_args(argv)

    cwd = Path(ns.cwd).resolve()
    if not cwd.is_dir():
        _say(f"[错误] 目录不存在：{cwd}")
        return 1

    npm_args = ns.args or ["run", "tauri", "build"]
    cmd = [_npm_executable(), *npm_args]

    # 让 npm 输出保持无颜色，避免日志里出现 ANSI 转义序列。
    # 注意：**不要**动 CI 变量——tauri CLI 把 `--ci` 解析成布尔值，
    # 环境里存在 `CI=0` 时会直接报 `invalid value '0' for '--ci'`（已实测踩过）。
    env = os.environ.copy()
    env.setdefault("NO_COLOR", "1")
    env.setdefault("FORCE_COLOR", "0")

    # 把 cargo 产物目录钉在本项目内：系统级 CARGO_TARGET_DIR 会把产物写到别的
    # 项目目录去（本机实测），导致本项目找不到 bundle 产物。
    target_dir = Path(ns.target_dir).resolve() if ns.target_dir else cwd / "src-tauri" / "target"
    env["CARGO_TARGET_DIR"] = str(target_dir)
    _say(f"    Cargo 产物目录：{target_dir}")
    _say(f"    Cargo 缓存目录：{env.get('CARGO_HOME', '(默认 ~/.cargo)')}")

    # rc.exe 默认不在 PATH 里（Windows SDK），缺失会让 tauri-build 直接 panic
    rc_exe = _find_rc_exe()
    if rc_exe:
        env["RC"] = rc_exe
        _say(f"    资源编译器 RC ：{rc_exe}")
    else:
        _say("    [注意] 没找到 rc.exe（Windows SDK）。若本次构建报 "
             "'Are you sure you have RC.EXE in your $PATH'，")
        _say("           请安装 Visual Studio Build Tools 的「Windows 10/11 SDK」组件后重试。")

    log_file = None
    if ns.log:
        log_path = Path(ns.log).resolve()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("w", encoding="utf-8", errors="replace")

    _say(f"    执行：{' '.join(cmd)}")
    _say(f"    目录：{cwd}")
    _say("")

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        _say(f"[错误] 无法启动 npm：{exc}")
        if log_file is not None:
            log_file.close()
        return 1

    last_output = [time.monotonic()]
    pump = threading.Thread(target=_pump, args=(proc.stdout, log_file, last_output), daemon=True)
    pump.start()

    started = time.monotonic()
    # 主线程只做心跳：子进程输出由 pump 线程转发，两者互不阻塞
    while proc.poll() is None:
        time.sleep(1.0)
        elapsed = time.monotonic() - started
        idle = time.monotonic() - last_output[0]
        if idle >= ns.interval:
            minutes, seconds = divmod(int(elapsed), 60)
            _say(f"{HEARTBEAT_TEXT}  已用 {minutes} 分 {seconds} 秒")
            last_output[0] = time.monotonic()

    pump.join(timeout=10.0)
    if log_file is not None:
        log_file.close()

    elapsed = int(time.monotonic() - started)
    code = proc.returncode if proc.returncode is not None else 1
    _say("")
    _say(f"    npm 退出码 {code}，耗时 {elapsed // 60} 分 {elapsed % 60} 秒")
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
