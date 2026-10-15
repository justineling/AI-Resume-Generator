"""请求 / 响应模型与文档化的素材结构。

素材结构约定（前端与 LLM 共享）：

links:        [{ "label": "GitHub", "url": "https://..." }]
education:    [{ "school": "", "degree": "", "major": "", "start": "", "end": "", "highlights": ["GPA/荣誉等"] }]
experiences:  [{ "company": "", "title": "", "location": "", "start": "", "end": "",
                 "summary": "", "achievements": ["量化成果..."], "tech": ["..."] }]
projects:     [{ "name": "", "role": "", "start": "", "end": "", "link": "",
                 "summary": "", "highlights": ["..."] }]
skills:       [{ "name": "", "level": "熟练/精通/了解", "category": "语言/框架/工具" }]
certificates: [{ "name": "", "issuer": "", "date": "" }]
languages:    [{ "name": "", "level": "" }]
awards:       [{ "name": "", "date": "", "detail": "" }]
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# 枚举约束（API 入参层收敛）
# 这些取值同时是 models.py 里对应字段的合法集合（DB 层不做 CHECK 迁移，
# 仅用注释说明），并在前端 resume.js 的 LAYOUTS / THEMES 中保持一致。
# --------------------------------------------------------------------------- #
# Chat 生命周期：draft=新建未生成；queued/running/succeeded/failed/cancelled 为调度态。
# （TaskStatus 为兼容层 /api/tasks 复用同一集合，因此也包含 draft。）
ChatStatus = Literal["draft", "queued", "running", "succeeded", "failed", "cancelled"]
TaskStatus = ChatStatus  # 兼容别名：旧 /api/tasks 的 TaskOut.status
ChatMessageRole = Literal["user", "assistant", "system"]
ChatMessageKind = Literal["jd", "text", "notice", "guidance"]
CoverageSource = Literal["grounded", "capability"]
EditScope = Literal["whole", "section"]
LayoutId = Literal["single-column", "sidebar-left", "two-column", "timeline", "compact-grid"]
ThemeId = Literal["ark", "endfield", "exa", "corporate"]


# ----------------------------- Profile ----------------------------- #
class ProfileIn(BaseModel):
    name: str = ""
    headline: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    age: Optional[int] = None
    avatar: str = ""
    summary: str = ""
    links: list[dict[str, Any]] = Field(default_factory=list)
    education: list[dict[str, Any]] = Field(default_factory=list)
    experiences: list[dict[str, Any]] = Field(default_factory=list)
    projects: list[dict[str, Any]] = Field(default_factory=list)
    skills: list[dict[str, Any]] = Field(default_factory=list)
    certificates: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[dict[str, Any]] = Field(default_factory=list)
    awards: list[dict[str, Any]] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


# --------------------------- AI 面试问答（flashcard） --------------------------- #
class InterviewQuestion(BaseModel):
    """一张 flashcard：向用户提一个具体问题。"""

    id: str
    topic: str = ""
    question: str
    hint: str = ""
    placeholder: str = ""


class InterviewAnswer(BaseModel):
    """用户对某张卡的回答（可为空 = 跳过）。"""

    id: str = ""
    topic: str = ""
    question: str = ""
    answer: str = ""


class ProfileInterviewIn(BaseModel):
    """资料页：请求生成问题（focus 可选，用来引导关注方向）。"""

    focus: str = ""


class InterviewQuestionsOut(BaseModel):
    questions: list[InterviewQuestion] = Field(default_factory=list)
    gaps: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "mock"          # llm | mock


class InterviewAnswersIn(BaseModel):
    answers: list[InterviewAnswer] = Field(default_factory=list)
    # 工作区用：提交后是否立即开始生成
    auto_generate: bool = True


class ProfileOut(ProfileIn):
    id: int
    created_at: datetime
    updated_at: datetime


class ProfileImportTextIn(BaseModel):
    """粘贴简历原文 -> 让 LLM / 启发式解析成完整资料并写库。"""

    text: str = ""


# ----------------------------- Chat ----------------------------- #
class ChatIn(BaseModel):
    """新建聊天工作区。"""

    title: str = ""


class ChatPatch(BaseModel):
    """部分更新（目前仅标题）。"""

    title: Optional[str] = None


class ChatMessageIn(BaseModel):
    """追加一条对话消息。kind=jd 时同步写入 Chat.raw_text。"""

    kind: ChatMessageKind = "text"
    content: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)


class ChatMessageOut(BaseModel):
    id: int
    chat_id: int
    role: ChatMessageRole
    kind: ChatMessageKind
    content: str
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ChatOut(BaseModel):
    id: int
    title: str
    status: ChatStatus
    stage: str
    progress: int
    error: str
    attempts: int
    max_retries: int
    raw_text: str
    source: str
    source_url: str
    profile_snapshot: dict[str, Any] = Field(default_factory=dict)
    profile_dirty: bool = False
    job_analysis: dict[str, Any] = Field(default_factory=dict)
    # 派生的简历 id（左连 Resume），非存储字段；一 chat 至多一份简历
    resume_id: Optional[int] = None
    # URL 导入时 LLM 抽取出的职位名称（可能与 title 相同；仅供前端提示用）
    extracted_title: Optional[str] = None
    # 仅 GET /api/chats/{id} 填充；列表接口留空
    messages: list[ChatMessageOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ----------------------------- 覆盖清单 ----------------------------- #
class CoverageItem(BaseModel):
    req_id: str = ""
    requirement: str = ""
    section: str = ""
    resume_evidence: str = ""
    source: CoverageSource = "capability"   # grounded（须有 evidence_ref）/ capability
    evidence_ref: str = ""
    note: str = ""


class CoverageSummary(BaseModel):
    total: int = 0
    grounded: int = 0
    capability: int = 0
    uncovered: int = 0


class CoverageOut(BaseModel):
    coverage: list[CoverageItem] = Field(default_factory=list)
    summary: CoverageSummary = Field(default_factory=CoverageSummary)


# ----------------------------- Task（兼容层）----------------------------- #
class JobTaskIn(BaseModel):
    """创建单个任务（兼容层 /api/tasks）。"""

    title: str = ""
    company: str = ""
    source: str = ""
    source_url: str = ""
    raw_text: str = ""
    max_retries: Optional[int] = None


class BatchTaskIn(BaseModel):
    """批量创建任务：前端把多段岗位文本按分隔切成多条。"""

    jobs: list[JobTaskIn]
    max_retries: Optional[int] = None


class TaskOut(BaseModel):
    """兼容层响应体（内部映射到 Chat）。"""

    id: int
    title: str
    company: str
    source: str
    source_url: str
    status: TaskStatus
    stage: str
    progress: int
    error: str
    attempts: int
    max_retries: int
    resume_id: Optional[int]
    job_analysis: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


# ----------------------------- Resume ----------------------------- #
class ResumeMeta(BaseModel):
    id: int
    chat_id: Optional[int]
    title: str
    theme: str
    version: int
    created_at: datetime
    updated_at: datetime


class ResumeOut(ResumeMeta):
    html: str
    spec: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)


class ResumeSaveIn(BaseModel):
    """保存编辑器里的内容。html 为唯一真相来源。"""

    html: Optional[str] = None
    title: Optional[str] = None
    theme: Optional[ThemeId] = None


# ----------------------------- AI 编辑 ----------------------------- #
class AIEditIn(BaseModel):
    instruction: str
    scope: EditScope = "whole"    # whole / section
    target: str = ""              # 局部编辑时的 section id
    current_html: Optional[str] = None  # 前端未保存的最新 HTML（优先于库内）


class AIEditOut(BaseModel):
    ok: bool
    html: str
    summary: str = ""
    error: str = ""


# ----------------------------- 设置 ----------------------------- #
class LLMSettingsIn(BaseModel):
    """保存 LLM 设置。除 provider_id 外全部可选（None 表示保持不变）。

    向后兼容：老前端只传 base_url / api_key / model 也能正常工作。
    """

    provider_id: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    worker_count: Optional[int] = None
    # 单次生成的最大输出 token（None 表示保持不变）。<= 0 表示「不限制」：
    # 生成时不发送 max_tokens 字段，由服务端决定上限。仅 < 0 会被拒绝（400）。
    max_tokens: Optional[int] = None


class LLMSettingsOut(BaseModel):
    provider_id: str       # 已解析的供应商 id（存储值优先，其次按 base_url 反查，最后 custom）
    base_url: str
    model: str
    temperature: float
    worker_count: int
    max_tokens: int
    has_key: bool
    key_hint: str          # 脱敏后的提示，如 "sk-...a1b2"
    enabled: bool
    mock_mode: bool


class LLMTestIn(BaseModel):
    """测试连接：校验 API Key 并尝试拉取真实模型列表。"""

    provider_id: str
    base_url: Optional[str] = None   # 为空时使用 provider 预设；provider_id=custom 时必须显式提供
    api_key: Optional[str] = None
    model: Optional[str] = None


class LLMModel(BaseModel):
    id: str
    name: str = ""


class LLMTestOut(BaseModel):
    ok: bool
    message: str                                  # 人类可读的中文结论
    latency_ms: int = 0
    models: list[LLMModel] = Field(default_factory=list)
    status_code: Optional[int] = None
