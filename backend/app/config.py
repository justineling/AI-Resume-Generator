"""应用配置。

所有配置都可通过环境变量或项目根目录的 .env 覆盖。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/
PROJECT_ROOT = BASE_DIR.parent                      # 项目根目录
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
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
        return BASE_DIR / "app" / "static"

    @property
    def templates_dir(self) -> Path:
        return BASE_DIR / "app" / "templates"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
