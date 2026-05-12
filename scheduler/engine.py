"""Scheduler engine — polls kanban for dev cards and triggers AI agents."""

import asyncio
import logging
from datetime import datetime

import aiosqlite

from core.database import DB_PATH
from core.config import get_project_repo_path
from core.session_manager import SessionManager

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


class SchedulerEngine:
    def __init__(self):
        self.session_manager = SessionManager()
        self.paused = False
        self.running = False
        self._task: asyncio.Task | None = None
        self._started_at: datetime | None = None
        self._tick_count = 0

    @property
    def status(self) -> dict:
        return {
            "mode": "paused" if self.paused else ("running" if self.running else "stopped"),
            "running_tasks": 0,
            "autopilot_level": 2 if not self.paused else 0,
            "started_at": self._started_at.isoformat() if self._started_at else None,
            "tick_count": self._tick_count,
            "poll_interval": POLL_INTERVAL,
        }

    async def start(self):
        if self.running:
            return
        self.running = True
        self._started_at = datetime.now()
        await self.session_manager.start_timeout_checker()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Scheduler started")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.session_manager.stop()
        logger.info("Scheduler stopped")

    def pause(self):
        self.paused = True
        logger.info("Scheduler paused")

    def resume(self):
        self.paused = False
        logger.info("Scheduler resumed")

    async def _poll_loop(self):
        while self.running:
            try:
                if not self.paused:
                    await self._tick()
                    self._tick_count += 1
            except Exception as e:
                logger.error(f"Scheduler tick error: {e}")
            await asyncio.sleep(POLL_INTERVAL)

    async def _tick(self):
        cards = await self._find_actionable_cards()
        if not cards:
            return

        for card in cards:
            has_running = await self._has_running_session(card["id"])
            if has_running:
                continue
            logger.info(f"Triggering Coach-Dev for [{card['code']}] {card['title']}")
            await self._trigger_coach_dev(card)

    async def _find_actionable_cards(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*, v.project_id, p.git_remote_url FROM requirements r "
                "JOIN versions v ON r.version_id = v.id "
                "JOIN projects p ON v.project_id = p.id "
                "WHERE r.status = 'dev' AND r.archived = 0 "
                "ORDER BY r.priority, r.position"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _has_running_session(self, requirement_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT 1 FROM agent_sessions "
                "WHERE status = 'running' AND input_context LIKE ?",
                (f'%"requirement_id": {requirement_id}%',),
            )
            return await cursor.fetchone() is not None

    async def _trigger_coach_dev(self, card: dict):
        from agents.coach_dev import CoachDev

        repo_path = await get_project_repo_path(
            card["project_id"], card.get("git_remote_url", "")
        )

        input_context = (
            f'{{"requirement_id": {card["id"]}, "code": "{card["code"]}", '
            f'"title": "{card["title"]}"}}'
        )
        session_id = await self.session_manager.create_session(
            project_id=card["project_id"],
            agent_role="coach_dev",
            trigger_type="scheduler:dev_card",
            input_context=input_context,
        )

        asyncio.create_task(self._run_agent(session_id, card, repo_path))

    async def _run_agent(self, session_id: int, card: dict, repo_path: str):
        try:
            from agents.coach_dev import CoachDev
            agent = CoachDev(repo_path=repo_path)
            result = await agent.execute(card)
            await self.session_manager.complete_session(session_id, result.get("summary", ""))

            if result.get("success"):
                commit_hash = result.get("commit", "")
                commit_msg = result.get("commit_message", "")
                branch = result.get("branch", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    await db.execute(
                        "UPDATE requirements SET status='testing', "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (card["id"],),
                    )
                    if commit_hash:
                        await db.execute(
                            "INSERT OR IGNORE INTO requirement_commits "
                            "(requirement_id, commit_hash, message, committed_at) "
                            "VALUES (?, ?, ?, datetime('now','localtime'))",
                            (card["id"], commit_hash, commit_msg),
                        )
                    comment = (
                        f"**Coach-Dev** 已完成开发\n\n"
                        f"- 分支: `{branch}`\n"
                        f"- Commit: `{commit_hash[:8]}`\n"
                        f"- 说明: {commit_msg}"
                    )
                    await db.execute(
                        "INSERT INTO comments (requirement_id, author, content) VALUES (?, ?, ?)",
                        (card["id"], "Coach-Dev", comment),
                    )
                    await db.commit()
                logger.info(f"[{card['code']}] moved to testing, commit {commit_hash[:8]} linked")
        except Exception as e:
            logger.error(f"Agent execution failed for [{card['code']}]: {e}")
            await self.session_manager.fail_session(session_id, str(e))
