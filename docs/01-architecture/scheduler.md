# 任务调度器 / Scheduler

> **这篇讲什么**：`scheduler.py` 为什么要用 asyncio.Queue + 条件 UPDATE 抢占、worker 模型与并发度来源、为什么必须保留 `_recover()`、状态机与不变式，以及它与 WebSocket 事件的关系。
> **适合谁读**：要改并发/重试/取消逻辑，或排查「任务卡在 running」「进度乱跳」的开发者。
>
> 权威来源：`backend/app/scheduler.py`、`backend/app/events.py`、`backend/app/db.py`。

---

## 1. 为什么是 asyncio.Queue + 条件 UPDATE 抢占

| 方案 | 取舍 |
|---|---|
| **asyncio.Queue + worker（本项目）** | 零外部依赖、开箱即跑；队列只是**进程内触发器**，真正的队列状态是 DB 里的 `status` 字段 → 可重启恢复。 |
| 内存队列（无 DB 状态） | 进程重启后排队中的任务全丢，无法恢复。 |
| Celery + Redis | 引入外部 broker/worker 进程，违背「本地优先、零外部依赖」的定位；且本场景单机、任务量小，杀鸡用牛刀。 |

**抢占的原子性**：worker 从队列拿到 `chat_id` 后，用一条**条件 UPDATE** 抢占（`_handle`）：

```sql
UPDATE chat SET status='running', stage='开始处理',
       started_at=COALESCE(started_at, now), attempts=attempts+1
WHERE id=? AND status='queued'
```

- `result.rowcount == 0` 表示**没抢到**（已被其它 worker 处理，或状态已不是 `queued`）→ 直接返回。
- 这保证同一任务被**重复入队**时不会被两个 worker 同时处理（避免重复简历 / 重复 LLM 计费）。
- `COALESCE(started_at, now)`：保留首次开始时间，重试时不重置。

---

## 2. worker 协程模型与并发度

- `TaskScheduler.__init__`：`_queue = asyncio.Queue[int]`、`_workers: list[asyncio.Task]`、`_cancelled: set[int]`、`_running: set[int]`、`_desired_workers = settings.worker_count`、`_supervisor`。
- **并发度来源**：`worker_count`（默认 `settings.worker_count = 3`）。
  - 启动时 `_read_worker_count()` 从运行时配置读取（DB 覆盖 env），并 **clamp 到 `[1, 16]`**。
  - `_watch_settings` 每 **5 秒**轮询一次 `worker_count`，变化则 `_restart_workers` 平滑重启 worker 池。
- **每个 worker** 是一个 `async def _worker(index)` 死循环：`await queue.get()` → `_handle` → `task_done()`；任何异常都被兜底，防止 worker 退出。

### 平滑重启的关键点（`_restart_workers`）

1. **先收集** `self._running` 里正在处理的任务，把它们复位为 `queued`（`progress=0`、`stage="worker 调整，重新排队"`）；
2. 再取消旧 worker；
3. 起新 worker，把收集到的任务**重新入队**并广播 `chat_updated`。

> 否则 worker 协程被 `CancelledError` 打断后，任务会**永久停在 running**，只有进程重启才会被 `_recover` 捞回。

---

## 3. DB 是真相来源，Queue 只是触发器——为什么必须保留 `_recover()`

**核心心智模型**：进程内 `asyncio.Queue` 在进程退出时消失，但 `chat.status` 持久在 SQLite 里。因此：

- `start()` 第一步就是 `await self._recover()`（在拉起 worker **之前**）。
- `_recover()`：
  1. 把所有 `status == "running"` 的残留任务复位为 `queued`（`progress=0`、`stage="重启恢复，重新排队"`）并重新入队；
  2. 把所有 `status == "queued"` 的任务也补进队列。

**没有 `_recover()` 的后果**：进程崩溃/重启后，`running` 的任务永远卡住（没有任何 worker 会处理它），`queued` 的任务也不会被消费。这正是「DB 是真相来源」的直接推论。

---

## 4. 状态机与不变式

```
draft ──enqueue──► queued ──抢占──► running ──成功──► succeeded
  ▲                  ▲  │              │
  │                  │  └──取消/失败重试─┘
  │                  │                    └──超过 max_retries──► failed
  └────（新建）───────┘        cancelled ◄──cancel()
```

### 不变式（Hard Invariants）

1. **把 `status` 改写为 `queued` 或 `failed` 时，必须同时把 `progress = 0`。**
   - 位置：`_restart_workers`（复位 running）、`_handle` 失败重试分支、`_handle` 达上限失败分支、`enqueue`（排队中）、`_recover`。
   - 原因：进度条不能停在 60% 却显示「排队中/失败」，否则用户以为还在跑。
   - **`succeeded` / `cancelled` 不归零**：成功保持 100%，取消保持当时进度。
