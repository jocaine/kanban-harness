"""Chat task lifecycle management — create, execute, complete, fail, recover (Layer 3)."""

import asyncio
import json
import logging
import uuid
from typing import AsyncGenerator

import aiosqlite

from core.database import DB_PATH
from core.task_buffer import task_buffers

logger = logging.getLogger("kh.core.chat_task")


class ChatTaskManager:
    """Manages background chat task lifecycle, analogous to SessionManager for agent sessions."""

    async def create_task(self, project_id: int, user_message: str, model: str, provider: str) -> str:
        """Create a new chat task record and in-memory buffer. Returns task_id."""
        task_id = str(uuid.uuid4())
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT INTO chat_tasks (id, project_id, status, user_message, model, provider) "
                "VALUES (?, ?, 'running', ?, ?, ?)",
                (task_id, project_id, user_message, model, provider),
            )
            await db.commit()
        task_buffers.create(task_id)
        logger.info("[TASK] created %s for project %d", task_id[:8], project_id)
        return task_id

    async def run_task(self, task_id: str, gen: AsyncGenerator[str, None], project_id: int):
        """Consume an AI generator in the background, writing chunks to buffer and DB on completion."""
        full_response = []

        try:
            chunk_count = 0
            async for event in gen:
                task_buffers.append_chunk(task_id, event)
                chunk_count += 1
                if event.startswith("data: "):
                    try:
                        payload = json.loads(event[6:].strip())
                        if payload.get("type") == "text":
                            full_response.append(payload["content"])
                    except (json.JSONDecodeError, KeyError):
                        pass

            task_buffers.mark_done(task_id)
            text = "".join(full_response)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE chat_tasks SET status='completed', response_text=?, "
                    "chunk_count=?, completed_at=datetime('now','localtime') WHERE id=?",
                    (text, chunk_count, task_id),
                )
                await db.commit()

            logger.info("[TASK] %s completed (%d chunks, %d chars)", task_id[:8], chunk_count, len(text))
            return text

        except Exception as e:
            logger.error("[TASK] %s failed: %s", task_id[:8], e, exc_info=True)
            error_event = f"data: {json.dumps({'type': 'error', 'content': str(e)})}\n\n"
            task_buffers.append_chunk(task_id, error_event)
            task_buffers.mark_done(task_id)

            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute(
                    "UPDATE chat_tasks SET status='failed', error_message=?, "
                    "completed_at=datetime('now','localtime') WHERE id=?",
                    (str(e), task_id),
                )
                await db.commit()
            return None

    async def get_task(self, task_id: str) -> dict | None:
        """Get task status — checks in-memory buffer first, then DB."""
        state = task_buffers.get(task_id)
        if state:
            return {
                "task_id": task_id,
                "status": "completed" if state.done else "running",
                "chunk_count": len(state.chunks),
            }

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, status, chunk_count, created_at, completed_at FROM chat_tasks WHERE id=?",
                (task_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_active_task(self, project_id: int) -> dict | None:
        """Get the most recent running task for a project."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, status, chunk_count, created_at FROM chat_tasks "
                "WHERE project_id=? AND status='running' ORDER BY created_at DESC LIMIT 1",
                (project_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_completed_response(self, task_id: str) -> dict | None:
        """Get a completed task's response from DB (for replay after buffer eviction)."""
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT status, response_text, error_message FROM chat_tasks WHERE id=?",
                (task_id,),
            )
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def recover_orphans(self):
        """Mark any 'running' chat_tasks as failed — called on startup."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM chat_tasks WHERE status='running'")
            count = (await cursor.fetchone())[0]
            if count:
                await db.execute(
                    "UPDATE chat_tasks SET status='failed', error_message='orphan:restart', "
                    "completed_at=datetime('now','localtime') WHERE status='running'"
                )
                await db.commit()
                logger.warning("Recovered %d orphan chat tasks from previous run", count)

    async def cleanup_expired(self):
        """Delete chat_tasks past their expires_at."""
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "DELETE FROM chat_tasks WHERE expires_at < datetime('now','localtime')"
            )
            await db.commit()
            if cursor.rowcount:
                logger.info("Cleaned up %d expired chat tasks", cursor.rowcount)


# Singleton
chat_task_manager = ChatTaskManager()
