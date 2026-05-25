"""Layer 3 integration tests: core/chat_task_manager.py — ChatTaskManager

Integration tests with real SQLite DB. Validates:
- Full lifecycle: create → run → complete
- Failure path: create → run → exception → failed
- DB consistency after each state transition
- Orphan recovery on startup
- Log sequence (structured log assertions)
"""

import asyncio
import os
import tempfile
import pytest

_test_db = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _test_db

from core.database import init_db, DB_PATH
from core.task_buffer import task_buffers
from core.chat_task_manager import ChatTaskManager

import aiosqlite


@pytest.fixture(autouse=True)
async def setup_db():
    if os.path.exists(_test_db):
        os.unlink(_test_db)
    await init_db()
    task_buffers._tasks.clear()
    yield
    if os.path.exists(_test_db):
        os.unlink(_test_db)


@pytest.fixture
def manager():
    return ChatTaskManager()


class TestCreateTask:
    async def test_create_returns_uuid(self, manager):
        task_id = await manager.create_task(1, "hello", "model-x", "openai")
        assert len(task_id) == 36
        assert "-" in task_id

    async def test_create_inserts_db_record(self, manager):
        task_id = await manager.create_task(1, "test msg", "m", "p")
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
        assert row is not None
        assert row["status"] == "running"
        assert row["user_message"] == "test msg"
        assert row["project_id"] == 1

    async def test_create_initializes_buffer(self, manager):
        task_id = await manager.create_task(1, "x", "m", "p")
        state = task_buffers.get(task_id)
        assert state is not None
        assert state.done is False


class TestRunTask:
    async def test_successful_run(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "text", "content": "hello "}\n\n'
            yield 'data: {"type": "text", "content": "world"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        text = await manager.run_task(task_id, fake_gen(), 1)
        assert text == "hello world"

    async def test_successful_run_updates_db(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "text", "content": "result"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        await manager.run_task(task_id, fake_gen(), 1)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
        assert row["status"] == "completed"
        assert row["response_text"] == "result"
        assert row["completed_at"] is not None
        assert row["chunk_count"] == 2

    async def test_successful_run_marks_buffer_done(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "text", "content": "x"}\n\n'

        await manager.run_task(task_id, fake_gen(), 1)
        state = task_buffers.get(task_id)
        assert state.done is True

    async def test_failed_run(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def failing_gen():
            yield 'data: {"type": "text", "content": "partial"}\n\n'
            raise RuntimeError("boom")

        result = await manager.run_task(task_id, failing_gen(), 1)
        assert result is None

    async def test_failed_run_updates_db(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def failing_gen():
            raise ValueError("test error")
            yield

        await manager.run_task(task_id, failing_gen(), 1)

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
        assert row["status"] == "failed"
        assert "test error" in row["error_message"]
        assert row["completed_at"] is not None

    async def test_failed_run_writes_error_to_buffer(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def failing_gen():
            raise RuntimeError("oops")
            yield

        await manager.run_task(task_id, failing_gen(), 1)
        state = task_buffers.get(task_id)
        assert state.done is True
        assert any("error" in c.data for c in state.chunks)


class TestGetTask:
    async def test_get_running_from_buffer(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")
        task = await manager.get_task(task_id)
        assert task["status"] == "running"

    async def test_get_completed_from_db(self, manager):
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "done"}\n\n'

        await manager.run_task(task_id, fake_gen(), 1)
        del task_buffers._tasks[task_id]

        task = await manager.get_task(task_id)
        assert task["status"] == "completed"

    async def test_get_nonexistent_returns_none(self, manager):
        task = await manager.get_task("nonexistent-id")
        assert task is None


class TestGetActiveTask:
    async def test_finds_running_task(self, manager):
        await manager.create_task(1, "hi", "m", "p")
        active = await manager.get_active_task(1)
        assert active is not None
        assert active["status"] == "running"

    async def test_no_active_task(self, manager):
        active = await manager.get_active_task(999)
        assert active is None

    async def test_returns_most_recent(self, manager):
        await manager.create_task(1, "first", "m", "p")
        await asyncio.sleep(0.01)
        task_id2 = await manager.create_task(1, "second", "m", "p")
        active = await manager.get_active_task(1)
        assert active["id"] == task_id2


class TestRecoverOrphans:
    async def test_marks_running_as_failed(self, manager):
        task_id = await manager.create_task(1, "orphan", "m", "p")
        task_buffers._tasks.clear()

        await manager.recover_orphans()

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
        assert row["status"] == "failed"
        assert row["error_message"] == "orphan:restart"

    async def test_does_not_touch_completed(self, manager):
        task_id = await manager.create_task(1, "done", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "done"}\n\n'

        await manager.run_task(task_id, fake_gen(), 1)
        await manager.recover_orphans()

        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM chat_tasks WHERE id=?", (task_id,))
            row = await cursor.fetchone()
        assert row["status"] == "completed"


class TestLogSequence:
    async def test_create_logs_task_id(self, manager, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="kh.core.chat_task"):
            task_id = await manager.create_task(1, "hi", "m", "p")
        assert any(task_id[:8] in r.message for r in caplog.records)

    async def test_complete_logs_chunk_count(self, manager, caplog):
        import logging
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "text", "content": "x"}\n\n'
            yield 'data: {"type": "text", "content": "y"}\n\n'

        with caplog.at_level(logging.INFO, logger="kh.core.chat_task"):
            await manager.run_task(task_id, fake_gen(), 1)
        assert any("completed" in r.message and "2 chunks" in r.message for r in caplog.records)

    async def test_failure_logs_error(self, manager, caplog):
        import logging
        task_id = await manager.create_task(1, "hi", "m", "p")

        async def failing_gen():
            raise RuntimeError("test_fail")
            yield

        with caplog.at_level(logging.ERROR, logger="kh.core.chat_task"):
            await manager.run_task(task_id, failing_gen(), 1)
        assert any("failed" in r.message for r in caplog.records)
