"""Retry Strategy tests: core/session_manager.py — fail_session

Validates:
- fail_session marks failed/blocked without scheduling retries
- Blocked after MAX_RETRIES
- No async tasks are scheduled (pure reconciliation model)
"""

import os
import tempfile

import pytest
from unittest.mock import patch

_test_db = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _test_db

from core.database import init_db, DB_PATH
from core.session_manager import SessionManager, MAX_RETRIES

import aiosqlite


@pytest.fixture(autouse=True)
async def setup_db():
    if os.path.exists(_test_db):
        os.unlink(_test_db)
    await init_db()
    yield
    if os.path.exists(_test_db):
        os.unlink(_test_db)


@pytest.fixture
def sm():
    return SessionManager()


class TestFailSession:
    async def test_marks_failed_below_max_retries(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
        )

        result = await sm.fail_session(session_id, "crash")

        assert result["status"] == "failed"
        assert result["error_message"] == "crash"

    async def test_no_async_task_scheduled(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
        )

        with patch("asyncio.create_task") as mock_task:
            await sm.fail_session(session_id, "stall")
            mock_task.assert_not_called()

    async def test_blocked_after_max_retries(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            retry_count=MAX_RETRIES,
        )

        result = await sm.fail_session(session_id, "final failure")

        assert result["status"] == "blocked"
        assert "final failure" in result["error_message"]

    async def test_multiple_failures_stay_failed(self, sm):
        """Each failure just marks failed — tick loop decides when to re-trigger."""
        s1 = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
        )
        await sm.fail_session(s1, "first")

        s2 = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            retry_count=1,
        )
        await sm.fail_session(s2, "second")

        session1 = await sm.get_session(s1)
        session2 = await sm.get_session(s2)
        assert session1["status"] == "failed"
        assert session2["status"] == "failed"
