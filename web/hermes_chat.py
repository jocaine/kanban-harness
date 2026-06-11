"""Hermes-based chat backend — subprocess bridge with context injection."""

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

from core.database import DB_PATH
from agents.registry import registry

logger = logging.getLogger("kh.web.hermes")

HERMES_BIN = os.getenv("HERMES_BIN", "hermes")
HERMES_MODEL = os.getenv("HERMES_MODEL", "")
HERMES_PROVIDER = os.getenv("HERMES_PROVIDER", "")
HERMES_TOOLSETS = os.getenv("HERMES_TOOLSETS", "")


def _api_key() -> str:
    return os.getenv("API_KEY", "") or os.getenv("OPENAI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")


def _api_base_url() -> str:
    return os.getenv("API_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "") or os.getenv("ANTHROPIC_BASE_URL", "")


CHAT_DIRECTIVES = """## 聊天专属指令

以下指令补充 PM prompt，仅在聊天界面生效（不在 PM agent 自动触发时生效）：
- 用户描述新想法/需求 → 创建一张 type='idea' 的想法卡（标题为想法摘要，用户原话作为 initial_comment）
- 用户说"你自己想/随便/你来决定/都行" → 基于产品记忆和项目上下文自主决策，给出方案并执行
- 用户闲聊/问问题 → 正常对话，结合项目上下文回答

原则：
1. 想法卡只记录 CEO 的原始意图，不要在聊天阶段拆解
2. 拆解由 PM agent 自动完成，保证每张子卡 type 正确（research/dev）
3. 创建想法卡后告知用户已收录，PM 会自动拆解
4. 想法卡的 title 是一句话摘要，CEO 原话放 initial_comment
"""


async def _get_chat_history(project_id: int, limit: int = 10) -> list[dict]:
    """Get recent chat messages for context injection into hermes prompt."""
    if not project_id:
        return []
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE project_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit * 2),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    rows.reverse()
    return rows


async def _get_chat_summary(project_id: int) -> str:
    """Get conversation summary if one exists."""
    if not project_id:
        return ""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT content FROM chat_messages "
            "WHERE project_id=? AND role='summary' "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        row = await cursor.fetchone()
    return row["content"] if row else ""


def _detect_role(user_message: str) -> str:
    """Detect which agent role best fits the user's message based on registry triggers.

    Priority: specific roles only match on strong signals; default is pm.
    """
    msg = user_message.lower()

    # coach_dev: explicit code/implementation keywords
    if any(kw in msg for kw in ("代码", "实现", "bug", "报错", "编译", "git", "commit", "重构", "接口设计", "函数", "debug", "部署")):
        return "coach_dev"

    # industry: only when explicitly asking for market/competitor research
    if any(kw in msg for kw in ("竞品分析", "市场调研", "行业趋势", "竞争对手", "市场规模")):
        return "industry"

    # coach_review: explicit QA/testing keywords
    if any(kw in msg for kw in ("测试用例", "验收标准", "code review", "质量检查", "回归测试")):
        return "coach_review"

    # Default: PM handles everything else (requirements, planning, general chat)
    return "pm"


def _get_role_prompt(role: str) -> str:
    """Load the real agent system prompt from YAML config."""
    agent = registry.get(role)
    if agent:
        return agent.system_prompt
    return f"你是 {role}。负责理解用户意图并主动执行。"


