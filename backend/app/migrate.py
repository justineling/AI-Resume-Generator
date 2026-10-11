"""幂等数据库迁移：旧模型 `jobtask` -> 新模型 `chat`。

背景
----
旧模型是「粘贴一大段文本 -> 切成 N 个岗位 -> 批量建 N 个任务 -> N 份简历」，
对应 `jobtask` 表 + `resume.task_id`。新模型是「一个 chat = 一份简历」，
对应 `chat` 表 + `resume.chat_id` + `resume.coverage`。

SQLite 的 `ALTER TABLE` 能力有限（改列名 / 删列 / 改外键都要重建表），
且 SQLModel 的 `create_all` **不会**修改已存在的表结构。因此这里用原生
`sqlite3` 连接显式完成表结构迁移与数据搬迁，并在 `init_db()` 末尾调用。

迁移步骤（幂等）
----------------
1. 若 `resume` 表仍是旧结构（有 `task_id`、无 `chat_id`）：**重建** `resume` 表，
   把 `task_id` 的值搬到 `chat_id`，新增 `coverage` 列，并把外键目标从
   `jobtask(id)` 修正为 `chat(id)`。
2. 若存在旧表 `jobtask` 且 `chat` 表为空：逐行搬为 `Chat`；
   `profile_snapshot` 取**迁移当刻的全局 Profile 深拷贝**（旧库未存快照，
   无法还原原始生成时点，这一点会在迁移日志中明确说明）；
   为每条旧任务补一条 `ChatMessage(kind="jd", role="user", content=raw_text)`。
3. 丢弃旧 `jobtask` 表。

无旧数据（全新安装）时整个迁移是 no-op。提供 `python -m app.migrate --fresh`
供想彻底重建的用户（**会丢失全部数据**）。
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from typing import Any, Optional

from sqlmodel import SQLModel

from . import models  # noqa: F401  确保所有表注册到 SQLModel.metadata
from .db import engine

log = logging.getLogger("resume.migrate")

# Profile 里以 JSON 列存储的字段（迁移时需从 TEXT 反序列化）
_PROFILE_JSON_COLS = {
    "links", "education", "experiences", "projects", "skills",
    "certificates", "languages", "awards", "extra",
}
_PROFILE_SCALAR_COLS = (
    "name", "headline", "email", "phone", "location", "age", "avatar", "summary",
)
_PROFILE_ALL_COLS = _PROFILE_SCALAR_COLS + tuple(sorted(_PROFILE_JSON_COLS))

# Chat 表列顺序（与 models.Chat 一致），用于 INSERT
_CHAT_COLS = (
    "id", "title", "source", "source_url", "raw_text", "status", "stage", "progress",
    "error", "attempts", "max_retries", "profile_snapshot", "profile_dirty",
    "job_analysis", "created_at", "updated_at", "started_at", "finished_at",
)


# --------------------------------------------------------------------------- #
# 低层工具
# --------------------------------------------------------------------------- #
def _db_path() -> Optional[str]:
    """返回 SQLite 数据库文件路径；非 SQLite 或内存库返回 None。"""
    url = engine.url
    if url.get_backend_name() != "sqlite":
        return None
    db = url.database
    if not db or db == ":memory:":
        return None
    return db


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, name: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]


def _read_global_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    """读取全局 Profile（第一条），并转成与 pipeline._profile_to_dict 同形的 dict。"""
    if not _table_exists(conn, "profile"):
        return {}
    cols = _columns(conn, "profile")
    if not cols:
        return {}
    row = conn.execute("SELECT * FROM profile ORDER BY id LIMIT 1").fetchone()
    if not row:
        return {}
    data = dict(zip(cols, row))
    out: dict[str, Any] = {}
    for key in _PROFILE_ALL_COLS:
        val = data.get(key)
        if key in _PROFILE_JSON_COLS:
            if isinstance(val, str):
                try:
                    val = json.loads(val)
                except (ValueError, TypeError):
                    val = {} if key == "extra" else []
            if val is None:
                val = {} if key == "extra" else []
        else:
            if val is None:
                val = None if key == "age" else ""
        out[key] = val
    return out


# --------------------------------------------------------------------------- #
# 表结构迁移：resume.task_id -> resume.chat_id + coverage
# --------------------------------------------------------------------------- #
def _migrate_resume_schema(conn: sqlite3.Connection) -> bool:
    """把旧 resume 表重建为新结构。返回是否发生了重建。"""
    if not _table_exists(conn, "resume"):
        return False
    cols = _columns(conn, "resume")
    if "chat_id" in cols:
        # 已是新结构；仅兜底补 coverage 列（极端情况：手工改过表）
        if "coverage" not in cols:
            conn.execute("ALTER TABLE resume ADD COLUMN coverage JSON")
        return False
    if "task_id" not in cols:
        # 结构无法识别，放弃（避免误删数据）
        log.warning("resume 表结构无法识别，跳过表结构迁移：%s", cols)
        return False

    log.info("重建 resume 表：task_id -> chat_id，新增 coverage，外键指向 chat(id)")
    # 显式重建，保证：①列名正确 ②新增 coverage ③外键目标改为 chat(id)
    conn.execute(
        """
        CREATE TABLE resume_new (
            id INTEGER PRIMARY KEY,
            chat_id INTEGER,
            title VARCHAR NOT NULL DEFAULT '',
            theme VARCHAR NOT NULL DEFAULT 'ark',
            html TEXT NOT NULL DEFAULT '',
            spec JSON,
            coverage JSON,
            version INTEGER NOT NULL DEFAULT 1,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            FOREIGN KEY(chat_id) REFERENCES chat (id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO resume_new
            (id, chat_id, title, theme, html, spec, coverage, version, created_at, updated_at)
        SELECT id, task_id, title, theme, html, spec, '{}', version, created_at, updated_at
        FROM resume
        """
    )
    conn.execute("DROP TABLE resume")
    conn.execute("ALTER TABLE resume_new RENAME TO resume")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_resume_chat_id ON resume (chat_id)")
    return True


# --------------------------------------------------------------------------- #
# 数据迁移：jobtask -> chat
# --------------------------------------------------------------------------- #
def _migrate_jobtask_to_chat(conn: sqlite3.Connection) -> int:
    """把旧 jobtask 行搬为 Chat，并补 ChatMessage(jd)。返回搬迁条数。"""
    if not _table_exists(conn, "jobtask"):
        return 0
    if not _table_exists(conn, "chat"):
        log.warning("chat 表不存在，暂不迁移 jobtask（应由 create_all 先建表）")
        return 0

    jt_cols = _columns(conn, "jobtask")
    rows = conn.execute("SELECT * FROM jobtask ORDER BY id").fetchall()
    chat_count = conn.execute("SELECT COUNT(*) FROM chat").fetchone()[0]

    migrated = 0
    if chat_count == 0 and rows:
        snapshot = _read_global_profile(conn)
        snapshot_json = json.dumps(snapshot, ensure_ascii=False)
        for row in rows:
            d = dict(zip(jt_cols, row))
            job_analysis = d.get("job_analysis")
            if not job_analysis:
                job_analysis = "{}"
            max_retries = d.get("max_retries")
            if max_retries is None:
                max_retries = 2
            created = d.get("created_at")
            conn.execute(
                f"INSERT INTO chat ({', '.join(_CHAT_COLS)}) "
                f"VALUES ({', '.join('?' for _ in _CHAT_COLS)})",
                (
                    d.get("id"),
                    d.get("title") or "新对话",
                    d.get("source") or "",
                    d.get("source_url") or "",
                    d.get("raw_text") or "",
                    d.get("status") or "draft",
                    d.get("stage") or "",
                    d.get("progress") or 0,
                    d.get("error") or "",
                    d.get("attempts") or 0,
                    max_retries,
                    snapshot_json,
                    0,                       # profile_dirty
                    job_analysis,
                    created,                 # created_at
                    created,                 # updated_at
                    d.get("started_at"),
                    d.get("finished_at"),
                ),
            )
            raw = (d.get("raw_text") or "").strip()
            if raw:
                conn.execute(
                    "INSERT INTO chatmessage (chat_id, role, kind, content, meta, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (d.get("id"), "user", "jd", d.get("raw_text") or "", "{}", created),
                )
            migrated += 1
        msg = (
            f"[migrate] 已将 {migrated} 条旧任务(jobtask)迁移为 Chat；"
            "profile_snapshot 使用【迁移当刻】的全局资料深拷贝"
            "（旧库未存快照，无法还原原始生成时点）。"
        )
        log.warning(msg)
        print(msg)

    # 无论是否搬迁，旧表都已无用（数据已由 resume 重建时引用；或本来就空）
    conn.execute("DROP TABLE jobtask")
    log.info("已丢弃旧表 jobtask")
    return migrated


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def run(fresh: bool = False) -> None:
    """执行迁移。在 init_db() 末尾调用；也可 `python -m app.migrate`。"""
    db_path = _db_path()
    if not db_path:
        # 非文件型 SQLite（内存库）或非 SQLite：无需迁移
        return

    if fresh:
        _rebuild_all(db_path)
        return

    # 确保新表（chat / chatmessage）存在，再迁移
    SQLModel.metadata.create_all(engine)

    conn = sqlite3.connect(db_path, isolation_level=None)  # autocommit，便于 PRAGMA / DDL
    try:
        # 关闭外键约束：重建 resume 表、丢弃被引用的 jobtask 表时避免约束报错。
        # （迁移完成后所有外键目标都已修正，重新开启由 db.py 的连接事件保证。）
        conn.execute("PRAGMA foreign_keys=OFF")
        _migrate_resume_schema(conn)
        _migrate_jobtask_to_chat(conn)
    finally:
        conn.close()


def _rebuild_all(db_path: str) -> None:
    """--fresh：删除所有表并重建（会丢失全部数据）。"""
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in ("chatmessage", "resume", "editlog", "chat", "jobtask", "profile", "appsetting"):
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        conn.close()
    SQLModel.metadata.create_all(engine)
    msg = "[migrate] --fresh 已删除并重建全部表（原有数据已全部丢失）。"
    log.warning(msg)
    print(msg)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AI Resume Generator 数据库迁移工具（幂等，安全）。",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="⚠️ 危险：删除所有表并重建，会丢失全部数据（含简历）。仅在确认不需要旧数据时使用。",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(fresh=args.fresh)
    print("[migrate] 完成。")


if __name__ == "__main__":
    main()
