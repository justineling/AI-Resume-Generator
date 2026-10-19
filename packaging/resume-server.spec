# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 FastAPI 后端打成单文件 sidecar。

用法（**必须在项目根目录执行**）：

    .venv\\Scripts\\pyinstaller.exe packaging\\resume-server.spec ^
        --noconfirm --clean ^
        --distpath packaging\\out --workpath packaging\\build

产物：``packaging/out/resume-server.exe``

设计要点：
  * ``console=True`` 是**硬性要求**：windowed（无控制台）模式下 ``sys.stdout``
    为 ``None``，桌面外壳读不到 ``APP_READY <port>`` 协议行，窗口会永远停在占位页。
    Tauri spawn 子进程时带 ``CREATE_NO_WINDOW``，用户看不到黑框。
  * ``datas`` 把 ``backend/app/static`` 收集到 ``app/static``（**不是**
    ``backend/app/static``）：``pathex=['backend']`` 让 ``app`` 成为**顶层包**，
    打包后 ``config.__file__`` 是 ``_MEIPASS/app/config.py``，于是
    ``config.BASE_DIR`` = ``_MEIPASS/app``、``settings.static_dir`` =
    ``_MEIPASS/app/static``。收集位置必须与之对齐，否则启动时报
    ``Directory '...\\app\\static' does not exist``（已实测踩过）。
  * hiddenimports 覆盖 uvicorn / sqlalchemy 的**动态导入**模块（静态分析看不到）。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

PACKAGING_DIR = Path(SPECPATH).resolve()      # noqa: F821 - SPECPATH 由 PyInstaller 注入
ROOT = PACKAGING_DIR.parent
BACKEND = ROOT / "backend"
STATIC_DIR = BACKEND / "app" / "static"
TEMPLATES_DIR = BACKEND / "app" / "templates"
ICON_FILE = ROOT / "desktop" / "src-tauri" / "icons" / "icon.ico"

# 让 collect_submodules() 能 import 到 app 包
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# --------------------------------------------------------------------------- #
# 数据文件
# --------------------------------------------------------------------------- #
datas = []
if STATIC_DIR.is_dir():
    datas.append((str(STATIC_DIR), "app/static"))
else:  # pragma: no cover - 正常仓库里一定存在
    raise SystemExit(f"[spec] 找不到前端静态资源目录：{STATIC_DIR}")
if TEMPLATES_DIR.is_dir():
    datas.append((str(TEMPLATES_DIR), "app/templates"))

# --------------------------------------------------------------------------- #
# 隐藏导入：静态分析看不到的动态导入
# --------------------------------------------------------------------------- #
hiddenimports = [
    # uvicorn 通过字符串拼接动态加载协议/事件循环实现
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # SQLAlchemy 的方言（sqlite 驱动）也是运行期按名字加载
    "sqlalchemy.dialects.sqlite",
    "sqlalchemy.dialects.sqlite.pysqlite",
    "sqlmodel",
    # 表单解析（python-multipart 以 import 方式注册）
    "multipart",
    "multipart.multipart",
]

# 本项目所有子模块都显式收进来，避免任何动态引用漏掉
hiddenimports += collect_submodules("app")

# --------------------------------------------------------------------------- #
# 打包
# --------------------------------------------------------------------------- #
a = Analysis(  # noqa: F821 - Analysis/PYZ/EXE 由 PyInstaller 注入
    [str(PACKAGING_DIR / "server_entry.py")],
    pathex=[str(BACKEND)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",          # 仅构建期生成图标用，运行时不需要
        "pytest",
        "PyInstaller",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="resume-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,               # 必须：否则 sys.stdout 为 None，外壳读不到 APP_READY
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_FILE) if ICON_FILE.is_file() else None,
)
