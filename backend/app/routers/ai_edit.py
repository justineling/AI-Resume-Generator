"""AI 二次编辑：自然语言指令 -> 修改简历 HTML。

安全取舍：生成阶段（pipeline）里模型产出的"内容"始终走 Jinja2 autoescape 转义；
AI 二次编辑返回的 HTML 会被当作 HTML 处理（这是用户显式发起的编辑），
但入库前一律先经 sanitize.sanitize_fragment() 净化（剥离脚本 / 事件处理器 / 危险协议），
避免 /preview、/download 同源返回时形成存储型 XSS。
每次编辑都记录 EditLog，支持一键回退到上一版。
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from .. import mock, prompts
from ..db import get_session
from ..llm import LLMError, build_client
from ..models import EditLog, Resume
from ..sanitize import sanitize_fragment
from ..schemas import AIEditIn, AIEditOut

router = APIRouter(prefix="/api/resumes", tags=["ai-edit"])


@router.post("/{resume_id}/ai-edit", response_model=AIEditOut)
async def ai_edit(resume_id: int, payload: AIEditIn, session: Session = Depends(get_session)) -> AIEditOut:
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    instruction = (payload.instruction or "").strip()
    if not instruction:
        raise HTTPException(400, "指令不能为空")

    before_html = payload.current_html or r.html
    client = build_client(session)

    try:
        if client.enabled:
            result = await client.chat_json(
                prompts.SYSTEM_AI_EDIT,
                prompts.build_ai_edit_user(instruction, before_html, payload.scope, payload.target),
            )
            if not isinstance(result, dict) or not str(result.get("html", "")).strip():
                raise LLMError("模型未返回有效 HTML")
            new_html = str(result["html"])
            summary = str(result.get("summary") or "已按指令修改")
        else:
            result = mock.edit_html(instruction, before_html, payload.scope, payload.target)
            new_html = result["html"]
            summary = result["summary"]
    except (LLMError, ValueError) as exc:
        session.add(EditLog(
            resume_id=resume_id, instruction=instruction, scope=payload.scope,
            target=payload.target, ok=False, error=str(exc)[:1000],
            before_html=before_html, after_html="",
        ))
        session.commit()
        return AIEditOut(ok=False, html=before_html, error=f"AI 编辑失败：{exc}")

    # 存储前净化：AI 返回的 HTML 可能含脚本 / 事件处理器，入库前必须剥离，
    # 否则 /preview、/download 同源返回时会形成存储型 XSS。
    new_html, removed = sanitize_fragment(new_html)
    if removed:
        summary = f"{summary}（已自动净化模型输出中的不安全内容）"

    r.html = new_html
    r.version += 1
    r.updated_at = datetime.now()
    session.add(r)
    session.add(EditLog(
        resume_id=resume_id, instruction=instruction, scope=payload.scope,
        target=payload.target, ok=True, before_html=before_html, after_html=new_html,
    ))
    session.commit()
    return AIEditOut(ok=True, html=new_html, summary=summary)


@router.get("/{resume_id}/edits")
def list_edits(resume_id: int, session: Session = Depends(get_session)) -> list[dict]:
    stmt = select(EditLog).where(EditLog.resume_id == resume_id).order_by(EditLog.created_at.desc()).limit(50)  # type: ignore[attr-defined]
    return [
        {
            "id": e.id, "instruction": e.instruction, "scope": e.scope, "target": e.target,
            "ok": e.ok, "error": e.error, "created_at": e.created_at,
        }
        for e in session.exec(stmt).all()
    ]


@router.post("/{resume_id}/undo", response_model=AIEditOut)
def undo_last_edit(resume_id: int, session: Session = Depends(get_session)) -> AIEditOut:
    """回退到最近一次成功编辑之前的状态。"""
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    stmt = (
        select(EditLog)
        .where(EditLog.resume_id == resume_id, EditLog.ok == True)  # noqa: E712
        .order_by(EditLog.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    )
    last = session.exec(stmt).first()
    if not last:
        raise HTTPException(400, "没有可回退的编辑记录")
    r.html = last.before_html or r.html
    r.version += 1
    r.updated_at = datetime.now()
    session.add(r)
    session.commit()
    return AIEditOut(ok=True, html=r.html, summary="已回退到上一次编辑前")
