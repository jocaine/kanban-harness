"""Result handlers — process agent outputs, route cards, emit events."""

import json
import logging
import re
from datetime import date, datetime

import aiosqlite

from core.database import DB_PATH
from core.card_logger import card_log
from core.workflow_config import workflow_config
from agents.registry import registry
from web.board_events import broadcast

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
            await session_manager.complete_session(session_id, result.get("summary", ""), tokens=result.get("tokens"))
            is_scaffold = result.get("is_scaffold", False)
            commit_hash = result.get("commit", "")
            commit_msg = result.get("commit_message", "")
            branch = result.get("branch", "")
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                if is_scaffold:
                    logger.info(f"[{card['code']}] 脚手架已完成, 留在 dev 进行实现轮次")
                    await card_log(card["id"], "coach_dev 完成(脚手架), 留在 dev", source="coach_dev")
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
                    logger.info(f"[{card['code']}] 已移至 testing, 关联 commit {commit_hash[:8]}")
                    await card_log(card["id"], "coach_dev 完成, 已移至 testing", source="coach_dev")
                await db.commit()
                if not is_scaffold:
                    broadcast("card_moved", {"id": card["id"], "old_status": "dev", "new_status": "testing"})
        else:
            logger.info(f"[{card['code']}] 未产生 commit, 调度后续执行")
            await card_log(card["id"], "coach_dev 未产生 commit, 调度后续执行", level="warning", source="coach_dev")
            await session_manager.continuation_retry(session_id)

    except Exception as e:
        logger.error("[FAULT:AGENT] coach_dev 失败 [%s]: %s", card['code'], e)
        await card_log(card["id"], f"coach_dev 失败: {e}", level="error", source="coach_dev")
        session_manager.unregister_process(session_id)
        await session_manager.fail_session(session_id, str(e))

