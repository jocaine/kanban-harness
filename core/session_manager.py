"""Agent Session lifecycle management — timeout, retry, blocked states, stall detection."""

import asyncio
import logging
import time
from datetime import datetime, timedelta

import aiosqlite

from core.database import get_db, DB_PATH

logger = logging.getLogger("kh.core.session")

MAX_RETRIES = 2
DEFAULT_TIMEOUT = 600  # 10 minutes
DEFAULT_STALL_TIMEOUT = 120  # 2 minutes without output → stalled
BACKOFF_BASE_SECONDS = 10  # exponential backoff base for crash retries
BACKOFF_MAX_SECONDS = 300  # cap at 5 minutes


class SessionManager:
    def __init__(self):
        self._timeout_task: asyncio.Task | None = None
        self._heartbeats: dict[int, float] = {}  # session_id → monotonic timestamp
        self._processes: dict[int, asyncio.subprocess.Process] = {}  # session_id → process

    async def start_timeout_checker(self, interval: int = 30):
        self._timeout_task = asyncio.create_task(self._reconcile_loop(interval))

    async def stop(self):
        if self._timeout_task:
            self._timeout_task.cancel()
            try:
                await self._timeout_task
            except asyncio.CancelledError:
                pass
        self._heartbeats.clear()
        self._processes.clear()

    def heartbeat(self, session_id: int):
        """Update heartbeat timestamp — call whenever agent produces output."""
        self._heartbeats[session_id] = time.monotonic()

    def register_process(self, session_id: int, proc: asyncio.subprocess.Process):
        """Track a subprocess so stall detection can kill it."""
        self._processes[session_id] = proc
        self._heartbeats[session_id] = time.monotonic()

    def unregister_process(self, session_id: int):
        """Remove process tracking after session ends."""
        self._processes.pop(session_id, None)
        self._heartbeats.pop(session_id, None)

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

    async def complete_session(self, session_id: int, output_summary: str = "", tokens: dict | None = None) -> dict:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            token_sql = ""
            token_params = []
            if tokens:
                token_sql = ", input_tokens=?, output_tokens=?, total_tokens=?"
                token_params = [
                    tokens.get("input", 0),
                    tokens.get("output", 0),
                    tokens.get("total", tokens.get("input", 0) + tokens.get("output", 0)),
                ]
            await db.execute(
                f"UPDATE agent_sessions SET status='completed', output_summary=?, "
                f"completed_at=datetime('now','localtime'){token_sql} WHERE id=?",
                (output_summary, *token_params, session_id),
            )
            await db.commit()
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            result = dict(await row.fetchone())
            logger.info(f"Session {session_id} completed")
            return result

    async def fail_session(self, session_id: int, error: str = "") -> dict:
        """Fail a session with exponential backoff retry.

        Backoff: 10s → 20s → 40s → ... → 300s max.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            session = dict(await row.fetchone())

            if session["retry_count"] < MAX_RETRIES:
                delay = min(
                    BACKOFF_BASE_SECONDS * (2 ** session["retry_count"]),
                    BACKOFF_MAX_SECONDS,
                )
                await db.execute(
                    "UPDATE agent_sessions SET status='failed', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (error, session_id),
                )
                await db.commit()
                logger.info(
                    "Session %d failed (attempt %d/%d), retry in %ds: %s",
                    session_id, session["retry_count"] + 1, MAX_RETRIES, delay, error,
                )
                retry_id = await self._schedule_retry(session, delay)
                return {"status": "retrying", "new_session_id": retry_id, "retry_delay": delay}
            else:
                await db.execute(
                    "UPDATE agent_sessions SET status='blocked', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (error, session_id),
                )
                await db.commit()
                logger.warning("[FAULT:AGENT] session %d blocked after %d retries", session_id, MAX_RETRIES)
                row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
                return dict(await row.fetchone())

    async def continuation_retry(self, session_id: int) -> dict:
        """Immediate requeue — agent exited normally but work isn't done.

        No backoff, no retry_count increment. Creates a new session immediately.
        """
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            session = dict(await row.fetchone())

            await db.execute(
                "UPDATE agent_sessions SET status='completed', output_summary=?, "
                "completed_at=datetime('now','localtime') WHERE id=?",
                ("continuation:incomplete", session_id),
            )
            await db.commit()

        logger.info("Session %d completed (incomplete work), scheduling continuation", session_id)
        continuation_id = await self.create_session(
            project_id=session["project_id"],
            agent_role=session["agent_role"],
            trigger_type=f"continuation:{session_id}",
            input_context=session["input_context"],
            timeout_seconds=session["timeout_seconds"],
            parent_session_id=session_id,
            retry_count=0,  # continuation resets retry count
        )
        return {"status": "continuation", "new_session_id": continuation_id}

    async def _schedule_retry(self, session: dict, delay: int) -> int:
        """Schedule a retry after delay seconds."""
        async def _delayed_create():
            await asyncio.sleep(delay)
            await self.create_session(
                project_id=session["project_id"],
                agent_role=session["agent_role"],
                trigger_type=f"retry:{session['id']}",
                input_context=session["input_context"],
                timeout_seconds=session["timeout_seconds"],
                parent_session_id=session["id"],
                retry_count=session["retry_count"] + 1,
            )

        asyncio.create_task(_delayed_create())
        # Return a placeholder — the actual session_id is created after delay
        return -1

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

    async def recover_stale_sessions(self):
        """Mark any running sessions without heartbeat as failed on startup."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT id FROM agent_sessions WHERE status='running'"
            )
            rows = await cursor.fetchall()
            if rows:
                for row in rows:
                    await db.execute(
                        "UPDATE agent_sessions SET status='failed', error_message='stale:restart', "
                        "completed_at=datetime('now','localtime') WHERE id=?",
                        (row[0],),
                    )
                await db.commit()
                logger.info("Recovered %d stale sessions on startup", len(rows))

    async def reconcile_sessions(self):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_sessions WHERE status='running' AND started_at IS NOT NULL"
            )
            sessions = [dict(row) for row in await cursor.fetchall()]

        now_dt = datetime.now()
        now_mono = time.monotonic()

        for session in sessions:
            sid = session["id"]
            proc = self._processes.get(sid)

            # 1. Process crashed or was killed externally
            if proc is not None and proc.returncode is not None:
                logger.warning(
                    "[RECONCILE] session %d: process gone (rc=%s), agent=%s",
                    sid, proc.returncode, session["agent_role"],
                )
                await self._kill_and_fail(sid, f"process_gone:rc={proc.returncode}")
                continue

            # 2. Orphaned - no process, no heartbeat (scheduler restarted)
            if proc is None and sid not in self._heartbeats:
                logger.warning(
                    "[RECONCILE] session %d: orphaned, agent=%s",
                    sid, session["agent_role"],
                )
                await self.fail_session(sid, error="orphaned")
                continue

            # 3. Budget exhausted - total elapsed time exceeded
            try:
                started = datetime.strptime(session["started_at"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                continue
            elapsed = (now_dt - started).total_seconds()
            if elapsed > session["timeout_seconds"]:
                logger.warning(
                    "[RECONCILE] session %d: budget exhausted (%ds > %ds), agent=%s",
                    sid, int(elapsed), session["timeout_seconds"], session["agent_role"],
                )
                await self._kill_and_fail(sid, "budget_exhausted")
                continue

            # 4. Stall - has heartbeat but too long since last output
            last_beat = self._heartbeats.get(sid)
            if last_beat is not None:
                silent = now_mono - last_beat
                if silent > DEFAULT_STALL_TIMEOUT:
                    logger.warning(
                        "[RECONCILE] session %d: stall (%ds no output), agent=%s",
                        sid, int(silent), session["agent_role"],
                    )
                    await self._kill_and_fail(sid, f"stall:{int(silent)}s")

    async def cancel_session(self, session_id: int, reason: str = "reconciliation") -> dict:
        """Cancel a running session — kill process, mark cancelled. No retry."""
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            try:
                proc.kill()
                logger.info("Cancelled session %d: killed process (pid=%d)", session_id, proc.pid)
            except ProcessLookupError:
                pass
        self.unregister_process(session_id)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            await db.execute(
                "UPDATE agent_sessions SET status='failed', error_message=?, "
                "completed_at=datetime('now','localtime') WHERE id=? AND status='running'",
                (f"cancelled:{reason}", session_id),
            )
            await db.commit()
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            result = await row.fetchone()
            logger.info("Session %d cancelled: %s", session_id, reason)
            return dict(result) if result else {}

    async def _kill_and_fail(self, session_id: int, error: str):
        """Kill the tracked process (if any) and fail the session."""
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            try:
                proc.kill()
                logger.info("Killed process for session %d (pid=%d)", session_id, proc.pid)
            except ProcessLookupError:
                pass
        self.unregister_process(session_id)
        await self.fail_session(session_id, error=error)

    async def _reconcile_loop(self, interval: int):
        while True:
            try:
                await self.reconcile_sessions()
            except Exception as e:
                logger.error("[FAULT:OBSERVE] reconcile error: %s", e)
            await asyncio.sleep(interval)
