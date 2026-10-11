"""任务接口 —— **兼容层（deprecated）**。

新模型是「聊天式工作区」（Chat），本路由仅为向后兼容旧前端与既有 HTTP 级测试
（smoke / regression / progress_probe / xss_probe / e2e_verify）而保留，
内部一律映射到 Chat。**新代码请使用 /api/chats**，本路由后续会下线。

映射规则：
- POST /api/tasks（单条）、/api/tasks/batch（批量）→ 各建一个 Chat 并入队；
- GET /api/tasks[/{id}] → 返回 TaskOut（resume_id / company 由 Chat 派生）；
- retry / cancel / DELETE → 与 /api/chats/{id} 同语义（DELETE 保留 cascade 参数）。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlmodel import Session, select

from ..config import settings
from ..db import get_session
from ..models import Chat, ChatMessage, EditLog, Resume
from ..pipeline import _profile_to_dict
from ..scheduler import scheduler
from ..schemas import BatchTaskIn, JobTaskIn, TaskOut

router = APIRouter(prefix="/api/tasks", tags=["tasks"], deprecated=True)


def _global_profile_dict(session: Session) -> dict:
    from ..models import Profile  # 局部导入，避免顶部 import 过长
    p = session.exec(select(Profile).order_by(Profile.id)).first()
    return _profile_to_dict(p)


def _resume_id_for(session: Session, chat_id: int) -> int | None:
    r = session.exec(
        select(Resume).where(Resume.chat_id == chat_id).order_by(Resume.id.desc())  # type: ignore[attr-defined]
    ).first()
    return r.id if r else None


def _task_out(chat: Chat, session: Session) -> TaskOut:
    ja = chat.job_analysis or {}
    return TaskOut(
        id=chat.id,
        title=chat.title,
        company=str(ja.get("company") or ""),
        source=chat.source,
        source_url=chat.source_url,
        status=chat.status,  # type: ignore[arg-type]
        stage=chat.stage,
        progress=chat.progress,
        error=chat.error,
        attempts=chat.attempts,
        max_retries=chat.max_retries,
        resume_id=_resume_id_for(session, chat.id),
        job_analysis=ja,
        created_at=chat.created_at,
        started_at=chat.started_at,
        finished_at=chat.finished_at,
    )


def _new_chat(session: Session, job: JobTaskIn, max_retries: int) -> Chat:
    chat = Chat(
        title=job.title.strip() or "未命名岗位",
        source=job.source.strip(),
        source_url=job.source_url.strip(),
        raw_text=job.raw_text,
        status="draft",
        max_retries=max_retries,
        profile_snapshot=_global_profile_dict(session),
        profile_dirty=False,
    )
    session.add(chat)
    session.flush()
    if (job.raw_text or "").strip():
        session.add(ChatMessage(chat_id=chat.id, role="user", kind="jd",
                                content=job.raw_text, meta={}))
    return chat


@router.get("", response_model=list[TaskOut])
def list_tasks(limit: int = 100, session: Session = Depends(get_session)) -> list[TaskOut]:
    stmt = select(Chat).order_by(Chat.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    return [_task_out(c, session) for c in session.exec(stmt).all()]


@router.get("/{task_id}", response_model=TaskOut)
def get_task(task_id: int, session: Session = Depends(get_session)) -> TaskOut:
    chat = session.get(Chat, task_id)
    if not chat:
        raise HTTPException(404, "任务不存在")
    return _task_out(chat, session)


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(payload: JobTaskIn, session: Session = Depends(get_session)) -> TaskOut:
    if not payload.raw_text.strip():
        raise HTTPException(400, "岗位要求文本不能为空")
    max_retries = payload.max_retries if payload.max_retries is not None else settings.max_retries
    chat = _new_chat(session, payload, max_retries)
    session.commit()
    session.refresh(chat)
    await scheduler.enqueue(chat.id)
    session.refresh(chat)
    return _task_out(chat, session)


@router.post("/batch", response_model=list[TaskOut], status_code=201)
async def create_tasks_batch(payload: BatchTaskIn, session: Session = Depends(get_session)) -> list[TaskOut]:
    if not payload.jobs:
        raise HTTPException(400, "至少需要一个岗位")
    max_retries = payload.max_retries if payload.max_retries is not None else settings.max_retries
    created: list[Chat] = []
    for job in payload.jobs:
        if not job.raw_text.strip():
            continue
        created.append(_new_chat(session, job, max_retries))
    session.commit()
    for chat in created:
        session.refresh(chat)
    # 统一入队，由 worker 池并发消费
    for chat in created:
        await scheduler.enqueue(chat.id)
    for chat in created:
        session.refresh(chat)
    return [_task_out(c, session) for c in created]


@router.post("/{task_id}/retry", response_model=TaskOut)
async def retry_task(task_id: int, session: Session = Depends(get_session)) -> TaskOut:
    chat = session.get(Chat, task_id)
    if not chat:
        raise HTTPException(404, "任务不存在")
    # 运行中 / 排队中都不允许重试：否则会造成同一任务被重复入队处理
    if chat.status in ("running", "queued"):
        raise HTTPException(409, "任务正在运行或排队中，无法重试")
    chat.attempts = 0          # 手动重试重置计数
    chat.error = ""
    session.add(chat)
    session.commit()
    await scheduler.enqueue(task_id)
    session.refresh(chat)
    return _task_out(chat, session)


@router.post("/{task_id}/cancel", response_model=TaskOut)
async def cancel_task(task_id: int, session: Session = Depends(get_session)) -> TaskOut:
    chat = session.get(Chat, task_id)
    if not chat:
        raise HTTPException(404, "任务不存在")
    await scheduler.cancel(task_id)
    session.refresh(chat)
    return _task_out(chat, session)


@router.delete("/{task_id}", status_code=204, response_class=Response)
def delete_task(
    task_id: int,
    cascade: bool = False,
    session: Session = Depends(get_session),
) -> Response:
    """删除任务（映射为删除 Chat）。

    - 运行中 -> 400，让前端先取消；
    - 已生成简历且 cascade=False -> 409，让前端确认后再级联删除；
    - cascade=True -> 先删关联简历（及其编辑日志）与消息，再删 Chat；
    - 无关联简历 -> 直接删除。
    """
    chat = session.get(Chat, task_id)
    if not chat:
        raise HTTPException(404, "任务不存在")
    if chat.status == "running":
        raise HTTPException(400, "任务运行中，请先取消")

    resumes = list(session.exec(select(Resume).where(Resume.chat_id == task_id)).all())
    if resumes and not cascade:
        raise HTTPException(
            409,
            "该任务已生成简历，删除任务会一并删除简历。确认后请使用 cascade=true 重试。",
        )

    for r in resumes:
        for edit in session.exec(select(EditLog).where(EditLog.resume_id == r.id)).all():
            session.delete(edit)
        session.flush()
        session.delete(r)
    session.flush()
    for msg in session.exec(select(ChatMessage).where(ChatMessage.chat_id == task_id)).all():
        session.delete(msg)
    session.flush()
    session.delete(chat)
    session.commit()
    return Response(status_code=204)
