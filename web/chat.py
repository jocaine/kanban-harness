"""Chat API — SSE streaming chat with PM engine (tool loop + streaming)."""

import os
import json
import logging
import time
from typing import AsyncGenerator

import aiosqlite
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.database import DB_PATH

logger = logging.getLogger("kh.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "") or os.getenv("API_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "") or os.getenv("API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "") or os.getenv("API_BASE_URL", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-opus-4-6")
CHAT_PROVIDER = os.getenv("CHAT_PROVIDER", "openai")  # openai / anthropic / ollama


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_requirements",
            "description": "List requirements for the current project, optionally filtered by status",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["research", "pending", "dev", "testing", "done"], "description": "Filter by status"},
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_requirement",
            "description": "Create a new requirement card (status=pending for actionable tasks)",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Requirement title"},
                    "description": {"type": "string", "description": "Markdown description with goals and acceptance criteria"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "default": "P2"},
                    "initial_comment": {"type": "string", "description": "User's original words as the first comment on the card"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_research_card",
            "description": "Create a research card (status=research). Use when the topic needs investigation — competitor analysis, market research, tech feasibility study. The industry advisor will pick it up asynchronously.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Research topic title"},
                    "description": {"type": "string", "description": "What to investigate, key questions to answer"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "default": "P2"},
                    "initial_comment": {"type": "string", "description": "User's original words as the first comment on the card"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_requirement",
            "description": "Move a requirement to a different status column",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Requirement code like KH-001"},
                    "status": {"type": "string", "enum": ["research", "pending", "dev", "testing", "done"]},
                },
                "required": ["code", "status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_scheduler_status",
            "description": "Get the current scheduler/AI team status",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_project",
            "description": "Update project settings like git_repo_path or description",
            "parameters": {
                "type": "object",
                "properties": {
                    "git_repo_path": {"type": "string", "description": "Local git repository path"},
                    "git_remote_url": {"type": "string", "description": "Remote git URL"},
                    "description": {"type": "string", "description": "Project description"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_architecture",
            "description": "Set or update the project architecture document. Use when user confirms tech stack, framework choices, or project structure. Content should be markdown describing: tech stack, directory structure, key dependencies, deployment approach.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "Architecture document in markdown format"},
                },
                "required": ["content"],
            },
        },
    },
]


class ChatMessage(BaseModel):
    message: str
    project_id: int = 0
    model: str = ""
    provider: str = ""