async def handle_comment_agent_result(session_manager, session_id: int, role_name: str, card: dict, project_id: int = 0):
    """Execute a comment agent and validate its decision via DB state.

    With atomic decision tools, agents call a single tool (e.g. complete,
    reject, ask_ceo) that combines comment + state transition. The harness
    just validates the final invariant: did the card move or get escalated?
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

        heartbeat_cb = lambda: session_manager.heartbeat(session_id)
        register_cb = lambda proc: session_manager.register_process(session_id, proc)

        agent = CommentAgent(role_name, project_id=project_id)
        logger.info("[SCHED] 运行 comment_agent '%s': [%s] (状态=%s)",
                    role_name, card.get("code", ""), card.get("status", ""))
        result = await agent.execute(card, comments, on_heartbeat=heartbeat_cb, on_process_started=register_cb)
        session_manager.unregister_process(session_id)

        # Fallback: if industry agent completed but card didn't move,
        # directly call complete on its behalf
        if role_name == "industry":
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute("SELECT status FROM requirements WHERE id=?", (card["id"],))
                row = await cursor.fetchone()
            if row and row["status"] == "research":
                comment_text = result.get("comment") or "调研完成（agent 未显式提交，由 harness 代为提交）"
                detail_text = result.get("detail", "")
                logger.info("[DECISION-FALLBACK] industry 未调用工具, 自动提交 [%s]", card.get("code"))
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute(
                        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
                        (card["id"], "行业顾问", comment_text),
                    )
                    if detail_text:
                        await db.execute(
                            "INSERT INTO comment_details (requirement_id, author, content) VALUES (?,?,?)",
                            (card["id"], "行业顾问", detail_text),
                        )
                    await db.execute(
                        "UPDATE requirements SET status='organizing', progressed_at=datetime('now','localtime'), "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (card["id"],),
                    )
                    await db.commit()

        await _validate_agent_decision(session_manager, session_id, card, role_name, project_id, tokens=result.get("tokens"))

    except Exception as e:
        logger.error("[FAULT:AGENT] comment_agent '%s' 失败: %s", role_name, e)
        await card_log(card["id"], f"comment_agent '{role_name}' 失败: {e}", level="error", source=role_name)
        session_manager.unregister_process(session_id)
        await session_manager.fail_session(session_id, str(e))


async def _validate_agent_decision(session_manager, session_id: int, card: dict, role_name: str, project_id: int, tokens: dict | None = None):
    """Unified post-agent validation: did the agent make a decision?

    Checks the DB for either a status change or a ceo_decision being set.
    If neither happened, the agent failed to decide - escalate immediately.
    """
    req_id = card["id"]
    old_status = card.get("status", "")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT status, ceo_decision, type FROM requirements WHERE id=?",
            (req_id,),
        )
        current = await cursor.fetchone()

    if not current:
        logger.warning("[DECISION-SKIP] 卡片 %d 在 agent 运行期间被删除, 取消会话", req_id)
        await session_manager.cancel_session(session_id, f"card_deleted:{req_id}")
        return

    current_status = current["status"]
    has_ceo_decision = bool(current["ceo_decision"])
    status_changed = current_status != old_status
    req_type = current["type"] or "dev"

    if status_changed or has_ceo_decision:
        logger.info("[DECISION-OK] %s 已完成 [%s]: 状态 %s→%s, ceo_escalated=%s",
                    role_name, card.get("code"), old_status, current_status, has_ceo_decision)
        await card_log(req_id, f"{role_name} 决策完成: 状态 {old_status}→{current_status}", source=role_name)

        # Handle research conclusion archival
        if role_name == "pm" and current_status == "done" and req_type == "research":
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT content FROM comments WHERE requirement_id=? AND author='产品经理' "
                    "ORDER BY created_at DESC LIMIT 1",
                    (req_id,),
                )
                latest_pm = await cursor.fetchone()
            if latest_pm:
                parsed = parse_pm_research_conclusion(latest_pm["content"])
                if parsed:
                    await append_research_to_memory(project_id, card.get("code", ""), parsed)
                    logger.info("[PRODUCT-MEMORY] 已追加 [%s]", card.get("code", ""))

            # Trigger wiki archiver (non-blocking)
            import asyncio
            asyncio.create_task(
                _trigger_wiki_archive(project_id, card, req_id)
            )

        await session_manager.complete_session(session_id, f"{role_name} decided [{card.get('code', '')}]", tokens=tokens)
    else:
        # Agent did NOT make a decision - immediate escalation
        logger.warning("[DECISION-FAIL] %s 未做出决策 [%s], 升级给 CEO",
                       role_name, card.get("code"))
        await card_log(req_id, f"{role_name} 未做出决策, 升级给 CEO", level="warning", source=role_name)
        escalation_comment = f"{role_name} 执行完毕但未调用决策工具，系统自动升级给 CEO。"
        ceo_dec = json.dumps({
            "role": role_name,
            "reason": "agent_no_decision",
            "message": f"{role_name} 执行完毕但未做出决策（未调用决策工具）",
            "actions": ["retry", "reply_to_role", "move_to_dev", "archive"],
            "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False)
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
                (req_id, f"[系统] {role_name}", escalation_comment),
            )
            await db.execute(
                "UPDATE requirements SET ceo_decision=?, updated_at=datetime('now','localtime') WHERE id=?",
                (ceo_dec, req_id),
            )
            await db.commit()
        await session_manager.complete_session(session_id, f"{role_name} escalated to CEO (no decision tool called)")


# ==================== Research conclusion extraction ====================


def parse_pm_research_conclusion(comment: str) -> dict | None:
    """Extract structured research conclusions from PM's evaluation comment."""
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
            r"(### 调结论.*?)(?=\n### |\n## |\Z)", re.DOTALL,
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

    logger.info("[PRODUCT-MEMORY] 研究结论已追加: card=[%s] project=%d",
                card_code, project_id)


async def _trigger_wiki_archive(project_id: int, card: dict, req_id: int) -> None:
    """Trigger wiki_archiver agent to extract structured wiki page from research card."""
    try:
        from agents.comment_agent import CommentAgent

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT author, content FROM comments WHERE requirement_id=? ORDER BY created_at",
                (req_id,),
            )
            comments = [dict(row) for row in await cursor.fetchall()]

        if not comments:
            logger.warning("[WIKI-ARCHIVE] [%s] 无评论, 跳过", card.get("code", ""))
            return

        agent = CommentAgent("wiki_archiver", project_id=project_id)
        await agent.execute(card, comments)
        logger.info("[WIKI-ARCHIVE] [%s] 归档完成", card.get("code", ""))

    except Exception as e:
        logger.warning("[WIKI-ARCHIVE] [%s] 归档失败: %s", card.get("code", ""), e)
