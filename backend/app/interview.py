"""AI 面试问答（flashcard）：先问清楚，再写简历。

两个使用场景共用同一套机制：
1. 资料页：基于当前资料找出「可以更专业/更具体」的点，提问后整合回全局资料。
2. 工作区：基于 岗位要求 vs 资料快照 找出「岗位需要但资料缺依据」的缺口，提问后整合进该对话的快照，
   确保生成简历时「匹配分析」的每一条都有据可依。

无 API Key 时走启发式降级（mock），保证功能链路可用。
"""
from __future__ import annotations

import re
from typing import Any, Optional

from .llm import LLMClient, LLMError
from .prompts import (
    SYSTEM_INTERVIEW_APPLY,
    SYSTEM_INTERVIEW_APPLY_CHAT,
    SYSTEM_INTERVIEW_QUESTIONS,
)

MAX_QUESTIONS = 6


#: 判断「资料里是否有依据」时用到的文本来源
def _profile_text(profile: dict[str, Any]) -> str:
    parts: list[str] = [
        str(profile.get("headline") or ""),
        str(profile.get("summary") or ""),
    ]
    for e in profile.get("experiences") or []:
        if isinstance(e, dict):
            parts += [str(e.get("company") or ""), str(e.get("title") or ""),
                      " ".join(str(a) for a in (e.get("achievements") or [])),
                      " ".join(str(t) for t in (e.get("tech") or []))]
    for p in profile.get("projects") or []:
        if isinstance(p, dict):
            parts += [str(p.get("name") or ""), str(p.get("summary") or ""),
                      " ".join(str(h) for h in (p.get("highlights") or []))]
    for s in profile.get("skills") or []:
        if isinstance(s, dict):
            parts += [str(s.get("name") or ""), str(s.get("note") or "")]
    for c in profile.get("certificates") or []:
        if isinstance(c, dict):
            parts.append(str(c.get("name") or ""))
    return " ".join(parts).lower()