async def _execute_tool(name: str, args: dict, project_id: int) -> str:
    """Execute a tool call and return the result as a string."""
    logger.info("[PM] tool_exec: %s(%s) project=%d", name, json.dumps(args, ensure_ascii=False)[:120], project_id)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            if name == "list_requirements":
                status_filter = args.get("status")
                limit = args.get("limit", 20)
                if project_id:
                    query = (
                        "SELECT r.code, r.title, r.status, r.priority, r.assignee "
                        "FROM requirements r JOIN versions v ON r.version_id=v.id "
                        "WHERE v.project_id=? AND r.archived=0"
                    )
                    params: list = [project_id]
                    if status_filter:
                        query += " AND r.status=?"
                        params.append(status_filter)
                    query += " ORDER BY r.priority, r.position LIMIT ?"
                    params.append(limit)
                    cursor = await db.execute(query, params)
                else:
                    cursor = await db.execute(
                        "SELECT code, title, status, priority FROM requirements WHERE archived=0 LIMIT ?",
                        (limit,),
                    )
                rows = [dict(r) for r in await cursor.fetchall()]
                return json.dumps(rows, ensure_ascii=False)

            elif name in ("create_requirement", "create_research_card"):
                if not project_id:
                    return json.dumps({"error": "no project selected"})
                cursor = await db.execute(
                    "SELECT id FROM versions WHERE project_id=? AND status IN ('active','planning') ORDER BY position LIMIT 1",
                    (project_id,),
                )
                ver = await cursor.fetchone()
                if not ver:
                    # Auto-create a default version if none exists
                    cursor = await db.execute(
                        "SELECT prefix FROM projects WHERE id=?", (project_id,),
                    )
                    proj_row = await cursor.fetchone()
                    prefix = proj_row["prefix"] if proj_row else "v"
                    await db.execute(
                        "INSERT INTO versions (project_id, name, description, status, position, created_at, updated_at) "
                        "VALUES (?, 'v0.1 MVP', ?, 'active', 0, datetime('now','localtime'), datetime('now','localtime'))",
                        (project_id, f"{prefix} 最小可用版本"),
                    )
                    cursor = await db.execute("SELECT last_insert_rowid()")
                    ver = await cursor.fetchone()
                    logger.info("[PM] auto-created version v0.1 MVP for project %d", project_id)
                version_id = ver[0] if isinstance(ver[0], int) else ver["id"]
                from core.database import next_code
                code = await next_code(db, version_id)
                cursor = await db.execute(
                    "SELECT COALESCE(MAX(position),-1)+1 FROM requirements WHERE version_id=? AND archived=0",
                    (version_id,),
                )
                pos = (await cursor.fetchone())[0]
                title = args.get("title", "")
                desc = args.get("description", "")
                priority = args.get("priority", "P2")
                initial_comment = args.get("initial_comment", "")
                is_research = name == "create_research_card"
                req_type = "research" if is_research else "dev"
                status = "research" if is_research else "pending"
                # Retry with incremented code on unique constraint failure
                import re as _re
                for _attempt in range(5):
                    try:
                        await db.execute(
                            "INSERT INTO requirements (version_id,title,description,priority,type,status,code,position) VALUES (?,?,?,?,?,?,?,?)",
                            (version_id, title, desc, priority, req_type, status, code, pos),
                        )
                        break
                    except Exception as _e:
                        if "UNIQUE constraint" in str(_e) and _attempt < 4:
                            # Increment code suffix and retry
                            _m = _re.search(r'(\d+)$', code)
                            code = code[:_m.start()] + str(int(_m.group()) + 1).zfill(_m.end() - _m.start()) if _m else code + "-2"
                            logger.warning("[PM] code conflict, retrying with %s", code)
                        else:
                            raise
                # Get the new requirement ID for event emit
                cursor = await db.execute("SELECT last_insert_rowid()")
                new_req_id = (await cursor.fetchone())[0]
                # User's original message as first comment
                if initial_comment:
                    await db.execute(
                        "INSERT INTO comments (requirement_id, author, content) VALUES (?,?,?)",
                        (new_req_id, "CEO", initial_comment),
                    )
                # Emit requirement_created event so scheduler triggers industry/pm
                await db.execute(
                    "INSERT INTO agent_events (project_id, event_type, requirement_id, context) VALUES (?,?,?,?)",
                    (project_id, "requirement_created", new_req_id,
                     json.dumps({"status": status, "code": code, "title": title})),
                )
                await db.commit()
                logger.info("[PM] created %s: %s (status=%s, priority=%s)", code, title, status, priority)
                return json.dumps({"created": code, "title": title, "status": status, "priority": priority}, ensure_ascii=False)

            elif name == "move_requirement":
                code = args.get("code", "")
                status = args.get("status", "")
                cursor = await db.execute("SELECT id, title FROM requirements WHERE code=?", (code,))
                row = await cursor.fetchone()
                if not row:
                    return json.dumps({"error": f"requirement {code} not found"})
                await db.execute(
                    "UPDATE requirements SET status=?, updated_at=datetime('now','localtime') WHERE id=?",
                    (status, row["id"]),
                )
                await db.commit()
                return json.dumps({"moved": code, "to": status, "title": row["title"]}, ensure_ascii=False)

            elif name == "get_scheduler_status":
                from main import scheduler
                return json.dumps(scheduler.status, ensure_ascii=False)

            elif name == "update_project":
                if not project_id:
                    return json.dumps({"error": "no project selected"})
                updates, params = [], []
                for field in ("git_repo_path", "git_remote_url", "description"):
                    if field in args and args[field]:
                        updates.append(f"{field}=?")
                        params.append(args[field])
                if not updates:
                    return json.dumps({"error": "nothing to update"})
                updates.append("updated_at=datetime('now','localtime')")
                params.append(project_id)
                await db.execute(f"UPDATE projects SET {','.join(updates)} WHERE id=?", params)
                await db.commit()
                return json.dumps({"updated": True, "fields": list(args.keys())}, ensure_ascii=False)

            elif name == "set_architecture":
                if not project_id:
                    return json.dumps({"error": "no project selected"})
                content = args.get("content", "")
                if not content.strip():
                    return json.dumps({"error": "content cannot be empty"})
                await db.execute(
                    "INSERT INTO project_architecture (project_id, content, updated_at) "
                    "VALUES (?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(project_id) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                    (project_id, content),
                )
                await db.execute(
                    "INSERT INTO agent_events (project_id, event_type, context) VALUES (?,?,?)",
                    (project_id, "architecture_confirmed", json.dumps({"length": len(content)})),
                )
                await db.commit()
                logger.info("[PM] set_architecture for project %d (%d chars)", project_id, len(content))
                return json.dumps({"success": True, "project_id": project_id, "chars": len(content)}, ensure_ascii=False)

            return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _build_pm_system_prompt(project_id: int) -> str:
    """Build PM-role system prompt from real pm.yaml + project context."""
    from agents.registry import registry

    sections = []

    # Load real PM system prompt from pm.yaml
    pm_role = registry.get("pm")
    if pm_role:
        sections.append(pm_role.system_prompt)
    else:
        sections.append("你是 PM。负责理解用户意图并主动执行。\n")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if project_id:
            cursor = await db.execute(
                "SELECT name, description, prefix, product_memory FROM projects WHERE id=?",
                (project_id,),
            )
            proj = await cursor.fetchone()
            if proj:
                sections.append(f"\n## 当前项目\n\n**{proj['name']}** (prefix: {proj['prefix']})\n{proj['description'] or ''}")
                if proj["product_memory"]:
                    sections.append(f"\n## 产品记忆（决策历史）\n\n{proj['product_memory'][:1500]}")

            # Architecture doc
            cursor = await db.execute(
                "SELECT content FROM project_architecture WHERE project_id=?",
                (project_id,),
            )
            arch_row = await cursor.fetchone()
            if arch_row and arch_row["content"]:
                sections.append(f"\n## 项目架构\n\n{arch_row['content'][:2000]}")

            cursor = await db.execute(
                "SELECT r.code, r.title, r.status, r.priority "
                "FROM requirements r JOIN versions v ON r.version_id=v.id "
                "WHERE v.project_id=? AND r.archived=0 "
                "ORDER BY CASE r.status "
                "  WHEN 'dev' THEN 0 WHEN 'testing' THEN 1 "
                "  WHEN 'pending' THEN 2 WHEN 'research' THEN 3 WHEN 'done' THEN 4 END, "
                "r.priority LIMIT 20",
                (project_id,),
            )
            reqs = await cursor.fetchall()
            if reqs:
                sections.append("\n## 当前看板状态\n")
                for r in reqs:
                    sections.append(f"- [{r['code']}] {r['title']} ({r['status']}, {r['priority']})")
        else:
            cursor = await db.execute(
                "SELECT id, name, prefix FROM projects WHERE archived=0 ORDER BY updated_at DESC LIMIT 5"
            )
            projects = await cursor.fetchall()
            if projects:
                sections.append("\n## 可用项目\n")
                for p in projects:
                    sections.append(f"- [{p['prefix']}] {p['name']} (id: {p['id']})")

    # Chat-specific directives (complement pm.yaml, not duplicate it)
    sections.append("""
## 聊天专属指令

仅用于聊天界面（不改变 pm.yaml 中的角色边界）：
- 用户描述需求/想法 → 调用 create_requirement 建卡（status=pending, type=dev）
- 用户的需求涉及调研 → 调用 create_research_card 建卡（type=research）
- 用户问进度 → 调用 list_requirements
- 用户要移动卡片 → 调用 move_requirement
- 用户要更新架构 → 调用 set_architecture

原则：
1. 绝不追问，宁可先建卡再让用户调整
2. 需要调研的内容不要自己做，建 research 卡交给行业顾问
3. 每张卡片包含：title、description（功能目标+验收标准）、priority
4. 建卡后告知用户创建了什么
""")

    # Inject recent conversation history
    history = await _get_recent_history(project_id)
    if history:
        sections.append("\n## 近期对话\n")
        for msg in history[-10:]:
            prefix = "用户" if msg["role"] == "user" else "PM"
            sections.append(f"**{prefix}:** {msg['content'][:500]}")

    return "\n".join(sections)


