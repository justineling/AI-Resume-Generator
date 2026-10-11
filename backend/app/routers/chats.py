"""聊天式工作区接口。

一个 chat = 一个求职场景 = 恰好一份简历。提供：
- Chat CRUD（列表 / 详情 / 改名 / 级联删除）
- per-chat 资料快照（读 / 改 / 重置回全局）
- 对话消息（贴 JD / 追加消息）
- 生成 / 取消 / 重试 / 覆盖清单

统一错误约定：``HTTPException(status, 中文 detail)``；前端用 ``err.status`` 区分 409。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..events import EVENT_CHAT_MESSAGE, hub
from ..llm import build_client, get_runtime_config
from ..models import Chat, ChatMessage, EditLog, Profile, Resume
from ..pipeline import _profile_to_dict
from ..scheduler import scheduler
from ..schemas import (
    ChatIn,
    ChatMessageIn,
    ChatMessageOut,
    ChatOut,
    ChatPatch,
    CoverageItem,
    CoverageOut,
    CoverageSummary,
    ProfileIn,
    ProfileOut,
)

router = APIRouter(prefix="/api/chats", tags=["chats"])

_COVERAGE_ITEM_KEYS = ("req_id", "requirement", "section", "resume_evidence", "source", "evidence_ref", "note")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _global_profile_dict(session: Session) -> dict:
    """读取全局资料库并转成 dict（与 pipeline._profile_to_dict 同形）。"""
    p = session.exec(select(Profile).order_by(Profile.id)).first()
    return _profile_to_dict(p)


def _resume_id_for(session: Session, chat_id: int) -> int | None:
    r = session.exec(
        select(Resume).where(Resume.chat_id == chat_id).order_by(Resume.id.desc())  # type: ignore[attr-defined]
    ).first()
    return r.id if r else None


def _msg_out(m: ChatMessage) -> ChatMessageOut:
    return ChatMessageOut(
        id=m.id, chat_id=m.chat_id, role=m.role, kind=m.kind,  # type: ignore[arg-type]
        content=m.content, meta=m.meta or {}, created_at=m.created_at,
    )


def _chat_out(chat: Chat, session: Session, messages: list[ChatMessageOut] | None = None) -> ChatOut:
    return ChatOut(
        id=chat.id,
        title=chat.title,
        status=chat.status,  # type: ignore[arg-type]
        stage=chat.stage,
        progress=chat.progress,
        error=chat.error,
        attempts=chat.attempts,
        max_retries=chat.max_retries,
        raw_text=chat.raw_text,
        source=chat.source,
        source_url=chat.source_url,
        profile_snapshot=chat.profile_snapshot or {},
        profile_dirty=chat.profile_dirty,
        job_analysis=chat.job_analysis or {},
        resume_id=_resume_id_for(session, chat.id),
        messages=messages or [],
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        started_at=chat.started_at,
        finished_at=chat.finished_at,
    )


def _profile_out(chat: Chat) -> ProfileOut:
    snap = chat.profile_snapshot or {}
    data = ProfileIn().model_dump()
    data.update({k: v for k, v in snap.items() if k in data})
    return ProfileOut(id=chat.id, created_at=chat.created_at, updated_at=chat.updated_at, **data)


def _coverage_out(cov: dict) -> CoverageOut:
    items = []
    for raw in (cov.get("coverage") or []):
        if not isinstance(raw, dict):
            continue
        data = {k: raw.get(k) for k in _COVERAGE_ITEM_KEYS}
        if data.get("source") not in ("grounded", "capability"):
            data["source"] = "grounded" if str(data.get("evidence_ref") or "").strip() else "capability"
        items.append(CoverageItem(**data))
    summary_raw = cov.get("summary") if isinstance(cov.get("summary"), dict) else {}
    summary = CoverageSummary(
        total=int(summary_raw.get("total", len(items)) or 0),
        grounded=int(summary_raw.get("grounded", 0) or 0),
        capability=int(summary_raw.get("capability", 0) or 0),
        uncovered=int(summary_raw.get("uncovered", 0) or 0),
    )
    return CoverageOut(coverage=items, summary=summary)


def _cascade_delete_chat(session: Session, chat: Chat) -> None:
    """级联删除：Resume(+EditLog) + ChatMessage + Chat。"""
    resumes = session.exec(select(Resume).where(Resume.chat_id == chat.id)).all()
    for r in resumes:
        for edit in session.exec(select(EditLog).where(EditLog.resume_id == r.id)).all():
            session.delete(edit)
        session.flush()
        session.delete(r)
    session.flush()
    for msg in session.exec(select(ChatMessage).where(ChatMessage.chat_id == chat.id)).all():
        session.delete(msg)
    session.flush()
    session.delete(chat)


def _get_chat_or_404(session: Session, chat_id: int) -> Chat:
    chat = session.get(Chat, chat_id)
    if not chat:
        raise HTTPException(404, "对话不存在")
    return chat


# --------------------------------------------------------------------------- #
# 从招聘链接导入（URL → 抓取 → 抽取职位名称与要求）
# --------------------------------------------------------------------------- #
class URLImportIn(BaseModel):
    """从 URL 导入岗位信息。"""

    url: str
    auto_generate: bool = False   # True 时抽取成功后自动触发生成


@router.post("/import-url", response_model=ChatOut, status_code=201)
async def import_chat_from_url(
    payload: URLImportIn, session: Session = Depends(get_session)
) -> ChatOut:
    """抓取招聘页面，抽取职位名称与岗位要求，创建一个已填好的新对话。

    抽取优先用 LLM（已配置 key 时），失败回落启发式分节。
    auto_generate=True 且抽取到岗位要求时，创建后自动入队生成。
    """
    from ..fetch import FetchError, extract_jd_from_text, fetch_url_text

    try:
        page = await fetch_url_text(payload.url)
    except FetchError as exc:
        raise HTTPException(422, str(exc))

    client = build_client(session)
    jd = await extract_jd_from_text(page["text"], client if client.enabled else None)
    requirements = (jd.get("requirements") or "").strip()
    job_title = (jd.get("job_title") or "").strip()
    if not requirements:
        raise HTTPException(
            422,
            "已抓取网页，但未能识别出岗位要求。该页面可能是职位列表或由脚本渲染，"
            "请打开链接手动复制职位名称与岗位要求。",
        )
    if not job_title:
        job_title = page["page_title"][:80] or "新对话"

    chat = Chat(
        title=job_title[:80] or "新对话",
        status="draft",
        profile_snapshot=_global_profile_dict(session),
        profile_dirty=False,
        source=page["final_url"][:500],
    )
    session.add(chat)
    session.commit()
    session.refresh(chat)

    msg = ChatMessage(
        chat_id=chat.id, role="user", kind="jd",
        content=requirements, meta={"imported_from": page["final_url"][:500]},
    )
    session.add(msg)
    chat.raw_text = requirements
    chat.updated_at = datetime.now()
    session.add(chat)
    session.commit()

    if payload.auto_generate:
        await scheduler.enqueue(chat.id)

    out = _chat_out(chat, session)
    out.extracted_title = job_title[:120]
    return out


# --------------------------------------------------------------------------- #
# Chat CRUD
# --------------------------------------------------------------------------- #
@router.post("", response_model=ChatOut, status_code=201)
def create_chat(payload: ChatIn, session: Session = Depends(get_session)) -> ChatOut:
    """新建 chat，并对全局 Profile 做整体快照。"""
    chat = Chat(
        title=payload.title.strip() or "新对话",
        status="draft",
        profile_snapshot=_global_profile_dict(session),
        profile_dirty=False,
    )
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return _chat_out(chat, session)


@router.get("", response_model=list[ChatOut])
def list_chats(limit: int = 100, session: Session = Depends(get_session)) -> list[ChatOut]:
    stmt = select(Chat).order_by(Chat.updated_at.desc()).limit(limit)  # type: ignore[attr-defined]
    return [_chat_out(c, session) for c in session.exec(stmt).all()]


@router.get("/{chat_id}", response_model=ChatOut)
def get_chat(chat_id: int, session: Session = Depends(get_session)) -> ChatOut:
    chat = _get_chat_or_404(session, chat_id)
    msgs = session.exec(
        select(ChatMessage).where(ChatMessage.chat_id == chat_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)  # type: ignore[attr-defined]
    ).all()
    return _chat_out(chat, session, messages=[_msg_out(m) for m in msgs])


@router.patch("/{chat_id}", response_model=ChatOut)
def patch_chat(chat_id: int, payload: ChatPatch, session: Session = Depends(get_session)) -> ChatOut:
    chat = _get_chat_or_404(session, chat_id)
    if payload.title is not None and payload.title.strip():
        chat.title = payload.title.strip()
        chat.updated_at = datetime.now()
        session.add(chat)
        session.commit()
        session.refresh(chat)
    return _chat_out(chat, session)


@router.delete("/{chat_id}", status_code=204, response_class=Response)
def delete_chat(chat_id: int, session: Session = Depends(get_session)) -> Response:
    """删除 chat 并级联删除 Resume + EditLog + ChatMessage。运行中返回 400。"""
    chat = _get_chat_or_404(session, chat_id)
    if chat.status in ("running", "queued"):
        raise HTTPException(400, "对话正在生成中，请先取消后再删除")
    _cascade_delete_chat(session, chat)
    session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------- #
# per-chat 资料快照
# --------------------------------------------------------------------------- #
@router.get("/{chat_id}/profile", response_model=ProfileOut)
def get_chat_profile(chat_id: int, session: Session = Depends(get_session)) -> ProfileOut:
    chat = _get_chat_or_404(session, chat_id)
    return _profile_out(chat)


@router.put("/{chat_id}/profile", response_model=ChatOut)
def put_chat_profile(chat_id: int, payload: ProfileIn, session: Session = Depends(get_session)) -> ChatOut:
    """修改 chat 内的资料快照——只影响本 chat，不影响全局，也不影响其它 chat。"""
    chat = _get_chat_or_404(session, chat_id)
    chat.profile_snapshot = payload.model_dump()
    chat.profile_dirty = True
    chat.updated_at = datetime.now()
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return _chat_out(chat, session)


@router.post("/{chat_id}/profile/reset", response_model=ChatOut)
def reset_chat_profile(chat_id: int, session: Session = Depends(get_session)) -> ChatOut:
    """把 chat 内资料快照重置为当前全局 Profile。"""
    chat = _get_chat_or_404(session, chat_id)
    chat.profile_snapshot = _global_profile_dict(session)
    chat.profile_dirty = False
    chat.updated_at = datetime.now()
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return _chat_out(chat, session)


# --------------------------------------------------------------------------- #
# 消息
# --------------------------------------------------------------------------- #
@router.post("/{chat_id}/messages", response_model=ChatMessageOut, status_code=201)
async def post_message(chat_id: int, payload: ChatMessageIn, session: Session = Depends(get_session)) -> ChatMessageOut:
    """追加一条消息；kind=jd 时同步写入 Chat.raw_text 并广播 chat_message。"""
    chat = _get_chat_or_404(session, chat_id)
    content = payload.content or ""
    if payload.kind == "jd" and not content.strip():
        raise HTTPException(400, "岗位要求（JD）不能为空")
    role = "user" if payload.kind in ("jd", "text") else "assistant"
    msg = ChatMessage(chat_id=chat_id, role=role, kind=payload.kind, content=content, meta=payload.meta or {})
    session.add(msg)
    if payload.kind == "jd":
        chat.raw_text = content
        chat.updated_at = datetime.now()
        session.add(chat)
    session.commit()
    session.refresh(msg)
    out = _msg_out(msg)
    await hub.broadcast(EVENT_CHAT_MESSAGE, {"chat_id": chat_id, "message": out.model_dump(mode="json")})
    return out


@router.get("/{chat_id}/messages", response_model=list[ChatMessageOut])
def list_messages(chat_id: int, session: Session = Depends(get_session)) -> list[ChatMessageOut]:
    _get_chat_or_404(session, chat_id)
    msgs = session.exec(
        select(ChatMessage).where(ChatMessage.chat_id == chat_id)
        .order_by(ChatMessage.created_at, ChatMessage.id)  # type: ignore[attr-defined]
    ).all()
    return [_msg_out(m) for m in msgs]


# --------------------------------------------------------------------------- #
# 生成 / 取消 / 重试 / 覆盖清单
# --------------------------------------------------------------------------- #
@router.post("/{chat_id}/generate", response_model=ChatOut, status_code=202)
async def generate_chat(chat_id: int, session: Session = Depends(get_session)) -> ChatOut:
    chat = _get_chat_or_404(session, chat_id)
    if not (chat.raw_text or "").strip():
        raise HTTPException(400, "请先粘贴岗位要求（JD）后再生成")
    if chat.status in ("running", "queued"):
        raise HTTPException(409, "对话正在生成中，请勿重复触发")
    await scheduler.enqueue(chat_id)
    session.refresh(chat)
    return _chat_out(chat, session)


@router.post("/{chat_id}/cancel", response_model=ChatOut)
async def cancel_chat(chat_id: int, session: Session = Depends(get_session)) -> ChatOut:
    chat = _get_chat_or_404(session, chat_id)
    await scheduler.cancel(chat_id)
    session.refresh(chat)
    return _chat_out(chat, session)


@router.post("/{chat_id}/retry", response_model=ChatOut)
async def retry_chat(chat_id: int, session: Session = Depends(get_session)) -> ChatOut:
    chat = _get_chat_or_404(session, chat_id)
    if chat.status in ("running", "queued"):
        raise HTTPException(409, "对话正在运行或排队中，无法重试")
    chat.attempts = 0
    chat.error = ""
    session.add(chat)
    session.commit()
    await scheduler.enqueue(chat_id)
    session.refresh(chat)
    return _chat_out(chat, session)


@router.get("/{chat_id}/coverage", response_model=CoverageOut)
def get_coverage(chat_id: int, session: Session = Depends(get_session)) -> CoverageOut:
    _get_chat_or_404(session, chat_id)
    r = session.exec(
        select(Resume).where(Resume.chat_id == chat_id).order_by(Resume.id.desc())  # type: ignore[attr-defined]
    ).first()
    if not r:
        raise HTTPException(404, "该对话尚未生成简历")
    return _coverage_out(r.coverage or {})
