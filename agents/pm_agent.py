"""PM Agent — Claude CLI-based product manager with kanban MCP tools.

Replaces the raw API call in CommentAgent for the PM role, giving PM
full agentic capabilities: tool use, skill loading, memory, multi-turn reasoning.
"""

import asyncio
import logging
import os

from agents.mcp_config import ensure_agent_mcp_config
from agents.registry import registry, AgentRole

logger = logging.getLogger("kh.agent.pm")

DEFAULT_TIMEOUT = 300


class PMAgent:
    """PM agent that executes via Claude CLI with kanban MCP tools."""

    def __init__(self, project_id: int):
        self.project_id = project_id
        self.role_config: AgentRole = registry.get("pm")
        self.timeout = self.role_config.model.timeout or DEFAULT_TIMEOUT

    async def execute(self, card: dict, existing_comments: list[dict] | None = None) -> dict:
        """Execute PM review for a card.

        Returns: {"success": bool, "comment": str, "summary": str}
        """
        config_dir = ensure_agent_mcp_config("pm", self.project_id)
        prompt = self._build_prompt(card, existing_comments or [])

        try:
            output = await self._run_claude(prompt, config_dir)
            return {
                "success": bool(output),
                "comment": output,
                "summary": f"PM reviewed [{card.get('code', '')}] via CLI",
            }
        except asyncio.TimeoutError:
            logger.error("PMAgent timed out for [%s] after %ds", card.get("code", ""), self.timeout)
            return {"success": False, "comment": "", "summary": "PM timed out"}
        except Exception as e:
            logger.error("PMAgent failed for [%s]: %s", card.get("code", ""), e)
            return {"success": False, "comment": "", "summary": str(e)}

    def _build_prompt(self, card: dict, comments: list[dict]) -> str:
        """Build the prompt with pre-injected card context."""
        card_context = (
            f"## 当前任务\n\n"
            f"你正在评审需求卡片，请使用你的 MCP 工具完成操作。\n\n"
            f"- 编号: {card.get('code', '')}\n"
            f"- ID: {card.get('id', '')}\n"
            f"- 标题: {card.get('title', '')}\n"
            f"- 优先级: {card.get('priority', 'P2')}\n"
            f"- 当前状态: {card.get('status', '')}\n\n"
            f"### 描述\n\n{card.get('description', '(无描述)')}\n"
        )

        if comments:
            card_context += "\n### 已有评论\n\n"
            for c in comments:
                text = c.get('content', '')
                if c.get('detail'):
                    text += f"\n\n_(有详细数据，comment_id={c['id']}，可通过 read_comment_detail 工具查看)_"
                card_context += f"**{c.get('author', 'unknown')}:** {text}\n\n"

        suffix = self._build_suffix(card, comments)
        card_context += f"\n---\n\n{suffix}"

        return card_context

    def _build_suffix(self, card: dict, comments: list[dict]) -> str:
        """Build role-specific instruction suffix based on card state."""
        status = card.get("status", "")
        has_industry = any(c.get("author") == "行业顾问" for c in comments)

        if status == "pending" and has_industry:
            return (
                "## 你的任务\n\n"
                "你正在评估行业顾问的调研结果。请判断调研材料是否足够支撑开发决策。\n\n"
                "**操作步骤：**\n"
                "1. 如果需要更多上下文，用 `get_project_context` 工具加载项目背景\n"
                "2. 分析调研材料的完整度（有具体数据？有竞品对比？方案可落地？）\n"
                "3. 用 `add_comment` 工具发表你的评审意见\n"
                "4. 用 `move_requirement` 工具移动卡片：\n"
                "   - 材料充分 → 移到 `dev`\n"
                "   - 材料不足 → 移到 `research`\n\n"
                "**注意：** 你必须同时执行评论和移动两个操作。"
            )

        return (
            "## 你的任务\n\n"
            "请从产品经理视角评审这个需求。\n\n"
            "**操作步骤：**\n"
            "1. 如果需要更多上下文，用 `get_project_context` 工具加载项目背景\n"
            "2. 用 `add_comment` 工具发表你的评审意见（具体、可操作）\n"
            "3. 如果需要移动卡片，用 `move_requirement` 工具\n\n"
            "如果没有补充意见，写一句简短确认即可。"
        )

    async def _run_claude(self, prompt: str, cwd: str) -> str:
        """Run Claude CLI subprocess with kanban MCP tools."""
        tools = ",".join(
            f"mcp__kanban__{t}" for t in self.role_config.allowed_tools
        )

        cmd = [
            "claude",
            "-p", prompt,
            "--print",
            "--allowedTools", tools,
            "--model", self.role_config.model.name,
            "--append-system-prompt", self.role_config.system_prompt,
        ]

        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1"}
        # Map project env vars to Claude CLI expected vars
        if "API_KEY" in os.environ and "ANTHROPIC_AUTH_TOKEN" not in env:
            env["ANTHROPIC_AUTH_TOKEN"] = os.environ["API_KEY"]
        if "API_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in env:
            base = os.environ["API_BASE_URL"].rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            env["ANTHROPIC_BASE_URL"] = base

        logger.info("PMAgent starting CLI for [%s...], cwd=%s", prompt[:50], cwd)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise

        output = stdout.decode(errors="replace").strip()

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning("PMAgent CLI exited %d: %s", proc.returncode, err[:300])
            if not output:
                logger.error("PMAgent produced no output, stderr: %s", err[:500])

        return output
