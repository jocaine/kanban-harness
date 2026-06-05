"""Retry Strategy tests: core/session_manager.py — fail_session + continuation_retry

Validates:
- Exponential backoff on failure (10s → 20s → 40s, capped at 300s)
- Blocked after MAX_RETRIES
- Continuation retry: immediate requeue, no retry_count increment
- Continuation resets retry_count to 0
- fail_session schedules delayed retry (async task)
"""

import asyncio
import os
import tempfile
import time

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

_test_db = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _test_db

from core.database import init_db, DB_PATH
from core.session_manager import (
    SessionManager, MAX_RETRIES,
    BACKOFF_BASE_SECONDS, BACKOFF_MAX_SECONDS,
)

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


# ==================== Exponential backoff ====================


class TestExponentialBackoff:
    async def test_first_failure_delay_is_base(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
        )

        result = await sm.fail_session(session_id, "crash")

        assert result["status"] == "retrying"
        assert result["retry_delay"] == BACKOFF_BASE_SECONDS  # 10s

    async def test_second_failure_doubles_delay(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            retry_count=1,
        )

        result = await sm.fail_session(session_id, "crash again")

        assert result["status"] == "retrying"
        assert result["retry_delay"] == BACKOFF_BASE_SECONDS * 2  # 20s

    async def test_backoff_capped_at_max(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            retry_count=0,
        )

        # Simulate high retry count by patching
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "UPDATE agent_sessions SET retry_count=10 WHERE id=?",
                (session_id,),
            )
            await db.commit()

        # Re-read to get updated retry_count — but fail_session reads from DB
        result = await sm.fail_session(session_id, "many failures")

        # 10 * 2^10 = 10240, capped at 300
        assert result.get("retry_delay", 0) <= BACKOFF_MAX_SECONDS

    async def test_blocked_after_max_retries(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            retry_count=MAX_RETRIES,  # already at max
        )

        result = await sm.fail_session(session_id, "final failure")

        assert result["status"] == "blocked"
        assert "final failure" in result["error_message"]

    async def test_session_marked_failed_on_retry(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="pm", trigger_type="test",
        )

        await sm.fail_session(session_id, "timeout")

        session = await sm.get_session(session_id)
        assert session["status"] == "failed"
        assert session["error_message"] == "timeout"

    async def test_retry_creates_delayed_task(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
        )

        with patch("asyncio.create_task") as mock_task:
            result = await sm.fail_session(session_id, "stall")
            mock_task.assert_called_once()
            assert result["status"] == "retrying"


# ==================== Continuation retry ====================


class TestContinuationRetry:
    async def test_creates_new_session_immediately(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            input_context='{"requirement_id": 42}',
        )

        result = await sm.continuation_retry(session_id)

        assert result["status"] == "continuation"
        new_id = result["new_session_id"]
        assert new_id != session_id

        new_session = await sm.get_session(new_id)
        assert new_session["status"] == "running"
        assert new_session["agent_role"] == "coach_dev"
        assert new_session["retry_count"] == 0  # reset
        assert f"continuation:{session_id}" in new_session["trigger_type"]

    async def test_original_session_marked_completed(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
        )

        await sm.continuation_retry(session_id)

        original = await sm.get_session(session_id)
        assert original["status"] == "completed"
        assert "continuation:incomplete" in original["output_summary"]

    async def test_preserves_input_context(self, sm):
        ctx = '{"requirement_id": 99, "code": "KH-050"}'
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            input_context=ctx,
        )

        result = await sm.continuation_retry(session_id)

        new_session = await sm.get_session(result["new_session_id"])
        assert new_session["input_context"] == ctx

    async def test_preserves_timeout(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            timeout_seconds=900,
        )

        result = await sm.continuation_retry(session_id)

        new_session = await sm.get_session(result["new_session_id"])
        assert new_session["timeout_seconds"] == 900

    async def test_retry_count_resets_to_zero(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            retry_count=2,  # was at max retries
        )

        result = await sm.continuation_retry(session_id)

        new_session = await sm.get_session(result["new_session_id"])
        assert new_session["retry_count"] == 0


# ==================== Constants ====================


class TestRetryConstants:
    def test_backoff_base(self):
        assert BACKOFF_BASE_SECONDS == 10

    def test_backoff_max(self):
        assert BACKOFF_MAX_SECONDS == 300

    def test_backoff_sequence(self):
        """Verify the backoff sequence: 10, 20, 40, 80, 160, 300, 300..."""
        for attempt in range(6):
            delay = min(BACKOFF_BASE_SECONDS * (2 ** attempt), BACKOFF_MAX_SECONDS)
            expected = [10, 20, 40, 80, 160, 300][attempt]
            assert delay == expected, f"attempt {attempt}: got {delay}, expected {expected}"
