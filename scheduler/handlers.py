"""Result handlers — process agent outputs, route cards, emit events."""

import json
import logging
import re
from datetime import date, datetime

import aiosqlite

from core.database import DB_PATH
from core.workflow_config import workflow_config
from agents.registry import registry

logger = logging.getLogger("kh.sched.handlers")


async def handle_coach_dev_result(session_manager, session_id: int, card: dict, repo_path: str):
    """Execute coach_dev agent and handle its result."""
    try:
        from agents.coach_dev import CoachDev

        heartbeat_cb = lambda: session_manager.heartbeat(session_id)
        agent = CoachDev(repo_path=repo_path, project_id=card["project_id"], on_heartbeat=heartbeat_cb)
        result = await agent.execute(card)
        session_manager.unregister_process(session_id)

        if result.get("task_done", result.get("success")):
            await session_manager.complete_session(session_id, result.get("summary", ""))
            is_scaffold = result.get("is_scaffold", False)
            commit_hash = result.get("commit", "")
            commit_msg = result.get("commit_message", "")
            branch = result.get("branch", "")
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                if is_scaffold:
                    logger.info(f"[{card['code']}] scaffold complete, staying in dev for implementation round")
                else:
                    await db.execute(
                        "UPDATE requirements SET status='testing', assignee='Coach-Review', "
                        "updated_at=datetime('now','localtime'), progressed_at=datetime('now','localtime') WHERE id=?",
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
                    await db.execute(
                        "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                        (card["project_id"], "status_changed", card["id"],
                         json.dumps({"old_status": "dev", "new_status": "testing"})),
                    )
                    logger.info(f"[{card['code']}] moved to testing, commit {commit_hash[:8]} linked")
                await db.commit()
        else:
            logger.info(f"[{card['code']}] no commits produced, scheduling continuation")
            await session_manager.continuation_retry(session_id)

    except Exception as e:
        logger.error("[FAULT:AGENT] coach_dev failed for [%s]: %s", card['code'], e)
        session_manager.unregister_process(session_id)
        await session_manager.fail_session(session_id, str(e))


