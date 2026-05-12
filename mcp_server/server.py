"""Kanban Harness MCP Server — intent-driven interface for AI collaboration."""

import os
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

from mcp_server.kh_client import KHClient

mcp = FastMCP(
    "Kanban Harness",
    instructions="AI Team Orchestration Engine — intent-driven MCP interface for collaborating with the KH AI team.",
)

client = KHClient()


@mcp.tool()
async def kh_brief() -> str:
    """返回 KH 团队状态摘要：各角色状态、待审批项、进行中的卡片。"""
    projects = await client.list_projects()
    agents = await client.get_agents_status()
    scheduler = await client.get_scheduler_status()

    in_progress = []
    pending_review = []

    for proj in projects:
        versions = await client.list_versions(proj["id"])
        for ver in versions:
            if ver["status"] in ("active", "testing"):
                reqs = await client.list_requirements(ver["id"])
                for req in reqs:
                    if req["status"] == "dev":
                        in_progress.append(f"[{req['code']}] {req['title']} (P{req['priority'][-1]})")
                    elif req["status"] == "testing":
                        pending_review.append(f"[{req['code']}] {req['title']}")

    sections = []
    sections.append("## 团队状态")
    agent_info = agents.get("agents", {})
    for role, info in agent_info.items():
        sections.append(f"- {role}: {info['status']}")

    sections.append(f"\n## 调度器: {scheduler.get('mode', 'unknown')} (autopilot={scheduler.get('autopilot_level', 0)})")

    sections.append(f"\n## 进行中 ({len(in_progress)})")
    if in_progress:
        for item in in_progress[:10]:
            sections.append(f"- {item}")
    else:
        sections.append("- (无)")

    sections.append(f"\n## 待测试/审批 ({len(pending_review)})")
    if pending_review:
        for item in pending_review[:10]:
            sections.append(f"- {item}")
    else:
        sections.append("- (无)")

    return "\n".join(sections)


@mcp.tool()
async def kh_submit_idea(idea: str, project_id: int = 0, priority: str = "P2") -> str:
    """提交想法到看板，自动建卡。

    Args:
        idea: 想法描述，会作为卡片标题
        project_id: 目标项目 ID（0 则使用第一个活跃项目）
        priority: 优先级 P0/P1/P2/P3
    """
    if not idea.strip():
        return "错误：idea 不能为空"

    projects = await client.list_projects()
    if not projects:
        return "错误：没有可用的项目"

    target_project = None
    if project_id > 0:
        target_project = next((p for p in projects if p["id"] == project_id), None)
    if not target_project:
        target_project = projects[0]

    versions = await client.list_versions(target_project["id"])
    active_version = next((v for v in versions if v["status"] == "active"), None)
    if not active_version:
        active_version = next((v for v in versions if v["status"] == "planning"), None)
    if not active_version:
        return f"错误：项目 '{target_project['name']}' 没有活跃版本"

    req = await client.create_requirement(
        version_id=active_version["id"],
        title=idea.strip(),
        priority=priority,
    )

    return (
        f"已建卡：{req['code']} — {req['title']}\n"
        f"项目：{target_project['name']} / {active_version['name']}\n"
        f"优先级：{priority} | 状态：pending"
    )


@mcp.tool()
async def kh_notify_event(event_type: str, detail: str) -> str:
    """注入外部事件到 KH 系统，触发相应处理流程。

    Args:
        event_type: 事件类型 (deploy_done, bug_report, user_feedback, ci_failed, release_ready)
        detail: 事件详情描述
    """
    valid_types = ("deploy_done", "bug_report", "user_feedback", "ci_failed", "release_ready")
    if event_type not in valid_types:
        return f"错误：event_type 必须是 {valid_types} 之一"

    if event_type == "bug_report":
        projects = await client.list_projects()
        if projects:
            versions = await client.list_versions(projects[0]["id"])
            active_version = next((v for v in versions if v["status"] == "active"), None)
            if active_version:
                req = await client.create_requirement(
                    version_id=active_version["id"],
                    title=f"[Bug] {detail[:80]}",
                    description=f"## Bug Report\n\n{detail}",
                    priority="P1",
                )
                return f"Bug 已记录：{req['code']} — {req['title']}"

    return (
        f"事件已接收：{event_type}\n"
        f"详情：{detail}\n"
        f"状态：已记录，等待调度器处理"
    )


@mcp.tool()
async def kh_ask_pm(question: str) -> str:
    """向 PM 角色提问，返回基于看板数据的 PM 视角回答。

    Args:
        question: 要问 PM 的问题
    """
    projects = await client.list_projects()
    all_reqs = []
    for proj in projects:
        versions = await client.list_versions(proj["id"])
        for ver in versions:
            if ver["status"] in ("active", "testing"):
                reqs = await client.list_requirements(ver["id"])
                all_reqs.extend(reqs)

    stats = {"pending": 0, "dev": 0, "testing": 0, "done": 0}
    p0_items = []
    for req in all_reqs:
        stats[req["status"]] = stats.get(req["status"], 0) + 1
        if req["priority"] == "P0" and req["status"] != "done":
            p0_items.append(f"[{req['code']}] {req['title']}")

    context = (
        f"当前看板状态：pending={stats['pending']}, dev={stats['dev']}, "
        f"testing={stats['testing']}, done={stats['done']}\n"
    )
    if p0_items:
        context += f"P0 紧急项：{', '.join(p0_items)}\n"

    return (
        f"## PM 视角回答\n\n"
        f"**问题：** {question}\n\n"
        f"**上下文：**\n{context}\n"
        f"**回答：** 基于当前看板数据，"
        f"共有 {len(all_reqs)} 个活跃需求。"
        f"{'有 ' + str(len(p0_items)) + ' 个 P0 紧急项需要优先处理。' if p0_items else '无紧急阻塞项。'}\n\n"
        f"_注：完整 AI 回答需要 Claude API 集成（v0.2 后续迭代）_"
    )


@mcp.tool()
async def kh_approve(item_id: int) -> str:
    """批准待审批项（将需求从 testing 移到 done）。

    Args:
        item_id: 需求卡片 ID
    """
    try:
        result = await client.move_requirement(item_id, status="done")
        return f"已批准：[{result['code']}] {result['title']} → done"
    except Exception as e:
        return f"批准失败：{e}"


@mcp.tool()
async def kh_reject(item_id: int, reason: str) -> str:
    """驳回待审批项（将需求打回 dev 并添加驳回原因）。

    Args:
        item_id: 需求卡片 ID
        reason: 驳回原因
    """
    try:
        await client.move_requirement(item_id, status="dev")
        await client.add_comment(item_id, content=f"**驳回原因：** {reason}", author="reviewer")
        return f"已驳回：[ID={item_id}] → dev\n原因：{reason}"
    except Exception as e:
        return f"驳回失败：{e}"


if __name__ == "__main__":
    mcp.run()
