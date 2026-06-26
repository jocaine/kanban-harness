"""Result handlers — process agent outputs, route cards, emit events."""

import json
import logging
import os
import re
from datetime import date, datetime

import aiosqlite

from core.database import DB_PATH
from core.card_logger import card_log
from core.workflow_config import workflow_config
from agents.registry import registry
from web.board_events import broadcast

logger = logging.getLogger("kh.sched.handlers")


async def _verify_commit_exists(repo_path: str, commit_hash: str) -> str:
    """Verify commit object exists in repo. Returns hash if valid, empty string if not."""
    import asyncio as _asyncio
    proc = await _asyncio.create_subprocess_exec(
        "git", "-C", repo_path, "cat-file", "-t", commit_hash,
        stdout=_asyncio.subprocess.PIPE,
        stderr=_asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if "commit" in stdout.decode():
        return commit_hash
    logger.error("[VERIFY] commit %s 不存在于 %s", commit_hash[:8], repo_path)
    return ""


async def handle_coach_dev_result(session_manager, session_id: int, card: dict, repo_path: str):
    """Execute coach_dev agent and handle its result."""
    try:
        from agents.coach_dev import CoachDev

        heartbeat_cb = lambda: session_manager.heartbeat(session_id)
        agent = CoachDev(repo_path=repo_path, project_id=card["project_id"], on_heartbeat=heartbeat_cb)
        result = await agent.execute(card)
        session_manager.unregister_process(session_id)

        if result.get("task_done") and result.get("success"):
            await session_manager.complete_session(session_id, result.get("summary", ""), tokens=result.get("tokens"))
            is_scaffold = result.get("is_scaffold", False)
            commit_hash = result.get("commit", "")
            commit_msg = result.get("commit_message", "")
            branch = result.get("branch", "")
            verification = result.get("verification", {})
            files_changed = verification.get("files_changed", 0)

            # Independent commit verification
            if commit_hash:
                commit_hash = await _verify_commit_exists(repo_path, commit_hash)
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
                    f"- Commit: `{commit_hash[:8] if commit_hash else '(无)'}`\n"
                    f"- 说明: {commit_msg}\n"
                    f"- 验证: {files_changed} 个文件变更"
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
                    logger.info(f"[{card['code']}] 已移至 testing, 关联 commit {commit_hash[:8] if commit_hash else 'none'}")
                    await card_log(card["id"], "coach_dev 完成, 已移至 testing", source="coach_dev")
                await db.commit()
                if not is_scaffold:
                    broadcast("card_moved", {"id": card["id"], "old_status": "dev", "new_status": "testing"})

        elif result.get("task_done") and not result.get("success"):
            # Verification failed — agent hallucinated or produced no real output
            reason = result.get("summary", "verification failed")
            logger.warning("[VERIFY-FAIL] [%s] %s", card['code'], reason)
            await card_log(card["id"], f"coach_dev 验证失败: {reason}", level="warning", source="coach_dev")
            await session_manager.fail_session(session_id, reason)

        else:
            logger.info(f"[{card['code']}] 未产生 commit, 调度后续执行")
            await card_log(card["id"], "coach_dev 未产生 commit, 调度后续执行", level="warning", source="coach_dev")
            await session_manager.continuation_retry(session_id)

    except Exception as e:
        logger.error("[FAULT:AGENT] coach_dev 失败 [%s]: %s", card['code'], e)
        await card_log(card["id"], f"coach_dev 失败: {e}", level="error", source="coach_dev")
        session_manager.unregister_process(session_id)
        await session_manager.fail_session(session_id, str(e))


async def handle_coach_review_result(session_manager, session_id: int, card: dict, repo_path: str, project_id: int = 0):
    """Execute CoachReview agent with workspace access and validate decision."""
    try:
        from agents.coach_review import CoachReview

        heartbeat_cb = lambda: session_manager.heartbeat(session_id)
        agent = CoachReview(repo_path=repo_path, project_id=project_id, on_heartbeat=heartbeat_cb)

        branch_name = f"feature/{card.get('code', 'unknown').lower()}"
        result = await agent.execute(card, branch_name=branch_name)
        session_manager.unregister_process(session_id)

        # Validate decision was made (card moved or CEO escalated)
        await _validate_agent_decision(
            session_manager, session_id, card, "coach_review", project_id,
            tokens=result.get("tokens"),
        )

        # If QA approved (card moved to done), merge feature branch into main
        await _merge_if_approved(card, repo_path, branch_name)

    except Exception as e:
        logger.error("[FAULT:AGENT] coach_review 失败 [%s]: %s", card.get('code', ''), e)
        await card_log(card["id"], f"coach_review 失败: {e}", level="error", source="coach_review")
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

        # Handle research conclusion archival → write to wiki
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
                    _archive_research_to_wiki(
                        project_id, card.get("code", ""), parsed,
                        requirement_id=req_id, version_id=card.get("version_id", 0),
                    )
                    logger.info("[WIKI] 调研结论已归档 [%s]", card.get("code", ""))

        await session_manager.complete_session(session_id, f"{role_name} decided [{card.get('code', '')}]", tokens=tokens)
    else:
        # Agent did NOT make a decision - extract its last comment for context
        logger.warning("[DECISION-FAIL] %s 未做出决策 [%s], 升级给 CEO",
                       role_name, card.get("code"))
        await card_log(req_id, f"{role_name} 未做出决策, 升级给 CEO", level="warning", source=role_name)

        # Fetch agent's last comment to use as CEO speech (usually contains useful analysis)
        role_display = {"pm": "产品经理", "industry": "行业顾问", "coach_dev": "Coach-Dev", "coach_review": "Coach-QA"}
        author_name = role_display.get(role_name, role_name)
        agent_message = ""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT content FROM comments WHERE requirement_id=? AND author=? "
                "ORDER BY created_at DESC LIMIT 1",
                (req_id, author_name),
            )
            row = await cursor.fetchone()
            if row:
                agent_message = row["content"]

        if agent_message:
            display_message = agent_message[:600]
        else:
            display_message = f"{role_name} 执行完毕但未做出决策（未调用决策工具）"

        escalation_comment = f"{role_name} 执行完毕但未调用决策工具，系统自动升级给 CEO。"
        ceo_dec = json.dumps({
            "role": role_name,
            "reason": "agent_no_decision",
            "message": display_message,
            "actions": ["reply_to_role", "retry"],
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


async def _merge_if_approved(card: dict, repo_path: str, branch_name: str):
    """Merge feature branch into dev after QA approves (card → done)."""
    import asyncio
    req_id = card["id"]
    code = card.get("code", "")

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT status FROM requirements WHERE id=?", (req_id,))
        row = await cursor.fetchone()
        if not row or row[0] != "done":
            return

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_path, "branch", "--list", branch_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if not stdout.decode().strip():
        logger.debug("[MERGE] %s: branch %s not found, skip", code, branch_name)
        return

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_path, "checkout", "dev",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    proc = await asyncio.create_subprocess_exec(
        "git", "-C", repo_path, "merge", branch_name, "--no-edit",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        logger.info("[MERGE] %s: %s merged into dev", code, branch_name)
        await card_log(req_id, f"QA 通过，{branch_name} 已合入 dev", source="coach_review")
    else:
        logger.error("[MERGE] %s: merge failed: %s", code, stderr.decode()[:200])
        await card_log(req_id, f"合并失败: {stderr.decode()[:100]}", level="error", source="coach_review")


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

        if in_conclusions and (stripped.startswith("- ") or re.match(r'^\d+[\.\)]\s', stripped)):
            item = re.sub(r'^[-\d\.\)]+\s*', '', stripped).strip()
            if item:
                conclusions.append(item)

    if not conclusions:
        return None

    return {
        "reliability": reliability,
        "conclusions": conclusions,
        "archive_target": archive_target,
    }


def _archive_research_to_wiki(project_id: int, card_code: str, parsed: dict,
                              requirement_id: int = 0, version_id: int = 0) -> None:
    """Write PM's research conclusions to wiki as a structured page."""
    from core.wiki import write_wiki_page

    today = date.today().isoformat()
    slug = re.sub(r'[^a-zA-Z0-9_\-]', '', card_code.lower().replace("-", "_"))
    if not slug or slug.startswith("_"):
        slug = "do" + slug

    dashboard_port = os.getenv("DASHBOARD_PORT", "8766")
    card_url = f"http://localhost:{dashboard_port}/#p={project_id}&v={version_id}&r={requirement_id}"

    lines_out = [
        "---",
        "type: research",
        f"updated: {today}",
        f"tags: [research, {card_code}]",
        f"source_card: {card_code}",
        "---",
        "",
        f"# {card_code} 调研结论",
        "",
    ]
    if parsed.get("reliability"):
        lines_out.append(f"**可靠性:** {parsed['reliability']}")
        lines_out.append("")
    lines_out.append("## 核心结论")
    lines_out.append("")
    for point in parsed["conclusions"]:
        lines_out.append(f"- {point}")
    if parsed.get("archive_target"):
        lines_out.append("")
        lines_out.append(f"**归档方向:** {parsed['archive_target']}")
    lines_out.append("")
    lines_out.append("## 来源")
    lines_out.append("")
    lines_out.append(f"- 卡片: [{card_code}]({card_url})")
    lines_out.append(f"- 审核: 产品经理 ({today})")

    content = "\n".join(lines_out) + "\n"
    write_wiki_page(project_id, f"research/{slug}", content, f"归档调研结论 {card_code}")
    logger.info("[WIKI] 调研结论已写入 research/%s, project=%d", slug, project_id)

