"""Comment Agent — generates review comments by calling configured LLM or Hermes CLI."""

import asyncio
import logging
import os

import httpx

from agents.registry import registry, AgentRole

logger = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")


class CommentAgent:
    """Agent that reads requirement context and produces a review comment.

    For roles with provider='hermes', delegates to the hermes CLI subprocess
    which has its own tool loop (web_search, browser, etc.).
    For other providers, calls the LLM API directly (text-only, no tools).
    """

    def __init__(self, role_name: str):
        self.role_config: AgentRole = registry.get(role_name)
        if not self.role_config:
            raise ValueError(f"Unknown role: {role_name}")

    async def execute(self, card: dict, existing_comments: list[dict] | None = None) -> dict:
        """Generate a review comment for the given card.

        Returns: {"success": bool, "comment": str, "summary": str}
        """
        prompt = self._build_prompt(card, existing_comments or [])
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

    def _build_prompt(self, card: dict, comments: list[dict]) -> str:
        system = self.role_config.system_prompt

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

        card_context += (
            "\n---\n\n"
            "请从你的角色视角对这个需求进行评审，给出具体、可操作的建议。"
            "如果没有补充意见，写一句简短确认即可。"
        )

        return f"{system}\n\n{card_context}"

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
        return output

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
        return msg.content[0].text if msg.content else ""

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
