"""数据库引擎与会话管理。

SQLite 默认开启 WAL 与 check_same_thread=False，以便后台 worker 与请求线程共享。
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from .config import settings

engine = create_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def init_db() -> None:
    """建表（幂等）+ 执行旧模型迁移（jobtask -> chat）。

    create_all 只建缺失的表、不会修改已存在的表结构，因此旧库的
    `jobtask` -> `chat`、`resume.task_id` -> `chat_id` 由 migrate.run() 负责。
    """
    SQLModel.metadata.create_all(engine)
    from . import migrate  # 延迟导入：migrate 依赖本模块的 engine，避免循环导入
    migrate.run()


@contextmanager
def session_scope() -> Iterator[Session]:
    """请求 / worker 通用的事务上下文。"""
    session = Session(engine)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖注入用。"""
    with Session(engine) as session:
        yield session
