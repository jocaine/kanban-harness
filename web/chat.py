"""Chat API — SSE streaming chat with PM engine (tool loop + streaming)."""

import os
import json
import logging
import time
import asyncio
from typing import AsyncGenerator

import aiosqlite
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from core.database import DB_PATH
from core.task_buffer import task_buffers
from core.chat_task_manager import chat_task_manager

logger = logging.getLogger("kh.web.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

import re

def _strip_model_suffix(model: str) -> str:
    return re.sub(r'\[.*\]$', '', model)


def _chat_api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "") or os.getenv("ANTHROPIC_AUTH_TOKEN", "") or os.getenv("API_KEY", "")


def _anthropic_base_url() -> str:
    return os.getenv("ANTHROPIC_BASE_URL", "") or os.getenv("API_BASE_URL", "")


def _openai_api_key() -> str:
    return os.getenv("OPENAI_API_KEY", "") or os.getenv("API_KEY", "")


def _openai_base_url() -> str:
    return os.getenv("OPENAI_BASE_URL", "") or os.getenv("API_BASE_URL", "")


def _ollama_base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")


def _chat_model() -> str:
    return _strip_model_suffix(os.getenv("CHAT_MODEL", "claude-opus-4-6"))


def _chat_provider() -> str:
    return os.getenv("CHAT_PROVIDER", "openai")


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_requirements",
            "description": "List requirements for the current project, optionally filtered by status",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["research", "organizing", "dev", "testing", "done", "blocked"], "description": "Filter by status"},
                    "limit": {"type": "integer", "description": "Max results", "default": 20},
                },
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
            "name": "get_requirement",
            "description": "Get full details of a specific requirement card by code (e.g. KH-001). Returns all fields including agent_timeout, estimated_hours, deadline, status, priority, description, notes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Requirement code like KH-001"},
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wiki_read_page",
            "description": "Read a wiki page by path (e.g. 'research/wiki-patterns', 'product/user-persona')",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_path": {"type": "string", "description": "Page path like 'research/topic-name'"},
                },
                "required": ["page_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wiki_write_page",
            "description": "Write or update a wiki page. Use for recording decisions, user preferences, or knowledge worth preserving across sessions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_path": {"type": "string", "description": "Page path like 'product/user-persona'"},
                    "content": {"type": "string", "description": "Full page content with frontmatter"},
                    "log_message": {"type": "string", "description": "Short description of the change"},
                },
                "required": ["page_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wiki_list_pages",
            "description": "List all wiki pages, optionally filtered by subdir (research/product/arch)",
            "parameters": {
                "type": "object",
                "properties": {
                    "subdir": {"type": "string", "description": "Filter by subdir: research, product, or arch"},
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
    logger.info("[PM] 工具执行: %s(%s) project=%d", name, json.dumps(args, ensure_ascii=False)[:120], project_id)
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row

            if name == "list_requirements":
                status_filter = args.get("status")
                limit = args.get("limit", 20)
                if project_id:
                    query = (
                        "SELECT r.code, r.title, r.status, r.priority, r.assignee, r.agent_timeout, r.type "
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

            elif name == "get_scheduler_status":
                from main import scheduler
                return json.dumps(scheduler.status, ensure_ascii=False)

            elif name == "get_requirement":
                code = args.get("code", "")
                if not code:
                    return json.dumps({"error": "code is required"})
                cursor = await db.execute(
                    "SELECT r.*, v.name as version_name, v.project_id "
                    "FROM requirements r JOIN versions v ON r.version_id=v.id "
                    "WHERE r.code=? AND r.archived=0",
                    (code,),
                )
                row = await cursor.fetchone()
                if not row:
                    return json.dumps({"error": f"requirement {code} not found"})
                return json.dumps(dict(row), ensure_ascii=False, default=str)

            elif name == "wiki_read_page":
                from core.wiki import read_wiki_page
                page_path = args.get("page_path", "")
                content = read_wiki_page(project_id, page_path)
                if not content:
                    return json.dumps({"error": f"page not found: {page_path}"})
                return content

            elif name == "wiki_write_page":
                from core.wiki import write_wiki_page, update_index
                page_path = args.get("page_path", "")
                content = args.get("content", "")
                log_msg = args.get("log_message", "")
                if not page_path or not content:
                    return json.dumps({"error": "page_path and content are required"})
                relative = write_wiki_page(project_id, page_path, content, log_msg)
                update_index(project_id)
                return f"已写入 wiki: {relative}"

            elif name == "wiki_list_pages":
                from core.wiki import list_wiki_pages
                subdir = args.get("subdir", "")
                pages = list_wiki_pages(project_id, subdir or None)
                if not pages:
                    return "暂无 wiki 页面"
                return json.dumps(pages, ensure_ascii=False, indent=2)

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
                "  WHEN 'organizing' THEN 2 WHEN 'research' THEN 3 WHEN 'done' THEN 4 END, "
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

    # Chat-specific directives
    sections.append("""
## 聊天专属指令

你正在和 CEO 直接对话。你是 CEO 的项目观察者和顾问，不是执行者。

你的职责：
1. 回答 CEO 关于项目状态、进度、风险的提问（用 list_requirements / get_requirement / get_scheduler_status 查询真实数据）
2. 综合分析看板状态，主动识别瓶颈和风险
3. 当 CEO 想做某事时，告诉他应该怎么做（在 UI 上建卡、调整优先级等），但你不直接操作

## Wiki 实时写入（Karpathy Writeback）

你有 wiki 读写能力。在对话中发现以下内容时，主动写入 wiki：
- **用户偏好/习惯** → 写入 product/user-persona（技术水平、决策风格、领域熟悉度）
- **产品决策** → 写入 product/decisions（方向选择、否决理由）
- **值得沉淀的发现** → 写入对应目录

写入原则：
- 增量更新：先 wiki_read_page 读取现有内容，合并后写回，不要覆盖已有信息
- 不需要问用户"要不要记录" — 该记就记，这是你的职责
- 不确定是否值得记录时，不记录（宁缺毋滥）

你不能做的事：
- 不能建卡、不能移动卡片、不能修改项目设置
- 如果用户要求你建卡或修改状态，告知他："这个操作请在看板 UI 上完成，我可以帮你想清楚该建什么卡、怎么描述。"

原则：
1. 用 get_requirement 查卡片详情，不要凭记忆回答
2. 不确定的系统行为不要猜，用 get_scheduler_status 查实时状态
3. 回答要简洁、有判断，像一个了解全局的参谋
""")

    return "\n".join(sections)


async def _chat_with_tools(message: str, system_prompt: str, model: str, provider: str, project_id: int) -> AsyncGenerator[str, None]:
    """Multi-turn chat with tool use support via OpenAI-compatible API."""
    import httpx

    logger.info("[CHAT] 用户消息: \"%s\" (project=%d, provider=%s, model=%s)", message[:80], project_id, provider or "default", model or "default")

    base_url = (_openai_base_url() or _anthropic_base_url()).rstrip("/")
    # Strip trailing /v1 if present — we add it ourselves in the URL
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    api_key = _openai_api_key() or _chat_api_key()

    # Build messages with proper multi-turn history
    history_msgs = await _build_history_messages(project_id)
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history_msgs)
    messages.append({"role": "user", "content": message})

    yield f"data: {json.dumps({'type': 'thinking', 'stage': 'init'})}\n\n"

    total_usage = {"input": 0, "output": 0}

    max_rounds = 5
    tool_calls_acc = {}
    for round_idx in range(max_rounds):
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{base_url}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model or _chat_model(),
                        "messages": messages,
                        "tools": TOOLS,
                        "stream": True,
                        "stream_options": {"include_usage": True},
                        "max_tokens": 2048,
                    },
                    timeout=120,
                )
                if resp.status_code != 200:
                    error_body = await resp.aread()
                    error_detail = error_body.decode("utf-8", errors="replace")[:500]
                    logger.error("[PM] API 400 响应体: %s", error_detail)
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

                        if usage := data.get("usage"):
                            total_usage["input"] += usage.get("prompt_tokens", 0)
                            total_usage["output"] += usage.get("completion_tokens", 0)

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
            logger.info("[PM] 工具调用 round=%d: %s(%s) → %s", round_idx + 1, tc["name"], json.dumps(args, ensure_ascii=False)[:80], result[:120])
            yield f"data: {json.dumps({'type': 'tool_done', 'name': tc['name']})}\n\n"
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": result,
            })

    logger.info("[PM] 完成, %d 轮工具调用, tokens: in=%d out=%d",
                round_idx + 1 if tool_calls_acc else 0, total_usage["input"], total_usage["output"])
    if total_usage["input"] or total_usage["output"]:
        yield f"data: {json.dumps({'type': 'usage', 'input_tokens': total_usage['input'], 'output_tokens': total_usage['output'], 'total_tokens': total_usage['input'] + total_usage['output']})}\n\n"
    yield f"data: {json.dumps({'type': 'done'})}\n\n"