def _norm_questions(raw: Any) -> list[dict[str, str]]:
    """收敛模型返回的问题列表：字段补全、去重、限数量。"""
    items = raw
    if isinstance(raw, dict):
        items = raw.get("questions") or []
    if not isinstance(items, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()
        if not q or q in seen:
            continue
        seen.add(q)
        out.append({
            "id": f"q{i + 1}",
            "topic": str(it.get("topic") or "补充信息").strip()[:20],
            "question": q[:400],
            "hint": str(it.get("hint") or "").strip()[:200],
            "placeholder": str(it.get("placeholder") or "").strip()[:120],
        })
        if len(out) >= MAX_QUESTIONS:
            break
    return out


def _profile_gaps_heuristic(profile: dict[str, Any]) -> list[dict[str, str]]:
    """无 LLM 时：按资料里常见的「不够具体」之处生成问题。"""
    qs: list[dict[str, str]] = []
    exps = [e for e in (profile.get("experiences") or []) if isinstance(e, dict)]
    skills = [s for s in (profile.get("skills") or []) if isinstance(s, dict)]
    summary = str(profile.get("summary") or "").strip()

    if exps and all(len(e.get("achievements") or []) < 2 for e in exps):
        top = exps[0]
        qs.append({
            "id": "q1", "topic": "成果量化",
            "question": f"你在「{top.get('company') or '最近一段经历'}」主要负责什么？"
                        "有没有能体现结果的数据（性能提升、稳定性、效率或覆盖范围）？",
            "hint": "说清：做了什么 → 用了什么技术 → 带来什么可验证的结果；没有具体数字也可以描述影响范围。",
            "placeholder": "例：重构订单核心链路，引入状态机与幂等设计，接口 P99 从 800ms 降到 120ms",
        })
    if skills and all(not str(s.get("note") or "").strip() for s in skills):
        qs.append({
            "id": f"q{len(qs) + 1}", "topic": "技能深度",
            "question": f"你最拿手的技能（如 {skills[0].get('name') or '核心技能'}）"
                        "通常用在什么场景？做到过什么程度？",
            "hint": "说清使用场景与深度：是日常开发、性能调优，还是架构设计与故障排查。",
            "placeholder": "例：MySQL 用于交易库表设计与慢查询治理，独立完成索引重构与事务边界调整",
        })
    if not summary:
        qs.append({
            "id": f"q{len(qs) + 1}", "topic": "个人定位",
            "question": "用一句话概括你的专业定位与最擅长的方向？",
            "hint": "参考：领域 + 核心能力 + 代表成果。",
            "placeholder": "例：高并发后端工程师，专注性能优化与服务稳定性",
        })
    if not qs:
        qs.append({
            "id": "q1", "topic": "近期工作",
            "question": "最近一段经历里，你最有代表性的一项工作是什么？",
            "hint": "说清：背景 → 你的动作与所用技术 → 结果。",
            "placeholder": "例：主导支付链路重构，落地灰度发布，回滚时间从 30 分钟降到 2 分钟",
        })
    return qs[:MAX_QUESTIONS]


def _chat_gaps_heuristic(profile: dict[str, Any], analysis: dict[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """无 LLM 时：用关键词重合度找「岗位要求但资料缺依据」的缺口。"""
    ptext = _profile_text(profile)
    reqs = [r for r in (analysis.get("requirements") or []) if isinstance(r, dict)]
    gaps: list[dict[str, str]] = []
    for r in reqs:
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        # 取要求里的关键词（英文词 + 中文 2-4 字词块），看资料里是否出现
        words = re.findall(r"[a-zA-Z][a-zA-Z0-9+.#\-]{1,}", text) + re.findall(r"[\u4e00-\u9fff]{2,4}", text)
        keys = [w.lower() for w in words if len(w) >= 2]
        if not keys:
            continue
        hit = sum(1 for k in keys if k in ptext)
        if hit == 0:
            gaps.append({"requirement": text[:160], "why_missing": "资料中没有找到对应经历或技能描述"})
    out: list[dict[str, str]] = []
    for i, g in enumerate(gaps[:MAX_QUESTIONS]):
        out.append({
            "id": f"q{i + 1}",
            "topic": "待补充经历",
            "question": f"岗位要求「{g['requirement']}」，你的资料里暂未体现相关经历。"
                        "你在这方面的实际情况是怎样的？（没有也可以说没有，我们会如实处理）",
            "hint": "如实回答即可：可以是有相关实践、可迁移经验，或暂未接触。不要为了匹配而编造。",
            "placeholder": "例：在 ACME 用 Docker 做过服务容器化，了解 K8s 的 Pod/Service/Deployment 概念",
        })
    return out, gaps[:MAX_QUESTIONS]


# --------------------------------------------------------------------------- #
# 生成问题
# --------------------------------------------------------------------------- #
async def generate_questions(
    profile: dict[str, Any],
    analysis: Optional[dict[str, Any]],
    client: Optional[LLMClient],
) -> dict[str, Any]:
    """生成 flashcard 问题。有 analysis 时聚焦「岗位缺口」，否则聚焦「资料完善」。"""
    has_analysis = bool(analysis and (analysis.get("requirements") or analysis.get("sections")))
    if client is not None and client.enabled:
        try:
            user = (
                "【用户资料】\n" + _json(profile)
                + ("\n\n【岗位解析与匹配分析】\n" + _json(analysis) if has_analysis else
                   "\n\n（本次没有岗位信息，请聚焦如何把资料写得更专业、更具体）")
            )
            out = await client.chat_json(SYSTEM_INTERVIEW_QUESTIONS, user)
            qs = _norm_questions(out)
            if qs:
                return {"questions": qs, "source": "llm"}
        except (LLMError, ValueError):
            pass  # 回落启发式

    if has_analysis:
        qs, gaps = _chat_gaps_heuristic(profile, analysis or {})
        return {"questions": qs, "gaps": gaps, "source": "mock"}
    return {"questions": _profile_gaps_heuristic(profile), "source": "mock"}


# --------------------------------------------------------------------------- #
# 整合回答
# --------------------------------------------------------------------------- #
def _merge_delta(profile: dict[str, Any], delta: Any) -> dict[str, Any]:
    """把 LLM 返回的**增量**合并进资料。

    为什么用增量：早期版本让模型输出整份 profile JSON，资料一大就要 90s+ 才返回
    （输出 token 太多）。改为只输出新增片段后，速度回到 10s 量级。
    只做「追加」，不改写、不删除任何原有字段。
    """
    if not isinstance(delta, dict):
        return profile
    p = dict(profile)

    # 1) 个人概述追加
    add_summary = str(delta.get("summary_append") or "").strip()
    if add_summary:
        old = str(p.get("summary") or "").strip()
        if add_summary not in old:
            p["summary"] = (old + "\n" + add_summary).strip() if old else add_summary

    # 2) 经历要点追加（按 exp_index 指向已有经历；越界忽略，绝不新建假经历）
    exps = list(p.get("experiences") or [])
    changed_exp = False
    for item in (delta.get("achievements") or []):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        try:
            idx = int(item.get("exp_index", 0))
        except (TypeError, ValueError):
            idx = 0
        if 0 <= idx < len(exps) and isinstance(exps[idx], dict):
            exp = dict(exps[idx])
            ach = list(exp.get("achievements") or [])
            if text not in ach:
                ach.append(text[:400])
                changed_exp = True
            exp["achievements"] = ach
            exps[idx] = exp
    if changed_exp:
        p["experiences"] = exps

    # 3) 技能深度线索（只更新已存在的技能，不新增技能名——避免造出用户没有的技能）
    notes = [n for n in (delta.get("skill_notes") or []) if isinstance(n, dict)]
    if notes:
        skills = list(p.get("skills") or [])
        by_name = {str(s.get("name") or "").strip().lower(): i
                   for i, s in enumerate(skills) if isinstance(s, dict)}
        changed = False
        for n in notes:
            name = str(n.get("name") or "").strip().lower()
            note = str(n.get("note") or "").strip()
            if not name or not note or name not in by_name:
                continue
            i = by_name[name]
            sk = dict(skills[i])
            if not str(sk.get("note") or "").strip():
                sk["note"] = note[:200]
                skills[i] = sk
                changed = True
        if changed:
            p["skills"] = skills

    # 4) 新项目追加（按 name 去重）
    projs = list(p.get("projects") or [])
    existing = {str(x.get("name") or "").strip().lower() for x in projs if isinstance(x, dict)}
    added = False
    for pr in (delta.get("projects_append") or []):
        if not isinstance(pr, dict):
            continue
        nm = str(pr.get("name") or "").strip()
        if not nm or nm.lower() in existing:
            continue
        projs.append({
            "name": nm[:120],
            "summary": str(pr.get("summary") or "")[:400],
            "highlights": [str(h)[:300] for h in (pr.get("highlights") or []) if str(h).strip()][:6],
        })
        existing.add(nm.lower())
        added = True
    if added:
        p["projects"] = projs

    return p


def _apply_heuristic(profile: dict[str, Any], answers: list[dict[str, str]]) -> dict[str, Any]:
    """无 LLM 时：把回答作为要点追加到最相关的经历 / 概述里（只增补）。"""
    p = {k: (list(v) if isinstance(v, list) else v) for k, v in profile.items()}
    texts = [str(a.get("answer") or "").strip() for a in answers if str(a.get("answer") or "").strip()]
    if not texts:
        return p
    exps = p.get("experiences")
    if isinstance(exps, list) and exps and isinstance(exps[0], dict):
        exp = dict(exps[0])
        ach = list(exp.get("achievements") or [])
        for t in texts:
            if t not in ach:
                ach.append(t[:300])
        exp["achievements"] = ach
        exps = list(exps)
        exps[0] = exp
        p["experiences"] = exps
    else:
        summary = str(p.get("summary") or "").strip()
        extra = "；".join(texts)[:600]
        p["summary"] = (summary + "；" + extra).strip("；") if summary else extra
    return p


async def apply_answers(
    profile: dict[str, Any],
    answers: list[dict[str, str]],
    client: Optional[LLMClient],
    *,
    for_chat: bool = False,
) -> dict[str, Any]:
    """把用户回答整合进资料（只增补）。返回新的资料 dict。"""
    payload = [{"topic": a.get("topic", ""), "question": a.get("question", ""),
                "answer": a.get("answer", "")} for a in answers
               if str(a.get("answer") or "").strip()]
    if not payload:
        return profile

    if client is not None and client.enabled:
        system = SYSTEM_INTERVIEW_APPLY_CHAT if for_chat else SYSTEM_INTERVIEW_APPLY
        try:
            user = (
                "【当前资料 JSON】（只读参考，用于判断内容归属，**不要回显**）\n" + _json(profile)
                + "\n\n【用户的补充回答】\n" + _json(payload, indent=1)
                + "\n\n请按增量结构输出需要新增的内容。"
            )
            delta = await client.chat_json(system, user)
            return _merge_delta(profile, delta)
        except (LLMError, ValueError):
            pass

    return _apply_heuristic(profile, payload)


def _json(obj: Any, indent: int = 2) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, indent=indent)
