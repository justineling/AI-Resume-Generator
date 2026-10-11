"""设置接口：运行时配置 LLM 与并发数（写入 DB，覆盖 .env）。

新增能力（供应商预设）：
- ``GET  /api/settings/providers``  返回供应商预设列表（纯数据）。
- ``POST /api/settings/llm/test``   校验 API Key 有效性并尽力返回真实模型列表。
- ``GET/PUT /api/settings/llm``     增加 ``provider_id`` 字段（向后兼容）。
"""
from __future__ import annotations

import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session

from ..db import get_session
from ..llm import get_runtime_config, set_runtime_config
from ..providers import (
    CUSTOM_PROVIDER_ID,
    get_provider,
    is_local_url,
    list_providers,
    match_provider_by_base_url,
)
from ..schemas import (
    LLMModel,
    LLMSettingsIn,
    LLMSettingsOut,
    LLMTestIn,
    LLMTestOut,
)

router = APIRouter(prefix="/api/settings", tags=["settings"])

#: 测试连接的超时（秒）。设短一些，避免拖住前端。
TEST_TIMEOUT = 15.0


def _hint(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}{'*' * 6}{key[-4:]}"


def _client_factory(timeout: float, base_url: str) -> httpx.AsyncClient:
    """httpx 客户端工厂（独立出来便于测试注入 mock）。

    本机 / 内网地址绕过 HTTP 代理，外部地址遵循 trust_env。
    """
    return httpx.AsyncClient(timeout=timeout, trust_env=not is_local_url(base_url))


# --------------------------------------------------------------------------- #
# 供应商预设
# --------------------------------------------------------------------------- #
@router.get("/providers")
def get_providers() -> list[dict[str, Any]]:
    """返回供应商预设列表（纯数据，无敏感信息）。"""
    return list_providers()


# --------------------------------------------------------------------------- #
# LLM 设置
# --------------------------------------------------------------------------- #
@router.get("/llm", response_model=LLMSettingsOut)
def get_llm_settings(session: Session = Depends(get_session)) -> LLMSettingsOut:
    cfg = get_runtime_config(session)
    return LLMSettingsOut(
        provider_id=cfg["provider_id"],
        base_url=cfg["base_url"],
        model=cfg["model"],
        temperature=cfg["temperature"],
        worker_count=cfg["worker_count"],
        max_tokens=cfg["max_tokens"],
        has_key=bool(cfg["api_key"]),
        key_hint=_hint(cfg["api_key"]),
        enabled=bool(cfg["api_key"]),
        mock_mode=not bool(cfg["api_key"]),
    )


@router.put("/llm", response_model=LLMSettingsOut)
def update_llm_settings(
    payload: LLMSettingsIn, session: Session = Depends(get_session)
) -> LLMSettingsOut:
    # provider_id 与 base_url 必须保持一致：
    # - 显式给了 provider_id -> 用它
    # - 只给了 base_url（未给 provider_id）-> 按 base_url 反查，匹配不到则归为 custom
    #   （否则 DB 里旧的 provider_id 会变陈旧，与新的 base_url 自相矛盾）
    # - 两者都没给（如只改 worker_count / temperature）-> 不动 provider_id，保持原值
    provider_id = payload.provider_id
    if not provider_id and payload.base_url is not None:
        provider_id = match_provider_by_base_url(payload.base_url) or CUSTOM_PROVIDER_ID

    base_url = payload.base_url
    # 选了非自定义供应商但未显式给 base_url -> 用预设地址，保证保存后配置自洽
    if provider_id and base_url is None:
        preset = get_provider(provider_id)
        if preset and preset.get("base_url"):
            base_url = preset["base_url"]

    # max_tokens 语义：<= 0 表示「不限制」（生成时不发送该字段，由服务端决定上限），
    # 0 合法；仅负数无意义，直接拒绝。
    max_tokens = payload.max_tokens
    if max_tokens is not None and max_tokens < 0:
        raise HTTPException(
            status_code=400,
            detail="最大输出 token 不能为负数；填 0 表示不限制（由模型服务端决定上限）",
        )

    set_runtime_config(
        session,
        llm_provider_id=provider_id,
        llm_base_url=base_url,
        llm_api_key=payload.api_key,
        llm_model=payload.model,
        llm_temperature=payload.temperature,
        worker_count=payload.worker_count,
        llm_max_tokens=max_tokens,
    )
    return get_llm_settings(session)


# --------------------------------------------------------------------------- #
# 测试连接
# --------------------------------------------------------------------------- #
def _elapsed_ms(started: float) -> int:
    return max(0, int((time.perf_counter() - started) * 1000))


