"""Scheduler engine — polls kanban for dev cards and triggers AI agents."""

import asyncio
import json
import logging
from datetime import datetime

import aiosqlite

from core.database import DB_PATH
from core.config import get_project_repo_path
from core.session_manager import SessionManager
from agents.registry import registry

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
        events = await self._peek_pending_events()
        if cards or events:
            logger.info("[SCHED] tick #%d: %d dev cards, %d pending events", self._tick_count, len(cards), len(events))

        if cards:
            for card in cards:
                has_running = await self._has_running_session(card["id"])
                if has_running:
                    continue
                if not await self._repo_is_ready(card["project_id"], card.get("git_remote_url", "")):
                    continue
                logger.info("[SCHED] → trigger coach_dev for [%s] %s", card["code"], card["title"])
                await self._trigger_coach_dev(card)

        await self._process_events()

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

    async def _repo_is_ready(self, project_id: int, git_remote_url: str) -> bool:
        """Check if the project repo has real code (not just an empty init commit)."""
        repo_path = os.path.join(
            os.getenv("KH_WORKSPACE", os.path.expanduser("~/.kh/workspaces")),
            f"project_{project_id}",
        )
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            if not git_remote_url:
                logger.info("[SCHED] skip project_%d: no repo and no remote URL (needs init flow)", project_id)
                return False
            return True

        # Check if repo has more than just the init commit
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "rev-list", "--count", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        commit_count = int(stdout.decode().strip() or "0")
        if commit_count <= 1:
            logger.info("[SCHED] skip project_%d: repo is empty (%d commits, needs init flow)", project_id, commit_count)
            return False
        return True

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
            agent = CoachDev(repo_path=repo_path, project_id=card["project_id"])
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
                    # Emit status_changed event
                    await db.execute(
                        "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                        (card["project_id"], "status_changed", card["id"],
                         json.dumps({"old_status": "dev", "new_status": "testing"})),
                    )
                    await db.commit()
                logger.info(f"[{card['code']}] moved to testing, commit {commit_hash[:8]} linked")
        except Exception as e:
            logger.error(f"Agent execution failed for [{card['code']}]: {e}")
            await self.session_manager.fail_session(session_id, str(e))

    # ==================== Event-driven comment agents ====================

    async def _peek_pending_events(self) -> list[dict]:
        """Quick count of pending events without marking them processed."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_events WHERE processed=0 ORDER BY created_at LIMIT 10"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _process_events(self):
        """Check for unprocessed events and trigger comment agents."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_events WHERE processed=0 ORDER BY created_at LIMIT 10"
            )
            events = [dict(row) for row in await cursor.fetchall()]

        for event in events:
            try:
                context = json.loads(event.get("context", "{}"))
                roles = registry.roles_for_trigger(event["event_type"], context)

                for role_name in roles:
                    if role_name == "coach_dev":
                        continue  # coach_dev handled via worktree flow above
                    await self._trigger_comment_agent(role_name, event, context)

            except Exception as e:
                logger.error(f"Event processing failed for event {event['id']}: {e}")

            # Mark processed
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE agent_events SET processed=1 WHERE id=?", (event["id"],)
                )
                await db.commit()

    async def _trigger_comment_agent(self, role_name: str, event: dict, context: dict):
        """Spawn a CommentAgent for the given role and event."""
        requirement_id = event.get("requirement_id")
        if not requirement_id:
            return

        logger.info("[SCHED] → trigger comment_agent '%s' for req=%d, event=%s", role_name, requirement_id, event["event_type"])

        # Load card data
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,))
            card_row = await cursor.fetchone()
            if not card_row:
                return
            card = dict(card_row)

        input_context = json.dumps({"requirement_id": requirement_id, "code": card.get("code", "")})
        session_id = await self.session_manager.create_session(
            project_id=event["project_id"],
            agent_role=role_name,
            trigger_type=f"event:{event['event_type']}",
            input_context=input_context,
        )

        asyncio.create_task(self._run_comment_agent(session_id, role_name, card, event["project_id"]))

    async def _run_comment_agent(self, session_id: int, role_name: str, card: dict, project_id: int = 0):
        """Execute a comment agent and post its output."""
        try:
            from agents.comment_agent import CommentAgent

            # Fetch existing comments for context
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT * FROM comments WHERE requirement_id=? ORDER BY created_at",
                    (card["id"],),
                )
                comments = [dict(row) for row in await cursor.fetchall()]

            agent = CommentAgent(role_name, project_id=project_id)
            result = await agent.execute(card, comments)

            if result["success"] and result["comment"]:
                role_config = registry.get(role_name)
                author = role_config.display_name if role_config else role_name
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
                        (card["id"], author, result["comment"]),
                    )
                    await db.commit()

            await self.session_manager.complete_session(session_id, result.get("summary", ""))
        except Exception as e:
            logger.error(f"Comment agent {role_name} failed: {e}")
            await self.session_manager.fail_session(session_id, str(e))