async def _chat_with_tools(message: str, system_prompt: str, model: str, provider: str, project_id: int) -> AsyncGenerator[str, None]:
    """Multi-turn chat with tool use support via OpenAI-compatible API."""
    import httpx

    logger.info("[CHAT] user message: \"%s\" (project=%d, provider=%s, model=%s)", message[:80], project_id, provider or "default", model or "default")

    base_url = (OPENAI_BASE_URL or ANTHROPIC_BASE_URL).rstrip("/")
    # Strip trailing /v1 if present — we add it ourselves in the URL
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    api_key = OPENAI_API_KEY or ANTHROPIC_API_KEY
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'init'})}\n\n"

    max_rounds = 5
    tool_calls_acc = {}
    for round_idx in range(max_rounds):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model or CHAT_MODEL,
                        "messages": messages,
                        "tools": TOOLS,
                        "stream": True,
                        "max_tokens": 2048,
                    },
                    timeout=120,
                )
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_detail = error_body.decode("utf-8", errors="replace")[:500]
                    logger.error("[PM] API 400 response body: %s", error_detail)
                    resp.raise_for_status()

                content_text = ""
                reasoning_text = ""
                tool_calls_acc = {}

                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        data = json.loads(data_str)
                        choice = data.get("choices", [{}])[0]
                        delta = choice.get("delta", {})

                        if rc := delta.get("reasoning_content", ""):
                            reasoning_text += rc

                        if content := delta.get("content", ""):
                            content_text += content
                            yield f"data: {json.dumps({'type': 'text', 'content': content})}\n\n"

                        if tc_list := delta.get("tool_calls"):
                            for tc in tc_list:
                                idx = tc.get("index", 0)
                                if idx not in tool_calls_acc:
                                    tool_calls_acc[idx] = {"id": tc.get("id", ""), "name": "", "arguments": ""}
                                if tc.get("id"):
                                    tool_calls_acc[idx]["id"] = tc["id"]
                                if fn := tc.get("function"):
                                    if fn.get("name"):
                                        tool_calls_acc[idx]["name"] = fn["name"]
                                    if fn.get("arguments"):
                                        tool_calls_acc[idx]["arguments"] += fn["arguments"]
                    except (json.JSONDecodeError, IndexError, KeyError):
                        continue
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            break

        if not tool_calls_acc:
            break

        assistant_msg = {"role": "assistant", "content": content_text or None, "tool_calls": []}
        if reasoning_text:
            assistant_msg["reasoning_content"] = reasoning_text
        for idx in sorted(tool_calls_acc.keys()):
            tc = tool_calls_acc[idx]
            assistant_msg["tool_calls"].append({
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            })
        messages.append(assistant_msg)

        for idx in sorted(tool_calls_acc.keys()):
            tc = tool_calls_acc[idx]
            yield f"data: {json.dumps({'type': 'tool_start', 'name': tc['name']})}\n\n"
            try:
                args = json.loads(tc["arguments"]) if tc["arguments"] else {}
            except json.JSONDecodeError:
                args = {}
            result = await _execute_tool(tc["name"], args, project_id)
            logger.info("[PM] tool_call round=%d: %s(%s) → %s", round_idx + 1, tc["name"], json.dumps(args, ensure_ascii=False)[:80], result[:120])
            yield f"data: {json.dumps({'type': 'tool_done', 'name': tc['name']})}\n\n"
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    logger.info("[PM] done, %d tool rounds completed", round_idx + 1 if tool_calls_acc else 0)
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


