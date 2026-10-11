"""测试脚本共享的数据库护栏。

用途：以进程内方式（`TestClient(app)` / 直接访问 `app.db` 的 engine）运行的测试，
默认会打到共享库 `data/app.db`，测试数据会写入其中（并可能与正在运行的服务争抢同一
SQLite 文件）。这里提供一个「醒目但不阻断」的提示，引导改用隔离库。

设计要点：
- 只提示、不拒绝运行 —— 保持向后兼容，用户仍可对共享库跑测试。
- 判定依据是「解析后的 database_url 是否指向 data/app.db」，比只看环境变量更准确：
  即使 `DATABASE_URL` 未设置、但 `.env` 把它指到了别处，也不会误报。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许被各测试脚本（位于 backend/tests/）直接 import；同时确保能 import app.*
_BACKEND = Path(__file__).resolve().parent.parent
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

#: 共享库的文件名（默认配置 data/app.db）。
_SHARED_SUFFIX = "data/app.db"


def warn_if_shared_db(script: str = "backend/tests/<脚本名>.py") -> None:
    """若当前进程将使用共享库 `data/app.db`，打印醒目警告（不阻断运行）。

    Args:
        script: 用于提示文案里的示例脚本路径。
    """
    from app.config import settings

    url = settings.database_url
    explicit = os.environ.get("DATABASE_URL")
    # 解析后的地址指向共享库 -> 警告；指向别处（显式隔离库）-> 静默。
    if not url.rstrip("/").endswith(_SHARED_SUFFIX):
        return

    print("=" * 68, flush=True)
    print("[警告] 正在使用共享数据库 data/app.db 运行测试，测试数据会写入其中。", flush=True)
    print(f"        当前数据库：{url}", flush=True)
    print(f'        建议改用隔离库：DATABASE_URL="sqlite:///./data/test.db" python {script}', flush=True)
    if explicit:
        print("        （检测到 DATABASE_URL 已设置，但仍指向共享库，请确认路径。）", flush=True)
    print("=" * 68, flush=True)
