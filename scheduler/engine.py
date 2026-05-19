"""Scheduler engine — polls kanban for dev cards and triggers AI agents."""

import asyncio
import json
import logging
import os
from datetime import datetime

import aiosqlite

from core.database import DB_PATH
from core.config import get_project_repo_path
from core.session_manager import SessionManager
from agents.registry import registry

logger = logging.getLogger("kh.sched.engine")

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
        await self._recover_stuck_cards()

    async def _find_actionable_cards(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*, v.project_id, p.git_remote_url FROM requirements r "
                "JOIN versions v ON r.version_id = v.id "
                "JOIN projects p ON v.project_id = p.id "
                "WHERE r.status = 'dev' AND r.type = 'dev' AND r.archived = 0 "
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
        """Check if the project repo is ready for dev work.

        Ready means either:
        - Repo has real code (more than init commit), OR
        - Repo has architecture doc (init flow completed, scaffold can proceed), OR
        - Local workspace exists (init'd below for projects without remote repo)
        """
        # If there's a remote URL, assume the repo is ready (will be cloned)
        if git_remote_url:
            return True

        workspace = os.getenv("KH_WORKSPACE", os.path.expanduser("~/.kh/workspaces"))
        repo_path = os.path.join(workspace, f"project_{project_id}")
        os.makedirs(repo_path, exist_ok=True)

        git_dir = os.path.join(repo_path, ".git")
        if not os.path.isdir(git_dir):
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "init", "-b", "main",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            logger.info("[SCHED] init'd local workspace for project_%d at %s (no remote repo)", project_id, repo_path)

        # Check if repo has an initial commit; create one if empty
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "rev-list", "--count", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        commit_count = int(stdout.decode().strip() or "0")
        if commit_count == 0:
            # Empty repo — make an initial commit for worktree support
            for cfg in [("user.name", "Coach-Dev"), ("user.email", "coach-dev@kanban-harness")]:
                await asyncio.create_subprocess_exec(
                    "git", "-C", repo_path, "config", *cfg,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "commit", "--allow-empty", "-m", "init",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            logger.info("[SCHED] made initial commit for project_%d", project_id)

        # Ensure default branch is named 'main' (not 'master') — needed for coach_dev worktree/check logic
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "branch", "--list", "master",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if stdout.decode().strip():
            await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "branch", "-m", "master", "main",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            logger.info("[SCHED] renamed master→main for project_%d", project_id)

        logger.info("[SCHED] project_%d: local workspace ready for coach_dev", project_id)
        return True

    async def _has_architecture(self, project_id: int) -> bool:
        """Check if project has an architecture document."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM project_architecture WHERE project_id=? AND content != ''",
                (project_id,),
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

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE requirements SET assignee='Coach-Dev' WHERE id=?",
                (card["id"],),
            )
            await db.commit()

        asyncio.create_task(self._run_agent(session_id, card, repo_path))

    async def _run_agent(self, session_id: int, card: dict, repo_path: str):
        try:
            from agents.coach_dev import CoachDev
            agent = CoachDev(repo_path=repo_path, project_id=card["project_id"])
            result = await agent.execute(card)
            await self.session_manager.complete_session(session_id, result.get("summary", ""))

            if result.get("success"):
                is_scaffold = result.get("is_scaffold", False)
                commit_hash = result.get("commit", "")
                commit_msg = result.get("commit_message", "")
                branch = result.get("branch", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    db.row_factory = aiosqlite.Row
                    if is_scaffold:
                        # Scaffold: stay in dev so coach_dev triggers again for real implementation
                        logger.info(f"[{card['code']}] scaffold complete, staying in dev for implementation round")
                    else:
                        await db.execute(
                            "UPDATE requirements SET status='testing', assignee='Coach-Review', "
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
                    scaffold_label = "（脚手架）" if is_scaffold else ""
                    comment = (
                        f"**Coach-Dev** 已完成开发{scaffold_label}\n\n"
                        f"- 分支: `{branch}`\n"
                        f"- Commit: `{commit_hash[:8]}`\n"
                        f"- 说明: {commit_msg}"
                    )
                    await db.execute(
                        "INSERT INTO comments (requirement_id, author, content) VALUES (?, ?, ?)",
                        (card["id"], "Coach-Dev", comment),
                    )
                    if not is_scaffold:
                        # Emit status_changed event to trigger QA review
                        await db.execute(
                            "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                            (card["project_id"], "status_changed", card["id"],
                             json.dumps({"old_status": "dev", "new_status": "testing"})),
                        )
                        logger.info(f"[{card['code']}] moved to testing, commit {commit_hash[:8]} linked")
                    await db.commit()
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
                logger.info("[SCHED] processing event #%d: type=%s, req=%s, context=%s",
                            event["id"], event["event_type"], event.get("requirement_id"), context)
                roles = registry.roles_for_trigger(event["event_type"], context)
                logger.info("[SCHED] matched roles for event #%d: %s", event["id"], roles or "(none)")

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

            # 设 assignee 匹配前端 COL_ROLE_MAP，卡片显示为"活跃"而非"排队中"
            AGENT_COLUMN_ROLE = {
                "industry": "Industry",
                "pm": "PM",
                "coach_dev": "Coach-Dev",
                "coach_review": "Coach-Review",
            }
            col_role = AGENT_COLUMN_ROLE.get(role_name, role_name)
            await db.execute(
                "UPDATE requirements SET assignee=?, updated_at=datetime('now','localtime') WHERE id=?",
                (col_role, requirement_id),
            )
            await db.commit()

        input_context = json.dumps({"requirement_id": requirement_id, "code": card.get("code", "")})
        session_id = await self.session_manager.create_session(
            project_id=event["project_id"],
            agent_role=role_name,
            trigger_type=f"event:{event['event_type']}",
            input_context=input_context,
        )

        asyncio.create_task(self._run_comment_agent(session_id, role_name, card, event["project_id"]))

    async def _run_comment_agent(self, session_id: int, role_name: str, card: dict, project_id: int = 0):
        """Execute a comment agent and post its output.

        Workflow principle: 评论后必移动，移动后 emit event 触发下一个角色。
        Research loop: industry→organizing→PM evaluates→back to research or forward to dev (max 10 rounds).
        """
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

            # Count research rounds (how many times industry has commented)
            research_rounds = sum(
                1 for c in comments if c.get("author") == "行业顾问"
            )

            agent = CommentAgent(role_name, project_id=project_id)
            logger.info("[SCHED] running comment_agent '%s' for [%s] (status=%s, research_rounds=%d)",
                        role_name, card.get("code", ""), card.get("status", ""), research_rounds)
            result = await agent.execute(card, comments)
            logger.info("[SCHED] comment_agent '%s' result: success=%s, has_comment=%s",
                        role_name, result.get("success"), bool(result.get("comment")))

            if result["success"] and result["comment"]:
                role_config = registry.get(role_name)
                author = role_config.display_name if role_config else role_name
                comment_text = result["comment"]

                # Determine move: PM evaluating organizing research card parses decision
                old_status = card.get("status", "")
                req_type = card.get("type", "dev")
                if role_name == "pm" and old_status == "organizing":
                    new_status = self._parse_pm_research_decision(
                        comment_text, research_rounds, req_type
                    )
                elif role_name == "industry" and old_status == "research":
                    # Industry markers determine next step
                    new_status = self._parse_industry_decision(comment_text)
                else:
                    new_status = self._next_status_for_role(role_name, old_status)

                # === LOG: every role's decision & move ===
                if new_status and new_status != old_status:
                    logger.info("[MOVE] role=%s card=[%s] %s → %s | comment_has_signals=[转给PM]=%s [需要补充]=%s [调研充分]=%s",
                                role_name, card.get("code", ""), old_status, new_status,
                                "[转给PM]" in comment_text, "[需要补充]" in comment_text, "[调研充分]" in comment_text)
                elif role_name == "pm" and old_status == "organizing" and new_status == "":
                    logger.info("[MOVE] role=%s card=[%s] %s → %s | PM evaluated research card → staying in organizing, awaiting CEO approval via Reigns",
                                role_name, card.get("code", ""), old_status, new_status or "(stay)")
                elif role_name == "industry" and old_status == "research" and new_status == "research":
                    if "[需要补充]" in comment_text:
                        logger.info("[CEO-ASK] role=%s card=[%s] | industry marked [需要补充] → CEO must decide via Reigns panel",
                                    role_name, card.get("code", ""))
                    else:
                        logger.info("[MOVE] role=%s card=[%s] %s → %s | industry working, no status change",
                                    role_name, card.get("code", ""), old_status, new_status or "(stay)")
                else:
                    logger.info("[MOVE] role=%s card=[%s] %s → %s",
                                role_name, card.get("code", ""), old_status, new_status or "(stay)")

                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO comments (requirement_id, author, content, detail) VALUES (?,?,?,?)",
                        (card["id"], author, comment_text, result.get("detail", "")),
                    )

                    if new_status and new_status != old_status:
                        COL_ASSIGNEE = {
                            "research": "Industry",
                            "organizing": "PM",
                            "dev": "Coach-Dev",
                            "testing": "Coach-Review",
                        }
                        col_assignee = COL_ASSIGNEE.get(new_status, "")
                        await db.execute(
                            "UPDATE requirements SET status=?, assignee=?, updated_at=datetime('now','localtime') WHERE id=?",
                            (new_status, col_assignee, card["id"]),
                        )
                        logger.info("[STATUS-CHANGE] card=[%s] status %s → %s by %s",
                                    card.get("code", ""), old_status, new_status, role_name)

                        if new_status == "organizing":
                            # 默认静默等 CEO。但 [转给PM] 标记触发 PM
                            if role_name == "industry" and "[转给PM]" in comment_text:
                                await db.execute(
                                    "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                                    (project_id, "status_changed", card["id"],
                                     json.dumps({"old_status": old_status, "new_status": "organizing", "moved_by": "industry"})),
                                )
                                logger.info("[EVENT-EMIT] status_changed card=[%s] %s→organizing moved_by=industry → triggers PM",
                                            card.get("code", ""), old_status)
                            else:
                                logger.info("[EVENT-EMIT] card=[%s] moved to organizing by %s → PM will pick up for evaluation",
                                            card.get("code", ""), role_name)
                        else:
                            # Emit status_changed event to trigger next role in chain
                            await db.execute(
                                "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                                (project_id, "status_changed", card["id"],
                                 json.dumps({"old_status": old_status, "new_status": new_status, "moved_by": role_name})),
                            )
                            logger.info("[EVENT-EMIT] status_changed card=[%s] %s→%s moved_by=%s",
                                        card.get("code", ""), old_status, new_status, role_name)

                    # Industry [需要补充]: 不移动列，但在当前列标记为排队中等 CEO 回复
                    if role_name == "industry" and "[需要补充]" in comment_text:
                        await db.execute(
                            "UPDATE requirements SET assignee='', queue_reason='等待 CEO 补充信息', updated_at=datetime('now','localtime') WHERE id=?",
                            (card["id"],),
                        )
                        logger.info("[QUEUE] card=[%s] queued in research (assignee cleared), waiting for CEO reply",
                                    card.get("code", ""))

                    await db.commit()

                    # === PM research conclusion → product memory ===
                    if role_name == "pm" and new_status == "done" and req_type == "research":
                        parsed = self._parse_pm_research_conclusion(comment_text)
                        if parsed:
                            await self._append_research_to_memory(
                                project_id,
                                card.get("code", ""),
                                parsed,
                            )

            await self.session_manager.complete_session(session_id, result.get("summary", ""))
        except Exception as e:
            logger.error(f"Comment agent {role_name} failed: {e}")
            await self.session_manager.fail_session(session_id, str(e))

    def _parse_pm_research_decision(self, comment: str, research_rounds: int, req_type: str = "dev") -> str:
        """Parse PM's evaluation of research completeness.

        Normal flow: PM evaluates → [调研充分] → done (for research) or dev (for dev cards).
        CEO is only involved when PM flags [需要补充] (needs more info) or when role disagreement arises.

        Returns: 'research' (need more), 'done'/'dev' (ready), or '' (no move).
        """
        MAX_RESEARCH_ROUNDS = 10

        ready_target = "done" if req_type == "research" else "dev"

        if research_rounds >= MAX_RESEARCH_ROUNDS:
            logger.warning("[SCHED] research loop hit max %d rounds, forcing to %s", MAX_RESEARCH_ROUNDS, ready_target)
            return ready_target

        # Parse PM's decision signal from comment
        if "[需要补充]" in comment or "[NEED_MORE]" in comment:
            logger.info("[SCHED-DECISION] PM → [需要补充] → sending back to research (CEO may need to arbitrate)")
            return "research"
        if "[调研充分]" in comment or "[READY]" in comment:
            logger.info("[SCHED-DECISION] PM → [调研充分] → %s (normal flow, no CEO needed)", ready_target)
            return ready_target

        # Fallback heuristic
        if any(kw in comment for kw in ("移回调研", "退回调研", "补充调研", "继续调研", "需要进一步")):
            logger.info("[SCHED-DECISION] PM → heuristic 'need more research' → sending back to research")
            return "research"
        if any(kw in comment for kw in ("推进开发", "进入开发", "可以开发", "调研完成", "材料充分")):
            logger.info("[SCHED-DECISION] PM → heuristic 'ready' → %s", ready_target)
            return ready_target

        # No clear signal
        if research_rounds == 0:
            logger.info("[SCHED-DECISION] PM created card with no decision signal → defaulting to research")
            return "research"

        logger.info("[SCHED-DECISION] PM comment has no clear decision signal → staying in organizing")
        return ""

    def _parse_pm_research_conclusion(self, comment: str) -> dict | None:
        """Extract structured research conclusions from PM's evaluation comment.

        Expected format (from pm.yaml lines 110-123):
            [调研充分]
            可靠性：<source reliability assessment>
            提炼结论：
            - <point 1>
            - <point 2>
            归档建议：建议写入<area>

        Returns dict with keys: reliability (str), conclusions (list), archive_target (str).
        Returns None if comment lacks [调研充分] or has no parseable conclusions.
        """
        if "[调研充分]" not in comment:
            return None

        reliability = ""
        conclusions = []
        archive_target = ""
        in_conclusions = False

        for line in comment.split("\n"):
            stripped = line.strip().strip("*")  # strip bold markdown

            # Check for 可靠性 (reliability)
            for sep in ("：", ":"):
                if stripped.startswith(f"可靠性{sep}"):
                    reliability = stripped.split(sep, 1)[1].strip().strip("*")
                    in_conclusions = False
                    break

            # Check for 归档建议 (archive suggestion)
            for sep in ("：", ":"):
                if stripped.startswith(f"归档建议{sep}"):
                    archive_target = stripped.split(sep, 1)[1].strip().strip("*")
                    in_conclusions = False
                    break

            # Toggle conclusions section
            if stripped in ("提炼结论：", "提炼结论:", "提炼结论：**", "提炼结论:**"):
                in_conclusions = True
                continue

            # Collect bullet points in conclusions section
            if in_conclusions and stripped.startswith("- "):
                conclusions.append(stripped[2:].strip())

        if not conclusions:
            return None

        return {
            "reliability": reliability,
            "conclusions": conclusions,
            "archive_target": archive_target,
        }

    async def _append_research_to_memory(self, project_id: int, card_code: str, parsed: dict) -> None:
        """Append PM's research conclusions to project product memory.

        Creates or appends to a '### 调研结论' subsection. Each entry is a bullet
        with card code, date, reliability assessment, and conclusion items.
        """
        from datetime import date

        entry = f"- **{card_code}** ({date.today().isoformat()}):\n"
        if parsed.get("reliability"):
            entry += f"  - 可靠性: {parsed['reliability']}\n"
        entry += "  - 结论:\n"
        for point in parsed["conclusions"]:
            entry += f"    - {point}\n"
        if parsed.get("archive_target"):
            entry += f"  - 归档建议: {parsed['archive_target']}\n"

        import re

        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT product_memory FROM projects WHERE id=?", (project_id,)
            )
            row = await cursor.fetchone()
            current = row[0] if row else ""

            section_pattern = re.compile(
                r"(### 调研结论.*?)(?=\n### |\n## |\Z)", re.DOTALL,
            )
            match = section_pattern.search(current)
            if match:
                updated_section = match.group(1).rstrip() + f"\n{entry}"
                updated = current[:match.start()] + updated_section + current[match.end():]
            else:
                updated = current.rstrip() + f"\n\n### 调研结论\n\n{entry}\n"

            await db.execute(
                "UPDATE projects SET product_memory=?, updated_at=datetime('now','localtime') WHERE id=?",
                (updated, project_id),
            )
            await db.commit()

        logger.info("[PRODUCT-MEMORY] research conclusions appended for card=[%s] project=%d",
                    card_code, project_id)

    def _parse_industry_decision(self, comment: str) -> str:
        """Parse Industry's decision after reading CEO reply or completing research.

        Returns:
        - 'organizing' if [转给PM] (forward to PM for evaluation)
        - 'research' if [需要补充] (stay in research, CEO decides via Reigns panel)
        - 'research' if no marker (continue working in research)
        """
        if "[转给PM]" in comment:
            logger.info("[SCHED-DECISION] industry → [转给PM] → moving to organizing (PM will evaluate)")
            return "organizing"
        if "[需要补充]" in comment:
            logger.info("[SCHED-DECISION] industry → [需要补充] → staying in research (CEO decides via Reigns)")
            return "research"
        logger.info("[SCHED-DECISION] industry → no decision marker → staying in research")
        return "research"

    def _next_status_for_role(self, role_name: str, current_status: str, card: dict | None = None) -> str:
        """Determine what status a role should move the card to after commenting.

        评论后必移动原则：各角色完成工作后移入下一列的排队中状态等对应角色处理。
        - Industry (research) → organizing（转 PM 评估，由 [转给PM] 标记触发）
        - Coach-Dev (dev) → no comment-agent path, handled via _run_agent → testing
        - Coach-QA (testing) → no comment-agent path, handled via CEO reigns
        - PM 在 organizing 时不移动（由 CEO 通过王权面板决策）
        - PM 创建调研卡 → research
        """
        if role_name == "pm" and current_status == "organizing":
            return ""
        if role_name == "pm" and current_status in ("", "research"):
            return "research"
        if role_name == "industry" and current_status == "research":
            return "organizing"
        if role_name == "coach_dev" and current_status == "dev":
            return "organizing"
        if role_name == "coach_review" and current_status == "testing":
            return ""  # Stay in testing, CEO decides via reigns panel
        return ""

    # ==================== Stuck card recovery ====================

    STUCK_ROLE_MAP = {
        "research": "industry",
        "organizing": "pm",
        "testing": "coach_review",
    }

    STUCK_COOLDOWN_SECONDS = 30  # same as POLL_INTERVAL

    async def _find_stuck_cards(self) -> list[dict]:
        """Find cards in research/organizing/testing with no running session and stale updated_at."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*, v.project_id FROM requirements r "
                "JOIN versions v ON r.version_id = v.id "
                "WHERE r.status IN ('research', 'organizing', 'testing') "
                "AND r.archived = 0 "
                "AND r.updated_at < datetime('now', 'localtime', ?) "
                "ORDER BY r.priority, r.position",
                (f"-{self.STUCK_COOLDOWN_SECONDS} seconds",),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _recover_stuck_cards(self):
        """Fallback polling: pick up stuck cards that events failed to drive forward."""
        stuck = await self._find_stuck_cards()
        if not stuck:
            return

        for card in stuck:
            role_name = self.STUCK_ROLE_MAP.get(card["status"])
            if not role_name:
                continue
            if await self._has_running_session(card["id"]):
                continue

            logger.info("[SCHED-RECOVER] stuck card [%s] in '%s' → triggering %s",
                        card.get("code", ""), card["status"], role_name)

            event = {
                "id": 0,
                "project_id": card["project_id"],
                "event_type": "recovery",
                "requirement_id": card["id"],
                "context": json.dumps({"old_status": card["status"], "new_status": card["status"], "moved_by": "recovery"}),
            }
            context = {"old_status": card["status"], "new_status": card["status"], "moved_by": "recovery"}
            await self._trigger_comment_agent(role_name, event, context)

