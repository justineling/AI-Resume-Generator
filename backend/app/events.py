"""WebSocket 事件广播：聊天工作区状态 / 进度实时推送。

事件语义（由旧 task_* 改名而来，payload 携带 chat_id）：
- ``chat_message``  ：新增一条对话消息（如用户贴 JD）。
- ``chat_progress`` ：生成进度推进（progress + stage）。
- ``chat_updated``  ：chat 状态变化（queued/running/succeeded/failed/cancelled）。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket

# 事件名常量（与前端 chat.js 的分发逻辑保持一致）
EVENT_CHAT_MESSAGE = "chat_message"
EVENT_CHAT_PROGRESS = "chat_progress"
EVENT_CHAT_UPDATED = "chat_updated"


class EventHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, event: str, payload: dict[str, Any]) -> None:
        message = json.dumps({"event": event, "payload": payload}, ensure_ascii=False)
        async with self._lock:
            targets = list(self._clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(message)
            except Exception:  # pragma: no cover
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


hub = EventHub()