async def _build_hermes_prompt(project_id: int, user_message: str) -> tuple[str, dict]:
    """Build prompt with context. Returns (prompt_str, context_summary).

    context_summary contains factual info about what was loaded, for the route event.
    """
    sections = []
    summary = {}

    has_context = False

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if project_id:
            cursor = await db.execute(
                "SELECT name, description, prefix FROM projects WHERE id=?",
                (project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                summary["project"] = proj["name"]

                from core.wiki import get_wiki_for_prompt
                wiki_ctx = get_wiki_for_prompt(project_id)
                if wiki_ctx:
                    sections.append(f"\n## 项目知识库\n\n{wiki_ctx}")
                    has_context = True

            cursor = await db.execute(
                "SELECT v.id, v.name, v.status FROM versions v "
                "WHERE v.project_id=? AND v.status IN ('active','planning') "
                "ORDER BY v.position LIMIT 1",
                (project_id,),
            )
            active_ver = await cursor.fetchone()
            if active_ver:
                sections.append(f"\n## 活跃版本\n\n{active_ver['name']} (status: {active_ver['status']}, id: {active_ver['id']})")

            cursor = await db.execute(
                "SELECT r.code, r.title, r.status, r.priority "
                "FROM requirements r JOIN versions v ON r.version_id=v.id "
                "WHERE v.project_id=? AND r.archived=0 "
                "ORDER BY CASE r.status "
                "  WHEN 'dev' THEN 0 WHEN 'testing' THEN 1 "
                "  WHEN 'organizing' THEN 2 WHEN 'done' THEN 3 END, "
                "r.priority LIMIT 20",
                (project_id,),
            )
            reqs = await cursor.fetchall()
            if reqs:
                lines = ["\n## 当前看板状态\n"]
                for r in reqs:
                    lines.append(f"- [{r['code']}] {r['title']} ({r['status']}, {r['priority']})")
                sections.append("\n".join(lines))
                summary["cards"] = len(reqs)
        else:
            cursor = await db.execute(
                "SELECT id, name, prefix FROM projects WHERE archived=0 ORDER BY updated_at DESC LIMIT 5"
            )
            projects = await cursor.fetchall()
            if projects:
                lines = ["\n## 可用项目\n"]
                for p in projects:
                    lines.append(f"- [{p['prefix']}] {p['name']} (id: {p['id']})")
                sections.append("\n".join(lines))

    if not has_context:
        sections.append(
            "\n## 上下文提示\n\n"
            "当前项目尚未配置详细上下文。你可以通过 kanban MCP 工具获取更多信息：\n"
            "- 用 kanban_list_projects 查看可用项目\n"
            "- 用 kanban_wiki_list_pages 查看项目知识库\n"
            "- 用 kanban_list_requirements 查看当前需求\n"
            "如果用户要创建需求，先用 kanban_list_versions 找到活跃版本再创建。"
        )

    # Determine role and inject its system prompt (real agent YAML)
    role = _detect_role(user_message)
    role_prompt = _get_role_prompt(role)
    sections.insert(0, role_prompt)

    # Append chat-specific directives (complement role prompt, not duplicate it)
    sections.append(CHAT_DIRECTIVES)

    # Inject conversation history (last 10 exchanges, truncated)
    history = await _get_chat_history(project_id)
    if history:
        sections.append("\n## 近期对话\n")
        for msg in history:
            prefix = "用户" if msg["role"] == "user" else "AI"
            content = msg["content"][:500]
            sections.append(f"**{prefix}:** {content}")

    conv_summary = await _get_chat_summary(project_id)
    if conv_summary:
        sections.append(f"\n## 对话摘要（更早的讨论）\n\n{conv_summary}")

    sections.append(f"\n## 用户消息\n\n{user_message}")

    return "\n".join(sections), summary


async def stream_hermes(project_id: int, user_message: str) -> AsyncGenerator[str, None]:
    """Run hermes -z with context-injected prompt and stream output as SSE events."""
    t_start = time.time()

    # 1. Route: detect role (real architectural decision based on registry)
    role = _detect_role(user_message)
    logger.info("[hermes] 路由=%s 消息=%r project=%d", role, user_message[:60], project_id)
    yield f"data: {json.dumps({'type': 'route', 'role': role}, ensure_ascii=False)}\n\n"

    # 2. Build prompt (instant — DB queries are <50ms)
    prompt, ctx_summary = await _build_hermes_prompt(project_id, user_message)
    logger.info("[hermes] 提示词构建完成: %d 字符, context=%s", len(prompt), json.dumps(ctx_summary, ensure_ascii=False))

    # 3. Signal: context loaded, now waiting for AI
    yield f"data: {json.dumps({'type': 'status', 'state': 'waiting', 'context': ctx_summary}, ensure_ascii=False)}\n\n"

    # 4. Spawn hermes and stream real output
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="kh_prompt_")
    try:
        tmp.write(prompt)
        tmp.close()

        # Always pass -m explicitly: hermes auto-detects provider from model name
        # (claude-* → anthropic), bypassing "custom" provider's non-TTY output bug
        import re as _re
        effective_model = HERMES_MODEL or os.getenv("CHAT_MODEL", "claude-sonnet-4-6")
        effective_model = _re.sub(r'\[.*?\]', '', effective_model).strip()
        flags = f" -m {effective_model}"
        if HERMES_PROVIDER:
            flags += f" --provider {HERMES_PROVIDER}"
        if HERMES_TOOLSETS:
            flags += f" -t {HERMES_TOOLSETS}"

        shell_cmd = f'{HERMES_BIN} -z "$(cat {tmp.name})"{flags} --yolo'
        logger.info("[hermes] 启动进程: %s", shell_cmd[:120])

        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_hermes_env(),
        )
        logger.info("[hermes] 进程已启动 pid=%d", proc.pid)

        # Parse stderr for real hermes startup signals (MCP connection, plugin loading)
        stderr_lines = []

        async def _read_stderr():
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if text:
                    stderr_lines.append(text)
                    # Log MCP-related and error messages at higher level
                    if "MCP" in text or "ERROR" in text or "WARN" in text or "failed" in text.lower():
                        logger.warning("[hermes:stderr] %s", text)
                    else:
                        logger.debug("[hermes:stderr] %s", text)

        stderr_task = asyncio.create_task(_read_stderr())

        byte_buf = b""
        line_buf = ""
        first_output = False
        total_lines = 0
        tool_calls_seen = 0
        while True:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), timeout=0.5)
            except asyncio.TimeoutError:
                # Log if we've been waiting too long with no output
                elapsed = time.time() - t_start
                if not first_output and elapsed > 30 and int(elapsed) % 30 == 0:
                    logger.warning("[hermes] %.0f秒无输出, 仍在等待 (pid=%d)", elapsed, proc.pid)
                continue
            if not chunk:
                break

            if not first_output:
                first_output = True
                t_first = time.time() - t_start
                logger.info("[hermes] 首次输出耗时 %.1f秒", t_first)
                yield f"data: {json.dumps({'type': 'status', 'state': 'streaming'}, ensure_ascii=False)}\n\n"

            byte_buf += chunk

            try:
                text = byte_buf.decode("utf-8")
                byte_buf = b""
            except UnicodeDecodeError:
                for i in range(1, 4):
                    try:
                        text = byte_buf[:-i].decode("utf-8")
                        byte_buf = byte_buf[-i:]
                        break
                    except UnicodeDecodeError:
                        continue
                else:
                    continue

            line_buf += text
            while "\n" in line_buf:
                line, line_buf = line_buf.split("\n", 1)
                total_lines += 1
                event = _parse_hermes_line(line)
                if event:
                    if event["type"] == "tool_start":
                        tool_calls_seen += 1
                        logger.info("[hermes] 工具调用 #%d: %s", tool_calls_seen, event.get("name", "?"))
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        # Flush remaining
        if byte_buf:
            line_buf += byte_buf.decode("utf-8", errors="replace")
        if line_buf.strip():
            event = _parse_hermes_line(line_buf)
            if event:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass

        await proc.wait()
        t_total = time.time() - t_start

        # Log stderr summary for diagnostics
        mcp_lines = [l for l in stderr_lines if "MCP" in l or "mcp" in l]
        if mcp_lines:
            logger.info("[hermes] MCP stderr 摘要: %s", "; ".join(mcp_lines[-3:]))

        if proc.returncode != 0:
            stderr_out = await proc.stderr.read()
            err_msg = stderr_out.decode("utf-8", errors="replace").strip()
            logger.error("[hermes] 退出 code=%d, 耗时 %.1f秒: %s", proc.returncode, t_total, err_msg[:200])
            if err_msg:
                yield f"data: {json.dumps({'type': 'error', 'content': f'hermes error: {err_msg}'}, ensure_ascii=False)}\n\n"
        else:
            logger.info(
                "[hermes] 完成: 总耗时 %.1f秒, %d 行, %d 次工具调用, exit=0",
                t_total, total_lines, tool_calls_seen,
            )

    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"


