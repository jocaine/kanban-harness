"""Kanban Harness MCP Server — intent-driven interface for AI collaboration."""

import os
import sys
import logging

# Ensure project root is in sys.path so `mcp_server.kh_client` resolves
# when hermes spawns this file directly as `python /path/to/server.py`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

logger = logging.getLogger("kh.mcp.server")
logging.basicConfig(
    level=logging.DEBUG if os.getenv("MCP_DEBUG") else logging.INFO,
    format="%(asctime)s [MCP] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)

from mcp_server.kh_client import KHClient

logger.info("MCP 服务模块已加载, KH_BASE_URL=%s", os.getenv("KH_BASE_URL", "http://localhost:8000"))

mcp = FastMCP(
    "Kanban Harness",
    instructions="AI Team Orchestration Engine — intent-driven MCP interface for collaborating with the KH AI team.",
)

client = KHClient()
logger.info("MCP FastMCP 实例已创建, 目标地址 %s", client.base_url)


@mcp.tool()
async def kh_brief() -> str:
    """返回 KH 团队状态摘要：各角色状态、待审批项、进行中的卡片。"""
    logger.info("tool:kh_brief 已调用")
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
    logger.info("tool:kh_submit_idea 提交想法=%r project_id=%d priority=%s", idea[:60], project_id, priority)
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
        type="idea",
    )

    return (
        f"已建卡：{req['code']} — {req['title']}\n"
        f"项目：{target_project['name']} / {active_version['name']}\n"
        f"优先级：{priority} | 类型：想法卡 | 状态：organizing\n"
        f"PM 将自动拆解为具体执行卡片。"
    )


@mcp.tool()
async def kh_notify_event(event_type: str, detail: str) -> str:
    """注入外部事件到 KH 系统，触发相应处理流程。

    Args:
        event_type: 事件类型 (deploy_done, bug_report, user_feedback, ci_failed, release_ready)
        detail: 事件详情描述
    """
    logger.info("tool:kh_notify_event 类型=%s 详情=%r", event_type, detail[:80])
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
    logger.info("tool:kh_ask_pm 问题=%r", question[:80])
    projects = await client.list_projects()
    all_reqs = []
    for proj in projects:
        versions = await client.list_versions(proj["id"])
        for ver in versions:
            if ver["status"] in ("active", "testing"):
                reqs = await client.list_requirements(ver["id"])
                all_reqs.extend(reqs)

    stats = {"research": 0, "organizing": 0, "dev": 0, "testing": 0, "done": 0}
    p0_items = []
    for req in all_reqs:
        stats[req["status"]] = stats.get(req["status"], 0) + 1
        if req["priority"] == "P0" and req["status"] != "done":
            p0_items.append(f"[{req['code']}] {req['title']}")

    context = (
        f"当前看板状态：research={stats['research']}, organizing={stats['organizing']}, dev={stats['dev']}, "
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
    logger.info("tool:kh_approve 审批=%d", item_id)
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
    logger.info("tool:kh_reject 拒绝=%d 原因=%r", item_id, reason[:60])
    try:
        await client.move_requirement(item_id, status="dev")
        await client.add_comment(item_id, content=f"**驳回原因：** {reason}", author="reviewer")
        return f"已驳回：[ID={item_id}] → dev\n原因：{reason}"
    except Exception as e:
        return f"驳回失败：{e}"


@mcp.tool()
async def kh_web_search(query: str, limit: int = 5) -> str:
    """搜索互联网信息，返回标题、URL、摘要。用于行业调研、竞品分析、数据验证。

    Args:
        query: 搜索关键词，尽量具体（包含年份、品牌名、指标等）
        limit: 返回结果数量，默认5条
    """
    import httpx
    import json

    logger.info("tool:kh_web_search 搜索=%r limit=%d", query[:60], limit)
    searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8888").rstrip("/")

    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(
                f"{searxng_url}/search",
                params={"q": query, "format": "json", "pageno": 1, "language": "zh-CN"},
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        return f"搜索失败：SearXNG 返回 HTTP {e.response.status_code}"
    except httpx.RequestError as e:
        return f"搜索失败：无法连接 SearXNG ({searxng_url}): {e}"

    data = resp.json()
    raw_results = data.get("results", [])
    sorted_results = sorted(raw_results, key=lambda r: float(r.get("score", 0)), reverse=True)[:limit]

    results = []
    for i, r in enumerate(sorted_results, 1):
        results.append({
            "position": i,
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "description": r.get("content", ""),
            "engine": r.get("engine", ""),
        })

    return json.dumps({"success": True, "query": query, "results": results}, ensure_ascii=False, indent=2)


@mcp.tool()
async def kh_web_extract(url: str) -> str:
    """提取指定网页的正文内容（用于深入阅读搜索结果）。

    Args:
        url: 要提取内容的网页 URL（必须是 kh_web_search 返回的真实 URL）
    """
    import httpx

    logger.info("tool:kh_web_extract 提取=%r", url[:80])

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as http:
            resp = await http.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; KHBot/1.0)"})
            resp.raise_for_status()
            html = resp.text
    except httpx.RequestError as e:
        return f"提取失败：无法访问 {url}: {e}"
    except httpx.HTTPStatusError as e:
        return f"提取失败：HTTP {e.response.status_code}"

    # Simple HTML to text extraction
    import re
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()

    if len(text) > 5000:
        text = text[:5000] + f"\n...(截断，共 {len(text)} 字符)"

    return text if text else "提取失败：页面内容为空"


@mcp.tool()
async def kh_load_guideline(name: str) -> str:
    """加载 AI agent 工作指南。供 Industry/PM 等角色按需获取详细工作流程和模板。

    可用指南:
    - industry-advisor: 行业顾问输出格式、调研报告模板、耦合卡输出
    - market-research: 搜索方法论、工具使用、信源优先级
    - pm-research-audit: PM 调研评估标准、决策规则
    - pm-conflict-resolution: 退回争议处理流程
    - pm-coupling: 耦合卡技术判断流程

    Args:
        name: 指南名称
    """
    from pathlib import Path

    logger.info("tool:kh_load_guideline 指南=%r", name)

    # Search in both skill directories
    base = Path(__file__).parent.parent / "skills"
    search_paths = [
        base / "pm" / name / "SKILL.md",
        base / "research" / name / "SKILL.md",
    ]

    for skill_path in search_paths:
        if skill_path.exists():
            content = skill_path.read_text(encoding="utf-8")
            if content.startswith("---"):
                end = content.find("---", 3)
                if end > 0:
                    content = content[end + 3:].strip()
            return content

    # List available skills
    available = []
    for subdir in base.iterdir():
        if subdir.is_dir():
            for skill_dir in subdir.iterdir():
                if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
                    available.append(skill_dir.name)
    return f"错误：指南 '{name}' 不存在。可用: {', '.join(sorted(available))}"


if __name__ == "__main__":
    logger.info("MCP 服务启动 stdio 模式 (pid=%d)", os.getpid())
    mcp.run()