async def handle_comment_agent_result(session_manager, session_id: int, role_name: str, card: dict, project_id: int = 0):
    """Execute a comment agent and post its output.

    Workflow principle: 评论后必移动，移动后 emit event 触发下一个角色。
    """
    try:
        from agents.comment_agent import CommentAgent

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM comments WHERE requirement_id=? ORDER BY created_at",
                (card["id"],),
            )
            comments = [dict(row) for row in await cursor.fetchall()]

        research_rounds = sum(1 for c in comments if c.get("author") == "行业顾问")

        heartbeat_cb = lambda: session_manager.heartbeat(session_id)

        agent = CommentAgent(role_name, project_id=project_id)
        logger.info("[SCHED] running comment_agent '%s' for [%s] (status=%s, research_rounds=%d)",
                    role_name, card.get("code", ""), card.get("status", ""), research_rounds)
        result = await agent.execute(card, comments, on_heartbeat=heartbeat_cb)
        session_manager.unregister_process(session_id)
        logger.info("[SCHED] comment_agent '%s' result: success=%s, has_comment=%s",
                    role_name, result.get("success"), bool(result.get("comment")))

        if role_name == "pm":
            await handle_pm_tool_mode(session_manager, session_id, card, project_id, comments)
            return

        if result["success"] and result["comment"]:
            role_config = registry.get(role_name)
            author = role_config.display_name if role_config else role_name
            comment_text = result["comment"]

            old_status = card.get("status", "")
            req_type = card.get("type", "dev")
            if role_name == "pm" and old_status == "organizing":
                new_status = parse_pm_research_decision(comment_text, research_rounds, req_type)
            elif role_name == "industry" and old_status == "research":
                new_status = parse_industry_decision(comment_text)
            else:
                new_status = next_status_for_role(role_name, old_status)

            _log_move_decision(role_name, card, old_status, new_status, comment_text)

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
                        "UPDATE requirements SET status=?, assignee=?, "
                        "updated_at=datetime('now','localtime'), progressed_at=datetime('now','localtime') WHERE id=?",
                        (new_status, col_assignee, card["id"]),
                    )
                    logger.info("[STATUS-CHANGE] card=[%s] status %s → %s by %s",
                                card.get("code", ""), old_status, new_status, role_name)

                    if new_status == "organizing":
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
                        await db.execute(
                            "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                            (project_id, "status_changed", card["id"],
                             json.dumps({"old_status": old_status, "new_status": new_status, "moved_by": role_name})),
                        )
                        logger.info("[EVENT-EMIT] status_changed card=[%s] %s→%s moved_by=%s",
                                    card.get("code", ""), old_status, new_status, role_name)

                if role_name == "industry" and "[需要补充]" in comment_text:
                    ceo_dec = json.dumps({
                        "role": "industry",
                        "reason": "agent_d",
                        "message": "行业顾问需要 CEO 补充信息",
                        "actions": ["reply_to_role", "approve_dev", "request_more_research"],
                        "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    }, ensure_ascii=False)
                    await db.execute(
                        "UPDATE requirements SET assignee='', queue_reason='等待 CEO 补充信息', "
                        "ceo_decision=?, updated_at=datetime('now','localtime') WHERE id=?",
                        (ceo_dec, card["id"]),
                    )
                    logger.info("[QUEUE] card=[%s] queued in research (assignee cleared), waiting for CEO reply",
                                card.get("code", ""))

                await db.commit()

                if role_name == "pm" and new_status == "done" and req_type == "research":
                    parsed = parse_pm_research_conclusion(comment_text)
                    if parsed:
                        await append_research_to_memory(project_id, card.get("code", ""), parsed)

        await session_manager.complete_session(session_id, result.get("summary", ""))
    except Exception as e:
        logger.error("[FAULT:AGENT] comment_agent '%s' failed: %s", role_name, e)
        session_manager.unregister_process(session_id)
        await session_manager.fail_session(session_id, str(e))


async def handle_pm_tool_mode(session_manager, session_id: int, card: dict, project_id: int, pre_comments: list[dict]):
    """Handle PM agent result by checking DB state (PM writes via MCP tools directly)."""
    req_id = card["id"]
    old_status = card.get("status", "")
    req_type = card.get("type", "dev")
    pre_comment_count = len([c for c in pre_comments if c.get("author") == "产品经理"])

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT content FROM comments WHERE requirement_id=? AND author='产品经理' "
            "ORDER BY created_at DESC LIMIT 1",
            (req_id,),
        )
        latest_pm = await cursor.fetchone()
        cursor = await db.execute(
            "SELECT status FROM requirements WHERE id=?", (req_id,),
        )
        current_card = await cursor.fetchone()
        current_status = current_card["status"] if current_card else old_status

    new_comment_count = 0
    if latest_pm:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM comments WHERE requirement_id=? AND author='产品经理'",
                (req_id,),
            )
            row = await cursor.fetchone()
            new_comment_count = row[0] if row else 0

    has_new_comment = new_comment_count > pre_comment_count
    status_changed = current_status != old_status

    if has_new_comment or status_changed:
        logger.info(
            "[PM-TOOL-MODE] success for [%s]: new_comment=%s, status %s→%s",
            card.get("code", ""), has_new_comment, old_status, current_status,
        )
        if req_type == "research" and current_status == "done" and latest_pm:
            parsed = parse_pm_research_conclusion(latest_pm["content"])
            if parsed:
                await append_research_to_memory(project_id, card.get("code", ""), parsed)
                logger.info("[PRODUCT-MEMORY] appended for [%s]", card.get("code", ""))
        await session_manager.complete_session(session_id, f"PM completed [{card.get('code', '')}]")
    else:
        logger.warning(
            "[PM-TOOL-MODE] FAILED for [%s]: no new comment, no status change. "
            "Card stays in %s — will escalate via ceo_decision after threshold.",
            card.get("code", ""), old_status,
        )
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
                (req_id, "系统",
                 "⚠️ PM agent 执行未完成（未检测到新评论或状态变更）。等待自动重试或 CEO 介入。"),
            )
            await db.commit()
        await session_manager.fail_session(session_id, "PM produced no comment and no status change")


def parse_pm_research_decision(comment: str, research_rounds: int, req_type: str = "dev") -> str:
    """Parse PM's evaluation of research completeness."""
    ready_target = "done" if req_type == "research" else "dev"

    if research_rounds >= workflow_config.max_research_rounds:
        logger.warning("[SCHED] research loop hit max %d rounds, forcing to %s",
                       workflow_config.max_research_rounds, ready_target)
        return ready_target

    if "[需要补充]" in comment or "[NEED_MORE]" in comment:
        logger.info("[SCHED-DECISION] PM → [需要补充] → sending back to research")
        return "research"
    if "[调研充分]" in comment or "[READY]" in comment:
        logger.info("[SCHED-DECISION] PM → [调研充分] → %s", ready_target)
        return ready_target

    if any(kw in comment for kw in ("移回调研", "退回调研", "补充调研", "继续调研", "需要进一步")):
        logger.info("[SCHED-DECISION] PM → heuristic 'need more research'")
        return "research"
    if any(kw in comment for kw in ("推进开发", "进入开发", "可以开发", "调研完成", "材料充分")):
        logger.info("[SCHED-DECISION] PM → heuristic 'ready' → %s", ready_target)
        return ready_target

    if research_rounds == 0:
        logger.info("[SCHED-DECISION] PM created card with no decision signal → defaulting to research")
        return "research"

    logger.info("[SCHED-DECISION] PM comment has no clear decision signal → staying in organizing")
    return ""


