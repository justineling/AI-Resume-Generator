"""任务调度器：asyncio 队列 + 多 worker 并发 + 指数退避重试。

设计取舍：
- 用进程内 asyncio 队列而非 Celery/Redis，是为了"零外部依赖、开箱即跑"。
  队列本身是 DB 里的 status 字段，进程内队列只是触发器，因此重启后可从 DB 恢复。
- worker 数量可运行时调整（会平滑重启 worker 协程）。
- 失败重试：attempts < max_retries 时按指数退避延迟重新入队；否则置 failed 并保留错误。
- 任务"抢占"用条件 UPDATE 保证原子性，重复入队不会被两个 worker 同时处理。

命名说明：调度单元由旧的 JobTask 演进为 Chat（重命名 + 扩展），但调度器完全以
chat id 为键、逻辑未变，属机械替换，从而 100% 复用经过回归验证的并发/重试/取消逻辑。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy import func, update
from sqlmodel import select

from .config import settings
from .db import session_scope
from .events import hub
from .llm import get_runtime_config
from .models import Chat
from .pipeline import TaskCancelled, run_chat

log = logging.getLogger("resume.scheduler")

# 兼容别名：regression.py 通过 `sched_mod.run_task` 打桩注入"慢任务"，
# 因此这里保留模块级名字 run_task（指向 run_chat），_handle 在调用时按名字查找。
run_task = run_chat

# 指数退避上限（秒）
MAX_BACKOFF = 60.0


def _backoff_delay(attempts: int) -> float:
    """失败重试延迟：retry_backoff * 2^(attempts-1)，封顶 MAX_BACKOFF 秒。"""
    return min(settings.retry_backoff * (2 ** (attempts - 1)), MAX_BACKOFF)


class TaskScheduler:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._cancelled: set[int] = set()
        self._running: set[int] = set()
        self._desired_workers = settings.worker_count
        self._supervisor: asyncio.Task | None = None

    # --------------------------- 生命周期 --------------------------- #
    async def start(self) -> None:
        await self._recover()
        self._desired_workers = self._read_worker_count()
        await self._spawn_workers(self._desired_workers)
        self._supervisor = asyncio.create_task(self._watch_settings())
        log.info("调度器已启动，worker=%s", self._desired_workers)

    async def shutdown(self) -> None:
        if self._supervisor:
            self._supervisor.cancel()
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def _recover(self) -> None:
        """重启恢复：把上次残留的 running 任务重新入队。"""
        with session_scope() as session:
            stale = session.exec(select(Chat).where(Chat.status == "running")).all()
            for t in stale:
                t.status = "queued"
                t.progress = 0
                t.stage = "重启恢复，重新排队"
                self._queue.put_nowait(t.id)
            # 已排队但未处理的，也补进队列
            queued = session.exec(select(Chat).where(Chat.status == "queued")).all()
            for t in queued:
                self._queue.put_nowait(t.id)
        if stale:
            log.info("恢复 %s 个中断任务", len(stale))

    def _read_worker_count(self) -> int:
        with session_scope() as session:
            return max(1, min(16, int(get_runtime_config(session)["worker_count"])))

    async def _spawn_workers(self, n: int) -> None:
        self._workers = [asyncio.create_task(self._worker(i)) for i in range(n)]

    async def _watch_settings(self) -> None:
        """轮询 worker_count 变化，变化时平滑重启 worker 池。"""
        while True:
            await asyncio.sleep(5)
            desired = self._read_worker_count()
            if desired != self._desired_workers:
                log.info("worker 数量 %s -> %s", self._desired_workers, desired)
                await self._restart_workers(desired)

    async def _restart_workers(self, desired: int) -> None:
        """平滑重启 worker 池。

        关键点：在取消旧 worker 之前，先把 `self._running` 里正在处理的任务复位为
        queued 并收集起来；新 worker 起来后重新入队。否则 worker 协程被 CancelledError
        打断后，任务会永久停在 running（只有进程重启才会被 _recover 捞回）。
        """
        self._desired_workers = desired

        # 1) 收集运行中的任务并复位为 queued（趁取消之前）
        requeue_ids = list(self._running)
        if requeue_ids:
            with session_scope() as session:
                for cid in requeue_ids:
                    t = session.get(Chat, cid)
                    if t and t.status == "running":
                        t.status = "queued"
                        t.progress = 0
                        t.stage = "worker 调整，重新排队"

        # 2) 取消旧 worker
        old = self._workers
        self._workers = []
        for w in old:
            w.cancel()
        await asyncio.gather(*old, return_exceptions=True)

        # 3) 起新 worker 并重新入队
        await self._spawn_workers(desired)
        for cid in requeue_ids:
            self._running.discard(cid)
            await self._queue.put(cid)
            await hub.broadcast("chat_updated", {
                "chat_id": cid, "status": "queued", "progress": 0, "stage": "worker 调整，重新排队",
            })

    # --------------------------- 入队 / 取消 --------------------------- #
    async def enqueue(self, chat_id: int) -> None:
        self._cancelled.discard(chat_id)
        with session_scope() as session:
            t = session.get(Chat, chat_id)
            if t:
                t.status = "queued"
                t.progress = 0
                t.stage = "排队中"
                t.error = ""
                # 重新生成 / 重试时必须清空 started_at，否则前端会用上一次的
                # 开始时间起「已用时」表，表现为计时不是从 0 开始。
                t.started_at = None
                t.updated_at = datetime.now()
        await self._queue.put(chat_id)
        await hub.broadcast("chat_updated", {"chat_id": chat_id, "status": "queued", "progress": 0, "stage": "排队中"})

    async def cancel(self, chat_id: int) -> bool:
        self._cancelled.add(chat_id)
        with session_scope() as session:
            t = session.get(Chat, chat_id)
            if not t:
                return False
            if t.status in ("queued", "running"):
                t.status = "cancelled"
                t.stage = "已取消"
                t.finished_at = datetime.now()
                t.updated_at = datetime.now()
        await hub.broadcast("chat_updated", {"chat_id": chat_id, "status": "cancelled", "stage": "已取消"})
        return True

    def is_cancelled(self, chat_id: int) -> bool:
        return chat_id in self._cancelled

    # --------------------------- worker --------------------------- #
    async def _worker(self, index: int) -> None:
        while True:
            chat_id = await self._queue.get()
            try:
                await self._handle(chat_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - 兜底，避免 worker 退出
                log.exception("worker %s 处理 chat %s 时未捕获异常", index, chat_id)
            finally:
                self._queue.task_done()
                self._running.discard(chat_id)

    async def _handle(self, chat_id: int) -> None:
        if self.is_cancelled(chat_id):
            return

        # 用条件 UPDATE 原子地"抢占"任务：只有仍处于 queued 才能抢到，
        # 避免同一任务被重复入队时被两个 worker 同时处理（重复简历 / 重复 LLM 计费）。
        with session_scope() as session:
            result = session.execute(
                update(Chat)
                .where(Chat.id == chat_id, Chat.status == "queued")
                .values(
                    status="running",
                    stage="开始处理",
                    started_at=func.coalesce(Chat.started_at, datetime.now()),
                    attempts=Chat.attempts + 1,
                )
            )
            if result.rowcount == 0:
                # 没抢到：已被其它 worker 处理，或状态已不是 queued
                return
            task = session.get(Chat, chat_id)
            if not task:
                return
            attempts, max_retries = task.attempts, task.max_retries
        self._running.add(chat_id)
        await hub.broadcast("chat_updated", {"chat_id": chat_id, "status": "running", "stage": "开始处理"})

        try:
            resume_id = await run_task(chat_id, should_cancel=lambda: self.is_cancelled(chat_id))
            log.info("chat %s 完成 -> resume %s", chat_id, resume_id)
        except TaskCancelled:
            # 运行中被用户取消：保持 cancelled，绝不进入失败重试逻辑
            log.info("chat %s 已取消，停止处理", chat_id)
            with session_scope() as session:
                t = session.get(Chat, chat_id)
                if t:
                    t.status = "cancelled"
                    t.stage = "已取消"
                    t.finished_at = t.finished_at or datetime.now()
            await hub.broadcast("chat_updated", {"chat_id": chat_id, "status": "cancelled", "stage": "已取消"})
            return
        except Exception as exc:
            log.warning("chat %s 第 %s 次失败：%s", chat_id, attempts, exc)
            if attempts <= max_retries and not self.is_cancelled(chat_id):
                delay = _backoff_delay(attempts)
                with session_scope() as session:
                    t = session.get(Chat, chat_id)
                    if t:
                        t.status = "queued"
                        t.progress = 0
                        t.stage = f"第 {attempts} 次失败，{delay:.0f}s 后重试"
                        t.error = str(exc)[:1000]
                await hub.broadcast("chat_updated", {
                    "chat_id": chat_id, "status": "queued", "progress": 0,
                    "stage": f"第 {attempts} 次失败，{delay:.0f}s 后重试", "error": str(exc)[:300],
                })
                asyncio.create_task(self._requeue_later(chat_id, delay))
            else:
                with session_scope() as session:
                    t = session.get(Chat, chat_id)
                    if t:
                        t.status = "failed"
                        t.progress = 0
                        t.stage = "失败"
                        t.error = str(exc)[:1000]
                        t.finished_at = datetime.now()
                await hub.broadcast("chat_updated", {
                    "chat_id": chat_id, "status": "failed", "progress": 0, "stage": "失败", "error": str(exc)[:300],
                })
            return

        # run_chat 成功返回后再确认一次：若在收尾窗口内被取消，不要把状态覆盖成 succeeded
        if self.is_cancelled(chat_id):
            with session_scope() as session:
                t = session.get(Chat, chat_id)
                if t:
                    t.status = "cancelled"
                    t.stage = "已取消"
                    t.finished_at = t.finished_at or datetime.now()
            await hub.broadcast("chat_updated", {"chat_id": chat_id, "status": "cancelled", "stage": "已取消"})

    async def _requeue_later(self, chat_id: int, delay: float) -> None:
        await asyncio.sleep(delay)
        if not self.is_cancelled(chat_id):
            await self._queue.put(chat_id)


scheduler = TaskScheduler()
