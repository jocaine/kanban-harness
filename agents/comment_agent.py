"""Comment Agent — generates review comments via Hermes CLI, Claude CLI, or LLM API."""

import asyncio
import logging
import os
import re
from pathlib import Path

from agents.registry import registry, AgentRole
from agents.mcp_config import ensure_agent_mcp_config

logger = logging.getLogger("kh.agent.comment")


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~2 chars/token for CJK, ~4 for Latin."""
    cjk = sum(1 for c in text if '一' <= c <= '鿿')
    other = len(text) - cjk
    return cjk // 2 + other // 4 + 1


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

    async def execute(self, card: dict, existing_comments: list[dict] | None = None,
                      on_heartbeat=None, on_process_started=None) -> dict:
        """Generate a review comment for the given card.

        Args:
            on_heartbeat: Optional callback invoked when agent produces output (for stall detection).

        Returns: {"success": bool, "comment": str, "detail": str, "summary": str, "tokens": dict}
        """
        prompt = await self._build_prompt(card, existing_comments or [])
        try:
            if self.role_config.model.provider == "hermes":
                effective_timeout = card.get("agent_timeout") or self.role_config.model.timeout
            else:
                effective_timeout = self.role_config.model.timeout
            response, usage = await self._call_model(prompt, timeout=effective_timeout, on_heartbeat=on_heartbeat, on_process_started=on_process_started, requirement_id=card.get("id"))

            if usage:
                tokens = {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "total": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
                }
            else:
                input_tokens = _estimate_tokens(prompt)
                output_tokens = _estimate_tokens(response)
                tokens = {"input": input_tokens, "output": output_tokens, "total": input_tokens + output_tokens}

            # PM uses tools directly (add_comment + move_requirement);
            # stdout is irrelevant — harness checks DB for actual results.
            if self.role_config.role == "pm" and self.role_config.model.provider == "claude_cli":
                return {
                    "task_done": True,
                    "signal": "",
                    "success": True,
                    "comment": "",
                    "detail": "",
                    "summary": f"{self.role_config.display_name} reviewed [{card.get('code', '')}]",
                    "tokens": tokens,
                }

            comment, detail = self._split_detail(response)
            return {
                "task_done": bool(comment),
                "signal": "",
                "success": bool(comment),
                "comment": comment,
                "detail": detail,
                "summary": f"{self.role_config.display_name} reviewed [{card.get('code', '')}]",
                "tokens": tokens,
            }
        except Exception as e:
            logger.error(f"CommentAgent({self.role_config.role}) 失败: {e}")
            return {"task_done": False, "signal": "", "success": False, "comment": "", "detail": "", "summary": str(e)}

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
                    "模型输出缺少 DETAIL_SEPARATOR (%d 字符), 启发式分割位置 %d",
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

    def _load_skill(self, skill_name: str) -> str:
        """Load skill file content for prompt injection, stripping frontmatter."""
        skill_path = Path(__file__).parent.parent / "skills" / "pm" / skill_name / "SKILL.md"
        if not skill_path.exists():
            logger.warning("技能文件未找到: %s", skill_path)
            return ""
        content = skill_path.read_text(encoding="utf-8")
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                content = content[end + 3:].strip()
        return content

    def _build_suffix(self, card: dict, comments: list[dict]) -> str:
        """Build role-specific instruction suffix based on card state."""
        role = self.role_config.role
        status = card.get("status", "")
        card_type = card.get("type", "dev")

        # PM decomposing idea cards
        if role == "pm" and status == "organizing" and card_type == "idea":
            card_id = card.get("id", "")
            return (
                "## 你的任务\n\n"
                "这是一张**想法卡**（CEO 的原始想法），你需要将它拆解为具体的执行卡片。\n\n"
                "**操作步骤：**\n"
                "1. 阅读 CEO 的原始想法（在评论中）\n"
                f"2. 调用 `create_requirements(version_id, requirements, parent_id={card_id})` 创建 1-N 张子卡片，每张必须设置正确的 type：\n"
                "   - 需要调研（市场数据、技术可行性未知、竞品情况）→ type='research'\n"
                "   - 需求明确可直接开发 → type='dev'\n"
                f"3. **必须传 parent_id={card_id}**（当前想法卡的 ID），建立派生关系\n"
                "4. 每张子卡包含：title、description（功能目标+验收标准）、priority、type\n"
                "5. 全部子卡创建完成后，调用 `decide(target=done)` 关闭这张想法卡\n\n"
                "**注意：**\n"
                "- 想法卡是元卡片，不进入开发流程，拆解完即关闭\n"
                "- 子卡的 type 决定后续流转（research 触发行业顾问调研，dev 进入开发）\n"
                "- 你必须通过工具完成操作，不要只输出文字。"
            )

        # PM in organizing: two scenarios
        if role == "pm" and status == "organizing":
            has_industry = any(c.get("author") == "行业顾问" for c in comments)
            if has_industry:
                skill_content = self._load_skill("pm-research-audit")
                card_id = card.get("id", "")
                return (
                    "## 你的任务\n\n"
                    "你正在评估行业顾问的调研结果。请判断调研材料是否足够支撑开发决策。\n\n"
                    f"{skill_content}\n\n"
                    "**操作步骤：**\n\n"
                    "**情况 A：调研结论充分，可派生开发卡**\n"
                    "1. 在评论中写出结构化审计结论（可靠性判断+关键发现）\n"
                    f"2. 调用 `create_requirements(version_id, requirements, parent_id={card_id})` "
                    "创建 dev 子卡，每张子卡的 description 中引用调研结论作为技术依据\n"
                    "3. 子卡 type='dev'，parent_id 指向本调研卡，形成派生链\n"
                    "4. 全部子卡创建完成后，调用 `decide(target=done)` 关闭本调研卡\n\n"
                    "**情况 B：调研结论不充分，需补充**\n"
                    "- 调用 `decide(target=research)` 退回给行业顾问继续调研\n\n"
                    "**情况 C：调研结论表明不值得开发**\n"
                    "- 调用 `ask_ceo()` 上报 CEO 决策是否放弃\n\n"
                    "**派生逻辑说明：**\n"
                    "调研卡完成后派生的 dev 子卡通过 parent_id 关联到调研卡，"
                    "卡片链的树结构天然表达执行先后顺序（先调研后开发），无需额外依赖标记。\n\n"
                    "**注意：** 你必须通过工具完成操作，不要只输出文字。"
                )
            else:
                return (
                    "## 你的任务\n\n"
                    "这是一张新到达 organizing 列的卡片，你是 PM gatekeeper，负责拆解和分发。\n\n"
                    "请分析这张卡片的描述，做出以下判断：\n\n"
                    "1. 需求描述清晰、验收标准明确、无需额外调研 → 移到 `dev`\n"
                    "2. 需求涉及不确定因素（市场数据、竞品情况、技术可行性未知）→ 移到 `research`\n"
                    "3. 需求太大需要拆分 → 移到 `research`，评论中说明拆分建议\n\n"
                    "**操作步骤：**\n"
                    "调用决策工具: decide(target=dev/done 通过) 或 decide(target=research 退回) 或 ask_ceo(问CEO)\n"
                    "（决策工具会自动移动卡片）\n\n"
                    "**注意：** 你必须通过工具完成操作，不要只输出文字。"
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
                "2. 调用 ask_ceo(requirement_id, comment, question)\n"
                "3. 调用 decide(requirement_id, comment, target='organizing', detail)\n\n"
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
                "SELECT name, prefix FROM projects WHERE id=?",
                (self.project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                sections.append(f"## 项目：{proj['name']} ({proj['prefix']})")

            # Wiki context
            from core.wiki import get_wiki_for_prompt
            wiki_ctx = get_wiki_for_prompt(self.project_id)
            if wiki_ctx:
                sections.append(f"\n## 项目知识库\n\n{wiki_ctx}")

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

    async def _call_model(self, prompt: str, timeout: int | None = None, on_heartbeat=None, on_process_started=None, requirement_id: int | None = None) -> tuple[str, dict]:
        """Returns (response_text, usage_dict)."""
        cfg = self.role_config.model
        effective_timeout = timeout or cfg.timeout

        if cfg.provider == "hermes":
            text = await self._call_hermes(prompt, cfg, effective_timeout, on_heartbeat, requirement_id=requirement_id)
            return text, {}
        elif cfg.provider == "claude_cli":
            return await self._call_claude_cli(prompt, effective_timeout, on_heartbeat, on_process_started)
        else:
            raise RuntimeError(f"Unsupported provider: {cfg.provider}")

    async def _call_hermes(self, prompt: str, cfg, timeout: int, on_heartbeat=None, requirement_id: int | None = None) -> str:
        """Call hermes AIAgent in-process with MCP tools and streaming heartbeat.

        Key: discover_mcp_tools() must be called before AIAgent creation so the
        MCP server (agent_server.py) is connected and its tools are registered.
        Using enabled_toolsets=None picks up all configured tools including MCP.
        Streaming callbacks fire heartbeat on every token/tool event.
        """
        from web.hermes_chat import ensure_hermes_config, _api_key, _api_base_url
        await ensure_hermes_config(mode="agent")

        model_name = cfg.name or os.getenv("CHAT_MODEL", "claude-sonnet-4-6")
        model_name = re.sub(r'\[.*?\]', '', model_name).strip()

        api_key = _api_key()
        base_url = _api_base_url()
        provider = None
        if base_url:
            base = base_url.rstrip("/")
            if not base.endswith("/v1"):
                base += "/v1"
            base_url = base
            provider = "custom"

        logger.info("调用 hermes AIAgent (进程内): model=%s", model_name)

        def _heartbeat_cb(*_args, **_kwargs):
            if on_heartbeat:
                on_heartbeat()

        _tool_heartbeat_timer = [None]  # mutable ref for closure

        def _tool_keepalive():
            """Periodic heartbeat every 60s while a tool is executing."""
            _heartbeat_cb()
            import threading
            _tool_heartbeat_timer[0] = threading.Timer(60, _tool_keepalive)
            _tool_heartbeat_timer[0].daemon = True
            _tool_heartbeat_timer[0].start()

        def _tool_start_cb(*_args, **_kwargs):
            _heartbeat_cb()
            import threading
            _tool_heartbeat_timer[0] = threading.Timer(60, _tool_keepalive)
            _tool_heartbeat_timer[0].daemon = True
            _tool_heartbeat_timer[0].start()

        def _tool_complete_cb(*_args, **_kwargs):
            if _tool_heartbeat_timer[0]:
                _tool_heartbeat_timer[0].cancel()
                _tool_heartbeat_timer[0] = None
            _heartbeat_cb()

        def _clarify_cb(question, choices=None):
            if choices:
                return f"[No user available. Pick the best option from {choices} and continue.]"
            return "[No user available. Make a reasonable assumption and continue.]"

        def _run_agent():
            import logging as _logging
            _logging.disable(_logging.WARNING)
            try:
                os.environ["HERMES_YOLO_MODE"] = "1"
                os.environ["HERMES_ACCEPT_HOOKS"] = "1"
                os.environ["KH_AGENT_ROLE"] = self.role_config.role
                os.environ["KH_PROJECT_ID"] = str(self.project_id)
                os.environ["DB_PATH"] = os.path.abspath(os.getenv("DB_PATH", "data/kanban.db"))
                from tools.mcp_tool import discover_mcp_tools
                discover_mcp_tools()
                from run_agent import AIAgent
                agent = AIAgent(
                    api_key=api_key,
                    base_url=base_url,
                    provider=provider,
                    model=model_name,
                    enabled_toolsets=None,
                    quiet_mode=True,
                    platform="cli",
                    stream_delta_callback=_heartbeat_cb,
                    tool_start_callback=_tool_start_cb,
                    tool_complete_callback=_tool_complete_cb,
                    tool_gen_callback=_heartbeat_cb,
                    clarify_callback=_clarify_cb,
                )
                agent.suppress_status_output = True
                result = agent.chat(prompt) or ""
                # If agent produced research but didn't call decision tool,
                # nudge it with a focused follow-up
                if self.role_config.role == "industry" and requirement_id:
                    import sqlite3 as _sqlite3
                    _conn = _sqlite3.connect(os.getenv("DB_PATH", "data/kanban.db"))
                    _conn.row_factory = _sqlite3.Row
                    _cur = _conn.execute("SELECT status FROM requirements WHERE id=?",
                                         (requirement_id,))
                    _row = _cur.fetchone()
                    _conn.close()
                    if _row and _row["status"] == "research":
                        logger.info("CommentAgent(industry) 推动 agent 调用决策工具")
                        result = agent.chat(
                            "调研阶段已结束，请将你的调研结论通过 mcp_kanban_decide 工具提交。"
                            " requirement_id 是 %d。" % requirement_id
                        ) or ""
                return result
            finally:
                if _tool_heartbeat_timer[0]:
                    _tool_heartbeat_timer[0].cancel()
                _logging.disable(_logging.NOTSET)

        loop = asyncio.get_event_loop()
        try:
            output = await asyncio.wait_for(
                loop.run_in_executor(None, _run_agent),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.error("CommentAgent(%s) hermes 超时 (%d秒)", self.role_config.role, timeout)
            raise TimeoutError(f"hermes agent exceeded {timeout}s timeout")

        logger.info("CommentAgent(%s) 原始输出长度=%d, 前500: %s",
                    self.role_config.role, len(output), output[:500])

        output = re.sub(r'<HermesTool:[^>]*>.*?</HermesTool[^>]*>', '', output, flags=re.DOTALL)
        output = re.sub(r'<HermesTool:[^>]*/>', '', output)
        return output.strip()

    async def _call_claude_cli(self, prompt: str, timeout: int, on_heartbeat=None, on_process_started=None) -> str:
        """Call Claude CLI subprocess with kanban MCP tools.

        Uses --output-format stream-json: each stdout line is a heartbeat signal.
        Stall detection triggers only when the process truly stops producing output.
        """
        import json as _json
        import time as _time

        config_dir = ensure_agent_mcp_config(self.role_config.role, self.project_id)
        tools = ",".join(
            f"mcp__kanban__{t}" for t in self.role_config.allowed_tools
        )

        cmd = [
            "claude",
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--allowedTools", tools,
            "--model", self.role_config.model.name or "claude-sonnet-4-6",
            "--append-system-prompt", self.role_config.system_prompt,
        ]

        env = {**os.environ, "CLAUDE_CODE_DISABLE_NONESSENTIAL": "1"}
        if "API_KEY" in os.environ and "ANTHROPIC_API_KEY" not in env:
            env["ANTHROPIC_API_KEY"] = os.environ["API_KEY"]
        if "API_BASE_URL" in os.environ and "ANTHROPIC_BASE_URL" not in env:
            base = os.environ["API_BASE_URL"].rstrip("/")
            if base.endswith("/v1"):
                base = base[:-3]
            env["ANTHROPIC_BASE_URL"] = base

        role = self.role_config.role
        logger.info("CommentAgent(%s) 启动 CLI (超时=%d秒)", role, timeout)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=config_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        if on_process_started:
            on_process_started(proc)

        if on_heartbeat:
            on_heartbeat()

        result_text = ""
        usage = {}
        start_t = _time.monotonic()

        try:
            async for line in proc.stdout:
                if on_heartbeat:
                    on_heartbeat()

                elapsed = _time.monotonic() - start_t
                if elapsed > timeout:
                    raise asyncio.TimeoutError()

                line_str = line.decode(errors="replace").strip()
                if not line_str:
                    continue

                try:
                    event = _json.loads(line_str)
                except (ValueError, TypeError):
                    continue

                if event.get("type") == "result":
                    usage = event.get("usage", {})
                    result_text = event.get("result", "")

            await proc.wait()

        except asyncio.TimeoutError:
            elapsed = int(_time.monotonic() - start_t)
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                proc.kill()
                await proc.wait()
            logger.error("CommentAgent(%s) 超时 (%d秒)", role, elapsed)
            raise RuntimeError(f"CommentAgent({role}) timed out after {elapsed}s")

        elapsed = int(_time.monotonic() - start_t)
        if proc.returncode != 0:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            logger.warning(
                "CommentAgent(%s) CLI 失败 (exit=%d, %d秒). stderr: %s",
                role, proc.returncode, elapsed, err[:300] or "(空)",
            )
            if not result_text:
                raise RuntimeError(
                    f"CommentAgent({role}) exit {proc.returncode}: {err[:200] or 'no output'}"
                )
        else:
            logger.info("CommentAgent(%s) 完成 (%d秒)", role, elapsed)

        return result_text, usage
