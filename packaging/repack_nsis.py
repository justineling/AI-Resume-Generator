"""把 Tauri 打好的 NSIS 安装包重新打包，使**默认安装目录**变成指定盘符。

为什么要有这一步：
  Tauri 的 NSIS 模板在 currentUser 模式下默认装到 `$LOCALAPPDATA\\<产品名>`，
  且 `RestorePreviousInstallLocation` 会从注册表读回**上一次**的安装路径
  （所以只要有人用 `/D=` 装过一次，之后即使不带参数也会沿用那个目录）。
  本项目希望默认落在 `D:\\<产品名>`。

做法（复用 Tauri 自己生成的 .nsi，不自己维护整份模板）：
  1. 找到 `target/release/nsis/<arch>/installer.nsi`
     —— 这份 .nsi 已由 tauri 完全展开，里面的 `File` 指令都是绝对路径；
  2. 把 `StrCpy $INSTDIR "$LOCALAPPDATA\\<产品名>"` 换成 `StrCpy $INSTDIR "<盘>:\\<产品名>"`；
  3. 用 tauri 自带的 makensis 在**同一目录**里重新编译；
  4. 用新产物覆盖 `bundle/nsis/` 下的安装包（旧包先备份，失败则还原）。

失败时**不破坏**已有的安装包：makensis 失败就保持 tauri 原产物不动。

用法：
    python packaging/repack_nsis.py --desktop desktop [--drive D]
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

BS = chr(92)  # 反斜杠


def find_makensis() -> Path | None:
    """定位 tauri 缓存的 makensis（tauri bundler 下载到 LOCALAPPDATA\\tauri\\NSIS）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or "")
    candidates = [
        base / "tauri" / "NSIS" / "makensis.exe",
        Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
        Path(r"C:\Program Files\NSIS\makensis.exe"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    which = shutil.which("makensis")
    return Path(which) if which else None


def find_nsi(target: Path) -> Path | None:
    """在 tauri 输出目录里找 installer.nsi（通常在 nsis/<arch>/ 下）。"""
    nsis_root = target / "nsis"
    if not nsis_root.is_dir():
        return None
    found = sorted(nsis_root.glob("*/installer.nsi"))
    return found[0] if found else None


def main() -> int:
    ap = argparse.ArgumentParser(description="重打包 NSIS，改默认安装目录")
    ap.add_argument("--desktop", required=True, help="desktop 目录（含 src-tauri）")
    ap.add_argument("--drive", default="D", help="默认安装到的盘符（默认 D）")
    ap.add_argument("--product-name", default="", help="产品名（默认从 .nsi 里读 PRODUCTNAME）")
    ns = ap.parse_args()

    desktop = Path(ns.desktop).resolve()
    target = desktop / "src-tauri" / "target" / "release"
    nsi = find_nsi(target)
    if nsi is None:
        print(f"[skip] 没找到 installer.nsi（{target / 'nsis'}），保持 tauri 原产物不变")
        return 0

    drive = ns.drive.strip().rstrip(":" + BS) or "D"

    src = nsi.read_text(encoding="utf-8")

    # 产品名：优先命令行参数，其次从 .nsi 里读
    product = ns.product_name
    if not product:
        for line in src.splitlines():
            s = line.strip()
            if s.startswith("!define PRODUCTNAME"):
                product = s.split('"')[1] if '"' in s else s.split()[-1]
                break
    if not product:
        print("[错误] 无法确定产品名")
        return 1

    anchor = f'StrCpy $INSTDIR "$LOCALAPPDATA{BS}${{PRODUCTNAME}}"'
    replacement = f'StrCpy $INSTDIR "{drive}:{BS}${{PRODUCTNAME}}"'

    if replacement in src:
        print(f"[skip] 默认安装目录已经是 {drive}:{BS}{product}")
    elif anchor in src:
        note = (
            f"; === 本项目定制：默认安装到 {drive} 盘（与仓库名一致）===" + "\n"
            f"      ; Tauri 原值：$LOCALAPPDATA{BS}${{PRODUCTNAME}}" + "\n"
            f"      "
        )
        src = src.replace(anchor, note + replacement, 1)
        nsi.write_text(src, encoding="utf-8")
        print(f"[ok] 默认安装目录已改为 {drive}:{BS}{product}")
    else:
        print("[warn] 没找到预期的默认目录行（模板可能变了），跳过改写，保持 tauri 原包")
        print(f"       期望包含：{anchor}")
        return 0

    makensis = find_makensis()
    if makensis is None:
        print("[warn] 没找到 makensis.exe，无法重打包；保留 tauri 原安装包")
        return 0

    # 原有安装包先备份，失败可还原
    bundle_dir = target / "bundle" / "nsis"
    existing = sorted(bundle_dir.glob("*.exe")) if bundle_dir.is_dir() else []
    backup = None
    if existing:
        backup = existing[-1].with_suffix(".exe.bak")
        shutil.copy2(existing[-1], backup)

    print(f"[run] {makensis.name} {nsi.name}")
    proc = subprocess.run([str(makensis), nsi.name], cwd=str(nsi.parent),
                          capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    for line in out.strip().splitlines()[-6:]:
        print("      " + line)

    # .nsi 里的 OUTFILE 是相对名，产物落在 nsi 同目录
    outfile_name = "nsis-output.exe"
    for line in src.splitlines():
        s = line.strip()
        if s.startswith("!define OUTFILE"):
            outfile_name = s.split('"')[1] if '"' in s else outfile_name
            break
    produced = nsi.parent / outfile_name

    if proc.returncode != 0 or not produced.is_file():
        print("[错误] makensis 失败，安装包保持 tauri 原样（未被破坏）")
        return 1

    # 覆盖 bundle/nsis 下的安装包（保留原文件名，方便脚本按通配符找）
    bundle_dir.mkdir(parents=True, exist_ok=True)
    dest = (existing[-1] if existing else bundle_dir / produced.name)
    shutil.copy2(produced, dest)
    if backup and backup.exists():
        backup.unlink()

    size_mb = dest.stat().st_size / 1048576
    print(f"[ok] 已重打包：{dest.name}（{size_mb:.2f} MiB），默认装到 {drive}:{BS}{product}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