async def _stream_simple(message: str, system_prompt: str, model: str, provider: str) -> AsyncGenerator[str, None]:
    """Simple streaming without tool use (fallback for ollama/anthropic native)."""
    p = provider or CHAT_PROVIDER
    if p == "anthropic":
        async for chunk in _stream_anthropic(message, system_prompt, model):
            yield chunk
    elif p == "ollama":
        async for chunk in _stream_ollama(message, system_prompt, model):
            yield chunk
    else:
        async for chunk in _stream_openai_simple(message, system_prompt, model):
            yield chunk


async def _stream_openai_simple(message: str, system_prompt: str, model: str) -> AsyncGenerator[str, None]:
    """Stream without tools via OpenAI-compatible API."""
    try:
        import httpx
        base_url = (OPENAI_BASE_URL or ANTHROPIC_BASE_URL).rstrip("/")
        api_key = OPENAI_API_KEY or ANTHROPIC_API_KEY

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model or CHAT_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ],
                    "stream": True,
                    "max_tokens": 2048,
                },
                timeout=120,
            )
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    data = json.loads(data_str)
                    delta = data.get("choices", [{}])[0].get("delta", {})
                    if content := delta.get("content", ""):
                        yield f"data: {json.dumps({'type': 'text', 'content': content})}\n\n"
                except (json.JSONDecodeError, IndexError, KeyError):
                    continue

        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"


