"""Hermes-based chat backend — subprocess bridge with context injection."""

import asyncio
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite

from core.database import DB_PATH

logger = logging.getLogger(__name__)

HERMES_BIN = os.getenv("HERMES_BIN", "hermes")
HERMES_MODEL = os.getenv("HERMES_MODEL", "")
HERMES_PROVIDER = os.getenv("HERMES_PROVIDER", "")
HERMES_TOOLSETS = os.getenv("HERMES_TOOLSETS", "")

# LLM API configuration — users set these in .env or docker-compose environment
# Supports both OpenAI-compatible and Anthropic native APIs
# hermes uses OpenAI SDK internally, so OPENAI_* vars are the primary path
API_KEY = os.getenv("API_KEY", "") or os.getenv("OPENAI_API_KEY", "") or os.getenv("ANTHROPIC_API_KEY", "")
API_BASE_URL = os.getenv("API_BASE_URL", "") or os.getenv("OPENAI_BASE_URL", "") or os.getenv("ANTHROPIC_BASE_URL", "")


ACTION_DIRECTIVES = """## 行动指令

根据用户意图自主选择行动，不要追问：
- 用户描述新想法/需求 → 直接拆解为多张结构化卡片，调用 kanban MCP 创建（kanban_create_requirements）
- 用户要求调研/竞品分析 → 用 web_search 搜索，产出结构化报告
- 用户问技术可行性 → 基于项目架构评估，给出结论和建议
- 用户说"你自己想/随便/你来决定/都行" → 基于产品记忆和项目上下文自主决策，给出方案并执行
- 用户要操作看板（建卡、移动、查进度） → 调用 kanban MCP 对应工具
- 用户闲聊/问问题 → 正常对话，结合项目上下文回答

原则：
1. 宁可先行动再让用户调整，也不要反复追问细节
2. 拆解需求时用 MVP 思维：先拆最小可用版本，不过度设计
3. 每张卡片包含：title、description（功能目标+验收标准）、priority
4. 创建卡片后告知用户创建了什么，让用户可以微调
"""


