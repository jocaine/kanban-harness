"""Agent Session lifecycle management — timeout, retry, blocked states."""

import asyncio
import logging
from datetime import datetime, timedelta

import aiosqlite

from core.database import get_db, DB_PATH

logger = logging.getLogger(__name__)

MAX_RETRIES = 1
DEFAULT_TIMEOUT = 600  # 10 minutes


class SessionManager:
    def __init__(self):
        self._timeout_task: asyncio.Task | None = None

    async def start_timeout_checker(self, interval: int = 30):
        self._timeout_task = asyncio.create_task(self._timeout_loop(interval))

    async def stop(self):
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass

    async def create_session(
        self,
        project_id: int,
        agent_role: str,
        trigger_type: str = "",
        input_context: str = "",
        timeout_seconds: int = DEFAULT_TIMEOUT,
        parent_session_id: int | None = None,
        retry_count: int = 0,
    ) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "INSERT INTO agent_sessions "
                "(project_id, agent_role, status, trigger_type, input_context, "
                "timeout_seconds, parent_session_id, retry_count, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                (project_id, agent_role, "running", trigger_type, input_context,
                 timeout_seconds, parent_session_id, retry_count),
            )
            await db.commit()
            session_id = cursor.lastrowid
            logger.info(f"Session {session_id} created: {agent_role} for project {project_id}")
            return session_id

    async def complete_session(self, session_id: int, output_summary: str = "") -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE agent_sessions SET status='completed', output_summary=?, "
                "completed_at=datetime('now','localtime') WHERE id=?",
                (output_summary, session_id),
            )
            await db.commit()
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            result = dict(await row.fetchone())
            logger.info(f"Session {session_id} completed")
            return result

    async def fail_session(self, session_id: int, error: str = "") -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            session = dict(await row.fetchone())

            if session["retry_count"] < MAX_RETRIES:
                await db.execute(
                    "UPDATE agent_sessions SET status='failed', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (error, session_id),
                )
                await db.commit()
                logger.info(f"Session {session_id} failed, scheduling retry")
                retry_id = await self.create_session(
                    project_id=session["project_id"],
                    agent_role=session["agent_role"],
                    trigger_type=f"retry:{session_id}",
                    input_context=session["input_context"],
                    timeout_seconds=session["timeout_seconds"],
                    parent_session_id=session_id,
                    retry_count=session["retry_count"] + 1,
                )
                return {"status": "retrying", "new_session_id": retry_id}
            else:
                await db.execute(
                    "UPDATE agent_sessions SET status='blocked', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (error, session_id),
                )
                await db.commit()
                logger.warning(f"Session {session_id} blocked after {MAX_RETRIES} retries")
                row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
                return dict(await row.fetchone())

    async def get_session(self, session_id: int) -> dict | None:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            result = await row.fetchone()
            return dict(result) if result else None

    async def get_running_sessions(self, project_id: int | None = None) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            if project_id:
                cursor = await db.execute(
                    "SELECT * FROM agent_sessions WHERE status='running' AND project_id=?",
                    (project_id,),
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM agent_sessions WHERE status='running'"
                )
            return [dict(row) for row in await cursor.fetchall()]

    async def check_timeouts(self):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_sessions WHERE status='running' AND started_at IS NOT NULL"
            )
            sessions = [dict(row) for row in await cursor.fetchall()]

        now = datetime.now()
        for session in sessions:
            started = datetime.strptime(session["started_at"], "%Y-%m-%d %H:%M:%S")
            timeout = timedelta(seconds=session["timeout_seconds"])
            if now - started > timeout:
                logger.warning(f"Session {session['id']} timed out")
                await self.fail_session(session["id"], error="timeout")

    async def _timeout_loop(self, interval: int):
        while True:
            try:
                await self.check_timeouts()
            except Exception as e:
                logger.error(f"Timeout checker error: {e}")
            await asyncio.sleep(interval)
