"""单个 chat 的生成流水线。

阶段（每一步都写进度 + 广播事件）：
  10%  载入 chat 的资料快照
  25%  解析岗位要求（分节 + requirements + employer_questions）
  60%  规划版式 + 生成内容 + 覆盖清单（LLM / 启发式）
  85%  渲染自包含 HTML
  100% 落库（覆盖语义：删旧简历建新简历，并记一条"重新生成"的 EditLog）

关键设计：
- 入参是 `chat_id`（由旧的 task_id 演进而来）；资料来自 `Chat.profile_snapshot`
  而非全局 Profile，保证 chat 是"冻结的工作上下文"。
- 覆盖语义：一个 chat 恰好一份简历。再次生成会删旧建新；若已有旧简历，
  记一条 `EditLog(instruction="重新生成")`（填 before_html/after_html），
  使前端"回退上一版"仍可救回。

失败时抛异常，由调度器负责重试与状态更新。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from . import mock, prompts
from .db import session_scope
from .events import hub
from .llm import LLMError, build_client
from .models import Chat, ChatMessage, EditLog, Profile, Resume
from .renderer import render_resume_html


class TaskCancelled(Exception):
    """chat 在运行中被取消（区别于失败，不应触发重试逻辑）。"""


STAGE_LOAD = "载入资料快照"
STAGE_ANALYZE = "解析岗位要求"
STAGE_GENERATE = "规划版式并生成内容"
STAGE_RENDER = "渲染简历版式"
STAGE_DONE = "完成"

# 组 prompt 前的岗位文本上限，避免超长 JD 无截断地进入模型（成本/延迟不可控）
MAX_RAW_TEXT = 20000

EMPTY_PROFILE_MSG = "尚未填写资料库，请先在「资料」页保存你的资料后再生成简历"

# Profile 字段（快照 -> 生成用 dict）
_PROFILE_SCALAR = ("name", "headline", "email", "phone", "location", "age", "avatar", "summary")
_PROFILE_LISTS = ("links", "education", "experiences", "projects", "skills",
                  "certificates", "languages", "awards")


async def _emit(chat_id: int, progress: int, stage: str, **extra: Any) -> None:
    payload = {"chat_id": chat_id, "progress": progress, "stage": stage, **extra}
    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if chat:
            chat.progress = progress
            chat.stage = stage
            if extra.get("status"):
                chat.status = extra["status"]
            chat.updated_at = datetime.now()
    await hub.broadcast("chat_progress", payload)


def _profile_to_dict(p: Profile | None) -> dict[str, Any]:
    """把 Profile 转成 dict；p 为 None（尚未保存过资料）时返回空 dict。"""
    if p is None:
        return {}
    return {
        "name": p.name, "headline": p.headline, "email": p.email, "phone": p.phone,
        "location": p.location, "age": p.age, "avatar": p.avatar, "summary": p.summary,
        "links": p.links, "education": p.education, "experiences": p.experiences,
        "projects": p.projects, "skills": p.skills, "certificates": p.certificates,
        "languages": p.languages, "awards": p.awards, "extra": p.extra,
    }


def _profile_from_snapshot(snapshot: dict[str, Any] | None) -> dict[str, Any]:
    """把 Chat.profile_snapshot 归一化为生成用 dict（补齐默认值，避免缺键）。"""
    snap = snapshot or {}
    out: dict[str, Any] = {}
    for key in _PROFILE_SCALAR:
        val = snap.get(key)
        if key == "age":
            out[key] = val
        else:
            out[key] = val if val is not None else ""
    for key in _PROFILE_LISTS:
        val = snap.get(key)
        out[key] = list(val) if isinstance(val, list) else []
    out["extra"] = dict(snap.get("extra") or {})
    return out


def _is_profile_empty(profile: dict[str, Any]) -> bool:
    """姓名、经历、技能、教育全为空，即视为"空资料"——没有素材生成不出有意义的简历。"""
    if (profile.get("name") or "").strip():
        return False
    return not any(profile.get(k) for k in ("experiences", "skills", "education"))


# 空快照自动同步全局资料时，追加到对话里的说明消息文案
SNAPSHOT_SYNC_NOTICE = "检测到本对话资料为空，已自动采用最新的资料库内容。"


def ensure_snapshot_usable(session: Session, chat: Chat) -> bool:
    """生成前检查点：空快照 + 未定制 + 全局有资料时，自动同步全局资料进快照。

    修复「先建对话（快照拷贝到当时的空资料）→ 再去资料页填写 → 回来生成报
    『尚未填写资料库』」的新用户时序坑；等价于一次自动 ``profile/reset``，
    已存在的老数据同样可自愈。interview 端点与 :func:`run_chat` 共用本逻辑。

    三个条件都满足才同步：
    1. 快照还原出的 profile 为空（没有素材可生成）——由调用方先判定；
    2. ``profile_dirty == False``（用户没有在对话内定制过快照）。定制过的
       快照是"冻结的工作上下文"，静默覆盖会毁掉定制，绝不自动改写；
    3. 全局资料非空（确实有内容可同步）。

    成功同步时把快照与一条 notice 消息写入 session 但**不 commit**——由调用方
    负责（请求路径自行 commit；pipeline 走 ``session_scope`` 退出时 commit）。
    返回 True 表示同步完成、快照已可用；False 则维持原有报错路径
    （含「已定制」与「全局也为空」两种情况）。
    """
    if chat.profile_dirty:
        return False
    p = session.exec(select(Profile).order_by(Profile.id)).first()  # type: ignore[attr-defined]
    global_dict = _profile_to_dict(p)
    if _is_profile_empty(global_dict):
        return False
    chat.profile_snapshot = global_dict
    chat.updated_at = datetime.now()
    session.add(chat)
    session.add(ChatMessage(
        chat_id=chat.id, role="assistant", kind="notice",
        content=SNAPSHOT_SYNC_NOTICE, meta={"type": "snapshot_sync"},
    ))
    session.flush()
    return True


def load_profile() -> dict[str, Any]:
    """读取全局资料库（用于新建 chat 时做快照）。"""
    with session_scope() as session:
        p = session.exec(select(Profile).order_by(Profile.id)).first()
        return _profile_to_dict(p)


async def run_chat(chat_id: int, should_cancel: Callable[[], bool] | None = None) -> int:
    """执行一个 chat 的生成，返回 resume_id。异常向上抛给调度器。

    通过注入 ``should_cancel`` 回调（而非直接 import scheduler）检查取消状态，
    避免 pipeline <-> scheduler 的循环导入。每个阶段开始前检查一次，被取消时抛出
    ``TaskCancelled``，由调度器捕获并保持 cancelled 状态、不触发重试。
    """

    def _check_cancelled() -> None:
        if should_cancel is not None and should_cancel():
            raise TaskCancelled(f"chat {chat_id} 已被取消")

    _check_cancelled()

    # 1) 载入 chat 的资料快照
    await _emit(chat_id, 10, STAGE_LOAD, status="running")
    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if not chat:
            raise RuntimeError(f"chat {chat_id} 不存在")
        raw_text = chat.raw_text or ""
        # 超长 JD 截断，避免无上限地进入 prompt
        truncated = len(raw_text) > MAX_RAW_TEXT
        if truncated:
            raw_text = raw_text[:MAX_RAW_TEXT]
        profile = _profile_from_snapshot(chat.profile_snapshot)
        if _is_profile_empty(profile) and not ensure_snapshot_usable(session, chat):
            raise RuntimeError(EMPTY_PROFILE_MSG)
        profile = _profile_from_snapshot(chat.profile_snapshot)
        client = build_client(session)

    # 2) 岗位解析（分节 + requirements + employer_questions）
    _check_cancelled()
    await _emit(chat_id, 25, STAGE_ANALYZE)
    if client.enabled:
        try:
            analysis = await client.chat_json(prompts.SYSTEM_ANALYZE, prompts.build_analyze_user(raw_text))
        except (LLMError, ValueError) as exc:
            raise RuntimeError(f"岗位解析失败：{exc}") from exc
    else:
        analysis = mock.analyze_job(raw_text)
    analysis = _normalize_analysis(analysis)
    if truncated:
        analysis["truncated"] = True

    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        if chat:
            chat.job_analysis = analysis
            if (not chat.title or chat.title in ("新对话", "未命名岗位")) and analysis.get("job_title"):
                chat.title = analysis["job_title"][:80]
            chat.updated_at = datetime.now()

    # 3) 版式规划 + 内容生成 + 覆盖清单
    _check_cancelled()
    await _emit(chat_id, 60, STAGE_GENERATE)
    if client.enabled:
        try:
            spec = await client.chat_json(
                prompts.SYSTEM_GENERATE, prompts.build_generate_user(profile, analysis)
            )
        except (LLMError, ValueError) as exc:
            raise RuntimeError(f"内容生成失败：{exc}") from exc
    else:
        spec = mock.generate_spec(profile, analysis)
    normalized = _normalize_spec(spec, profile, analysis)
    coverage_items = normalized.pop("coverage", [])
    coverage_summary = normalized.pop("coverage_summary", {})
    coverage_obj = {"coverage": coverage_items, "summary": coverage_summary}

    # 4) 渲染（coverage 只属于 Resume 元数据，绝不进入正文）
    _check_cancelled()
    await _emit(chat_id, 85, STAGE_RENDER)
    html = render_resume_html(normalized)

    # 5) 落库（覆盖语义：一 chat 一简历；落库前最后确认一次取消状态）
    _check_cancelled()
    with session_scope() as session:
        chat = session.get(Chat, chat_id)
        job_title = (chat.title if chat else "") or ""
        if job_title in ("", "新对话", "未命名岗位"):
            job_title = analysis.get("job_title") or ""
        resume_title = job_title or normalized["header"].get("headline") or "简历"

        existing = list(session.exec(select(Resume).where(Resume.chat_id == chat_id)).all())
        old_html = ""
        prev_version = 0
        for r in existing:
            if r.html:
                old_html = r.html
            prev_version = max(prev_version, r.version or 0)
            for edit in session.exec(select(EditLog).where(EditLog.resume_id == r.id)).all():
                session.delete(edit)
            session.flush()
            session.delete(r)
        session.flush()

        resume = Resume(
            chat_id=chat_id,
            title=resume_title[:80],
            theme=normalized.get("theme", "ark"),
            html=html,
            spec=normalized,
            coverage=coverage_obj,
            version=(prev_version + 1) if prev_version else 1,
        )
        session.add(resume)
        session.flush()
        resume_id = resume.id

        # 重新生成：记一条可回退的 EditLog（前端"回退上一版"据此救回旧内容）
        if old_html:
            session.add(EditLog(
                resume_id=resume_id, instruction="重新生成", scope="whole", target="",
                ok=True, before_html=old_html, after_html=html,
            ))

        if chat:
            chat.status = "succeeded"
            chat.progress = 100
            chat.stage = STAGE_DONE
            chat.error = ""
            chat.updated_at = datetime.now()
            chat.finished_at = datetime.now()

        session.add(ChatMessage(
            chat_id=chat_id, role="assistant", kind="notice",
            content=f"已生成简历：{resume_title}", meta={"resume_id": resume_id},
        ))

    await _emit(chat_id, 100, STAGE_DONE, status="succeeded", resume_id=resume_id)
    await hub.broadcast("chat_updated", {
        "chat_id": chat_id, "status": "succeeded", "progress": 100,
        "stage": STAGE_DONE, "resume_id": resume_id,
    })
    return resume_id


# 兼容别名：旧调度器/测试以 run_task 名字引用（regression.py 通过 sched_mod.run_task 打桩）
run_task = run_chat


# --------------------------------------------------------------------------- #
# 规范化：无论来自 LLM 还是 mock，都收敛到渲染器能吃的形状
# --------------------------------------------------------------------------- #
def _normalize_analysis(a: Any) -> dict[str, Any]:
    if not isinstance(a, dict):
        a = {}
    out: dict[str, Any] = {}
    for key in ("job_title", "company", "seniority", "location"):
        out[key] = str(a.get(key) or "")
    for key in ("must_have", "nice_to_have", "responsibilities", "keywords",
                "tech_stack", "domain_keywords", "soft_skills", "culture_signals", "noise_dropped"):
        val = a.get(key) or []
        if isinstance(val, str):
            val = [val]
        out[key] = [str(x) for x in val if str(x).strip()]

    # 分节
    sections: list[dict[str, Any]] = []
    raw_sections = a.get("sections") or []
    if isinstance(raw_sections, list):
        for s in raw_sections:
            if not isinstance(s, dict):
                continue
            items = s.get("items") or []
            if isinstance(items, str):
                items = [items]
            sections.append({
                "heading": str(s.get("heading") or ""),
                "kind": str(s.get("kind") or "other"),
                "items": [str(x) for x in items if str(x).strip()],
            })
    out["sections"] = sections

    # 扁平需求列表（id 供覆盖清单引用）
    requirements: list[dict[str, Any]] = []
    raw_reqs = a.get("requirements") or []
    if isinstance(raw_reqs, list):
        for r in raw_reqs:
            if isinstance(r, dict):
                text = str(r.get("text") or "").strip()
                if not text:
                    continue
                kind = str(r.get("kind") or "must_have")
                if kind not in ("must_have", "responsibility", "nice_to_have"):
                    kind = "must_have"
                requirements.append({
                    "id": str(r.get("id") or f"R{len(requirements) + 1}"),
                    "kind": kind,
                    "text": text,
                    "section": str(r.get("section") or ""),
                })
            else:
                text = str(r).strip()
                if text:
                    requirements.append({
                        "id": f"R{len(requirements) + 1}", "kind": "must_have",
                        "text": text, "section": "",
                    })
    out["requirements"] = requirements

    # 申请表问题（不进简历正文）
    eq = a.get("employer_questions") or []
    if isinstance(eq, str):
        eq = [eq]
    out["employer_questions"] = [str(x) for x in eq if str(x).strip()]

    try:
        out["confidence"] = float(a.get("confidence") or 0.0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    return out


def _normalize_coverage(
    raw_coverage: Any, requirements: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """收敛覆盖清单：字段补默认、source 白名单、req_id 去重、补齐未覆盖需求。"""
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    if isinstance(raw_coverage, list):
        for c in raw_coverage:
            if not isinstance(c, dict):
                continue
            req_id = str(c.get("req_id") or "").strip()
            requirement = str(c.get("requirement") or "").strip()
            if not req_id and not requirement:
                continue
            if req_id and req_id in seen:
                continue
            source = str(c.get("source") or "").strip().lower()
            evidence_ref = str(c.get("evidence_ref") or "").strip()
            if source not in ("grounded", "capability"):
                source = "grounded" if evidence_ref else "capability"
            # grounded 必须有 evidence_ref，否则降级（硬规则：无依据不得标 grounded）
            if source == "grounded" and not evidence_ref:
                source = "capability"
            item = {
                "req_id": req_id or f"R{len(items) + 1}",
                "requirement": requirement,
                "section": str(c.get("section") or ""),
                "resume_evidence": str(c.get("resume_evidence") or ""),
                "source": source,
                "evidence_ref": evidence_ref,
                "note": str(c.get("note") or ""),
            }
            seen.add(item["req_id"])
            items.append(item)

    # 硬规则兜底：requirements 中未被覆盖的条目，补一条 capability（不得留空/漏条）
    covered_ids = {it["req_id"] for it in items}
    covered_texts = {it["requirement"] for it in items if it["requirement"]}
    uncovered_before = 0
    for r in requirements or []:
        rid = str(r.get("id") or "").strip()
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        if (rid and rid in covered_ids) or (text in covered_texts):
            continue
        uncovered_before += 1
        items.append({
            "req_id": rid or f"R{len(items) + 1}",
            "requirement": text,
            "section": str(r.get("section") or ""),
            "resume_evidence": "",
            "source": "capability",
            "evidence_ref": "",
            "note": "资料中无直接对应经历，按能力层面表述",
        })
        if rid:
            covered_ids.add(rid)
        covered_texts.add(text)

    grounded = sum(1 for it in items if it["source"] == "grounded")
    capability = sum(1 for it in items if it["source"] == "capability")
    summary = {
        "total": len(items),
        "grounded": grounded,
        "capability": capability,
        "uncovered": uncovered_before,
    }
    return items, summary


def _normalize_spec(spec: Any, profile: dict[str, Any], analysis: Any = None) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise RuntimeError("模型返回的 resume spec 不是对象")
    header = spec.get("header") if isinstance(spec.get("header"), dict) else {}
    header.setdefault("name", profile.get("name") or "姓名")
    header.setdefault("headline", "")
    header.setdefault("tagline", "")
    header["show_avatar"] = bool(header.get("show_avatar", True) and profile.get("avatar"))
    header["avatar"] = profile.get("avatar") or ""      # 注入真实头像 data-url
    if not isinstance(header.get("contacts"), list):
        header["contacts"] = []

    sections = []
    for i, s in enumerate(spec.get("sections") or []):
        if not isinstance(s, dict):
            continue
        content = s.get("content") if isinstance(s.get("content"), dict) else {}
        # skills 章节：收敛 items，保证 name/level/note 齐备（note 为技术深度线索）。
        if str(s.get("type")) == "skills":
            groups: list[dict[str, Any]] = []
            for g in content.get("groups") or []:
                if not isinstance(g, dict):
                    continue
                items: list[dict[str, str]] = []
                for it in g.get("items") or []:
                    if isinstance(it, dict):
                        items.append({
                            "name": str(it.get("name") or ""),
                            "level": str(it.get("level") or ""),
                            "note": str(it.get("note") or ""),
                        })
                    elif str(it).strip():
                        items.append({"name": str(it), "level": "", "note": ""})
                groups.append({"category": str(g.get("category") or ""), "items": items})
            content = {"groups": groups}
        sections.append({
            "id": str(s.get("id") or f"section-{i + 1}"),
            "type": str(s.get("type") or "text"),
            "title": str(s.get("title") or "章节"),
            "emphasis": s.get("emphasis") if s.get("emphasis") in ("high", "normal", "low") else "normal",
            "content": content,
        })
    if not sections:
        raise RuntimeError("模型没有产出任何章节")

    notes = spec.get("match_notes") or []
    if isinstance(notes, str):
        notes = [notes]

    requirements = (analysis or {}).get("requirements") if isinstance(analysis, dict) else []
    coverage, coverage_summary = _normalize_coverage(spec.get("coverage"), requirements or [])

    return {
        "theme": spec.get("theme") or "ark",
        "layout": spec.get("layout") or "single-column",
        "header": header,
        "sections": sections,
        "match_notes": [str(n) for n in notes][:6],
        # coverage 仅作为返回值携带，落库时会被 run_chat 取出放到 Resume.coverage，
        # 绝不进入 spec.sections，因此不会出现在简历正文里。
        "coverage": coverage,
        "coverage_summary": coverage_summary,
    }
