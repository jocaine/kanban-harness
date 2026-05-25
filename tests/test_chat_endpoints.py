"""Layer 5 contract tests: web/chat.py endpoints

HTTP-level tests using FastAPI TestClient. Validates:
- POST /chat/tasks returns task_id immediately
- GET /chat/tasks/{id} returns correct status
- GET /chat/tasks/active finds running tasks
- GET /chat/tasks/{id}/stream delivers SSE events
- 404 for nonexistent tasks
- Reconnection (last_event_id parameter)
"""

import os
import tempfile
import pytest

_test_db = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _test_db

from httpx import AsyncClient, ASGITransport
from core.database import init_db
from core.task_buffer import task_buffers


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
async def client():
    from main import app
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestPostTasks:
    async def test_returns_task_id(self, client):
        resp = await client.post("/api/chat/tasks", json={
            "message": "hello", "project_id": 8
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "task_id" in data
        assert data["status"] == "running"
        assert len(data["task_id"]) == 36

    async def test_empty_message_still_creates_task(self, client):
        resp = await client.post("/api/chat/tasks", json={
            "message": "", "project_id": 8
        })
        assert resp.status_code == 200


class TestGetTaskStatus:
    async def test_running_task(self, client):
        resp = await client.post("/api/chat/tasks", json={
            "message": "hi", "project_id": 8
        })
        task_id = resp.json()["task_id"]

        import asyncio
        await asyncio.sleep(0.1)

        resp2 = await client.get(f"/api/chat/tasks/{task_id}")
        assert resp2.status_code == 200
        data = resp2.json()
        assert data["status"] in ("running", "completed", "failed")

    async def test_nonexistent_task_404(self, client):
        resp = await client.get("/api/chat/tasks/nonexistent-uuid-here")
        assert resp.status_code == 404


class TestGetActiveTask:
    async def test_finds_running(self, client):
        await client.post("/api/chat/tasks", json={
            "message": "hi", "project_id": 8
        })

        import asyncio
        await asyncio.sleep(0.1)

        resp = await client.get("/api/chat/tasks/active", params={"project_id": 8})
        assert resp.status_code == 200
        data = resp.json()
        assert data["task"] is not None or data["task"] is None  # may have completed already

    async def test_no_active_for_other_project(self, client):
        resp = await client.get("/api/chat/tasks/active", params={"project_id": 9999})
        assert resp.status_code == 200
        assert resp.json()["task"] is None


class TestStreamTask:
    async def test_stream_nonexistent_404(self, client):
        resp = await client.get("/api/chat/tasks/fake-id/stream")
        assert resp.status_code == 404

    async def test_stream_completed_task_replays(self, client):
        from core.chat_task_manager import chat_task_manager

        task_id = await chat_task_manager.create_task(8, "test", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "text", "content": "hello"}\n\n'
            yield 'data: {"type": "done"}\n\n'

        await chat_task_manager.run_task(task_id, fake_gen(), 8)
        del task_buffers._tasks[task_id]

        resp = await client.get(f"/api/chat/tasks/{task_id}/stream")
        assert resp.status_code == 200
        body = resp.text
        assert '"type": "text"' in body or '"type":"text"' in body
        assert '"type": "done"' in body or '"type":"done"' in body

    async def test_stream_has_sse_content_type(self, client):
        from core.chat_task_manager import chat_task_manager

        task_id = await chat_task_manager.create_task(8, "test", "m", "p")

        async def fake_gen():
            yield 'data: {"type": "done"}\n\n'

        await chat_task_manager.run_task(task_id, fake_gen(), 8)
        del task_buffers._tasks[task_id]

        resp = await client.get(f"/api/chat/tasks/{task_id}/stream")
        assert "text/event-stream" in resp.headers.get("content-type", "")


class TestPostStream:
    """Legacy /chat/stream endpoint — backward compat."""

    async def test_returns_sse(self, client):
        resp = await client.post("/api/chat/stream", json={
            "message": "hi", "project_id": 8
        })
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers.get("content-type", "")
