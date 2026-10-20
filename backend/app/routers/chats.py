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
    InterviewAnswersIn,
    InterviewQuestionsOut,
    ChatIn,
    ChatMessageIn,
    ChatMessageOut,
    ChatOut,
    ChatPatch,
    CoverageItem,
    CoverageOut,
    CoverageSummary,
    ProfileImportTextIn,
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

    # 这里**不直接入队**：生成前需要插入「AI 问答」步骤（flashcard），
    # 由前端在导入完成后调用完整的生成流程（与手动点「生成简历」同一条路径）。
    # 若在此直接 enqueue，会绕过问答步骤 —— auto_generate 仅作为前端的行为提示。

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


def _import_delta_text(delta: dict[str, int]) -> str:
    """把「本次新增了多少条」转成一句可读中文（用于对话消息）。"""
    labels = [
        ("experiences", "段工作经历"),
        ("projects", "个项目"),
        ("skills", "项技能"),
        ("education", "条教育经历"),
        ("certificates", "个证书"),
        ("languages", "门语言"),
        ("awards", "项荣誉"),
        ("links", "个链接"),
    ]
    parts = [f"{delta[k]} {label}" for k, label in labels if delta.get(k)]
    return "、".join(parts) if parts else "未发现新的可增补内容"


@router.post("/{chat_id}/profile/import-text", response_model=ChatOut)
async def import_chat_profile_text(
    chat_id: int, payload: ProfileImportTextIn, session: Session = Depends(get_session)
) -> ChatOut:
    """工作区内直接粘贴简历原文 -> 解析并**增补**进本对话的资料快照。

    与「资料」页的 ``/api/profile/import-text``（整体覆盖写库）不同，这里采用与
    「AI 问答」一致的**只增补、不覆盖**策略：已有非空字段保留（用户手填 / 之前问答
    补充的更可信），仅用解析结果填空缺；列表字段按「身份键」去重后追加。

    解析逻辑完全复用 :func:`app.profile_import.parse_profile_text`（LLM 优先，
    失败 / 未配置 Key 时回落 ``mock.parse_resume_text`` 启发式），不重写解析。
    """
    from ..profile_import import (
        has_content,
        merge_profiles,
        merge_summary,
        normalize_profile,
        parse_profile_text,
    )

    chat = _get_chat_or_404(session, chat_id)
    if chat.status in ("running", "queued"):
        raise HTTPException(409, "对话正在生成中，请稍后再导入资料")

    text = (payload.text or "").strip()
    if not text:
        raise HTTPException(400, "请先粘贴简历文本")

    parsed = await parse_profile_text(text, build_client(session))

    base = chat.profile_snapshot or {}
    base_norm = normalize_profile(base)
    if not has_content(base_norm):
        # 快照为空（尚无任何资料）-> 直接采用解析结果
        merged = normalize_profile(parsed)
    else:
        # 非空 -> 按字段合并（只增补，不覆盖）
        merged = merge_profiles(base, parsed)

    chat.profile_snapshot = merged
    chat.profile_dirty = True            # 快照已被本对话定制
    chat.updated_at = datetime.now()
    session.add(chat)

    delta = merge_summary(base_norm, merged)
    # 往对话追加一条消息，便于回看（不广播，刷新后即可见）
    session.add(ChatMessage(
        chat_id=chat.id, role="assistant", kind="notice",
        content=f"已解析并写入本对话资料：{_import_delta_text(delta)}（只增补，不覆盖）",
        meta={"type": "profile_import", "added": delta},
    ))
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


@router.post("/{chat_id}/interview", response_model=InterviewQuestionsOut)
async def chat_interview(
    chat_id: int, session: Session = Depends(get_session)
) -> InterviewQuestionsOut:
    """在生成简历之前，先就「岗位需要但资料缺依据」的点向用户提问。

    岗位要求若尚未解析，会先解析一次（不改变 chat 状态）。
    """
    from ..interview import generate_questions
    from ..pipeline import (
        _is_profile_empty,
        _normalize_analysis,
        _profile_from_snapshot,
        ensure_snapshot_usable,
    )
    from .. import mock, prompts
    from ..llm import LLMError

    chat = _get_chat_or_404(session, chat_id)
    if chat.status in ("running", "queued"):
        raise HTTPException(409, "简历正在生成中，无法同时进行问答")

    raw_text = (chat.raw_text or "").strip()
    if not raw_text:
        raise HTTPException(400, "请先填写「岗位要求」再进行问答")

    profile = _profile_from_snapshot(chat.profile_snapshot)
    if _is_profile_empty(profile):
        # 空快照 + 未定制 + 全局有资料 -> 自动同步（修复"先建对话后填资料"的时序坑）；
        # 已定制或全局也为空 -> 维持原有报错。
        if not ensure_snapshot_usable(session, chat):
            raise HTTPException(400, "尚未填写资料库，请先在「资料」页保存你的资料")
        session.commit()
        session.refresh(chat)
        profile = _profile_from_snapshot(chat.profile_snapshot)

    analysis = chat.job_analysis or {}
    if not analysis.get("requirements"):
        client = build_client(session)
        if client.enabled:
            try:
                analysis = await client.chat_json(
                    prompts.SYSTEM_ANALYZE, prompts.build_analyze_user(raw_text)
                )
            except (LLMError, ValueError) as exc:
                raise HTTPException(422, f"岗位解析失败：{exc}")
        else:
            analysis = mock.analyze_job(raw_text)
        analysis = _normalize_analysis(analysis)
        chat.job_analysis = analysis
        chat.updated_at = datetime.now()
        session.add(chat)
        session.commit()

    result = await generate_questions(profile, analysis, build_client(session))
    return InterviewQuestionsOut(**result)


@router.post("/{chat_id}/interview/submit", response_model=ChatOut)
async def chat_interview_submit(
    chat_id: int, payload: InterviewAnswersIn, session: Session = Depends(get_session)
) -> ChatOut:
    """把问答补充整合进该对话的资料快照；auto_generate=True 时随即开始生成。"""
    from ..interview import apply_answers
    from ..pipeline import _profile_from_snapshot

    chat = _get_chat_or_404(session, chat_id)
    if chat.status in ("running", "queued"):
        raise HTTPException(409, "简历正在生成中")

    answers = [a.model_dump() for a in payload.answers]
    answers = [a for a in answers if str(a.get("answer") or "").strip()]
    if answers:
        snapshot = _profile_from_snapshot(chat.profile_snapshot) or _global_profile_dict(session)
        merged = await apply_answers(snapshot, answers, build_client(session), for_chat=True)
        chat.profile_snapshot = merged
        chat.profile_dirty = True          # 快照已被本对话定制
        chat.updated_at = datetime.now()
        session.add(chat)
        # 把问答记录进对话消息，便于回看
        session.add(ChatMessage(
            chat_id=chat.id, role="user", kind="guidance",
            content="\n\n".join(f"Q：{a.get('question','')}\nA：{a.get('answer','')}" for a in answers),
            meta={"type": "interview"},
        ))
        session.commit()
        session.refresh(chat)

    if payload.auto_generate:
        if not (chat.raw_text or "").strip():
            raise HTTPException(400, "请先填写「岗位要求」")
        await scheduler.enqueue(chat.id)
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