def _parse_hermes_line(line: str) -> dict | None:
    """Parse a line of hermes output into an SSE event."""
    stripped = line.strip()
    if not stripped:
        return None

    # Detect tool use patterns (hermes outputs these when using tools)
    if stripped.startswith("🔧") or stripped.startswith("[Tool:") or stripped.startswith("Using tool:"):
        tool_name = stripped.split(":", 1)[-1].strip().rstrip(".")
        return {"type": "tool_start", "name": tool_name}

    if stripped.startswith("✓") or stripped.startswith("[Done:") or stripped.startswith("Tool result:"):
        return {"type": "tool_done", "name": ""}

    # Regular text content
    return {"type": "text", "content": line + "\n"}


async def check_hermes_available() -> bool:
    """Check if hermes binary is available and functional."""
    try:
        proc = await asyncio.create_subprocess_exec(
            HERMES_BIN, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode == 0 and b"Hermes" in stdout
    except (FileNotFoundError, OSError):
        return False


def _build_hermes_env() -> dict:
    """Build environment for hermes subprocess.

    Passes API key/base_url to hermes via env vars.
    Users only need to set API_KEY + API_BASE_URL in .env or docker-compose.
    """
    env = {**os.environ, "NO_COLOR": "1", "TERM": "dumb"}
    key = _api_key()
    base_url = _api_base_url()
    if key:
        env["OPENAI_API_KEY"] = key
        env["ANTHROPIC_API_KEY"] = key
    if base_url:
        env["OPENAI_BASE_URL"] = base_url
        # Anthropic SDK appends /v1/messages to base_url, so strip trailing /v1
        anthropic_base = base_url.rstrip("/")
        if anthropic_base.endswith("/v1"):
            anthropic_base = anthropic_base[:-3]
        env["ANTHROPIC_BASE_URL"] = anthropic_base
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
    if tavily_key:
        env["TAVILY_API_KEY"] = tavily_key
    searxng_url = os.getenv("SEARXNG_URL", "").strip()
    if searxng_url:
        env["SEARXNG_URL"] = searxng_url
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    if firecrawl_key:
        env["FIRECRAWL_API_KEY"] = firecrawl_key
    firecrawl_url = os.getenv("FIRECRAWL_API_URL", "").strip()
    if firecrawl_url:
        env["FIRECRAWL_API_URL"] = firecrawl_url
    return env


async def ensure_hermes_config(mode: str = "chat"):
    """Ensure hermes config exists and MCP points to local KH server.

    Called on KH startup. Always syncs model/base_url from env vars,
    and patches mcp_servers.kanban to point to the local MCP server.

    Args:
        mode: "chat" uses server.py (high-level intent tools for CEO interaction),
              "agent" uses agent_server.py (granular atomic tools for agent roles).
    """
    import yaml

    config_dir = Path.home() / ".hermes"
    config_file = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)

    import sys
    if mode == "agent":
        mcp_server_path = str(Path(__file__).resolve().parent.parent / "mcp_server" / "agent_server.py")
        local_mcp = {
            "type": "stdio",
            "command": sys.executable,
            "args": [mcp_server_path],
            "env": {
                "DB_PATH": os.getenv("DB_PATH", "data/kanban.db"),
                "KH_AGENT_ROLE": "industry",
                "KH_PROJECT_ID": str(os.getenv("KH_PROJECT_ID", "0")),
            },
        }
    else:
        mcp_server_path = str(Path(__file__).resolve().parent.parent / "mcp_server" / "server.py")
        port = os.getenv("PORT", "8000")
        local_mcp = {
            "type": "stdio",
            "command": sys.executable,
            "args": [mcp_server_path],
            "env": {"KH_BASE_URL": f"http://localhost:{port}"},
        }

    # Always read model/base_url from env
    # Strip bracket suffixes like [1M] — context window hints not recognized by OpenAI-compat proxies
    import re as _re
    model = HERMES_MODEL or os.getenv("CHAT_MODEL", "claude-sonnet-4-6")
    model = _re.sub(r'\[.*?\]', '', model).strip()

    if config_file.exists():
        with open(config_file, "r") as f:
            config = yaml.safe_load(f) or {}
    else:
        config = {
            "_config_version": 1,
            "agent": {"max_turns": 30, "reasoning_effort": "medium"},
            "approvals": {"mode": "yolo"},
            "toolsets": ["hermes-cli"],
        }

    # Always sync model settings from env
    config.setdefault("model", {})
    config["model"]["default"] = model
    if _api_base_url():
        # Custom provider uses OpenAI SDK which needs /v1 in base_url
        base = _api_base_url().rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        config["model"]["base_url"] = base
        config["model"]["provider"] = "custom"

    # Always patch MCP server
    config.setdefault("mcp_servers", {})
    config["mcp_servers"]["kanban"] = local_mcp

    # Auto-select search/extract backends based on available credentials
    # firecrawl-lite only supports scrape (extract), NOT search.
    # Search priority: searxng > tavily > clear ddgs
    config.setdefault("web", {})
    firecrawl_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    firecrawl_url = os.getenv("FIRECRAWL_API_URL", "").strip()
    tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
    searxng_url = os.getenv("SEARXNG_URL", "").strip()
    if firecrawl_key or firecrawl_url:
        config["web"]["extract_backend"] = "firecrawl"
    if searxng_url:
        config["web"]["search_backend"] = "searxng"
    elif tavily_key:
        config["web"]["search_backend"] = "tavily"
    elif config["web"].get("search_backend") == "ddgs":
        config["web"]["search_backend"] = ""

    # Fix SSRF false positive: local DNS proxy resolves all domains to 198.18.x.x
    # (private range), causing web_extract to block all URLs. Allow private URLs
    # when running behind such a proxy.
    config.setdefault("security", {})
    config["security"]["allow_private_urls"] = True

    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"已确保 hermes 配置使用本地 MCP: {config_file}")