async def _stream_anthropic(message: str, system_prompt: str, model: str) -> AsyncGenerator[str, None]:
    """Stream response from Anthropic native API."""
    try:
        import anthropic
        kwargs = {"api_key": ANTHROPIC_API_KEY}
        if ANTHROPIC_BASE_URL:
            kwargs["base_url"] = ANTHROPIC_BASE_URL
        client = anthropic.AsyncAnthropic(**kwargs)

        async with client.messages.stream(
            model=model or CHAT_MODEL,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": message}],
        ) as stream:
            async for text in stream.text_stream:
                yield f"data: {json.dumps({'type': 'text', 'content': text})}\n\n"

        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"


async def _stream_ollama(message: str, system_prompt: str, model: str) -> AsyncGenerator[str, None]:
    """Stream response from Ollama-compatible API."""
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model or "hermes3",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": message},
                    ],
                    "stream": True,
                },
                timeout=120,
            )
            async for line in resp.aiter_lines():
                if line:
                    data = json.loads(line)
                    if content := data.get("message", {}).get("content", ""):
                        yield f"data: {json.dumps({'type': 'text', 'content': content})}\n\n"
                    if data.get("done"):
                        break

        yield f"data: {json.dumps({'type': 'done'})}\n\n"
    except Exception as e:
        yield f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"


