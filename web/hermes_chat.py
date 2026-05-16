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

logger = logging.getLogger("kh.hermes")

HERMES_BIN = os.getenv("HERMES_BIN", "hermes")
HERMES_MODEL = os.getenv("HERMES_MODEL", "")
HERMES_PROVIDER = os.getenv("HERMES_PROVIDER", "")
HERMES_TOOLSETS = os.getenv("HERMES_TOOLSETS", "")

# LLM API configuration — users set these in .env or docker-compose environment
# Supports both OpenAI-compatible and Anthropic native APIs
# hermes uses OpenAI SDK internally, so OPENAI_* vars are the primary path
API_KEY = os.getenv("API_KEY", "") or os.getenv("OPENAI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
API_BASE_URL = os.getenv("API_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "") or os.getenv("ANTHROPIC_BASE_URL", "")


CHAT_DIRECTIVES = """## 聊天专属指令

以下指令补充 PM prompt，仅在聊天界面生效（不在 PM agent 自动触发时生效）：
- 用户描述新想法/需求 → 直接拆解为多张结构化卡片，调用 kanban MCP 创建
- 用户说"你自己想/随便/你来决定/都行" → 基于产品记忆和项目上下文自主决策，给出方案并执行
- 用户闲聊/问问题 → 正常对话，结合项目上下文回答

原则：
1. 宁可先行动再让用户调整，也不要反复追问细节
2. 拆解需求时用 MVP 思维：先拆最小可用版本，不过度设计
3. 每张卡片包含：title、description（功能目标+验收标准）、priority
4. 创建卡片后告知用户创建了什么，让用户可以微调
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
                "SELECT name, description, prefix, advisor_skill, product_memory FROM projects WHERE id=?",
                (project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                summary["project"] = proj["name"]

                if proj["advisor_skill"]:
                    skill = proj["advisor_skill"][:2000]
                    sections.append(f"\n## 产品顾问知识\n\n{skill}")
                    has_context = True

                if proj["product_memory"]:
                    memory = proj["product_memory"][:1500]
                    sections.append(f"\n## 产品记忆（决策历史）\n\n{memory}")
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
                "  WHEN 'pending' THEN 2 WHEN 'done' THEN 3 END, "
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
            "- 用 kanban_get_advisor_skill 获取产品顾问知识\n"
            "- 用 kanban_get_product_memory 获取产品记忆\n"
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
    logger.info("[hermes] route=%s msg=%r project=%d", role, user_message[:60], project_id)
    yield f"data: {json.dumps({'type': 'route', 'role': role}, ensure_ascii=False)}\n\n"

    # 2. Build prompt (instant — DB queries are <50ms)
    prompt, ctx_summary = await _build_hermes_prompt(project_id, user_message)
    logger.info("[hermes] prompt built: %d chars, context=%s", len(prompt), json.dumps(ctx_summary, ensure_ascii=False))

    # 3. Signal: context loaded, now waiting for AI
    yield f"data: {json.dumps({'type': 'status', 'state': 'waiting', 'context': ctx_summary}, ensure_ascii=False)}\n\n"

    # 4. Spawn hermes and stream real output
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="kh_prompt_")
    try:
        tmp.write(prompt)
        tmp.close()

        flags = ""
        if HERMES_MODEL:
            flags += f" -m {HERMES_MODEL}"
        if HERMES_PROVIDER:
            flags += f" --provider {HERMES_PROVIDER}"
        if HERMES_TOOLSETS:
            flags += f" -t {HERMES_TOOLSETS}"

        shell_cmd = f'{HERMES_BIN} -z "$(cat {tmp.name})"{flags} --yolo'
        logger.info("[hermes] spawning: %s", shell_cmd[:120])

        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_hermes_env(),
        )
        logger.info("[hermes] process started pid=%d", proc.pid)

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
                    logger.warning("[hermes] no output after %.0fs, still waiting (pid=%d)", elapsed, proc.pid)
                continue
            if not chunk:
                break

            if not first_output:
                first_output = True
                t_first = time.time() - t_start
                logger.info("[hermes] first output after %.1fs", t_first)
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
                        logger.info("[hermes] tool_call #%d: %s", tool_calls_seen, event.get("name", "?"))
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
            logger.info("[hermes] MCP stderr summary: %s", "; ".join(mcp_lines[-3:]))

        if proc.returncode != 0:
            stderr_out = await proc.stderr.read()
            err_msg = stderr_out.decode("utf-8", errors="replace").strip()
            logger.error("[hermes] exited code=%d after %.1fs: %s", proc.returncode, t_total, err_msg[:200])
            if err_msg:
                yield f"data: {json.dumps({'type': 'error', 'content': f'hermes error: {err_msg}'}, ensure_ascii=False)}\n\n"
        else:
            logger.info(
                "[hermes] done: %.1fs total, %d lines, %d tool_calls, exit=0",
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
    if API_KEY:
        env["OPENAI_API_KEY"] = API_KEY
    if API_BASE_URL:
        env["OPENAI_BASE_URL"] = API_BASE_URL
    return env


async def ensure_hermes_config():
    """Ensure hermes config exists and MCP points to local KH server.

    Called on KH startup. Always syncs model/base_url from env vars,
    and patches mcp_servers.kanban to point to the local MCP server.
    """
    import yaml

    config_dir = Path.home() / ".hermes"
    config_file = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)

    import sys
    mcp_server_path = str(Path(__file__).resolve().parent.parent / "mcp_server" / "server.py")
    port = os.getenv("PORT", "8000")
    local_mcp = {
        "type": "stdio",
        "command": sys.executable,
        "args": [mcp_server_path],
        "env": {"KH_BASE_URL": f"http://localhost:{port}"},
    }

    # Always read model/base_url from env
    model = HERMES_MODEL or os.getenv("CHAT_MODEL", "claude-sonnet-4-6")

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
    if API_BASE_URL:
        config["model"]["base_url"] = API_BASE_URL

    # Always patch MCP server
    config.setdefault("mcp_servers", {})
    config["mcp_servers"]["kanban"] = local_mcp

    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"Ensured hermes config with local MCP at {config_file}")


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
        logger.warning("sync_claude_settings: no API env vars found, skipping")
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

    logger.info("Synced %d env vars into %s", len(merged), settings_file)