def sync_claude_settings():
    """Sync API env vars into project .claude/settings.json for Claude Code.

    Reads API_KEY / API_BASE_URL / CHAT_MODEL / ANTHROPIC_* from environment
    and writes them into the project's .claude/settings.json so that Claude Code
    sessions and subprocesses inherit the same API configuration as the container.
    """
    project_root = Path(__file__).resolve().parent.parent
    claude_dir = project_root / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_file = claude_dir / "settings.json"

    # Gather env vars that matter for both hermes (OpenAI-compat) and anthropic SDK
    env = {}
    for key in ("API_KEY", "API_BASE_URL", "CHAT_MODEL",
                "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL"):
        val = os.getenv(key)
        if val:
            env[key] = val

    if not env:
        logger.warning("sync_claude_settings: 未找到 API 环境变量, 跳过")
        return

    # Read existing settings
    if settings_file.exists():
        with open(settings_file) as f:
            settings = json.load(f)
    else:
        settings = {"permissions": {"allow": []}}

    # Merge env — do NOT overwrite keys already present (manual config wins)
    existing_env = settings.get("env", {})
    merged = {**env, **existing_env}  # existing_env takes priority
    settings["env"] = merged

    with open(settings_file, "w") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
        f.write("\n")

    logger.info("已同步 %d 个环境变量到 %s", len(merged), settings_file)
