"""Chat API — SSE streaming chat with tool use support."""

import os
import json
import logging
from typing import AsyncGenerator

import aiosqlite
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.database import DB_PATH

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_BASE_URL = os.getenv("ANTHROPIC_BASE_URL", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "")
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
                    "status": {"type": "string", "enum": ["pending", "dev", "testing", "done"], "description": "Filter by status"},
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_requirement",
            "description": "Create a new requirement card",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Requirement title"},
                    "description": {"type": "string", "description": "Markdown description"},
                    "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"], "default": "P2"},
                    "status": {"type": "string", "enum": ["pending", "dev", "testing", "done"], "default": "pending"},
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
                    "status": {"type": "string", "enum": ["pending", "dev", "testing", "done"]},
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
]


class ChatMessage(BaseModel):
    message: str
    project_id: int = 0
    model: str = ""
    provider: str = ""


async def _execute_tool(name: str, args: dict, project_id: int) -> str:
    """Execute a tool call and return the result as a string."""
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

            elif name == "create_requirement":
                if not project_id:
                    return json.dumps({"error": "no project selected"})
                cursor = await db.execute(
                    "SELECT id FROM versions WHERE project_id=? AND status IN ('active','planning') ORDER BY position LIMIT 1",
                    (project_id,),
                )
                ver = await cursor.fetchone()
                if not ver:
                    return json.dumps({"error": "no active version found"})
                version_id = ver["id"]
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
                status = args.get("status", "pending")
                await db.execute(
                    "INSERT INTO requirements (version_id,title,description,priority,status,code,position) VALUES (?,?,?,?,?,?,?)",
                    (version_id, title, desc, priority, status, code, pos),
                )
                await db.commit()
                return json.dumps({"created": code, "title": title, "status": status}, ensure_ascii=False)

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

            return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _get_kanban_context(project_id: int) -> str:
    """Build system prompt context from kanban state."""
    lines = [
        "You are a helpful AI assistant integrated into Kanban Harness, an AI team orchestration engine. "
        "Answer the user's questions directly and helpfully. You can discuss anything the user asks about. "
        "When relevant, reference the project state below to provide context-aware answers.\n"
        "You have tools available to manage the kanban board. Use them when the user asks to create cards, "
        "check progress, move cards, or configure the project. After using a tool, summarize the result naturally.\n"
    ]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if project_id:
            cursor = await db.execute("SELECT name, description, prefix FROM projects WHERE id=?", (project_id,))
            proj = await cursor.fetchone()
            if proj:
                lines.append(f"**Project:** {proj['name']} (prefix: {proj['prefix']}) — {proj['description']}")

            cursor = await db.execute(
                "SELECT v.name as vname, r.code, r.title, r.status, r.priority "
                "FROM requirements r JOIN versions v ON r.version_id=v.id "
                "WHERE v.project_id=? AND r.archived=0 "
                "ORDER BY r.status, r.priority LIMIT 20",
                (project_id,),
            )
            reqs = [dict(row) for row in await cursor.fetchall()]
            if reqs:
                lines.append("\n**Active requirements:**")
                for r in reqs:
                    lines.append(f"- [{r['code']}] {r['title']} ({r['status']}, {r['priority']})")
        else:
            cursor = await db.execute(
                "SELECT id, name FROM projects WHERE archived=0 ORDER BY updated_at DESC LIMIT 5"
            )
            projects = [dict(row) for row in await cursor.fetchall()]
            lines.append(f"**Projects:** {', '.join(p['name'] for p in projects)}")

    return "\n".join(lines)


async def _chat_with_tools(message: str, system_prompt: str, model: str, provider: str, project_id: int) -> AsyncGenerator[str, None]:
    """Multi-turn chat with tool use support via OpenAI-compatible API."""
    import httpx

    base_url = (OPENAI_BASE_URL or ANTHROPIC_BASE_URL).rstrip("/")
    api_key = OPENAI_API_KEY or ANTHROPIC_API_KEY
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    max_rounds = 5
    for _ in range(max_rounds):
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
                resp.raise_for_status()

                content_text = ""
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
            yield f"data: {json.dumps({'type': 'tool_done', 'name': tc['name']})}\n\n"
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

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
    """SSE streaming chat endpoint with tool use."""
    system_prompt = await _get_kanban_context(data.project_id)
    model = data.model or CHAT_MODEL
    provider = data.provider or CHAT_PROVIDER

    if provider == "openai" or (not provider and OPENAI_BASE_URL):
        generator = _chat_with_tools(data.message, system_prompt, model, provider, data.project_id)
    else:
        generator = _stream_simple(data.message, system_prompt, model, provider)

    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("")
async def chat_sync(data: ChatMessage):
    """Non-streaming chat (collects full response)."""
    system_prompt = await _get_kanban_context(data.project_id)
    model = data.model or CHAT_MODEL
    provider = data.provider or CHAT_PROVIDER

    if provider == "openai" or (not provider and OPENAI_BASE_URL):
        gen = _chat_with_tools(data.message, system_prompt, model, provider, data.project_id)
    else:
        gen = _stream_simple(data.message, system_prompt, model, provider)

    chunks = []
    async for event in gen:
        if event.startswith("data: "):
            payload = json.loads(event[6:].strip())
            if payload["type"] == "text":
                chunks.append(payload["content"])

    return {"response": "".join(chunks), "model": model}
