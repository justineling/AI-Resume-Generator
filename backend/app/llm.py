"""LLM 接入层。

- 走 OpenAI 兼容的 /chat/completions，可指向 DeepSeek / Moonshot / 通义 / OpenAI 等。
- 提供 chat_json()：要求模型输出 JSON，并做容错解析（去掉 ```json 围栏、截取首个
  JSON 片段、修复常见尾逗号）。
- 未配置 api_key 时自动降级为本地 mock，保证项目开箱即可端到端跑通。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from sqlmodel import Session

from .config import settings
from .models import AppSetting
from .providers import (
    DEFAULT_PROVIDER_ID,
    get_provider,
    is_local_url,
    match_provider_by_base_url,
)

logger = logging.getLogger("app.llm")

#: 与供应商强相关的存储键；只要其中任意一个存在，就说明用户保存过配置
_STORED_KEYS = ("llm_provider_id", "llm_base_url", "llm_model")


# --------------------------------------------------------------------------- #
# 运行时配置（DB 覆盖 env）
# --------------------------------------------------------------------------- #
def resolve_provider_id(stored_provider_id: str | None, stored_base_url: str | None) -> str:
    """解析供应商 id。

    优先级：显式存储的 provider_id > 按历史 base_url 反查 > 默认 DeepSeek。
    注意：只对**历史存储**的 base_url 反查，环境变量里的默认地址不参与，
    以保证全新安装时 DeepSeek 默认选中。
    """
    if stored_provider_id and stored_provider_id.strip():
        return stored_provider_id.strip()
    if stored_base_url and stored_base_url.strip():
        return match_provider_by_base_url(stored_base_url) or "custom"
    return DEFAULT_PROVIDER_ID


def get_runtime_config(session: Session) -> dict[str, Any]:
    rows = {s.key: s.value for s in session.query(AppSetting).all()}
    api_key = rows.get("llm_api_key") or settings.llm_api_key
    has_stored = any(k in rows for k in _STORED_KEYS)

    if has_stored:
        provider_id = resolve_provider_id(rows.get("llm_provider_id"), rows.get("llm_base_url"))
        base_url = rows.get("llm_base_url") or settings.llm_base_url
        model = rows.get("llm_model") or settings.llm_model
    elif api_key:
        # 仅通过 .env 配置了 Key：尊重 .env 的 base_url / model
        provider_id = match_provider_by_base_url(settings.llm_base_url) or "custom"
        base_url = settings.llm_base_url
        model = settings.llm_model
    else:
        # 全新安装且未配置 Key：默认使用 DeepSeek 预设，开箱即用
        preset = get_provider(DEFAULT_PROVIDER_ID) or {}
        provider_id = DEFAULT_PROVIDER_ID
        base_url = preset.get("base_url") or settings.llm_base_url
        model = (preset.get("models") or [settings.llm_model])[0]

    stored_mt = rows.get("llm_max_tokens")
    max_tokens = (
        int(stored_mt)
        if stored_mt is not None and str(stored_mt).strip() != ""
        else int(settings.llm_max_tokens)
    )

    return {
        "provider_id": provider_id,
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "temperature": float(rows.get("llm_temperature") or settings.llm_temperature),
        "worker_count": int(rows.get("worker_count") or settings.worker_count),
        # 0 是合法值（表示不限制），不能用 `or` 兜底成默认值。
        "max_tokens": max_tokens,
    }


def set_runtime_config(session: Session, **kwargs: Any) -> None:
    for key, value in kwargs.items():
        if value is None:
            continue
        row = session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=str(value))
            session.add(row)
        else:
            row.value = str(value)
    session.commit()


# --------------------------------------------------------------------------- #
# JSON 容错解析
# --------------------------------------------------------------------------- #
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def extract_json(text: str) -> Any:
    """从模型输出里尽力抽取 JSON 对象/数组。"""
    if not text:
        raise ValueError("empty model output")
    raw_output = text
    text = text.strip()

    # 1) 优先匹配 ```json ... ``` 围栏
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    # 2) 直接尝试
    for candidate in (text, _slice_braces(text, "{", "}"), _slice_braces(text, "[", "]")):
        if not candidate:
            continue
        for attempt in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                return json.loads(attempt)
            except json.JSONDecodeError:
                continue
    raise ValueError(f"无法从模型输出中解析出 JSON；原始输出前 300 字符：{raw_output[:300]!r}")


def _slice_braces(text: str, open_ch: str, close_ch: str) -> str | None:
    start = text.find(open_ch)
    end = text.rfind(close_ch)
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = config["base_url"].rstrip("/")
        self.api_key = config["api_key"]
        self.model = config["model"]
        self.temperature = float(config.get("temperature", 0.4))
        # 单次生成的最大输出 token；<= 0 表示「不限制」（不发送 max_tokens，由服务端决定）。
        # 缺省回落到全局设置（默认 0 = 不限制）。注意 0 是合法值，不能用 `or` 兜底。
        mt = config.get("max_tokens")
        self.max_tokens = int(mt) if mt is not None else int(settings.llm_max_tokens)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_key.strip())

    async def chat(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if not self.enabled:
            raise LLMError("LLM 未配置 api_key")

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        # 温度不在此设置：不发送该字段，让模型服务端使用自己的默认值。
        # max_tokens <= 0 表示「不限制」：完全不发送该字段，让服务端用自己的默认/最大值。
        if self.max_tokens > 0:
            payload["max_tokens"] = self.max_tokens
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # 本机 / 内网地址绕过 HTTP 代理（否则 localhost 的 Ollama 等会被代理拦截）；
        # 外部地址仍遵循 trust_env，兼容需要走代理的企业环境。
        async with httpx.AsyncClient(
            timeout=settings.llm_timeout, trust_env=not is_local_url(self.base_url)
        ) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
            if resp.status_code >= 400:
                raise LLMError(f"LLM 返回 {resp.status_code}: {resp.text[:300]}")
            data = resp.json()

        try:
            choice = data["choices"][0]
            content = choice["message"]["content"] or ""
        except (KeyError, IndexError) as exc:  # pragma: no cover
            raise LLMError(f"LLM 响应格式异常: {data}") from exc

        # 记录 finish_reason 与 completion_tokens，便于排查截断类问题。
        finish_reason = choice.get("finish_reason")
        usage = data.get("usage") or {}
        completion_tokens = usage.get("completion_tokens")
        logger.debug(
            "LLM 响应 model=%s finish_reason=%s completion_tokens=%s",
            self.model,
            finish_reason,
            completion_tokens,
        )

        # 关键：输出被 max_tokens 截断时，内容几乎必然是残缺的 JSON，
        # 直接抛出可读错误，避免下游报出难以理解的「无法解析 JSON」。
        # （不限制模式下一般不会触发，此处保留作为防御。）
        if finish_reason == "length":
            if self.max_tokens > 0:
                raise LLMError(
                    f"模型输出被 max_tokens 截断（当前上限 {self.max_tokens}）。"
                    "请在「设置」中调高「最大输出 token」后重试，"
                    "或缩短岗位描述后重试。"
                )
            raise LLMError(
                "模型输出被服务端上限截断。请缩短岗位描述后重试，"
                "或在「设置」中改用支持更大输出上限的模型。"
            )

        return content

    async def chat_json(self, system: str, user: str) -> Any:
        raw = await self.chat(system, user, json_mode=True)
        return extract_json(raw)

    async def chat_text(self, system: str, user: str) -> str:
        return await self.chat(system, user, json_mode=False)


def build_client(session: Session) -> LLMClient:
    return LLMClient(get_runtime_config(session))