@router.post("/stream")
async def chat_stream(data: ChatMessage):
    """SSE streaming chat endpoint — hermes primary, OpenAI fallback."""
    provider = data.provider or CHAT_PROVIDER

    # Save user message to history
    await _save_message(data.project_id, "user", data.message)

    # Wrap generators to capture and save assistant response
    async def _wrap_and_save(gen, agent_role=""):
        full_response = []
        try:
            async for event in gen:
                yield event
                if event.startswith("data: "):
                    try:
                        payload = json.loads(event[6:].strip())
                        if payload.get("type") == "text":
                            full_response.append(payload["content"])
                    except (json.JSONDecodeError, KeyError):
                        pass
        finally:
            # Save whatever was collected, even if client disconnected mid-stream
            text = "".join(full_response)
            if text.strip():
                try:
                    await _save_message(data.project_id, "assistant", text, agent_role)
                except Exception:
                    pass

    # PM engine is the primary backend (v0.6 architecture)
    # Hermes only used when explicitly requested via provider="hermes"
    if provider == "hermes":
        from web.hermes_chat import stream_hermes, check_hermes_available
        if await check_hermes_available():
            generator = _wrap_and_save(stream_hermes(data.project_id, data.message), "pm")
            return StreamingResponse(
                generator,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

    # Default: PM engine with streaming tool loop
    system_prompt = await _build_pm_system_prompt(data.project_id)
    model = data.model or CHAT_MODEL

    generator = _wrap_and_save(_chat_with_tools(data.message, system_prompt, model, provider, data.project_id), "pm")

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("")
async def chat_sync(data: ChatMessage):
    """Non-streaming chat (collects full response)."""
    provider = data.provider or CHAT_PROVIDER

    # Hermes only when explicitly requested
    if provider == "hermes":
        from web.hermes_chat import stream_hermes, check_hermes_available
        if await check_hermes_available():
            gen = stream_hermes(data.project_id, data.message)
            chunks = []
            async for event in gen:
                if event.startswith("data: "):
                    payload = json.loads(event[6:].strip())
                    if payload["type"] == "text":
                        chunks.append(payload["content"])
            return {"response": "".join(chunks), "model": "hermes", "provider": "hermes"}

    # Default: PM engine
    system_prompt = await _build_pm_system_prompt(data.project_id)
    model = data.model or CHAT_MODEL

    gen = _chat_with_tools(data.message, system_prompt, model, provider, data.project_id)

    chunks = []
    async for event in gen:
        if event.startswith("data: "):
            payload = json.loads(event[6:].strip())
            if payload["type"] == "text":
                chunks.append(payload["content"])

    return {"response": "".join(chunks), "model": model}


# ==================== Chat History ====================

def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~2 chars per token for Chinese, ~4 for English."""
    cn_chars = sum(1 for c in text if '一' <= c <= '鿿')
    en_chars = len(text) - cn_chars
    return cn_chars // 2 + en_chars // 4 + 1


async def _save_message(project_id: int, role: str, content: str, agent_role: str = ""):
    """Save a chat message to the database."""
    if not project_id or not content.strip():
        return
    token_est = _estimate_tokens(content)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO chat_messages (project_id, role, content, agent_role, token_estimate) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, role, content, agent_role, token_est),
        )
        await db.commit()


async def _get_recent_history(project_id: int, limit: int = 10) -> list[dict]:
    """Get recent messages for context injection. Returns oldest-first."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT role, content, agent_role FROM chat_messages "
            "WHERE project_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit * 2),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    rows.reverse()
    return rows


async def _get_conversation_summary(project_id: int) -> str:
    """Get the most recent conversation summary if one exists."""
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


@router.get("/history")
async def chat_history(project_id: int = 0, limit: int = 30):
    """Load recent chat messages for frontend display."""
    if not project_id:
        return {"messages": []}
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, role, content, agent_role, created_at FROM chat_messages "
            "WHERE project_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        rows = [dict(r) for r in await cursor.fetchall()]
    rows.reverse()
    return {"messages": rows}


@router.delete("/history")
async def clear_history(project_id: int = 0):
    """Clear chat history for a project."""
    if not project_id:
        return {"cleared": 0}
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "DELETE FROM chat_messages WHERE project_id=?", (project_id,)
        )
        await db.commit()
        return {"cleared": cursor.rowcount}


async def _maybe_compact(project_id: int):
    """If message count exceeds threshold, summarize older messages."""
    if not project_id:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM chat_messages WHERE project_id=? AND role IN ('user','assistant')",
            (project_id,),
        )
        row = await cursor.fetchone()
        if row["cnt"] <= 40:
            return

        # Get oldest messages to summarize (keep last 20, summarize the rest)
        cursor = await db.execute(
            "SELECT id, role, content FROM chat_messages "
            "WHERE project_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at ASC",
            (project_id,),
        )
        all_msgs = [dict(r) for r in await cursor.fetchall()]
        to_summarize = all_msgs[:-20]
        if len(to_summarize) < 10:
            return

        # Build summary text (simple extraction, no LLM call for now)
        summary_parts = []
        for msg in to_summarize:
            prefix = "用户" if msg["role"] == "user" else "AI"
            content = msg["content"][:200]
            summary_parts.append(f"{prefix}: {content}")
        summary_text = "对话摘要（" + str(len(to_summarize)) + "条消息）:\n" + "\n".join(summary_parts[-10:])
        if len(summary_text) > 1500:
            summary_text = summary_text[:1500] + "..."

        # Delete old messages and insert summary
        ids_to_delete = [m["id"] for m in to_summarize]
        placeholders = ",".join("?" * len(ids_to_delete))
        await db.execute(f"DELETE FROM chat_messages WHERE id IN ({placeholders})", ids_to_delete)
        # Remove old summaries
        await db.execute("DELETE FROM chat_messages WHERE project_id=? AND role='summary'", (project_id,))
        await db.execute(
            "INSERT INTO chat_messages (project_id, role, content, token_estimate) VALUES (?, 'summary', ?, ?)",
            (project_id, summary_text, len(summary_text) // 3),
        )
        await db.commit()
        logger.info("[CHAT] compacted %d messages for project %d", len(ids_to_delete), project_id)