async def build_hermes_prompt(project_id: int, user_message: str) -> str:
    """Build the full prompt with context injection for hermes."""
    sections = []

    sections.append("你是 Kanban Harness 的 AI 团队协调员（PM 角色）。你负责理解用户意图并主动执行。")

    has_context = False

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if project_id:
            # Project info
            cursor = await db.execute(
                "SELECT name, description, prefix, advisor_skill, product_memory FROM projects WHERE id=?",
                (project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                sections.append(f"\n## 当前项目\n\n**{proj['name']}** (prefix: {proj['prefix']})\n{proj['description']}")

                # Advisor skill excerpt (truncate to ~2000 chars)
                if proj["advisor_skill"]:
                    skill = proj["advisor_skill"][:2000]
                    sections.append(f"\n## 产品顾问知识\n\n{skill}")
                    has_context = True

                # Product memory
                if proj["product_memory"]:
                    memory = proj["product_memory"][:1500]
                    sections.append(f"\n## 产品记忆（决策历史）\n\n{memory}")
                    has_context = True

            # Active version + requirements summary
            cursor = await db.execute(
                "SELECT v.id, v.name, v.status FROM versions v "
                "WHERE v.project_id=? AND v.status IN ('active','planning') "
                "ORDER BY v.position LIMIT 1",
                (project_id,),
            )
            active_ver = await cursor.fetchone()
            if active_ver:
                sections.append(f"\n## 活跃版本\n\n{active_ver['name']} (status: {active_ver['status']}, id: {active_ver['id']})")

            # Kanban state summary
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
        else:
            # No project selected — list available projects
            cursor = await db.execute(
                "SELECT id, name, prefix FROM projects WHERE archived=0 ORDER BY updated_at DESC LIMIT 5"
            )
            projects = await cursor.fetchall()
            if projects:
                lines = ["\n## 可用项目\n"]
                for p in projects:
                    lines.append(f"- [{p['prefix']}] {p['name']} (id: {p['id']})")
                sections.append("\n".join(lines))

    # If no rich context available, tell hermes to use kanban MCP tools
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

    # Action directives
    sections.append(ACTION_DIRECTIVES)

    # User message
    sections.append(f"\n## 用户消息\n\n{user_message}")

    return "\n".join(sections)


async def stream_hermes(project_id: int, user_message: str) -> AsyncGenerator[str, None]:
    """Run hermes -z with context-injected prompt and stream output as SSE events."""
    prompt = await build_hermes_prompt(project_id, user_message)

    # Write prompt to temp file to avoid shell argument length limits
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, prefix="kh_prompt_")
    try:
        tmp.write(prompt)
        tmp.close()

        # Build shell command that reads prompt from file
        flags = ""
        if HERMES_MODEL:
            flags += f" -m {HERMES_MODEL}"
        if HERMES_PROVIDER:
            flags += f" --provider {HERMES_PROVIDER}"
        if HERMES_TOOLSETS:
            flags += f" -t {HERMES_TOOLSETS}"

        shell_cmd = f'{HERMES_BIN} -z "$(cat {tmp.name})"{flags} --yolo'

        yield f"data: {json.dumps({'type': 'route', 'role': 'pm'}, ensure_ascii=False)}\n\n"

        proc = await asyncio.create_subprocess_shell(
            shell_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_build_hermes_env(),
        )

        byte_buf = b""
        line_buf = ""
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                break
            byte_buf += chunk

            # Decode only complete UTF-8 sequences
            try:
                text = byte_buf.decode("utf-8")
                byte_buf = b""
            except UnicodeDecodeError:
                # Incomplete multi-byte char at the end — trim until valid
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
                event = _parse_hermes_line(line)
                if event:
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        # Flush remaining
        if byte_buf:
            line_buf += byte_buf.decode("utf-8", errors="replace")
        if line_buf.strip():
            event = _parse_hermes_line(line_buf)
            if event:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

        await proc.wait()

        if proc.returncode != 0:
            stderr_out = await proc.stderr.read()
            err_msg = stderr_out.decode("utf-8", errors="replace").strip()
            if err_msg:
                logger.warning(f"hermes exited with code {proc.returncode}: {err_msg}")
                yield f"data: {json.dumps({'type': 'error', 'content': f'hermes error: {err_msg}'}, ensure_ascii=False)}\n\n"

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

    Called on KH startup. Creates ~/.hermes/config.yaml if missing,
    and always patches mcp_servers.kanban to point to the local MCP server
    (stdio transport → localhost FastAPI), preventing data from being
    written to a remote server.
    """
    import yaml

    config_dir = Path.home() / ".hermes"
    config_file = config_dir / "config.yaml"
    config_dir.mkdir(parents=True, exist_ok=True)

    mcp_server_path = str(Path(__file__).resolve().parent.parent / "mcp_server" / "server.py")
    port = os.getenv("PORT", "8000")
    local_mcp = {
        "type": "stdio",
        "command": "python",
        "args": [mcp_server_path],
        "env": {"KH_BASE_URL": f"http://localhost:{port}"},
    }

    if config_file.exists():
        with open(config_file, "r") as f:
            config = yaml.safe_load(f) or {}
        config.setdefault("mcp_servers", {})
        config["mcp_servers"]["kanban"] = local_mcp
    else:
        model = HERMES_MODEL or os.getenv("CHAT_MODEL", "claude-sonnet-4-6")
        config = {
            "_config_version": 1,
            "model": {"default": model},
            "agent": {"max_turns": 30, "reasoning_effort": "medium"},
            "approvals": {"mode": "yolo"},
            "toolsets": ["hermes-cli"],
            "mcp_servers": {"kanban": local_mcp},
        }
        if API_BASE_URL:
            config["model"]["base_url"] = API_BASE_URL

    with open(config_file, "w") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True)

    logger.info(f"Ensured hermes config with local MCP at {config_file}")