def parse_pm_research_conclusion(comment: str) -> dict | None:
    """Extract structured research conclusions from PM's evaluation comment."""
    if "[调研充分]" not in comment:
        return None

    reliability = ""
    conclusions = []
    archive_target = ""
    in_conclusions = False

    for line in comment.split("\n"):
        stripped = line.strip().strip("*")

        for sep in ("：", ":"):
            if stripped.startswith(f"可靠性{sep}"):
                reliability = stripped.split(sep, 1)[1].strip().strip("*")
                in_conclusions = False
                break

        for sep in ("：", ":"):
            if stripped.startswith(f"归档建议{sep}"):
                archive_target = stripped.split(sep, 1)[1].strip().strip("*")
                in_conclusions = False
                break

        if stripped in ("提炼结论：", "提炼结论:", "提炼结论：**", "提炼结论:**"):
            in_conclusions = True
            continue

        if in_conclusions and stripped.startswith("- "):
            conclusions.append(stripped[2:].strip())

    if not conclusions:
        return None

    return {
        "reliability": reliability,
        "conclusions": conclusions,
        "archive_target": archive_target,
    }


def parse_industry_decision(comment: str) -> str:
    """Parse Industry's decision after reading CEO reply or completing research."""
    if "[转给PM]" in comment:
        logger.info("[SCHED-DECISION] industry → [转给PM] → moving to organizing")
        return "organizing"
    if "[需要补充]" in comment:
        logger.info("[SCHED-DECISION] industry → [需要补充] → staying in research")
        return "research"
    logger.info("[SCHED-DECISION] industry → no decision marker → staying in research")
    return "research"


def next_status_for_role(role_name: str, current_status: str) -> str:
    """Determine what status a role should move the card to after commenting."""
    if role_name == "pm" and current_status == "organizing":
        return ""
    if role_name == "pm" and current_status in ("", "research"):
        return "research"
    if role_name == "industry" and current_status == "research":
        return "organizing"
    if role_name == "coach_dev" and current_status == "dev":
        return "organizing"
    if role_name == "coach_review" and current_status == "testing":
        return ""
    return ""


async def append_research_to_memory(project_id: int, card_code: str, parsed: dict) -> None:
    """Append PM's research conclusions to project product memory."""
    entry = f"- **{card_code}** ({date.today().isoformat()}):\n"
    if parsed.get("reliability"):
        entry += f"  - 可靠性: {parsed['reliability']}\n"
    entry += "  - 结论:\n"
    for point in parsed["conclusions"]:
        entry += f"    - {point}\n"
    if parsed.get("archive_target"):
        entry += f"  - 归档建议: {parsed['archive_target']}\n"

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


def _log_move_decision(role_name: str, card: dict, old_status: str, new_status: str, comment_text: str):
    """Log the move decision for debugging."""
    code = card.get("code", "")
    if new_status and new_status != old_status:
        logger.info("[MOVE] role=%s card=[%s] %s → %s | signals=[转给PM]=%s [需要补充]=%s [调研充分]=%s",
                    role_name, code, old_status, new_status,
                    "[转给PM]" in comment_text, "[需要补充]" in comment_text, "[调研充分]" in comment_text)
    elif role_name == "pm" and old_status == "organizing" and new_status == "":
        logger.info("[MOVE] role=%s card=[%s] %s → (stay) | PM evaluated → awaiting CEO via Reigns",
                    role_name, code, old_status)
    elif role_name == "industry" and old_status == "research" and new_status == "research":
        if "[需要补充]" in comment_text:
            logger.info("[CEO-ASK] role=%s card=[%s] | industry marked [需要补充] → CEO decides via Reigns",
                        role_name, code)
        else:
            logger.info("[MOVE] role=%s card=[%s] %s → (stay) | industry working",
                        role_name, code, old_status)
    else:
        logger.info("[MOVE] role=%s card=[%s] %s → %s",
                    role_name, code, old_status, new_status or "(stay)")
