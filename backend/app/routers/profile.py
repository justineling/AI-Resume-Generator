"""用户资料库接口。全局单份资料（首次访问自动创建空档案）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..db import get_session
from ..llm import build_client
from ..models import Profile
from ..schemas import (
    InterviewAnswersIn,
    InterviewQuestionsOut,
    ProfileIn,
    ProfileImportTextIn,
    ProfileInterviewIn,
    ProfileOut,
)

router = APIRouter(prefix="/api/profile", tags=["profile"])


def _get_or_create(session: Session) -> Profile:
    p = session.exec(select(Profile).order_by(Profile.id)).first()
    if p is None:
        p = Profile(name="")
        session.add(p)
        session.commit()
        session.refresh(p)
    return p


@router.get("", response_model=ProfileOut)
def get_profile(session: Session = Depends(get_session)) -> Profile:
    return _get_or_create(session)


@router.put("", response_model=ProfileOut)
def update_profile(payload: ProfileIn, session: Session = Depends(get_session)) -> Profile:
    p = _get_or_create(session)
    for field, value in payload.model_dump().items():
        setattr(p, field, value)
    p.updated_at = datetime.now()
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


@router.post("/import-text", response_model=ProfileOut)
async def import_profile_text(
    payload: ProfileImportTextIn, session: Session = Depends(get_session)
) -> Profile:
    """粘贴简历原文 -> 解析成完整资料并**直接写库**（语义同 PUT /api/profile）。

    有 API Key 时用 LLM 解析（只提取真实信息，绝不编造）；无 Key 时回落启发式，
    保证功能链路可用、不抛错。
    """
    from ..profile_import import parse_profile_text

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="请先粘贴简历文本")

    data = await parse_profile_text(text, build_client(session))
    p = _get_or_create(session)
    valid = ProfileIn().model_dump().keys()
    for field, value in data.items():
        if field in valid:
            setattr(p, field, value)
    p.updated_at = datetime.now()
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


# --------------------------------------------------------------------------- #
# AI 面试问答（flashcard）：让资料更专业、更具体
# --------------------------------------------------------------------------- #
def _profile_dict(p: Profile) -> dict:
    return {k: getattr(p, k) for k in (
        "name", "headline", "email", "phone", "location", "age", "avatar", "summary",
        "links", "education", "experiences", "projects", "skills",
        "certificates", "languages", "awards",
    )}


@router.post("/interview", response_model=InterviewQuestionsOut)
async def profile_interview(
    payload: ProfileInterviewIn, session: Session = Depends(get_session)
) -> InterviewQuestionsOut:
    """生成一组 flashcard 问题，帮助把资料写得更专业、更具体。

    focus 可用于引导方向（例如「前端」）。
    """
    from ..interview import generate_questions

    p = _get_or_create(session)
    prof = _profile_dict(p)
    focus = (payload.focus or "").strip()
    analysis = {"requirements": [{"id": "F1", "kind": "must_have", "text": focus}]} if focus else None
    result = await generate_questions(prof, analysis, build_client(session))
    return InterviewQuestionsOut(**result)


@router.post("/interview/apply", response_model=ProfileOut)
async def profile_interview_apply(
    payload: InterviewAnswersIn, session: Session = Depends(get_session)
) -> Profile:
    """把用户的补充回答整合进全局资料（只增补，不覆盖原有真实信息）。"""
    from ..interview import apply_answers

    p = _get_or_create(session)
    answers = [a.model_dump() for a in payload.answers]
    merged = await apply_answers(_profile_dict(p), answers, build_client(session), for_chat=False)
    for field, value in merged.items():
        if hasattr(p, field):
            setattr(p, field, value)
    p.updated_at = datetime.now()
    session.add(p)
    session.commit()
    session.refresh(p)
    return p
