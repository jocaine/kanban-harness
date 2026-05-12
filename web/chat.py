"""Chat API — SSE streaming chat with Claude/Ollama, kanban-context-aware."""

import os
import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
CHAT_MODEL = os.getenv("CHAT_MODEL", "claude-sonnet-4-20250514")


class ChatMessage(BaseModel):
    message: str
    project_id: int = 0
    model: str = ""


async def _get_kanban_context(project_id: int) -> str:
    """Build system prompt context from kanban state."""
    import aiosqlite
    from core.database import DB_PATH

    lines = ["You are the AI assistant for Kanban Harness, an AI team orchestration engine.",
             "Below is the current project state:\n"]

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        if project_id:
            cursor = await db.execute("SELECT name, description FROM projects WHERE id=?", (project_id,))
            proj = await cursor.fetchone()
            if proj:
                lines.append(f"**Project:** {proj['name']} — {proj['description']}")

            cursor = await db.execute(
                "SELECT v.name as vname, r.code, r.title, r.status, r.priority "
                "FROM requirements r JOIN versions v ON r.version_id=v.id "
                "WHERE v.project_id=? AND r.archived=0 AND v.status IN ('active','testing') "
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


async def _stream_claude(message: str, system_prompt: str, model: str) -> AsyncGenerator[str, None]:
    """Stream response from Claude API."""
    try:
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

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
    """SSE streaming chat endpoint."""
    system_prompt = await _get_kanban_context(data.project_id)
    model = data.model or CHAT_MODEL

    if model.startswith("claude") or ANTHROPIC_API_KEY:
        generator = _stream_claude(data.message, system_prompt, model)
    else:
        generator = _stream_ollama(data.message, system_prompt, model)

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

    chunks = []
    if model.startswith("claude") or ANTHROPIC_API_KEY:
        gen = _stream_claude(data.message, system_prompt, model)
    else:
        gen = _stream_ollama(data.message, system_prompt, model)

    async for event in gen:
        if event.startswith("data: "):
            payload = json.loads(event[6:].strip())
            if payload["type"] == "text":
                chunks.append(payload["content"])

    return {"response": "".join(chunks), "model": model}
