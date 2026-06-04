from fastapi import FastAPI, Depends, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import logging
from contextlib import asynccontextmanager
import os
import logging
import uvicorn
from dotenv import load_dotenv
from collections import deque
import threading

load_dotenv(override=True)


class RingBufferHandler(logging.Handler):
    """In-memory ring buffer log handler — captures last N lines for /dev/logs."""

    def __init__(self, capacity: int = 1000):
        super().__init__()
        self.capacity = capacity
        self.buffer = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        with self._lock:
            self.buffer.append(msg)

    def get_all(self) -> list[str]:
        with self._lock:
            return list(self.buffer)


LOG_BUFFER = RingBufferHandler(1000)
LOG_BUFFER.setFormatter(logging.Formatter(
    "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
))

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.getLogger().addHandler(LOG_BUFFER)

from core.secret_filter import install_secret_filter
install_secret_filter()

from core.database import init_db
from core.telemetry import init_telemetry
from scheduler import SchedulerEngine
from web.hermes_chat import ensure_hermes_config, sync_claude_settings

scheduler = SchedulerEngine()

async def _recover_orphan_sessions():
    """Mark any 'running' sessions as failed on startup — their subprocesses died with the old container."""
    import aiosqlite
    from core.database import DB_PATH

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM agent_sessions WHERE status='running'")
        count = (await cursor.fetchone())[0]
        if count:
            await db.execute(
                "UPDATE agent_sessions SET status='failed', error_message='orphan:restart', "
                "completed_at=datetime('now','localtime') WHERE status='running'"
            )
            await db.commit()
            logging.getLogger("kh.startup").warning(
                "Recovered %d orphan sessions from previous run", count
            )


async def _recover_orphan_tasks():
    """Mark any 'running' chat_tasks as failed — delegated to ChatTaskManager (Layer 3)."""
    from core.chat_task_manager import chat_task_manager
    await chat_task_manager.recover_orphans()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_telemetry()

    # Startup: sync API config from env to hermes and claude settings
    await ensure_hermes_config()
    sync_claude_settings()

    await init_db()
    await _recover_orphan_sessions()
    await _recover_orphan_tasks()
    await scheduler.start()
    yield
    await scheduler.stop()

app = FastAPI(
    title="Kanban Harness",
    description="AI Team Orchestration Engine — Dashboard API",
    version="0.6.0",
    lifespan=lifespan,
)

from web.middleware import PermissionGateway, RequestLogger
app.add_middleware(PermissionGateway)
app.add_middleware(RequestLogger)

from web.api import router as api_router
from web.board_events import router as board_events_router
app.include_router(api_router, prefix="/api")
app.include_router(board_events_router, prefix="/api")

static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def index():
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"status": "running", "version": "0.1.0"}


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8765"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("main:app", host=host, port=port, reload=True, access_log=False)
