"""用户资料库接口。全局单份资料（首次访问自动创建空档案）。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from sqlmodel import Session, select

from ..db import get_session
from ..models import Profile
from ..schemas import ProfileIn, ProfileOut

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