def _extract_models(data: Any) -> list[LLMModel]:
    """从 /models 响应里解析模型列表，兼容多种常见结构。"""
    items: Any = []
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or data.get("result") or []
    elif isinstance(data, list):
        items = data
    out: list[LLMModel] = []
    for it in items:
        if isinstance(it, str):
            mid = it.strip()
            if mid:
                out.append(LLMModel(id=mid, name=mid))
        elif isinstance(it, dict):
            mid = str(it.get("id") or it.get("model") or it.get("name") or "").strip()
            if not mid:
                continue
            name = str(it.get("name") or it.get("id") or mid)
            out.append(LLMModel(id=mid, name=name))
    return out


def _success(latency_ms: int, models: list[LLMModel]) -> LLMTestOut:
    return LLMTestOut(
        ok=True,
        message=f"连接成功 · 延迟 {latency_ms}ms · 发现 {len(models)} 个可用模型",
        latency_ms=latency_ms,
        models=models,
        status_code=200,
    )


def _fail(status_code: int, latency_ms: int) -> LLMTestOut:
    if status_code in (401, 403):
        msg = "API Key 无效或已过期"
    elif status_code == 404:
        msg = "接口地址不正确，请检查 Base URL"
    elif status_code == 405:
        msg = "接口不支持该请求方式，请检查 Base URL"
    elif status_code == 429:
        msg = "请求过于频繁或额度不足"
    elif status_code >= 500:
        msg = f"服务端错误（HTTP {status_code}），请稍后重试"
    else:
        msg = f"连接失败（HTTP {status_code}）"
    return LLMTestOut(ok=False, message=msg, latency_ms=latency_ms, models=[], status_code=status_code)


def _error(message: str, latency_ms: int, status_code: int | None = None) -> LLMTestOut:
    return LLMTestOut(ok=False, message=message, latency_ms=latency_ms, models=[], status_code=status_code)


@router.post("/llm/test", response_model=LLMTestOut)
async def test_llm(
    payload: LLMTestIn, session: Session = Depends(get_session)
) -> LLMTestOut:
    """测试 API Key 有效性，并尽力返回真实模型列表。

    未填写 api_key 时回落到已存储的 key（仅当供应商一致时），
    这样已配置过的用户无需重新输入即可测试连接。
    绝不把 api_key 写入日志或错误信息。
    """
    provider = get_provider(payload.provider_id)
    if provider is None:
        raise HTTPException(status_code=400, detail=f"未知的供应商：{payload.provider_id}")

    base_url = (payload.base_url or provider.get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail="请填写 Base URL")

    key = (payload.api_key or "").strip()
    if not key:
        # 回落到已存储的 key：仅当「当前测试的供应商」与「已配置的供应商」一致时，
        # 避免拿 A 家的 key 去测 B 家造成误判。
        cfg = get_runtime_config(session)
        stored_key = (cfg.get("api_key") or "").strip()
        stored_provider = cfg.get("provider_id")
        stored_base = (cfg.get("base_url") or "").strip().rstrip("/")
        same_provider = stored_provider == payload.provider_id
        same_custom_base = (
            payload.provider_id == CUSTOM_PROVIDER_ID
            and stored_provider == CUSTOM_PROVIDER_ID
            and stored_base == base_url
        )
        if stored_key and (same_provider or same_custom_base):
            key = stored_key
    if not key:
        return _error("请先填写 API Key", 0)

    preset_models = [LLMModel(id=m, name=m) for m in (provider.get("models") or [])]
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    started = time.perf_counter()

    try:
        async with _client_factory(TEST_TIMEOUT, base_url) as client:
            resp = await client.get(f"{base_url}/models", headers=headers)

            if resp.status_code == 200:
                latency = _elapsed_ms(started)
                try:
                    models = _extract_models(resp.json())
                except ValueError:
                    models = []
                return _success(latency, models or preset_models)

            # 有些兼容端点未实现 /models -> 回退一次最小 chat 请求判定 key 有效性
            if resp.status_code in (404, 405):
                fallback_model = payload.model or (provider.get("models") or [None])[0]
                payload_body = {
                    "model": fallback_model or "gpt-3.5-turbo",
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                }
                resp2 = await client.post(
                    f"{base_url}/chat/completions", json=payload_body, headers=headers
                )
                latency = _elapsed_ms(started)
                if resp2.status_code == 200:
                    return _success(latency, preset_models)
                return _fail(resp2.status_code, latency)

            return _fail(resp.status_code, _elapsed_ms(started))

    except httpx.TimeoutException:
        return _error("连接超时，请检查网络或 Base URL", _elapsed_ms(started))
    except httpx.ConnectError:
        return _error("无法连接到该地址，请检查网络或 Base URL", _elapsed_ms(started))
    except httpx.HTTPError:
        return _error("网络请求失败，请检查网络或 Base URL", _elapsed_ms(started))