2. **WebSocket 广播必须带 `progress`**（`queued`/`failed` 广播都带 `"progress": 0`）。
   - 原因：前端 `chat.js::applyChatEvent` 用 `{...existing, ...payload}` 合并；若 payload 缺 `progress`，会**沿用旧值**，进度条不回退。
3. **取消绝不进入失败重试**：`except TaskCancelled` 分支置 `cancelled` 后直接 return。
4. **`enqueue` 时必须清空 `started_at`**：`t.started_at = None`。
   - 原因：前端 `renderHead` 用 `started_at` 起「已用时」表；不清空会用上一次的开始时间，表现为计时不从 0 开始。

### `status` 取值

`draft`（新建未生成）/ `queued` / `running` / `succeeded` / `failed` / `cancelled`。合法集合由 `schemas.ChatStatus` 收敛，**DB 层无 CHECK**。

---

## 5. 指数退避重试

`_backoff_delay(attempts) = min(retry_backoff * 2^(attempts-1), MAX_BACKOFF=60.0)`。

- 失败时：若 `attempts <= max_retries` **且未被取消** → 写 `status=queued`、`progress=0`、`stage="第 N 次失败，Xs 后重试"`、`error=str(exc)[:1000]`，广播，然后 `asyncio.create_task(self._requeue_later(chat_id, delay))`。
- `_requeue_later`：`await asyncio.sleep(delay)` 后，**未被取消**才重新 `put`。
- 否则：`status=failed`、`progress=0`、`finished_at=now`，广播。
- `max_retries` 每个 chat 可不同（`Chat.max_retries`，默认 2；`/api/tasks` 兼容层可传入）。

---

## 6. 取消的实现

`cancel(chat_id)`：

1. `self._cancelled.add(chat_id)`（**先记住**，供 `is_cancelled` 回调与 pipeline 检查点用）；
2. 若 `status in ("queued","running")` → 置 `cancelled`、`stage="已取消"`、`finished_at`；
3. 广播 `chat_updated {status:"cancelled"}`。

`enqueue()` 开头会 `self._cancelled.discard(chat_id)`——重新排队 = 清除取消标记。

pipeline 侧通过注入的 `should_cancel=lambda: self.is_cancelled(chat_id)` 在每个阶段检查点感知取消（见 [pipeline.md §4](./pipeline.md#4-取消机制为什么用-should_cancel-回调)）。

---

## 7. 与 WebSocket 事件的关系

事件定义在 `events.py`（`EventHub`），**事件名由旧 `task_*` 改名而来，但 WebSocket 路径仍是 `/ws/tasks`**（`main.py`）。

| 事件名 | 常量 | 何时广播 | payload |
|---|---|---|---|
| `chat_message` | `EVENT_CHAT_MESSAGE` | 新增对话消息（如用户贴 JD） | `{chat_id, message}` |
| `chat_progress` | `EVENT_CHAT_PROGRESS` | 生成进度推进 | `{chat_id, progress, stage, …}` |
| `chat_updated` | `EVENT_CHAT_UPDATED` | chat 状态变化 | `{chat_id, status, progress, stage, …}` |

- `EventHub.broadcast`：加锁取客户端快照 → 逐个 `send_text` → 收集死连接并移除。
- 调度器在各处主动广播（enqueue / 抢占成功 / 重试 / 失败 / 取消）；pipeline 通过 `_emit` 广播 `chat_progress`。
- 前端收敛点：`chat.js::applyChatEvent`（`app.js` 的 `connectEvents` 只做转发 + 成功后 `refreshResumes`）。

---

## 8. 常见陷阱

1. **改 `queued`/`failed` 忘记归零 `progress`**：会破坏不变式，前端进度条卡在旧值。已在 5 处显式处理，新增状态迁移时务必同步。
2. **广播 payload 漏 `progress`**：前端合并会沿用旧值，同样导致进度条不回退。
3. **`_restart_workers` 顺序不能颠倒**：必须**先复位再取消**，否则 running 任务永久卡住。
4. **`_running` 的维护**：`_handle` 抢占成功后才 `add`，`_worker` 的 `finally` 里 `discard`；`_restart_workers` 重排后也 `discard`。漏掉会导致误判。
5. **`_recover()` 必须在拉起 worker 之前调用**（在 `start()` 内）：否则刚入队的任务可能先于恢复被消费，或恢复逻辑与运行冲突。

---

**相关文档**：[系统总览](./overview.md) · [生成管线](./pipeline.md) · [数据模型](./data-model.md) · [API 参考](../03-api/reference.md)
