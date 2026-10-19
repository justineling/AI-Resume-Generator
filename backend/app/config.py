"""应用配置。

所有配置都可通过环境变量或项目根目录的 .env 覆盖。

数据目录（DATA_DIR）的解析顺序：
  1. 环境变量 ``APP_DATA_DIR``（桌面外壳 Tauri 会注入
     ``%LOCALAPPDATA%\\AIResumeGenerator\\data``）；
  2. 未设该变量、但运行在 PyInstaller 打包产物里（``sys.frozen``）时，
     默认落到 ``%LOCALAPPDATA%\\AIResumeGenerator\\data``；
  3. 其他情况（源码运行 / ``start.py`` / 测试）：项目根目录下的 ``data/``。

为什么打包后不能用 ``PROJECT_ROOT / "data"``：PyInstaller onefile 模式下
``PROJECT_ROOT`` 是 ``sys._MEIPASS``——**每次启动都是一个全新的临时目录**，
数据库会在每次重启时被重建，用户数据全部丢失。所以打包场景必须落到用户级
可写目录。
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_ROOT = BASE_DIR.parent                      # 项目根目录（打包后为 _MEIPASS）

#: 桌面版数据目录名（位于 %LOCALAPPDATA% 下）。
APP_DIR_NAME = "AIResumeGenerator"

#: 环境变量名：由桌面外壳注入，指向用户级可写数据目录。
ENV_DATA_DIR = "APP_DATA_DIR"


def is_frozen() -> bool:
    """当前进程是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def default_frozen_data_dir() -> Path:
    """打包运行时的默认数据目录：``%LOCALAPPDATA%\\AIResumeGenerator\\data``。

    ``LOCALAPPDATA`` 缺失时依次退回 ``APPDATA`` 与用户主目录，保证任何环境下
    都能得到一个可写的绝对路径（绝不返回相对路径，否则会落到 CWD 里，
    用户双击安装版时 CWD 可能是 ``C:\\Windows\\System32``）。
    """
    base = (os.environ.get("LOCALAPPDATA") or "").strip() or (os.environ.get("APPDATA") or "").strip()
    if base:
        return Path(base) / APP_DIR_NAME / "data"
    return Path.home() / f".{APP_DIR_NAME}" / "data"


def resolve_data_dir() -> Path:
    """按「环境变量 → 打包默认 → 项目内 data/」的优先级解析数据目录。"""
    override = (os.environ.get(ENV_DATA_DIR) or "").strip()
    if override:
        return Path(override).expanduser()
    if is_frozen():
        return default_frozen_data_dir()
    return PROJECT_ROOT / "data"


def _ensure_data_dir(path: Path) -> None:
    """创建数据目录并验证可写。

    目录建不出来（权限不足 / 路径非法 / 磁盘只读）时必须给出**可读错误**，
    不能静默：静默的后果是后面 sqlite 连接失败，用户只看到一段 traceback。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"无法创建数据目录：{path}\n"
            f"  原因：{exc}\n"
            f"  请确认该路径可写，或用环境变量 {ENV_DATA_DIR} 指定另一个目录。"
        ) from exc

    probe = path / f".write-probe-{os.getpid()}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"数据目录不可写：{path}\n"
            f"  原因：{exc}\n"
            f"  请检查目录权限，或用环境变量 {ENV_DATA_DIR} 指定另一个目录。"
        ) from exc


#: 数据目录（数据库、日志等运行时产物都放这里）。
DATA_DIR = resolve_data_dir()
_ensure_data_dir(DATA_DIR)


def _env_files() -> tuple[str, ...]:
    """配置文件位置。

    - 源码运行：项目根目录的 ``.env``（保持与历史行为完全一致）；
    - 打包运行：数据目录下的 ``.env``（用户可编辑；``_MEIPASS`` 里的 .env
      不存在且每次启动都会变，指向它没有意义）。
    """
    if is_frozen():
        return (str(DATA_DIR / ".env"),)
    return (str(PROJECT_ROOT / ".env"),)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 服务 ----
    host: str = "127.0.0.1"
    port: int = 8000

    # ---- 数据库 ----
    database_url: str = f"sqlite:///{(DATA_DIR / 'app.db').as_posix()}"

    # ---- LLM（OpenAI 兼容接口，可指向 DeepSeek / Moonshot / 通义 / OpenAI 等）----
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.4
    llm_timeout: float = 120.0
    # 单次生成的最大输出 token。默认 0 表示「不限制」：构造请求时不发送 max_tokens 字段，
    # 由服务端用自己的默认/最大上限决定（用户明确不介意 token 消耗）。
    # 约定：<= 0 一律视为不限制；仅 > 0 时才作为 max_tokens 发给 API。
    llm_max_tokens: int = 0

    # 没有配置 api_key 时自动进入 mock 模式，保证项目开箱可跑通
    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key.strip())

    # ---- 并发与重试 ----
    worker_count: int = 3          # 后台并发 worker 数量
    max_retries: int = 2           # 单个任务最大重试次数
    retry_backoff: float = 2.0     # 指数退避基数（秒）：delay = base * 2^(attempts-1)，封顶 60s

    @property
    def static_dir(self) -> Path:
        # 打包后 __file__ 在 _MEIPASS/backend/app/ 下，.parent.parent 即 _MEIPASS/backend，
        # 与 spec 里 static 的收集位置（backend/app/static）对齐，无需特判。
        return BASE_DIR / "app" / "static"

    @property
    def templates_dir(self) -> Path:
        return BASE_DIR / "app" / "templates"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
