"""Comment Agent — generates review comments by calling configured LLM or Hermes CLI."""

import asyncio
import logging
import os

import httpx

from agents.registry import registry, AgentRole

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "") or os.getenv("API_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "") or os.getenv("API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "") or os.getenv("API_BASE_URL", "")


class CommentAgent:
    """Agent that reads requirement context and produces a review comment.

    For roles with provider='hermes', delegates to the hermes CLI subprocess
    which has its own tool loop (web_search, browser, etc.).
    For other providers, calls the LLM API directly (text-only, no tools).
    """

    def __init__(self, role_name: str, project_id: int = 0):
        self.role_config: AgentRole = registry.get(role_name)
        if not self.role_config:
            raise ValueError(f"Unknown role: {role_name}")
        self.project_id = project_id

    async def execute(self, card: dict, existing_comments: list[dict] | None = None) -> dict:
        """Generate a review comment for the given card.

        Returns: {"success": bool, "comment": str, "summary": str}
        """
        prompt = await self._build_prompt(card, existing_comments or [])
        try:
            response = await self._call_model(prompt)
            return {
                "success": bool(response),
                "comment": response,
                "summary": f"{self.role_config.display_name} reviewed [{card.get('code', '')}]",
            }
        except Exception as e:
            logger.error(f"CommentAgent({self.role_config.role}) failed: {e}")
            return {"success": False, "comment": "", "summary": str(e)}

    async def _build_prompt(self, card: dict, comments: list[dict]) -> str:
        system = self.role_config.system_prompt

        # Inject project context for hermes provider (it has tool capabilities)
        context_section = ""
        if self.project_id and self.role_config.model.provider == "hermes":
            context_section = await self._get_project_context()

        card_context = (
            f"## 需求卡片\n\n"
            f"- 编号: {card.get('code', '')}\n"
            f"- 标题: {card.get('title', '')}\n"
            f"- 优先级: {card.get('priority', 'P2')}\n"
            f"- 状态: {card.get('status', '')}\n\n"
            f"### 描述\n\n{card.get('description', '(无描述)')}\n"
        )

        if comments:
            card_context += "\n### 已有评论\n\n"
            for c in comments[-5:]:
                card_context += f"**{c.get('author', 'unknown')}:** {c.get('content', '')}\n\n"

        # Context-specific instruction suffix
        suffix = self._build_suffix(card, comments)
        card_context += f"\n---\n\n{suffix}"

        return f"{system}\n\n{context_section}\n\n{card_context}"

    def _build_suffix(self, card: dict, comments: list[dict]) -> str:
        """Build role-specific instruction suffix based on card state."""
        role = self.role_config.role
        status = card.get("status", "")

        # PM evaluating a pending card that came from research (has industry comments)
        if role == "pm" and status == "pending":
            has_industry = any(c.get("author") == "行业顾问" for c in comments)
            if has_industry:
                return (
                    "你正在评估行业顾问的调研结果。请判断调研材料是否足够支撑决策：\n\n"
                    "- 如果材料充分（有具体数据、竞品对比、可落地方案）→ 评论开头写 [调研充分]\n"
                    "  - 开发需求（type=dev）：整理最终验收标准\n"
                    "  - 调研需求（type=research）：提炼结论要点，后续系统会自动归档\n"
                    "- 如果材料不足（缺少关键数据、方案不具体、风险未量化）→ 评论开头写 [需要补充]，然后明确列出需要补充的具体内容和重点方向\n\n"
                    "必须以 [调研充分] 或 [需要补充] 开头，这是系统解析你决策的唯一方式。"
                )

        # Industry absolute boundary reinforcement
        if role == "industry" and status == "research":
            return (
                "你现在在 research 列。你是行业顾问，只负责调研工作。\n\n"
                "🚫 你绝对不能说的话（逐字禁止）：\n"
                "- 不能说「请 PM」任何内容\n"
                "- 不能说「等 PM」或「等待 PM」\n"
                "- 不能说「请 CEO 决策」（改用 [需要补充] 标记）\n"
                "- 不能教其他角色怎么做\n\n"
                "✅ 你只能说以下三种之一：\n"
                "1. 直接输出调研结果（数据、分析、对比表）\n"
                "2. 评论开头写 [需要补充] + 简短说明缺什么\n"
                "3. 评论开头写 [转给PM] + 调研结论\n\n"
                "记住：你不需要 PM 告诉你做什么，你是行业专家。如果信息不足，用 [需要补充] 找 CEO。"
            )

        return (
            "请从你的角色视角对这个需求进行评审，给出具体、可操作的建议。"
            "如果没有补充意见，写一句简短确认即可。"
        )

    async def _get_project_context(self) -> str:
        """Read project context from database for prompt injection."""
        import aiosqlite
        from core.database import DB_PATH

        sections = []
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT name, prefix, advisor_skill, product_memory FROM projects WHERE id=?",
                (self.project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                sections.append(f"## 项目：{proj['name']} ({proj['prefix']})")
                if proj["advisor_skill"]:
                    sections.append(f"\n## 产品顾问知识\n\n{proj['advisor_skill'][:1500]}")
                if proj["product_memory"]:
                    sections.append(f"\n## 产品记忆\n\n{proj['product_memory'][:1000]}")

            cursor = await db.execute(
                "SELECT r.code, r.title, r.status, r.priority "
                "FROM requirements r JOIN versions v ON r.version_id=v.id "
                "WHERE v.project_id=? AND r.archived=0 "
                "ORDER BY r.priority LIMIT 15",
                (self.project_id,),
            )
            reqs = await cursor.fetchall()
            if reqs:
                sections.append("\n## 当前看板\n")
                for r in reqs:
                    sections.append(f"- [{r['code']}] {r['title']} ({r['status']}, {r['priority']})")

        return "\n".join(sections)

    async def _call_model(self, prompt: str) -> str:
        cfg = self.role_config.model

        if cfg.provider == "hermes":
            return await self._call_hermes(prompt, cfg)
        elif cfg.provider == "anthropic":
            return await self._call_anthropic(prompt, cfg)
        else:
            return await self._call_openai(prompt, cfg)

    async def _call_hermes(self, prompt: str, cfg) -> str:
        """Call hermes CLI as subprocess. Hermes manages its own tool loop."""
        from web.hermes_chat import ensure_hermes_config, _build_hermes_env
        await ensure_hermes_config()

        toolsets = cfg.toolsets if hasattr(cfg, "toolsets") and cfg.toolsets else []
        cmd = ["hermes", "-z", prompt]
        if toolsets:
            cmd.extend(["-t", ",".join(toolsets)])
        if cfg.name:
            cmd.extend(["--model", cfg.name])

        logger.info(f"Calling hermes: {' '.join(cmd[:4])}...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_hermes_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=cfg.timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"hermes timed out after {cfg.timeout}s")

        output = stdout.decode(errors="replace").strip()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            logger.warning(f"hermes exited {proc.returncode}: {err[:200]}")
            if not output:
                raise RuntimeError(f"hermes failed: {err[:300]}")
        # Strip Hermes tool call XML tags (both <HermesTool: name>...</HermesTool: name> and <HermesTool: name>...</HermesTool>)
        import re
        output = re.sub(r'<HermesTool:[^>]*>.*?</HermesTool[^>]*>', '', output, flags=re.DOTALL)
        output = re.sub(r'<HermesTool:[^>]*/>', '', output)
        return output.strip()

    async def _call_anthropic(self, prompt: str, cfg) -> str:
        import anthropic

        api_key = ANTHROPIC_API_KEY
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        kwargs = {"api_key": api_key}
        if cfg.base_url or ANTHROPIC_BASE_URL:
            kwargs["base_url"] = cfg.base_url or ANTHROPIC_BASE_URL
        client = anthropic.AsyncAnthropic(**kwargs)
        msg = await client.messages.create(
            model=cfg.name,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        for block in msg.content:
            if hasattr(block, "text"):
                return block.text
        return ""

    async def _call_openai(self, prompt: str, cfg) -> str:
        base = (cfg.base_url or OPENAI_BASE_URL or "https://api.openai.com").rstrip("/")
        api_key = OPENAI_API_KEY
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY not set")
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": cfg.name,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 1024,
                },
                timeout=cfg.timeout,
            )
            resp.raise_for_status()
            choices = resp.json().get("choices", [])
            return choices[0]["message"]["content"] if choices else ""
