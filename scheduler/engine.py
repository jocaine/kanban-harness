"""Scheduler engine — polls kanban for dev cards and triggers AI agents."""

import asyncio
import json
import logging
import os
from datetime import datetime

import aiosqlite

from core.database import DB_PATH
from core.card_logger import card_log
from core.workspace import get_project_repo_path
from core.session_manager import SessionManager, DEFAULT_TIMEOUT
from core.workflow_config import workflow_config
from agents.registry import registry
from scheduler import handlers
from web.board_events import broadcast

logger = logging.getLogger("kh.sched.engine")


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
            "poll_interval": workflow_config.poll_interval,
        }

    async def start(self):
        if self.running:
            return
        self.running = True
        self._started_at = datetime.now()
        await self.session_manager.recover_stale_sessions()
        await self.session_manager.start_timeout_checker()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("调度器已启动")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.session_manager.stop()
        logger.info("调度器已停止")

    def pause(self):
        self.paused = True
        logger.info("调度器已暂停")

    def resume(self):
        self.paused = False
        logger.info("调度器已恢复")

    async def _poll_loop(self):
        while self.running:
            try:
                if not self.paused:
                    await self._tick()
                    self._tick_count += 1
            except Exception as e:
                logger.error("[FAULT:TICK] %s", e)
            await asyncio.sleep(workflow_config.poll_interval)

    async def _tick(self):
        workflow_config.reload_if_changed()
        await self._reconcile_running_sessions()
        await self._reconcile_ceo_decisions()
        await self._reconcile_merged_dev_cards()

        cards = await self._find_actionable_cards()
        events = await self._peek_pending_events()
        if cards or events:
            logger.info("[SCHED] tick #%d: %d 张开发卡, %d 个待处理事件", self._tick_count, len(cards), len(events))

        if cards:
            running_count = len(await self.session_manager.get_running_sessions())
            for card in cards:
                if running_count >= workflow_config.max_concurrent_sessions:
                    logger.info("[SCHED] 并发上限已达 (%d), 延迟剩余卡片", workflow_config.max_concurrent_sessions)
                    break
                has_running = await self._has_running_session(card["id"])
                if has_running:
                    continue
                if not await self._repo_is_ready(card["project_id"], card.get("git_remote_url", "")):
                    continue
                logger.info("[SCHED] → 触发 coach_dev: [%s] %s", card["code"], card["title"])
                await card_log(card["id"], "调度触发 coach_dev", source="sched")
                await self._trigger_coach_dev(card)
                running_count += 1

        await self._process_events()
        await self._recover_stuck_cards()

    # ==================== Reconciliation ====================

    TERMINAL_STATUSES = {"done", "archived"}
    AGENT_EXPECTED_STATUS = {
        "coach_dev": {"dev"},
        "industry": {"research"},
        "pm": {"organizing"},
        "coach_review": {"testing"},
    }

    async def _reconcile_running_sessions(self):
        """Check all running agents — cancel those whose cards no longer need them."""
        running = await self.session_manager.get_running_sessions()
        if not running:
            return

        for session in running:
            req_id = session.get("requirement_id") or self._extract_requirement_id(session.get("input_context", ""))
            if not req_id:
                continue

            card_status = await self._get_card_status(req_id)
            if card_status is None:
                continue

            role = session["agent_role"]
            sid = session["id"]

            if card_status in self.TERMINAL_STATUSES:
                logger.info(
                    "[RECONCILE] 会话 %d (%s): 卡片 %d 状态为 '%s' → 取消",
                    sid, role, req_id, card_status,
                )
                await self.session_manager.cancel_session(sid, f"card_status:{card_status}")
                continue

            expected = self.AGENT_EXPECTED_STATUS.get(role)
            if expected and card_status not in expected:
                logger.info(
                    "[RECONCILE] 会话 %d (%s): 卡片 %d 已移至 '%s' (预期 %s) → 取消",
                    sid, role, req_id, card_status, expected,
                )
                await self.session_manager.cancel_session(sid, f"card_moved:{card_status}")

    def _extract_requirement_id(self, input_context: str) -> int | None:
        if not input_context:
            return None
        try:
            data = json.loads(input_context)
            return data.get("requirement_id")
        except (json.JSONDecodeError, TypeError):
            return None

    async def _get_card_status(self, requirement_id: int) -> str | None:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT status, archived FROM requirements WHERE id=?",
                    (requirement_id,),
                )
                row = await cursor.fetchone()
                if not row:
                    return "archived"
                if row[1]:
                    return "archived"
                return row[0]
        except Exception as e:
            logger.error("[FAULT:DB] 对账查询卡片 %d 失败: %s", requirement_id, e)
            return None

    # ==================== CEO Decision Reconciliation ====================

    ROLE_FOR_STATUS = {
        "research": "industry",
        "organizing": "pm",
        "dev": "coach_dev",
        "testing": "coach_review",
    }

    async def _reconcile_ceo_decisions(self):
        """Unified escalation: detect stuck cards and escalate to CEO."""
        threshold = workflow_config.escalation_threshold

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, code, status, ceo_decision, progressed_at "
                "FROM requirements "
                "WHERE status NOT IN ('done') AND archived = 0"
            )
            cards = [dict(row) for row in await cursor.fetchall()]

        for card in cards:
            has_running = await self._has_running_session(card["id"])

            if card["ceo_decision"]:
                continue

            if has_running:
                continue

            if card["progressed_at"]:
                try:
                    progressed = datetime.strptime(card["progressed_at"], "%Y-%m-%d %H:%M:%S")
                    elapsed = (datetime.now() - progressed).total_seconds()
                except (ValueError, TypeError):
                    continue
                if elapsed < threshold:
                    continue
            else:
                continue

            role = self.ROLE_FOR_STATUS.get(card["status"])
            if role and await self._has_running_session_for_role(role):
                continue

            await self._set_ceo_decision(card, elapsed)
            await card_log(card["id"], "空闲过久, 升级给 CEO", level="warning", source="sched")
            logger.info("[CEO-DECISION] 已升级 [%s] (状态=%s, 空闲 %.0f秒)",
                        card.get("code", ""), card["status"], elapsed)

    async def _has_running_session_for_role(self, role: str) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM agent_sessions WHERE status='running' AND agent_role=?",
                (role,),
            )
            return await cursor.fetchone() is not None

    async def _set_ceo_decision(self, card: dict, elapsed: float = 0):
        role = self.ROLE_FOR_STATUS.get(card["status"], "unknown")
        minutes = int(elapsed // 60) if elapsed else 0
        escalation_comment = (
            f"卡片在 {card['status']} 列停滞 {minutes} 分钟，系统自动升级给 CEO。"
        )
        decision = json.dumps({
            "role": role,
            "reason": "stuck_timeout",
            "message": f"卡片在 {card['status']} 列停滞 {minutes} 分钟，需要 CEO 介入",
            "actions": ["retry", "reply_to_role"],
            "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
                (card["id"], f"[系统] {role}", escalation_comment),
            )
            await db.execute(
                "UPDATE requirements SET ceo_decision=? WHERE id=?",
                (decision, card["id"]),
            )
            await db.commit()

    async def _clear_ceo_decision(self, requirement_id: int):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE requirements SET ceo_decision=NULL WHERE id=?",
                (requirement_id,),
            )
            await db.commit()

    # ==================== Git state reconciliation ====================

    async def _reconcile_merged_dev_cards(self):
        """Detect dev cards whose feature branch is already merged into main."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.id, r.code, r.status, v.project_id, p.git_remote_url "
                "FROM requirements r "
                "JOIN versions v ON r.version_id = v.id "
                "JOIN projects p ON v.project_id = p.id "
                "WHERE r.status = 'dev' AND r.type = 'dev' AND r.archived = 0"
            )
            dev_cards = [dict(row) for row in await cursor.fetchall()]

        for card in dev_cards:
            try:
                if await self._was_rejected_by_qa(card["id"]):
                    continue
                repo_path = await get_project_repo_path(
                    card["project_id"], card.get("git_remote_url", "")
                )
                branch_name = f"feature/{card['code'].lower()}"
                if await self._is_branch_merged(repo_path, branch_name):
                    logger.info(
                        "[RECONCILE] %s: feature branch 已在 main，自动推进 testing",
                        card["code"],
                    )
                    await card_log(
                        card["id"],
                        "代码已在 main（reconciliation 检测），自动推进 testing",
                        source="sched",
                    )
                    async with aiosqlite.connect(DB_PATH) as db:
                        await db.execute(
                            "UPDATE requirements SET status='testing', ceo_decision=NULL, "
                            "progressed_at=? WHERE id=?",
                            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), card["id"]),
                        )
                        await db.commit()
                    broadcast("card_updated", {
                        "id": card["id"],
                        "action": "status_changed",
                        "status": "testing",
                    })
            except Exception as e:
                logger.debug("[RECONCILE] %s: git 检查跳过: %s", card.get("code", ""), e)

    async def _was_rejected_by_qa(self, requirement_id: int) -> bool:
        """Card has been through QA (any state) -- don't auto-advance."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM agent_sessions "
                "WHERE requirement_id=? AND agent_role='coach_review' "
                "LIMIT 1",
                (requirement_id,),
            )
            return await cursor.fetchone() is not None

    async def _is_branch_merged(self, repo_path: str, branch_name: str) -> bool:
        """Check if branch_name is an ancestor of main (i.e. fully merged)."""
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "branch", "--list", branch_name,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if not stdout.decode().strip():
            merge_proc = await asyncio.create_subprocess_exec(
                "git", "-C", repo_path, "log", "main", "--oneline", "-1",
                "--grep", f"Merge branch '{branch_name}'",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await merge_proc.communicate()
            return bool(stdout.decode().strip())

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "merge-base", "--is-ancestor", branch_name, "main",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        return proc.returncode == 0


    # ==================== Card discovery & dispatch ====================

    async def _find_actionable_cards(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*, v.project_id, p.git_remote_url FROM requirements r "
                "JOIN versions v ON r.version_id = v.id "
                "JOIN projects p ON v.project_id = p.id "
                "WHERE r.status = 'dev' AND r.type = 'dev' AND r.archived = 0 "
                "AND r.ceo_decision IS NULL "
                "ORDER BY r.priority, r.position"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _has_running_session(self, requirement_id: int) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT 1 FROM agent_sessions "
                "WHERE status = 'running' AND requirement_id = ?",
                (requirement_id,),
            )
            return await cursor.fetchone() is not None

    async def _repo_is_ready(self, project_id: int, git_remote_url: str) -> bool:
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
            logger.info("[SCHED] 已初始化项目_%d 本地工作区: %s", project_id, repo_path)

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", repo_path, "rev-list", "--count", "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        commit_count = int(stdout.decode().strip() or "0")
        if commit_count == 0:
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
            logger.info("[SCHED] 已为项目_%d 创建初始提交", project_id)

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
            logger.info("[SCHED] 已为项目_%d 重命名 master→main", project_id)

        logger.info("[SCHED] 项目_%d: 本地工作区就绪", project_id)
        return True

    async def _trigger_coach_dev(self, card: dict):
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
            requirement_id=card["id"],
        )

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE requirements SET assignee='Coach-Dev' WHERE id=?",
                (card["id"],),
            )
            await db.commit()

        broadcast("card_updated", {"id": card["id"], "action": "assigned", "assignee": "Coach-Dev"})

        asyncio.create_task(handlers.handle_coach_dev_result(self.session_manager, session_id, card, repo_path))

    async def _trigger_coach_review(self, event: dict, context: dict):
        """Trigger CoachReview with workspace access instead of CommentAgent."""
        requirement_id = event.get("requirement_id")
        if not requirement_id:
            return
        if await self._has_running_session(requirement_id):
            return

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,))
            card_row = await cursor.fetchone()
            if not card_row:
                return
            card = dict(card_row)
            cursor2 = await db.execute(
                "SELECT v.project_id, p.git_remote_url FROM versions v "
                "JOIN projects p ON v.project_id=p.id WHERE v.id=?",
                (card["version_id"],),
            )
            proj_row = await cursor2.fetchone()
            if not proj_row:
                return
            project_id = proj_row["project_id"]
            git_remote_url = proj_row["git_remote_url"] or ""

            await db.execute(
                "UPDATE requirements SET assignee='Coach-QA', updated_at=datetime('now','localtime') WHERE id=?",
                (requirement_id,),
            )
            await db.commit()

        broadcast("card_updated", {"id": requirement_id, "action": "assigned", "assignee": "Coach-QA"})

        repo_path = await get_project_repo_path(project_id, git_remote_url)
        input_context = json.dumps({"requirement_id": requirement_id, "code": card.get("code", "")})
        session_id = await self.session_manager.create_session(
            project_id=project_id,
            agent_role="coach_review",
            trigger_type=f"event:{event['event_type']}",
            input_context=input_context,
            requirement_id=requirement_id,
        )

        asyncio.create_task(
            handlers.handle_coach_review_result(self.session_manager, session_id, card, repo_path, project_id)
        )

    # ==================== Event-driven comment agents ===============

    async def _peek_pending_events(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_events WHERE processed=0 ORDER BY created_at LIMIT 10"
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _process_events(self):
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_events WHERE processed=0 ORDER BY created_at LIMIT 10"
            )
            events = [dict(row) for row in await cursor.fetchall()]

        for event in events:
            try:
                context = json.loads(event.get("context", "{}"))
                logger.info("[SCHED] 处理事件 #%d: type=%s, req=%s, context=%s",
                            event["id"], event["event_type"], event.get("requirement_id"), context)
                roles = registry.roles_for_trigger(event["event_type"], context)
                logger.info("[SCHED] 事件 #%d 匹配的角色: %s", event["id"], roles or "(无)")

                for role_name in roles:
                    if role_name == "coach_dev" and event["event_type"] != "ceo_replied":
                        continue
                    if role_name == "coach_review" and event["event_type"] != "ceo_replied":
                        await self._trigger_coach_review(event, context)
                        continue
                    await self._trigger_comment_agent(role_name, event, context)

            except Exception as e:
                logger.error("[FAULT:DB] 事件 %d 处理失败: %s", event['id'], e)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE agent_events SET processed=1 WHERE id=?", (event["id"],)
                )
                await db.commit()

    async def _trigger_comment_agent(self, role_name: str, event: dict, context: dict):
        requirement_id = event.get("requirement_id")
        if not requirement_id:
            return

        if await self._has_running_session(requirement_id):
            logger.info("[SCHED] 跳过 '%s': req=%d 已有 running session", role_name, requirement_id)
            return

        if role_name == "industry":
            running = await self.session_manager.get_running_sessions()
            industry_running = [s for s in running if s["agent_role"] == "industry"]
            if len(industry_running) >= workflow_config.max_concurrent_industry:
                logger.info("[SCHED] 跳过 industry: 并发上限 %d 已达 (req=%d 排队等待)",
                            workflow_config.max_concurrent_industry, requirement_id)
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "UPDATE requirements SET queue_reason='waiting_turn', "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (requirement_id,),
                    )
                    await db.commit()
                broadcast("card_updated", {"id": requirement_id, "action": "queued"})
                return

        logger.info("[SCHED] → 触发 comment_agent '%s': req=%d, event=%s", role_name, requirement_id, event["event_type"])
        await card_log(requirement_id, f"触发 comment_agent '{role_name}'", source="sched")

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM requirements WHERE id=?", (requirement_id,))
            card_row = await cursor.fetchone()
            if not card_row:
                return
            card = dict(card_row)

            AGENT_COLUMN_ROLE = {
                "industry": "Industry",
                "pm": "PM",
                "coach_dev": "Coach-Dev",
                "coach_review": "Coach-Review",
            }
            col_role = AGENT_COLUMN_ROLE.get(role_name, role_name)
            await db.execute(
                "UPDATE requirements SET assignee=?, queue_reason='', "
                "updated_at=datetime('now','localtime') WHERE id=?",
                (col_role, requirement_id),
            )
            await db.commit()

        broadcast("card_updated", {"id": requirement_id, "action": "assigned", "assignee": col_role})

        input_context = json.dumps({"requirement_id": requirement_id, "code": card.get("code", "")})
        role_cfg = registry.get(role_name)
        if role_cfg and role_cfg.model.provider == "hermes":
            effective_timeout = card.get("agent_timeout") or DEFAULT_TIMEOUT
        else:
            effective_timeout = DEFAULT_TIMEOUT
        session_id = await self.session_manager.create_session(
            project_id=event["project_id"],
            agent_role=role_name,
            trigger_type=f"event:{event['event_type']}",
            input_context=input_context,
            timeout_seconds=effective_timeout,
            requirement_id=requirement_id,
        )

        asyncio.create_task(handlers.handle_comment_agent_result(self.session_manager, session_id, role_name, card, event["project_id"]))

    # ==================== Stuck card recovery ====================

    STUCK_ROLE_MAP = {
        "research": "industry",
        "organizing": "pm",
        "testing": "coach_review",
    }

    async def _find_stuck_cards(self) -> list[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT r.*, v.project_id FROM requirements r "
                "JOIN versions v ON r.version_id = v.id "
                "WHERE r.status IN ('research', 'organizing', 'testing') "
                "AND r.archived = 0 "
                "AND r.ceo_decision IS NULL "
                "AND r.updated_at < datetime('now', 'localtime', ?) "
                "ORDER BY r.priority, r.position",
                (f"-{workflow_config.stuck_cooldown} seconds",),
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def _recover_stuck_cards(self):
        stuck = await self._find_stuck_cards()
        if not stuck:
            return

        for card in stuck:
            role_name = self.STUCK_ROLE_MAP.get(card["status"])
            if not role_name:
                continue
            if await self._has_running_session(card["id"]):
                continue

            logger.info("[SCHED-RECOVER] 卡住的卡片 [%s] 状态='%s' → 触发 %s",
                        card.get("code", ""), card["status"], role_name)
            await card_log(card["id"], f"检测到卡住, 触发恢复: {role_name}", level="warning", source="sched")

            event = {
                "id": 0,
                "project_id": card["project_id"],
                "event_type": "recovery",
                "requirement_id": card["id"],
                "context": json.dumps({"old_status": card["status"], "new_status": card["status"], "moved_by": "recovery"}),
            }
            context = {"old_status": card["status"], "new_status": card["status"], "moved_by": "recovery"}
            await self._trigger_comment_agent(role_name, event, context)
