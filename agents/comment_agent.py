"""Comment Agent — generates review comments via Hermes CLI, Claude CLI, or LLM API."""

import asyncio
import logging
import os
import re

from agents.registry import registry, AgentRole
from agents.mcp_config import ensure_agent_mcp_config

logger = logging.getLogger("kh.agent.comment")


class CommentAgent:
    """Agent that reads requirement context and produces a review comment.

    For roles with provider='hermes', delegates to the hermes CLI subprocess
    which has its own tool loop (web_search, browser, etc.).
    For roles with provider='claude_cli', delegates to the Claude CLI subprocess
    with kanban MCP tools.
    """

    def __init__(self, role_name: str, project_id: int = 0):
        self.role_config: AgentRole = registry.get(role_name)
        if not self.role_config:
            raise ValueError(f"Unknown role: {role_name}")
        self.project_id = project_id

    async def execute(self, card: dict, existing_comments: list[dict] | None = None) -> dict:
        """Generate a review comment for the given card.

        Returns: {"success": bool, "comment": str, "detail": str, "summary": str}
        """
        prompt = await self._build_prompt(card, existing_comments or [])
        try:
            # agent_timeout is for long-running research (hermes); other roles use their own default
            if self.role_config.model.provider == "hermes":
                effective_timeout = card.get("agent_timeout") or self.role_config.model.timeout
            else:
                effective_timeout = self.role_config.model.timeout
            response = await self._call_model(prompt, timeout=effective_timeout)
            comment, detail = self._split_detail(response)
            return {
                "success": bool(comment),
                "comment": comment,
                "detail": detail,
                "summary": f"{self.role_config.display_name} reviewed [{card.get('code', '')}]",
            }
        except Exception as e:
            logger.error(f"CommentAgent({self.role_config.role}) failed: {e}")
            return {"success": False, "comment": "", "detail": "", "summary": str(e)}

    DETAIL_SEPARATOR = "---DETAIL---"
    _SUMMARY_MIN_LEN = 150
    _SUMMARY_MAX_LEN = 800
    _DETAIL_MARKERS = [
        re.compile(r'\n\|.+\|.+\|'),                # markdown 表格行
        re.compile(r'\n#{2,3}\s*(来源|参考|数据|Sources|References)'),
        re.compile(r'\n#{2,3}\s*双视角分析'),
        re.compile(r'\n#{2,3}\s*技术环境画像'),
        re.compile(r'\n-\s*\[.+\]\(https?://'),     # markdown 链接列表
        re.compile(r'\n\d+\.\s*https?://'),          # 编号链接
    ]

    def _split_detail(self, response: str) -> tuple[str, str]:
        """Split response into summary and detail by separator marker.

        Falls back to heuristic splitting when the model doesn't output
        the DETAIL_SEPARATOR (common with local models).
        """
        if self.DETAIL_SEPARATOR in response:
            parts = response.split(self.DETAIL_SEPARATOR, 1)
            return parts[0].strip(), parts[1].strip()

        # Fallback: heuristic split for long outputs missing the separator
        if len(response) > self._SUMMARY_MAX_LEN:
            split_point = self._find_detail_boundary(response)
            if split_point > 0:
                logger.warning(
                    "Model output missing DETAIL_SEPARATOR (%d chars), heuristic split at %d",
                    len(response), split_point,
                )
                return response[:split_point].strip(), response[split_point:].strip()

        return response, ""

    def _find_detail_boundary(self, text: str) -> int:
        """Find the earliest position where detail-level content begins."""
        candidates = []
        for pattern in self._DETAIL_MARKERS:
            m = pattern.search(text)
            if m and m.start() > self._SUMMARY_MIN_LEN:
                candidates.append(m.start())

        if candidates:
            return min(candidates)

        # No marker matched — try splitting at paragraph boundary near max length
        pos = 0
        for para in text.split('\n\n'):
            if pos + len(para) + 2 > self._SUMMARY_MAX_LEN:
                return pos if pos > self._SUMMARY_MIN_LEN else 0
            pos += len(para) + 2
        return 0

    async def _build_prompt(self, card: dict, comments: list[dict]) -> str:
        """Build prompt. For claude_cli/hermes, system prompt is passed separately."""

        # Inject project context for providers with tool capabilities
        context_section = ""
        if self.project_id and self.role_config.model.provider in ("hermes", "claude_cli"):
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
            for c in comments:
                text = c.get('content', '')
                if c.get('detail'):
                    text += "\n\n_(有详细数据，可通过 read_comment_detail 工具查看)_"
                card_context += f"**{c.get('author', 'unknown')}:** {text}\n\n"

        suffix = self._build_suffix(card, comments)
        card_context += f"\n---\n\n{suffix}"

        # For claude_cli, system prompt goes via --append-system-prompt; return card context only
        if self.role_config.model.provider == "claude_cli":
            return f"{context_section}\n\n{card_context}"

        # For hermes, include system prompt inline
        system = self.role_config.system_prompt
        return f"{system}\n\n{context_section}\n\n{card_context}"

    def _build_suffix(self, card: dict, comments: list[dict]) -> str:
        """Build role-specific instruction suffix based on card state."""
        role = self.role_config.role
        status = card.get("status", "")

        # PM in organizing: two scenarios
        if role == "pm" and status == "organizing":
            has_industry = any(c.get("author") == "行业顾问" for c in comments)
            if has_industry:
                # Scenario A: evaluating industry research results
                return (
                    "你正在评估行业顾问的调研结果。请判断调研材料是否足够支撑决策：\n\n"
                    "- 如果材料充分（有具体数据、竞品对比、可落地方案）→ 评论开头写 [调研充分]\n"
                    "  - 开发需求（type=dev）：整理最终验收标准\n"
                    "  - 调研需求（type=research）：提炼结论要点，后续系统会自动归档\n"
                    "- 如果材料不足（缺少关键数据、方案不具体、风险未量化）→ 评论开头写 [需要补充]，然后明确列出需要补充的具体内容和重点方向\n\n"
                    "必须以 [调研充分] 或 [需要补充] 开头，这是系统解析你决策的唯一方式。"
                )
            else:
                # Scenario B: new card from user, PM does triage/breakdown
                return (
                    "这是一张新到达 organizing 列的卡片，你是 PM gatekeeper，负责拆解和分发。\n\n"
                    "请分析这张卡片的描述，做出以下判断：\n\n"
                    "1. 如果需求描述清晰、验收标准明确、无需额外调研 → 评论开头写 [调研充分]\n"
                    "   然后补充验收标准和技术要点，系统会将卡片推进到 dev 列\n\n"
                    "2. 如果需求涉及不确定因素（市场数据、竞品情况、技术可行性未知）→ 评论开头写 [需要补充]\n"
                    "   然后明确列出需要行业顾问调研的具体问题，系统会将卡片移到 research 列\n\n"
                    "3. 如果需求太大需要拆分 → 评论开头写 [需要补充]\n"
                    "   说明拆分建议，等 CEO 确认后再建子卡\n\n"
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
                "SELECT name, prefix, product_memory FROM projects WHERE id=?",
                (self.project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                sections.append(f"## 项目：{proj['name']} ({proj['prefix']})")
                if proj["product_memory"]:
                    sections.append(f"\n## 产品记忆\n\n{proj['product_memory']}")

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

    async def _call_model(self, prompt: str, timeout: int | None = None) -> str:
        cfg = self.role_config.model
        effective_timeout = timeout or cfg.timeout

        if cfg.provider == "hermes":
            return await self._call_hermes(prompt, cfg, effective_timeout)
        elif cfg.provider == "claude_cli":
            return await self._call_claude_cli(prompt, effective_timeout)
        else:
            raise RuntimeError(f"Unsupported provider: {cfg.provider}")

    async def _call_hermes(self, prompt: str, cfg, timeout: int) -> str:
        """Call hermes CLI as subprocess. Hermes manages its own tool loop."""
        import time as _time

        from web.hermes_chat import ensure_hermes_config, _build_hermes_env
        await ensure_hermes_config()

        toolsets = cfg.toolsets if hasattr(cfg, "toolsets") and cfg.toolsets else []
        skills = cfg.skills if hasattr(cfg, "skills") and cfg.skills else []
        cmd = ["hermes", "-z", prompt]
        if toolsets:
            cmd.extend(["-t", ",".join(toolsets)])
        if skills:
            cmd.extend(["--skills", ",".join(skills)])
        if cfg.name:
            cmd.extend(["--model", cfg.name])

        logger.info(f"Calling hermes: {' '.join(cmd[:4])}...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_hermes_env(),
        )

        async def _read_until(stream, deadline):
            chunks = []
            while True:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(stream.read(4096), timeout=max(remaining, 1))
                    if not chunk:
                        break
                    chunks.append(chunk)
                except asyncio.TimeoutError:
                    break
            return b"".join(chunks)

        deadline = _time.monotonic() + timeout
        stdout_task = asyncio.create_task(_read_until(proc.stdout, deadline))
        stderr_task = asyncio.create_task(_read_until(proc.stderr, deadline))
        stdout_data, stderr_data = await asyncio.gather(stdout_task, stderr_task)

        timed_out = proc.returncode is None
        if timed_out:
            proc.kill()

        await proc.wait()

        output = stdout_data.decode(errors="replace").strip()
        if timed_out:
            import re
            output = re.sub(r'<HermesTool:[^>]*>.*?</HermesTool[^>]*>', '', output, flags=re.DOTALL)
            output = re.sub(r'<HermesTool:[^>]*/>', '', output)
            output = re.sub(r'<HermesTool:[^>]*>.*$', '', output, flags=re.DOTALL)
            timeout_min = timeout // 60
            logger.warning(f"hermes timed out after {timeout}s, partial output: {output[:200]}")
            return (
                f"[转给PM]\n\n"
                f"[调研超时] 调研在 {timeout_min} 分钟时限内未完成。部分进展如下：\n\n"
                f"{output[:2000]}\n\n"
                f"---\n"
                f"⚠️ 以上为超时前部分结果。PM 请评估：1) 加时重试 2) 基于部分信息推进 3) 缩减调研范围"
            )

        if proc.returncode != 0:
            err = stderr_data.decode(errors="replace").strip()
            logger.warning(f"hermes exited {proc.returncode}: {err[:200]}")
            if not output:
                raise RuntimeError(f"hermes failed: {err[:300]}")
        import re
        output = re.sub(r'<HermesTool:[^>]*>.*?</HermesTool[^>]*>', '', output, flags=re.DOTALL)
        output = re.sub(r'<HermesTool:[^>]*/>', '', output)
        return output.strip()

    async def _call_claude_cli(self, prompt: str, timeout: int) -> str:
        """Call Claude CLI subprocess with kanban MCP tools."""
        import time as _time

        config_dir = ensure_agent_mcp_config(self.role_config.role, self.project_id)
        tools = ",".join(
            f"mcp__kanban__{t}" for t in self.role_config.allowed_tools
        )

        cmd = [
            "claude",
            "-p", prompt,
            "--print",
            "--allowedTools", tools,
            "--model", self.role_config.model.name or "claude-sonnet-4-6",
            "--append-system-prompt", self.role_config.system_prompt,
        ]

        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1"}
        if "API_KEY" in os.environ and "ANTHROPIC_AUTH_TOKEN" not in env:
            env["ANTHROPIC_AUTH_TOKEN"] = os.environ["API_KEY"]
        if "API_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in env:
            base = os.environ["API_BASE_URL"].rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            env["ANTHROPIC_BASE_URL"] = base

        role = self.role_config.role
        logger.info("CommentAgent(%s) starting CLI (timeout=%ds)", role, timeout)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=config_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        start = _time.monotonic()
        heartbeat_interval = 60

        try:
            while True:
                remaining = timeout - (_time.monotonic() - start)
                if remaining <= 0:
                    raise asyncio.TimeoutError()
                wait_time = min(remaining, heartbeat_interval)
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(), timeout=wait_time
                    )
                    break
                except asyncio.TimeoutError:
                    if proc.returncode is not None:
                        stdout, stderr = await proc.communicate()
                        break
                    elapsed = int(_time.monotonic() - start)
                    if elapsed < timeout:
                        logger.info(
                            "CommentAgent(%s) still running... %ds/%ds",
                            role, elapsed, timeout,
                        )
                    else:
                        raise
        except asyncio.TimeoutError:
            elapsed = int(_time.monotonic() - start)
            err_snippet = ""
            try:
                proc.terminate()
                _, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=5)
                err_snippet = stderr_data.decode(errors="replace").strip()[-500:]
            except Exception:
                proc.kill()
                await proc.wait()
            logger.error(
                "CommentAgent(%s) timed out after %ds. stderr: %s",
                role, elapsed, err_snippet[:300] or "(empty)",
            )
            raise RuntimeError(
                f"CommentAgent({role}) timed out after {elapsed}s"
                + (f" — {err_snippet[:200]}" if err_snippet else "")
            )

        elapsed = int(_time.monotonic() - start)
        output = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()

        if proc.returncode != 0:
            logger.warning(
                "CommentAgent(%s) CLI failed (exit=%d, %ds). stderr: %s",
                role, proc.returncode, elapsed, err[:300] or "(empty)",
            )
            if not output:
                raise RuntimeError(
                    f"CommentAgent({role}) exit {proc.returncode}: {err[:200] or 'no output'}"
                )
        else:
            if not output:
                logger.warning(
                    "CommentAgent(%s) CLI exited 0 but produced no output (%ds). stderr: %s",
                    role, elapsed, err[:300] or "(empty)",
                )
            else:
                logger.info("CommentAgent(%s) done (%ds, %d chars)", role, elapsed, len(output))

        return output
