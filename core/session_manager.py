"""Agent Session lifecycle management — timeout, retry, blocked states, stall detection."""

import asyncio
import logging
import time
from datetime import datetime

import aiosqlite

from core.database import DB_PATH
from core.card_logger import card_log

logger = logging.getLogger("kh.core.session")

MAX_RETRIES = 2
DEFAULT_TIMEOUT = 600  # 10 minutes
DEFAULT_STALL_TIMEOUT = 120  # 2 minutes without output → stalled


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
        requirement_id: int | None = None,
    ) -> int:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "INSERT INTO agent_sessions "
                "(project_id, agent_role, status, trigger_type, input_context, "
                "timeout_seconds, parent_session_id, retry_count, requirement_id, started_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,datetime('now','localtime'))",
                (project_id, agent_role, "running", trigger_type, input_context,
                 timeout_seconds, parent_session_id, retry_count, requirement_id),
            )
            await db.commit()
            session_id = cursor.lastrowid
            logger.info(f"会话 {session_id} 已创建: {agent_role}, 项目 {project_id}")
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
            logger.info(f"会话 {session_id} 已完成")
            if result.get("requirement_id"):
                await card_log(result["requirement_id"], f"会话 {session_id} 已完成", source="session")
            return result

    async def fail_session(self, session_id: int, error: str = "") -> dict:
        """Mark session as failed or blocked. No auto-retry — tick loop handles recovery."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            row = await db.execute("SELECT * FROM agent_sessions WHERE id=?", (session_id,))
            session = dict(await row.fetchone())

            if session["retry_count"] >= MAX_RETRIES:
                await db.execute(
                    "UPDATE agent_sessions SET status='blocked', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (error, session_id),
                )
                await db.commit()
                logger.warning("[FAULT:AGENT] 会话 %d 重试 %d 次后被阻塞", session_id, MAX_RETRIES)
                if session.get("requirement_id"):
                    await card_log(session["requirement_id"], f"会话 {session_id} 重试 {MAX_RETRIES} 次后被阻塞: {error}", level="error", source="session")
            else:
                await db.execute(
                    "UPDATE agent_sessions SET status='failed', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (error, session_id),
                )
                await db.commit()
                logger.info("会话 %d 失败: %s (由 tick 循环负责恢复)", session_id, error)

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
                logger.info("启动时恢复了 %d 个残留会话", len(rows))

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
                    "[RECONCILE] 会话 %d: 进程已消失 (rc=%s), agent=%s",
                    sid, proc.returncode, session["agent_role"],
                )
                await self._kill_and_fail(sid, f"process_gone:rc={proc.returncode}")
                continue

            # 2. Orphaned - no process, no heartbeat (scheduler restarted)
            if proc is None and sid not in self._heartbeats:
                logger.warning(
                    "[RECONCILE] 会话 %d: 已孤立, agent=%s",
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
                    "[RECONCILE] 会话 %d: 预算耗尽 (%d秒 > %d秒), agent=%s",
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
                        "[RECONCILE] 会话 %d: 停滞 (%d秒无输出), agent=%s",
                        sid, int(silent), session["agent_role"],
                    )
                    await self._kill_and_fail(sid, f"stall:{int(silent)}s")

    async def cancel_session(self, session_id: int, reason: str = "reconciliation") -> dict:
        """Cancel a running session — kill process, mark cancelled. No retry."""
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            try:
                proc.kill()
                logger.info("取消会话 %d: 已终止进程 (pid=%d)", session_id, proc.pid)
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
            logger.info("会话 %d 已取消: %s", session_id, reason)
            return dict(result) if result else {}

    async def _kill_and_fail(self, session_id: int, error: str):
        """Kill the tracked process (if any) and fail the session."""
        proc = self._processes.get(session_id)
        if proc and proc.returncode is None:
            try:
                proc.kill()
                logger.info("已终止会话 %d 的进程 (pid=%d)", session_id, proc.pid)
            except ProcessLookupError:
                pass
        self.unregister_process(session_id)
        await self.fail_session(session_id, error=error)

    async def _reconcile_loop(self, interval: int):
        while True:
            try:
                await self.reconcile_sessions()
            except Exception as e:
                logger.error("[FAULT:OBSERVE] 会话对账出错: %s", e)
            await asyncio.sleep(interval)
