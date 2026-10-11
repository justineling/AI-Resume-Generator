"""LLM 供应商预设注册表（纯数据，无副作用、无网络请求）。

设计要点：
- 每个供应商给出一个 OpenAI 兼容的 ``base_url``；实际调用时 ``/chat/completions``
  会被拼接在其后（如 ``https://api.deepseek.com/chat/completions``）。
- DeepSeek 排第一并作为默认供应商（``DEFAULT_PROVIDER_ID``）。
- ``models`` 是该供应商的**常见示例模型名**，可能随版本变化；前端应通过
  「测试连接」调用 ``GET {base_url}/models`` 拉取真实列表。DeepSeek 的两个模型名
  已按官方文档核实，必须准确。
- ``requires_key``：是否需要 API Key（本地部署的 Ollama 为 False）。
- ``editable_base_url``：Base URL 是否允许用户手填（仅 ``custom`` 为 True）。
"""
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urlparse

#: 默认供应商（DeepSeek）
DEFAULT_PROVIDER_ID = "deepseek"

#: 「自定义」供应商 id，允许用户自行填写 Base URL 与模型名
CUSTOM_PROVIDER_ID = "custom"

#: 视为"本机"的主机名（命中时绕过 HTTP 代理）
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}

#: 非 DeepSeek 供应商的预设模型名可能过时，统一附带的提示语
_MODEL_HINT = "预设模型名可能随版本变化，建议点「测试连接」拉取真实列表。"

PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "deepseek",
        "label": "DeepSeek 深度求索",
        "short": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-flash", "deepseek-v4-pro"],
        "key_url": "https://platform.deepseek.com/api_keys",
        "docs_url": "https://api-docs.deepseek.com/",
        "note": "国内直连，OpenAI 兼容。deepseek-flash 性价比高，deepseek-v4-pro 能力更强。",
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "short": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "key_url": "https://platform.openai.com/api-keys",
        "docs_url": "https://platform.openai.com/docs/api-reference",
        "note": "官方接口。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "moonshot",
        "label": "Moonshot Kimi",
        "short": "Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "key_url": "https://platform.moonshot.cn/console/api-keys",
        "docs_url": "https://platform.moonshot.cn/docs",
        "note": "月之暗面 Kimi，长上下文见长。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "dashscope",
        "label": "阿里云百炼（通义千问）",
        "short": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
        "key_url": "https://bailian.console.aliyun.com/?apiKey=1",
        "docs_url": "https://help.aliyun.com/zh/model-studio/developer-reference/compatibility-of-openai-with-dashscope",
        "note": "使用百炼的 OpenAI 兼容模式。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "zhipu",
        "label": "智谱 GLM",
        "short": "智谱GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-flash"],
        "key_url": "https://open.bigmodel.cn/usercenter/apikeys",
        "docs_url": "https://open.bigmodel.cn/dev/api",
        "note": "智谱 AI 开放平台。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "volcengine",
        "label": "火山方舟（豆包）",
        "short": "豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "models": ["doubao-pro-32k", "doubao-lite-32k"],
        "key_url": "https://console.volcengine.com/ark/region:ark+cn-beijing/apiKey",
        "docs_url": "https://www.volcengine.com/docs/82379",
        "note": "火山方舟。注意：也可填推理接入点 id（ep-xxxx）。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "hunyuan",
        "label": "腾讯混元",
        "short": "混元",
        "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
        "models": ["hunyuan-turbo", "hunyuan-pro"],
        "key_url": "https://console.cloud.tencent.com/hunyuan/api-key",
        "docs_url": "https://cloud.tencent.com/document/product/1729",
        "note": "腾讯混元 OpenAI 兼容接口。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "siliconflow",
        "label": "硅基流动 SiliconFlow",
        "short": "硅基流动",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-7B-Instruct"],
        "key_url": "https://cloud.siliconflow.cn/account/ak",
        "docs_url": "https://docs.siliconflow.cn/",
        "note": "聚合多家开源模型。" + _MODEL_HINT,
        "requires_key": True,
        "editable_base_url": False,
    },
    {
        "id": "ollama",
        "label": "Ollama（本地部署）",
        "short": "Ollama",
        "base_url": "http://localhost:11434/v1",
        "models": ["llama3.1", "qwen2.5"],
        "key_url": "",
        "docs_url": "https://ollama.com/",
        "note": "本地部署，适合隐私 / 离线场景，通常无需 API Key。需先在本机启动 Ollama。" + _MODEL_HINT,
        "requires_key": False,
        "editable_base_url": False,
    },
    {
        "id": "custom",
        "label": "自定义 / Custom",
        "short": "自定义",
        "base_url": "",
        "models": [],
        "key_url": "",
        "docs_url": "",
        "note": "自行填写任意 OpenAI 兼容的 Base URL 与模型名。",
        "requires_key": True,
        "editable_base_url": True,
    },
]


def list_providers() -> list[dict[str, Any]]:
    """返回全部供应商预设（深拷贝，避免调用方误改注册表）。"""
    return [dict(p) for p in PROVIDERS]


def get_provider(provider_id: Optional[str]) -> Optional[dict[str, Any]]:
    """按 id 取供应商预设；不存在返回 None。"""
    if not provider_id:
        return None
    for p in PROVIDERS:
        if p["id"] == provider_id:
            return dict(p)
    return None


def _normalize_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def match_provider_by_base_url(base_url: Optional[str]) -> Optional[str]:
    """按 Base URL 反查供应商 id（用于向后兼容旧配置）。

    匹配不到返回 None；``custom`` 预设没有 base_url，因此不会被匹配到。
    """
    target = _normalize_url(base_url or "")
    if not target:
        return None
    for p in PROVIDERS:
        if p["base_url"] and _normalize_url(p["base_url"]) == target:
            return p["id"]
    return None


def is_local_url(url: str) -> bool:
    """判断 URL 是否指向本机（localhost / 127.0.0.1 / ::1 / 0.0.0.0 / *.localhost）。

    用于在设置了 http_proxy 的机器上让本地 / 内网 LLM 绕过代理。
    空串或非法 URL 返回 False，绝不抛异常。
    """
    try:
        host = (urlparse(url or "").hostname or "").lower()
    except (ValueError, TypeError):
        return False
    if not host:
        return False
    return host in _LOCAL_HOSTS or host.endswith(".localhost")
