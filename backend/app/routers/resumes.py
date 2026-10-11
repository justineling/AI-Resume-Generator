"""简历接口：查询、保存（HTML 为真相来源）、按 spec 重排版、预览、下载。"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from ..db import get_session
from ..models import Chat, ChatMessage, EditLog, Resume
from ..renderer import build_preview_document, render_resume_html
from ..schemas import LayoutId, ResumeMeta, ResumeOut, ResumeSaveIn, ThemeId

router = APIRouter(prefix="/api/resumes", tags=["resumes"])

# 合法版式 / 主题白名单，须与前端 app/static/js/resume.js 的 LAYOUTS / THEMES 保持一致，
# 取值类型与 schemas.LayoutId / ThemeId 对应。
VALID_LAYOUTS: tuple[LayoutId, ...] = (
    "single-column", "sidebar-left", "two-column", "timeline", "compact-grid",
)
VALID_THEMES: tuple[ThemeId, ...] = ("ark", "endfield", "exa", "corporate")


def _meta(r: Resume) -> ResumeMeta:
    return ResumeMeta(
        id=r.id, chat_id=r.chat_id, title=r.title, theme=r.theme,
        version=r.version, created_at=r.created_at, updated_at=r.updated_at,
    )


def _out(r: Resume) -> ResumeOut:
    return ResumeOut(**_meta(r).model_dump(), html=r.html, spec=r.spec, coverage=r.coverage or {})


@router.get("", response_model=list[ResumeMeta])
def list_resumes(limit: int = 100, session: Session = Depends(get_session)) -> list[ResumeMeta]:
    stmt = select(Resume).order_by(Resume.created_at.desc()).limit(limit)  # type: ignore[attr-defined]
    return [_meta(r) for r in session.exec(stmt).all()]


@router.get("/{resume_id}", response_model=ResumeOut)
def get_resume(resume_id: int, session: Session = Depends(get_session)) -> ResumeOut:
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    return _out(r)


@router.put("/{resume_id}", response_model=ResumeOut)
def save_resume(resume_id: int, payload: ResumeSaveIn, session: Session = Depends(get_session)) -> ResumeOut:
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    if payload.html is not None:
        r.html = payload.html
    if payload.title is not None:
        r.title = payload.title
    if payload.theme is not None:
        if payload.theme not in VALID_THEMES:
            raise HTTPException(400, f"不支持的主题：{payload.theme}。可选值：{', '.join(VALID_THEMES)}")
        r.theme = payload.theme
    r.version += 1
    r.updated_at = datetime.now()
    session.add(r)
    session.commit()
    session.refresh(r)
    return _out(r)


class LayoutIn(BaseModel):
    # 刻意保留 str + 显式白名单校验（返回 400 并附可选值），而不是用 Literal：
    # Literal 会走 pydantic 校验返回 422，detail 变成结构化列表，前端 toast 可读性更差。
    # 合法取值见下方 VALID_LAYOUTS / VALID_THEMES（与前端 LAYOUTS / THEMES 一致）。
    layout: str | None = None
    theme: str | None = None


@router.post("/{resume_id}/layout", response_model=ResumeOut)
def change_layout(resume_id: int, payload: LayoutIn, session: Session = Depends(get_session)) -> ResumeOut:
    """基于保存的 spec 重新渲染（切换版式 / 主题），不调用 LLM。"""
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    if not r.spec:
        raise HTTPException(400, "该简历没有结构快照，无法重排版")
    if payload.layout and payload.layout not in VALID_LAYOUTS:
        raise HTTPException(400, f"不支持的版式：{payload.layout}。可选值：{', '.join(VALID_LAYOUTS)}")
    if payload.theme and payload.theme not in VALID_THEMES:
        raise HTTPException(400, f"不支持的主题：{payload.theme}。可选值：{', '.join(VALID_THEMES)}")
    spec = dict(r.spec)
    if payload.layout:
        spec["layout"] = payload.layout
    if payload.theme:
        spec["theme"] = payload.theme
    r.spec = spec
    r.theme = spec.get("theme", r.theme)
    r.html = render_resume_html(spec)
    r.version += 1
    r.updated_at = datetime.now()
    session.add(r)
    session.commit()
    session.refresh(r)
    return _out(r)


@router.get("/{resume_id}/preview", response_class=HTMLResponse)
def preview_resume(resume_id: int, session: Session = Depends(get_session)) -> HTMLResponse:
    """返回完整 HTML 文档，供 iframe 预览与浏览器打印成 PDF。"""
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    return HTMLResponse(build_preview_document(r.html, r.title or "简历"))


@router.get("/{resume_id}/download", response_class=PlainTextResponse)
def download_resume(resume_id: int, session: Session = Depends(get_session)) -> PlainTextResponse:
    """下载为独立的 .html 文件（自带样式，可离线打开/打印）。"""
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    doc = build_preview_document(r.html, r.title or "简历")
    filename = (r.title or "resume").replace("/", "_").replace("\\", "_")[:60] or "resume"
    # 文件名可能含中文，需按 RFC 5987 做 UTF-8 编码，并提供 ASCII 回退
    ascii_name = "resume.html"
    quoted = quote(f"{filename}.html", safe="")
    return PlainTextResponse(
        doc,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{quoted}'
            )
        },
    )


@router.delete("/{resume_id}", status_code=204, response_class=Response)
def delete_resume(resume_id: int, session: Session = Depends(get_session)) -> Response:
    r = session.get(Resume, resume_id)
    if not r:
        raise HTTPException(404, "简历不存在")
    # Chat 与简历是"一对一单元"：删简历时同时删除关联 Chat（与"删 Chat 级联删简历"对称，
    # 避免出现"成功却没有简历"的不一致状态）。单向关联（Resume.chat_id）下单向删除即可，
    # 不再需要旧模型那套"先置空 resume_id 再删"的绕外键代码。
    chat = session.get(Chat, r.chat_id) if r.chat_id else None
    # 先删除 AI 编辑日志（EditLog.resume_id 外键指向简历），否则删简历会触发约束失败
    for edit in session.exec(select(EditLog).where(EditLog.resume_id == resume_id)).all():
        session.delete(edit)
    session.flush()
    session.delete(r)
    session.flush()
    if chat:
        for msg in session.exec(select(ChatMessage).where(ChatMessage.chat_id == chat.id)).all():
            session.delete(msg)
        session.flush()
        session.delete(chat)
    session.commit()
    return Response(status_code=204)
