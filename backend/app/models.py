"""数据模型（SQLModel + SQLite）。

设计要点（聊天式工作区模型）：
- Profile 保存"固定资料库"（全局单份），结构化字段用 JSON 列承载，便于任意扩展。
- Chat 是聊天式工作区的最小单元：**一个 chat = 一个求职场景 = 恰好一份简历**。
  它由旧的 JobTask 演进而来（重命名 + 扩展），继续作为调度与并发的最小单位，
  因此调度器（asyncio.Queue + worker + 条件 UPDATE 抢占 + 重启恢复）可 100% 复用。
- Chat.profile_snapshot 是"冻结的工作上下文"：新建 chat 时对全局 Profile 做整体快照，
  chat 内修改只影响本 chat，不影响全局也不影响其它 chat（提供"重置回全局"）。
- ChatMessage 是对话消息（贴 JD / 文本 / 系统提示 / 完成通知），与 EditLog 语义正交。
- Resume 以 HTML 为唯一真相来源（source of truth），同时保留生成时的 spec 快照与
  coverage 覆盖清单（覆盖清单是"这份简历"的属性，随简历版本走）。
- Resume 与 Chat 的关联**收敛为单向**：只保留 Resume.chat_id；Chat 列表所需的
  resume_id 由查询左连派生，不再存储，避免双向引用带来的删库绕外键代码。
- EditLog 记录对简历 HTML 的 AI 二次编辑历史，支持回溯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Column, Text
from sqlalchemy.types import JSON
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now()


# --------------------------------------------------------------------------- #
# 用户资料库
# --------------------------------------------------------------------------- #
class Profile(SQLModel, table=True):
    """用户的固定简历素材库（全局单份，也可扩展为多份）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(default="", index=True)
    headline: str = Field(default="")          # 一句话定位，如 "5 年后端工程师"
    email: str = Field(default="")
    phone: str = Field(default="")
    location: str = Field(default="")
    age: Optional[int] = Field(default=None)
    avatar: str = Field(default="", sa_column=Column(Text))  # data-url 或外链
    summary: str = Field(default="", sa_column=Column(Text))

    # 结构化素材，元素结构见 app/schemas.py 中的注释
    links: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    education: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    experiences: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    projects: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    skills: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    certificates: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    languages: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    awards: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    extra: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# 聊天式工作区（由 JobTask 演进而来）
# --------------------------------------------------------------------------- #
class Chat(SQLModel, table=True):
    """一次"为某个求职场景生成一份简历"的聊天式工作区。

    生命周期：draft（新建未生成）-> queued -> running -> succeeded / failed / cancelled。
    合法取值无 DB CHECK，由 API 入参层收敛（见 schemas.ChatStatus）。
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    title: str = Field(default="新对话")

    # 岗位来源元信息（沿用旧 JobTask 字段）
    source: str = Field(default="")            # JobStreet / Indeed / 手动粘贴 ...
    source_url: str = Field(default="")

    raw_text: str = Field(default="", sa_column=Column(Text))   # 原始岗位要求（非结构化）

    status: str = Field(default="draft", index=True)
    stage: str = Field(default="")             # 当前阶段的人类可读描述
    progress: int = Field(default=0)           # 0-100
    error: str = Field(default="", sa_column=Column(Text))

    attempts: int = Field(default=0)
    max_retries: int = Field(default=2)

    # per-chat 资料快照：新建时对全局 Profile 整体深拷贝；chat 内可改，不影响全局/其它 chat
    profile_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    profile_dirty: bool = Field(default=False)  # 快照是否被 chat 内修改过

    # 岗位解析产物：requirements / sections / employer_questions（不含 coverage）
    job_analysis: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=_now, index=True)
    updated_at: datetime = Field(default_factory=_now)
    started_at: Optional[datetime] = Field(default=None)
    finished_at: Optional[datetime] = Field(default=None)


# --------------------------------------------------------------------------- #
# 对话消息
# --------------------------------------------------------------------------- #
class ChatMessage(SQLModel, table=True):
    """聊天工作区里的一条消息。与 EditLog（简历 HTML 的二次编辑）语义正交。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(foreign_key="chat.id", index=True)

    role: str = Field(default="user")          # user / assistant / system
    kind: str = Field(default="text")          # jd / text / notice / guidance
    content: str = Field(default="", sa_column=Column(Text))
    meta: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    created_at: datetime = Field(default_factory=_now, index=True)


# --------------------------------------------------------------------------- #
# 简历
# --------------------------------------------------------------------------- #
class Resume(SQLModel, table=True):
    """生成结果。HTML 为唯一真相来源。与 Chat 单向关联（Resume.chat_id）。"""

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: Optional[int] = Field(default=None, foreign_key="chat.id", index=True)

    title: str = Field(default="")
    theme: str = Field(default="ark")          # 版式主题；合法取值 ark/endfield/exa/corporate
    html: str = Field(default="", sa_column=Column(Text))
    spec: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))  # 生成时的结构快照
    # 覆盖清单：{"coverage": [CoverageItem...], "summary": {total, grounded, capability, uncovered}}
    coverage: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    version: int = Field(default=1)

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# AI 二次编辑记录
# --------------------------------------------------------------------------- #
class EditLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    resume_id: int = Field(foreign_key="resume.id", index=True)
    instruction: str = Field(default="", sa_column=Column(Text))
    scope: str = Field(default="whole")        # 合法取值 whole / section（schemas.EditScope）
    target: str = Field(default="")            # 局部编辑时的目标（如 section id）
    ok: bool = Field(default=True)
    error: str = Field(default="", sa_column=Column(Text))
    before_html: str = Field(default="", sa_column=Column(Text))
    after_html: str = Field(default="", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=_now)


# --------------------------------------------------------------------------- #
# 应用设置（可运行时修改，覆盖 .env）
# --------------------------------------------------------------------------- #
class AppSetting(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = Field(default="", sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=_now)
