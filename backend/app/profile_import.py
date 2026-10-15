"""粘贴简历文本 -> 结构化资料。

编排逻辑与 ``fetch.extract_jd_from_text`` 一致：**LLM 优先，失败 / 未配置 Key 时回落启发式**。
- LLM：``SYSTEM_PARSE_RESUME`` 约束「只提取文本真实存在的信息，绝不编造」。
- 无 Key：``mock.parse_resume_text`` 做基础的正则 / 分块提取，绝不抛错。

两个来源的输出都先经过 :func:`normalize_profile` 收敛成 ``ProfileIn`` 形状，
保证写库时字段类型安全（列表里永远是 dict，字符串有长度上限）。
"""
from __future__ import annotations

from typing import Any, Optional

from .llm import LLMClient, LLMError
from .mock import parse_resume_text
from .prompts import SYSTEM_PARSE_RESUME

MAX_TEXT_CHARS = 20000

_SCALARS = {
    "name": 120,
    "headline": 160,
    "email": 160,
    "phone": 60,
    "location": 120,
    "summary": 4000,
    "avatar": 500,
}

# 每个结构化列表字段允许的键，以及值类型（str | list[str]）
_LIST_FIELDS: dict[str, dict[str, type]] = {
    "links": {"label": str, "url": str},
    "experiences": {
        "company": str, "title": str, "location": str, "start": str, "end": str,
        "summary": str, "achievements": list, "tech": list,
    },
    "projects": {
        "name": str, "role": str, "start": str, "end": str, "link": str,
        "summary": str, "highlights": list,
    },
    "skills": {"name": str, "level": str, "category": str},
    "education": {
        "school": str, "degree": str, "major": str, "start": str, "end": str,
        "highlights": list,
    },
    "certificates": {"name": str, "issuer": str, "date": str},
    "awards": {"name": str, "date": str, "detail": str},
    "languages": {"name": str, "level": str},
}

_STR_CAP = 400
_LIST_CAP = 12


def _s(value: Any, cap: int = _STR_CAP) -> str:
    if value is None:
        return ""
    s = str(value).strip()
    return s[:cap]


def _strings(value: Any, cap: int = _STR_CAP) -> list[str]:
    items = value if isinstance(value, list) else ([value] if value else [])
    out: list[str] = []
    for it in items:
        s = _s(it, cap)
        if s:
            out.append(s)
        if len(out) >= _LIST_CAP:
            break
    return out


def _age(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 < value < 120 else None
    if isinstance(value, str):
        digits = "".join(ch for ch in value if ch.isdigit())
        if digits:
            n = int(digits[:3])
            return n if 0 < n < 120 else None
    return None


def normalize_profile(raw: Any) -> dict[str, Any]:
    """把任意来源的解析结果收敛成 ProfileIn 形状的 dict。"""
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {k: _s(raw.get(k), cap) for k, cap in _SCALARS.items()}
    out["age"] = _age(raw.get("age"))

    for field, schema in _LIST_FIELDS.items():
        items = raw.get(field)
        clean: list[dict[str, Any]] = []
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                row: dict[str, Any] = {}
                for key, typ in schema.items():
                    if typ is list:
                        row[key] = _strings(it.get(key))
                    else:
                        row[key] = _s(it.get(key))
                if any((v if isinstance(v, str) else bool(v)) for v in row.values()):
                    clean.append(row)
                if len(clean) >= 30:
                    break
        out[field] = clean

    extra = raw.get("extra")
    out["extra"] = {str(k): _s(v) for k, v in extra.items()} if isinstance(extra, dict) else {}
    return out


def has_content(data: dict[str, Any]) -> bool:
    """判断解析结果是否含有效信息（用于决定是否回落到启发式）。"""
    for key in ("name", "headline", "email", "phone", "location", "summary"):
        if data.get(key):
            return True
    return any(data.get(f) for f in _LIST_FIELDS)


async def parse_profile_text(text: str, client: Optional[LLMClient]) -> dict[str, Any]:
    """把简历原文解析成 ProfileIn 形状的 dict。LLM 优先，失败回落启发式。"""
    text = (text or "").strip()
    if client is not None and client.enabled and text:
        try:
            out = await client.chat_json(
                SYSTEM_PARSE_RESUME,
                f"以下是用户的简历原文：\n\n<<<RESUME\n{text[:MAX_TEXT_CHARS]}\nRESUME>>>",
            )
            data = normalize_profile(out)
            if has_content(data):
                return data
        except (LLMError, ValueError):
            pass  # 回落启发式
    return normalize_profile(parse_resume_text(text))


# --------------------------------------------------------------------------- #
# 合并（只增补，不覆盖）
# --------------------------------------------------------------------------- #
#: 各列表字段的「去重身份键」。按这些字段判断一条记录是否已存在，
#: 避免同一段经历/同一个技能被重复追加。
_IDENTITY: dict[str, tuple[str, ...]] = {
    "links": ("url", "label"),
    "experiences": ("company", "title"),
    "projects": ("name",),
    "skills": ("name",),
    "education": ("school", "degree", "major"),
    "certificates": ("name",),
    "awards": ("name",),
    "languages": ("name",),
}


def _identity(rows: list[dict[str, Any]], keys: tuple[str, ...]) -> set[str]:
    out: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sig = "|".join(_s(row.get(k)).lower() for k in keys)
        if sig.strip("|"):
            out.add(sig)
    return out


def merge_profiles(base: Any, incoming: Any) -> dict[str, Any]:
    """把 `incoming` **增补**进 `base`，返回新 dict（不改动入参）。

    语义与「AI 问答」的回答整合一致 —— **只增补，不覆盖**：
    - 标量字段：base 已有非空值 → 保留 base（用户手填的更可信）；base 为空 → 用 incoming 填充。
    - 列表字段：base 在前，incoming 中「身份键」未出现的条目追加在后面（去重）。
    - 两者都先过 :func:`normalize_profile` 收敛，保证类型安全。

    这样「粘贴简历导入」不会把用户在表单里已经填好、或在之前问答中补充的内容冲掉。
    """
    b = normalize_profile(base)
    inc = normalize_profile(incoming)

    out: dict[str, Any] = dict(b)

    # 1) 标量：只填空缺
    for key in _SCALARS:
        if not b.get(key) and inc.get(key):
            out[key] = inc[key]
    if out.get("age") in (None, 0) and inc.get("age"):
        out["age"] = inc["age"]

    # 2) 列表：去重后追加
    for field, keys in _IDENTITY.items():
        cur = list(b.get(field) or [])
        seen = _identity(cur, keys)
        for row in (inc.get(field) or []):
            if not isinstance(row, dict):
                continue
            sig = "|".join(_s(row.get(k)).lower() for k in keys)
            if sig.strip("|") and sig in seen:
                continue
            seen.add(sig)
            cur.append(row)
        out[field] = cur

    # 3) extra：键级只填空缺
    merged_extra = dict(b.get("extra") or {})
    for k, v in (inc.get("extra") or {}).items():
        if not merged_extra.get(k) and v:
            merged_extra[k] = v
    out["extra"] = merged_extra
    return out


def merge_summary(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int]:
    """统计本次合并各列表字段新增了多少条（用于给用户可读反馈）。"""
    delta: dict[str, int] = {}
    for field in _IDENTITY:
        n = len(after.get(field) or []) - len(before.get(field) or [])
        if n > 0:
            delta[field] = n
    return delta
