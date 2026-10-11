"""FastAPI 应用入口。

- 启动时建表、拉起调度器；关闭时优雅停止。
- 挂载 REST 路由、WebSocket 事件流、前端静态资源。
"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from .config import settings
from .db import get_session, init_db
from .events import hub
from .llm import get_runtime_config
from .routers import ai_edit, chats, profile, resumes, settings as settings_router, tasks
from .scheduler import scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("resume")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    await scheduler.start()
    # 实际监听端口以启动器注入的 APP_PORT 为准（uvicorn 不会把绑定端口回传到这里），
    # 否则会打印配置里的默认端口，与实际不符。
    port = os.environ.get("APP_PORT") or settings.port
    log.info("AI Resume Generator 就绪 -> http://%s:%s", settings.host, port)
    yield
    await scheduler.shutdown()


app = FastAPI(title="AI Resume Generator", version="1.0.0", lifespan=lifespan)

app.include_router(profile.router)
app.include_router(chats.router)
app.include_router(tasks.router)
app.include_router(resumes.router)
app.include_router(ai_edit.router)
app.include_router(settings_router.router)


@app.get("/api/health")
def health(session: Session = Depends(get_session)) -> dict:
    # 读取运行时配置（DB 覆盖 .env），与「设置」页保持一致，而不是只看 .env
    cfg = get_runtime_config(session)
    return {"ok": True, "llm_enabled": bool((cfg.get("api_key") or "").strip())}


@app.websocket("/ws/tasks")
async def ws_tasks(ws: WebSocket) -> None:
    await hub.connect(ws)
    try:
        while True:
            # 客户端心跳 / 无需处理的消息
            await ws.receive_text()
    except WebSocketDisconnect:
        await hub.disconnect(ws)
    except Exception:  # pragma: no cover
        await hub.disconnect(ws)


# 前端静态资源（放在所有 API 之后，避免抢占路由）
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(settings.static_dir / "index.html"))