async def _stream_simple(message: str, system_prompt: str, model: str, provider: str) -> AsyncGenerator[str, None]:
    """Simple streaming without tool use (fallback for ollama/anthropic native)."""
    p = provider or _chat_provider()
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
        base_url = (_openai_base_url() or _anthropic_base_url()).rstrip("/")
        api_key = _openai_api_key() or _chat_api_key()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model or _chat_model(),
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
        kwargs = {"api_key": _chat_api_key()}
        if _anthropic_base_url():
            kwargs["base_url"] = _anthropic_base_url()
        client = anthropic.AsyncAnthropic(**kwargs)

        async with client.messages.stream(
            model=model or _chat_model(),
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
                f"{_ollama_base_url()}/api/chat",
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
    """SSE streaming chat — backed by background task (v0.7).

    Behavior unchanged for the client: POST, get SSE stream.
    Internally creates a background task so AI survives disconnects.
    """
    await _save_message(data.project_id, "user", data.message)
    model = _strip_model_suffix(data.model) or _chat_model()
    provider = data.provider or _chat_provider()

    task_id = await chat_task_manager.create_task(data.project_id, data.message, model, provider)
    asyncio.create_task(_execute_and_save(task_id, data))

    return StreamingResponse(
        _stream_from_buffer(task_id, 0),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/tasks")
async def create_chat_task(data: ChatMessage):
    """Create a background chat task. Returns task_id immediately."""
    await _save_message(data.project_id, "user", data.message)
    model = _strip_model_suffix(data.model) or _chat_model()
    provider = data.provider or _chat_provider()

    task_id = await chat_task_manager.create_task(data.project_id, data.message, model, provider)
    asyncio.create_task(_execute_and_save(task_id, data))

    return {"task_id": task_id, "status": "running"}


@router.get("/tasks/active")
async def get_active_task(project_id: int = Query(0)):
    """Get the most recent running task for a project (for reconnection on page load)."""
    task = await chat_task_manager.get_active_task(project_id)
    return {"task": task}


@router.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Get task status."""
    task = await chat_task_manager.get_task(task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task


@router.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str, last_event_id: int = Query(0)):
    """SSE observation endpoint — streams from buffer, supports reconnection."""
    state = task_buffers.get(task_id)

    if state:
        return StreamingResponse(
            _stream_from_buffer(task_id, last_event_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    row = await chat_task_manager.get_completed_response(task_id)
    if not row:
        raise HTTPException(404, "task not found")

    return StreamingResponse(
        _stream_completed_task(row),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================== Task Execution (thin Layer 5 glue) ====================


async def _execute_and_save(task_id: str, data: ChatMessage):
    """Build generator, delegate execution to ChatTaskManager, save result."""
    provider = data.provider or _chat_provider()
    if provider == "hermes":
        from web.hermes_chat import stream_hermes, check_hermes_available
        if await check_hermes_available():
            gen = stream_hermes(data.project_id, data.message)
        else:
            system_prompt = await _build_pm_system_prompt(data.project_id)
            model = _strip_model_suffix(data.model) or _chat_model()
            gen = _chat_with_tools(data.message, system_prompt, model, provider, data.project_id)
    else:
        system_prompt = await _build_pm_system_prompt(data.project_id)
        model = _strip_model_suffix(data.model) or _chat_model()
        gen = _chat_with_tools(data.message, system_prompt, model, provider, data.project_id)

    text = await chat_task_manager.run_task(task_id, gen, data.project_id)
    if text and text.strip():
        await _save_message(data.project_id, "assistant", text, "pm")


# ==================== SSE Formatting (Layer 5: HTTP presentation) ====================


async def _stream_from_buffer(task_id: str, start_index: int) -> AsyncGenerator[str, None]:
    """Yield SSE events from the live buffer, waiting for new ones."""
    state = task_buffers.get(task_id)
    if not state:
        return

    notify = task_buffers.subscribe(task_id)
    if not notify:
        return

    try:
        cursor = start_index
        while True:
            while cursor < len(state.chunks):
                chunk = state.chunks[cursor]
                yield f"id: {chunk.index}\n{chunk.data}"
                cursor += 1

            if state.done and cursor >= len(state.chunks):
                break

            notify.clear()
            try:
                await asyncio.wait_for(notify.wait(), timeout=30)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        task_buffers.unsubscribe(task_id, notify)


async def _stream_completed_task(row: dict) -> AsyncGenerator[str, None]:
    """Replay a completed task from DB as SSE events."""
    if row["status"] == "failed":
        yield f"data: {json.dumps({'type': 'error', 'content': row.get('error_message', 'unknown error')})}" + "\n\n"
    elif row.get("response_text"):
        yield f"data: {json.dumps({'type': 'text', 'content': row['response_text']})}" + "\n\n"
    yield f"data: {json.dumps({'type': 'done'})}" + "\n\n"


@router.post("")
async def chat_sync(data: ChatMessage):
    """Non-streaming chat (collects full response)."""
    provider = data.provider or _chat_provider()

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
    model = _strip_model_suffix(data.model) or _chat_model()

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


async def _build_history_messages(project_id: int, limit: int = 20) -> list[dict]:
    """Build proper message turns from chat history for multi-turn conversation."""
    if not project_id:
        return []

    messages = []

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Include conversation summary as context if available
        cursor = await db.execute(
            "SELECT content FROM chat_messages "
            "WHERE project_id=? AND role='summary' "
            "ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        )
        summary_row = await cursor.fetchone()
        if summary_row and summary_row["content"]:
            messages.append({"role": "user", "content": f"[对话摘要] {summary_row['content']}"})
            messages.append({"role": "assistant", "content": "好的，我已了解之前的对话背景。"})

        # Get recent user/assistant messages as proper turns
        cursor = await db.execute(
            "SELECT role, content FROM chat_messages "
            "WHERE project_id=? AND role IN ('user','assistant') "
            "ORDER BY created_at DESC LIMIT ?",
            (project_id, limit),
        )
        rows = [dict(r) for r in await cursor.fetchall()]

    rows.reverse()  # oldest first

    for row in rows:
        messages.append({"role": row["role"], "content": row["content"]})

    return messages


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
        logger.info("[CHAT] 已压缩 %d 条消息, 项目 %d", len(ids_to_delete), project_id)
