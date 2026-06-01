"""Agent MCP Server — granular kanban tools for AI agent roles (PM/QA/Industry).

Unlike server.py (high-level intent tools over HTTP), this server exposes
per-operation kanban tools directly against SQLite for use by Claude CLI
agent subprocesses.

Environment variables:
  DB_PATH         — absolute path to kanban.db
  KH_AGENT_ROLE   — role name (pm, coach_review, industry) for permission checks
  KH_PROJECT_ID   — project ID for context scoping
"""

import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from mcp.server.fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [AgentMCP] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kh.mcp.agent")

DB_PATH = os.getenv("DB_PATH", "data/kanban.db")
AGENT_ROLE = os.getenv("KH_AGENT_ROLE", "pm")
PROJECT_ID = int(os.getenv("KH_PROJECT_ID", "0"))

mcp = FastMCP("kanban", instructions="Granular kanban tools for AI agent roles.")


async def _get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


def _check_move_permission(from_status: str, to_status: str) -> bool:
    """Check if current agent role can perform this status transition."""
    from agents.registry import registry
    return registry.check_move(AGENT_ROLE, from_status, to_status)


@mcp.tool()
async def get_requirement(requirement_id: int) -> str:
    """读取单个需求卡片的完整信息。

    Args:
        requirement_id: 需求 ID
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM requirements WHERE id=?", (requirement_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return f"错误：需求 {requirement_id} 不存在"
        return json.dumps(dict(row), ensure_ascii=False, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def list_requirements(version_id: int) -> str:
    """列出某个版本下的所有需求卡片。

    Args:
        version_id: 版本 ID
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id, code, title, priority, status, assignee, deadline "
            "FROM requirements WHERE version_id=? AND archived=0 "
            "ORDER BY priority, position",
            (version_id,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        return json.dumps(rows, ensure_ascii=False, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def create_requirements(version_id: int, requirements: str) -> str:
    """批量创建需求卡片。

    Args:
        version_id: 目标版本 ID
        requirements: JSON 数组字符串，每条至少含 title。可选: description, priority, status
    """
    from agents.registry import registry
    if not registry.check_permission(AGENT_ROLE, "create", "requirements"):
        return f"错误：角色 '{AGENT_ROLE}' 无权创建需求卡片"

    try:
        reqs = json.loads(requirements)
    except json.JSONDecodeError as e:
        return f"错误：requirements 不是有效 JSON: {e}"

    if not isinstance(reqs, list) or not reqs:
        return "错误：requirements 必须是非空数组"

    db = await _get_db()
    try:
        from core.database import next_code
        created = []
        for req in reqs:
            title = req.get("title", "").strip()
            if not title:
                continue
            code = await next_code(db, version_id)
            await db.execute(
                "INSERT INTO requirements (version_id, title, description, priority, status, code, position) "
                "VALUES (?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(position),0)+1 FROM requirements WHERE version_id=?))",
                (
                    version_id,
                    title,
                    req.get("description", ""),
                    req.get("priority", "P2"),
                    req.get("status", "organizing"),
                    code,
                    version_id,
                ),
            )
            created.append({"code": code, "title": title})
        await db.commit()
        return json.dumps({"created": created, "count": len(created)}, ensure_ascii=False)
    finally:
        await db.close()


@mcp.tool()
async def update_requirement(
    requirement_id: int,
    title: str = "",
    description: str = "",
    priority: str = "",
    assignee: str = "",
    deadline: str = "",
    notes: str = "",
) -> str:
    """更新需求卡片字段（只传需要修改的字段）。

    Args:
        requirement_id: 需求 ID
        title: 新标题
        description: 新描述（Markdown）
        priority: 新优先级 P0/P1/P2/P3
        assignee: 负责人
        deadline: 截止日期 YYYY-MM-DD
        notes: 完成说明
    """
    updates = {}
    if title:
        updates["title"] = title
    if description:
        updates["description"] = description
    if priority:
        updates["priority"] = priority
    if assignee:
        updates["assignee"] = assignee
    if deadline:
        updates["deadline"] = deadline
    if notes:
        updates["notes"] = notes

    if not updates:
        return "错误：没有提供要更新的字段"

    db = await _get_db()
    try:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        values = list(updates.values()) + [requirement_id]
        await db.execute(
            f"UPDATE requirements SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?",
            values,
        )
        await db.commit()
        return f"已更新需求 {requirement_id}: {', '.join(updates.keys())}"
    finally:
        await db.close()


@mcp.tool()
async def move_requirement(requirement_id: int, status: str) -> str:
    """移动需求卡片到指定状态。会自动校验权限和发射事件触发下游角色。

    Args:
        requirement_id: 需求 ID
        status: 目标状态 (research/organizing/dev/testing/done/blocked)
    """
    valid_statuses = ("research", "organizing", "dev", "testing", "done", "blocked")
    if status not in valid_statuses:
        return f"错误：无效状态 '{status}'，必须是 {valid_statuses} 之一"

    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT r.status, r.code, r.title, v.project_id "
            "FROM requirements r JOIN versions v ON r.version_id=v.id "
            "WHERE r.id=?",
            (requirement_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return f"错误：需求 {requirement_id} 不存在"

        old_status = row["status"]
        code = row["code"]
        project_id = row["project_id"]

        if old_status == status:
            return f"卡片 [{code}] 已经是 {status} 状态"

        if not _check_move_permission(old_status, status):
            return (
                f"权限拒绝：角色 '{AGENT_ROLE}' 不能将卡片从 {old_status} 移到 {status}。"
                f"允许的流转: {_get_allowed_moves()}"
            )

        await db.execute(
            "UPDATE requirements SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
            (status, requirement_id),
        )

        await db.execute(
            "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
            (
                project_id,
                "status_changed",
                requirement_id,
                json.dumps({"old_status": old_status, "new_status": status, "moved_by": AGENT_ROLE}),
            ),
        )
        await db.commit()

        logger.info("Moved [%s] %s → %s (by %s)", code, old_status, status, AGENT_ROLE)
        return f"已移动 [{code}] {old_status} → {status}"
    finally:
        await db.close()


@mcp.tool()
async def add_comment(requirement_id: int, content: str) -> str:
    """为需求卡片添加评论。

    Args:
        requirement_id: 需求 ID
        content: 评论内容（Markdown）
    """
    if not content.strip():
        return "错误：评论内容不能为空"

    from agents.registry import registry
    role_config = registry.get(AGENT_ROLE)
    author = role_config.display_name if role_config else AGENT_ROLE

    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT code FROM requirements WHERE id=?", (requirement_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return f"错误：需求 {requirement_id} 不存在"

        await db.execute(
            "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
            (requirement_id, author, content),
        )
        await db.commit()
        return f"已添加评论到 [{row['code']}]（作者: {author}）"
    finally:
        await db.close()


@mcp.tool()
async def list_comments(requirement_id: int) -> str:
    """列出需求卡片的所有评论，按时间正序。

    Args:
        requirement_id: 需求 ID
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT author, content, created_at FROM comments "
            "WHERE requirement_id=? ORDER BY created_at",
            (requirement_id,),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        if not rows:
            return "暂无评论"
        return json.dumps(rows, ensure_ascii=False, indent=2)
    finally:
        await db.close()


@mcp.tool()
async def attach_detail(requirement_id: int, content: str) -> str:
    """将详细支撑数据附加到当前角色最近一条评论。

    用于行业顾问等角色在写完结论摘要后，补充详细的调研数据、竞品对比表等。
    PM 审核时会自动加载这些数据，开发阶段只看摘要。

    Args:
        requirement_id: 需求 ID
        content: 详细支撑数据（Markdown）
    """
    if not content.strip():
        return "错误：detail 内容不能为空"

    from agents.registry import registry
    role_config = registry.get(AGENT_ROLE)
    author = role_config.display_name if role_config else AGENT_ROLE

    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT id FROM comments WHERE requirement_id=? AND author=? "
            "ORDER BY created_at DESC LIMIT 1",
            (requirement_id, author),
        )
        row = await cursor.fetchone()
        if not row:
            return f"错误：未找到 {author} 在需求 {requirement_id} 下的评论，请先用 add_comment 写摘要"

        await db.execute(
            "UPDATE comments SET detail=? WHERE id=?",
            (content.strip(), row["id"]),
        )
        await db.commit()
        return f"已附加详细数据到评论 #{row['id']}（{len(content)} 字符）"
    finally:
        await db.close()


@mcp.tool()
async def read_comment_detail(comment_id: int) -> str:
    """读取评论的详细支撑数据。

    PM 审核调研结果时使用，获取行业顾问等角色附加的完整数据。

    Args:
        comment_id: 评论 ID
    """
    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT author, detail FROM comments WHERE id=?", (comment_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return f"错误：评论 #{comment_id} 不存在"
        if not row["detail"]:
            return f"评论 #{comment_id} 没有附加详细数据"
        return f"**{row['author']}** 的详细数据：\n\n{row['detail']}"
    finally:
        await db.close()
    """获取项目背景信息：产品记忆 + 架构文档。

    Args:
        project_id: 项目 ID（默认使用环境变量中的项目）
    """
    pid = project_id or PROJECT_ID
    if not pid:
        return "错误：未指定 project_id"

    db = await _get_db()
    try:
        sections = []

        cursor = await db.execute(
            "SELECT name, prefix, product_memory FROM projects WHERE id=?",
            (pid,),
        )
        proj = await cursor.fetchone()
        if not proj:
            return f"错误：项目 {pid} 不存在"

        sections.append(f"# 项目：{proj['name']} ({proj['prefix']})")

        if proj["product_memory"]:
            sections.append(f"\n## 产品记忆\n\n{proj['product_memory']}")

        cursor = await db.execute(
            "SELECT content FROM project_architecture WHERE project_id=?", (pid,)
        )
        arch = await cursor.fetchone()
        if arch and arch["content"]:
            sections.append(f"\n## 架构概要\n\n{arch['content']}")

        return "\n".join(sections)
    finally:
        await db.close()


def _get_allowed_moves() -> str:
    from agents.registry import registry
    perms = registry.get_permissions(AGENT_ROLE)
    if not perms:
        return "(无)"
    return ", ".join(perms.can_move)


@mcp.tool()
async def load_guideline(name: str) -> str:
    """加载工作指南获取详细指令。可用指南: pm-research-audit, pm-conflict-resolution, pm-coupling

    Args:
        name: 指南名称
    """
    import pathlib
    skills_base = pathlib.Path(__file__).parent.parent / "skills" / "pm"
    skill_path = skills_base / name / "SKILL.md"
    if not skill_path.exists():
        available = [d.name for d in skills_base.iterdir() if d.is_dir() and (d / "SKILL.md").exists()]
        return f"错误：指南 '{name}' 不存在。可用: {', '.join(available)}"
    content = skill_path.read_text(encoding="utf-8")
    if content.startswith("---"):
        end = content.find("---", 3)
        if end > 0:
            content = content[end + 3:].strip()
    return content


# ==================== Atomic Decision Tools ====================
# These combine comment + state transition into a single tool call,
# making "commented but didn't move" impossible by design.


async def _atomic_decision(
    requirement_id: int,
    comment: str,
    detail: str,
    new_status: str | None,
    ceo_question: str | None,
    expected_current_status: str,
    role: str,
) -> str:
    """Shared implementation for all atomic decision tools.

    Either moves the card (new_status) or sets ceo_decision (ceo_question), never both.
    """
    from datetime import datetime
    from agents.registry import registry

    if not comment.strip():
        return "错误：评论内容不能为空"

    role_config = registry.get(role)
    author = role_config.display_name if role_config else role

    db = await _get_db()
    try:
        cursor = await db.execute(
            "SELECT r.status, r.code, r.title, v.project_id "
            "FROM requirements r JOIN versions v ON r.version_id=v.id "
            "WHERE r.id=?",
            (requirement_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return f"错误：需求 {requirement_id} 不存在"

        current_status = row["status"]
        code = row["code"]
        project_id = row["project_id"]

        if current_status != expected_current_status:
            return (
                f"错误：卡片 [{code}] 当前状态是 {current_status}，"
                f"预期 {expected_current_status}。无法执行此操作。"
            )

        if new_status and new_status != current_status:
            if not _check_move_permission(current_status, new_status):
                return (
                    f"权限拒绝：角色 '{AGENT_ROLE}' 不能将卡片从 "
                    f"{current_status} 移到 {new_status}。"
                    f"允许的流转: {_get_allowed_moves()}"
                )

        await db.execute(
            "INSERT INTO comments (requirement_id, author, content, detail) VALUES (?,?,?,?)",
            (requirement_id, author, comment.strip(), detail.strip() if detail else ""),
        )

        COL_ASSIGNEE = {
            "research": "Industry",
            "organizing": "PM",
            "dev": "Coach-Dev",
            "testing": "Coach-Review",
            "done": "",
        }

        if ceo_question:
            ceo_dec = json.dumps({
                "role": role,
                "reason": "agent_d",
                "message": ceo_question,
                "actions": ["reply_to_role", "approve_dev", "request_more_research"],
                "since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, ensure_ascii=False)
            await db.execute(
                "UPDATE requirements SET assignee='', queue_reason=?, "
                "ceo_decision=?, updated_at=datetime('now','localtime') WHERE id=?",
                (f"等待 CEO: {ceo_question[:50]}", ceo_dec, requirement_id),
            )
            logger.info("CEO-ASK [%s] by %s: %s", code, role, ceo_question[:80])

        elif new_status and new_status != current_status:
            col_assignee = COL_ASSIGNEE.get(new_status, "")
            await db.execute(
                "UPDATE requirements SET status=?, assignee=?, "
                "updated_at=datetime('now','localtime'), "
                "progressed_at=datetime('now','localtime') WHERE id=?",
                (new_status, col_assignee, requirement_id),
            )
            await db.execute(
                "INSERT INTO agent_events "
                "(project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                (project_id, "status_changed", requirement_id,
                 json.dumps({"old_status": current_status, "new_status": new_status,
                             "moved_by": role})),
            )
            logger.info("ATOMIC-MOVE [%s] %s → %s by %s", code, current_status, new_status, role)

        await db.commit()

        if ceo_question:
            return f"已评论并请求 CEO 决策 [{code}]（问题: {ceo_question[:60]}）"
        elif new_status and new_status != current_status:
            return f"已评论并移动 [{code}] {current_status} → {new_status}"
        else:
            return f"已评论 [{code}]（状态不变）"
    finally:
        await db.close()


# --- PM Decision Tools (organizing column) ---

@mcp.tool()
async def pm_approve(requirement_id: int, comment: str, target: str = "dev", detail: str = "") -> str:
    """PM 审批通过：发表评论并将卡片推进到下一阶段。

    Args:
        requirement_id: 需求 ID
        comment: 评审意见（Markdown，200-500字摘要）
        target: 目标状态，"dev"（进入开发）或 "done"（调研类需求完成）
        detail: 可选的详细支撑数据
    """
    if target not in ("dev", "done"):
        return "错误：target 必须是 'dev' 或 'done'"
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail=detail,
        new_status=target,
        ceo_question=None,
        expected_current_status="organizing",
        role="pm",
    )


@mcp.tool()
async def pm_send_to_research(requirement_id: int, comment: str, detail: str = "") -> str:
    """PM 退回调研：发表评论并将卡片移回 research 列，由行业顾问补充调研。

    Args:
        requirement_id: 需求 ID
        comment: 退回原因和补充调研方向（Markdown）
        detail: 可选的详细说明
    """
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail=detail,
        new_status="research",
        ceo_question=None,
        expected_current_status="organizing",
        role="pm",
    )


@mcp.tool()
async def pm_ask_ceo(requirement_id: int, comment: str, question: str) -> str:
    """PM 请求 CEO 决策：发表评论并设置等待 CEO 决策。卡片留在 organizing 列。

    Args:
        requirement_id: 需求 ID
        comment: 当前分析和需要 CEO 决策的背景说明
        question: 需要 CEO 回答的具体问题（简洁明确）
    """
    if not question.strip():
        return "错误：question 不能为空"
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail="",
        new_status=None,
        ceo_question=question.strip(),
        expected_current_status="organizing",
        role="pm",
    )


# --- Industry Decision Tools (research column) ---

@mcp.tool()
async def industry_complete(requirement_id: int, comment: str, detail: str = "") -> str:
    """行业顾问调研完成：发表结论并将卡片转给 PM（research → organizing）。

    Args:
        requirement_id: 需求 ID
        comment: 调研结论摘要（200-500字）
        detail: 详细调研数据（表格、来源链接、搜索记录等）
    """
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail=detail,
        new_status="organizing",
        ceo_question=None,
        expected_current_status="research",
        role="industry",
    )


@mcp.tool()
async def industry_ask_ceo(requirement_id: int, comment: str, question: str) -> str:
    """行业顾问请求 CEO 补充信息：发表评论并设置等待 CEO。卡片留在 research 列。

    Args:
        requirement_id: 需求 ID
        comment: 当前调研进展和缺失信息说明
        question: 需要 CEO 补充的具体信息（简洁明确）
    """
    if not question.strip():
        return "错误：question 不能为空"
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail="",
        new_status=None,
        ceo_question=question.strip(),
        expected_current_status="research",
        role="industry",
    )


# --- Coach-Review Decision Tools (testing column) ---

@mcp.tool()
async def review_pass(requirement_id: int, comment: str, detail: str = "") -> str:
    """QA 审查通过：发表评论并将卡片移到 done。

    Args:
        requirement_id: 需求 ID
        comment: 审查通过结论
        detail: 可选的逐条验收详情
    """
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail=detail,
        new_status="done",
        ceo_question=None,
        expected_current_status="testing",
        role="coach_review",
    )


@mcp.tool()
async def review_reject(requirement_id: int, comment: str, detail: str = "") -> str:
    """QA 审查不通过：发表评论并将卡片打回 dev 列。

    Args:
        requirement_id: 需求 ID
        comment: 问题概述和打回原因
        detail: 可选的详细问题列表和复现步骤
    """
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail=detail,
        new_status="dev",
        ceo_question=None,
        expected_current_status="testing",
        role="coach_review",
    )


@mcp.tool()
async def review_ask_ceo(requirement_id: int, comment: str, question: str) -> str:
    """QA 请求 CEO 决策：发表评论并设置等待 CEO。卡片留在 testing 列。

    Args:
        requirement_id: 需求 ID
        comment: 审查中遇到的无法自行判断的问题
        question: 需要 CEO 裁决的具体问题（简洁明确）
    """
    if not question.strip():
        return "错误：question 不能为空"
    return await _atomic_decision(
        requirement_id=requirement_id,
        comment=comment,
        detail="",
        new_status=None,
        ceo_question=question.strip(),
        expected_current_status="testing",
        role="coach_review",
    )


if __name__ == "__main__":
    logger.info("Agent MCP server starting (role=%s, project=%d, db=%s)", AGENT_ROLE, PROJECT_ID, DB_PATH)
    mcp.run()
