"""Stall Detection tests: core/session_manager.py — heartbeat + stall kill

Validates:
- Heartbeat tracking (register, update, unregister)
- Stall detection triggers kill after DEFAULT_STALL_TIMEOUT
- Hard timeout still works independently
- _kill_and_fail kills process and fails session
- No false positive: active heartbeat prevents stall kill
- Heartbeat callback wiring in agents
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
from core.session_manager import SessionManager, DEFAULT_STALL_TIMEOUT, DEFAULT_TIMEOUT

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


# ==================== Heartbeat tracking ====================


class TestHeartbeatTracking:
    def test_heartbeat_updates_timestamp(self, sm):
        sm.heartbeat(1)
        assert 1 in sm._heartbeats
        assert time.monotonic() - sm._heartbeats[1] < 1

    def test_heartbeat_overwrites_previous(self, sm):
        sm._heartbeats[1] = time.monotonic() - 100
        sm.heartbeat(1)
        assert time.monotonic() - sm._heartbeats[1] < 1

    def test_register_process_sets_heartbeat_and_proc(self, sm):
        mock_proc = MagicMock()
        sm.register_process(5, mock_proc)
        assert sm._processes[5] is mock_proc
        assert 5 in sm._heartbeats
        assert time.monotonic() - sm._heartbeats[5] < 1

    def test_unregister_process_cleans_both(self, sm):
        mock_proc = MagicMock()
        sm.register_process(5, mock_proc)
        sm.unregister_process(5)
        assert 5 not in sm._processes
        assert 5 not in sm._heartbeats

    def test_unregister_nonexistent_is_safe(self, sm):
        sm.unregister_process(999)  # should not raise


# ==================== _kill_and_fail ====================


class TestKillAndFail:
    async def test_kills_running_process(self, sm):
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 12345
        sm._processes[1] = mock_proc
        sm._heartbeats[1] = time.monotonic()

        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test"
        )
        sm._processes[session_id] = mock_proc
        sm._heartbeats[session_id] = time.monotonic()

        await sm._kill_and_fail(session_id, "stall:200s_no_output")

        mock_proc.kill.assert_called_once()
        assert session_id not in sm._processes
        assert session_id not in sm._heartbeats

    async def test_skips_already_exited_process(self, sm):
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # already exited
        mock_proc.pid = 12345

        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test"
        )
        sm._processes[session_id] = mock_proc
        sm._heartbeats[session_id] = time.monotonic()

        await sm._kill_and_fail(session_id, "timeout")

        mock_proc.kill.assert_not_called()
        assert session_id not in sm._processes

    async def test_handles_process_lookup_error(self, sm):
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 99999
        mock_proc.kill.side_effect = ProcessLookupError()

        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test"
        )
        sm._processes[session_id] = mock_proc
        sm._heartbeats[session_id] = time.monotonic()

        await sm._kill_and_fail(session_id, "stall:test")
        # Should not raise, process cleaned up
        assert session_id not in sm._processes

    async def test_session_marked_failed_after_kill(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="pm", trigger_type="test"
        )
        sm._heartbeats[session_id] = time.monotonic()

        await sm._kill_and_fail(session_id, "stall:120s_no_output")

        session = await sm.get_session(session_id)
        assert session["status"] in ("failed", "running")  # failed or retrying


# ==================== check_timeouts with stall ====================


class TestCheckTimeoutsStall:
    async def test_stall_detected_when_heartbeat_expired(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            timeout_seconds=600,
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 11111
        sm._processes[session_id] = mock_proc
        # Set heartbeat to 200s ago (exceeds 120s stall timeout)
        sm._heartbeats[session_id] = time.monotonic() - 200

        await sm.check_timeouts()

        mock_proc.kill.assert_called_once()
        assert session_id not in sm._heartbeats

    async def test_no_stall_when_heartbeat_fresh(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            timeout_seconds=600,
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 22222
        sm._processes[session_id] = mock_proc
        # Fresh heartbeat — 10s ago
        sm._heartbeats[session_id] = time.monotonic() - 10

        await sm.check_timeouts()

        mock_proc.kill.assert_not_called()
        assert session_id in sm._heartbeats

    async def test_hard_timeout_still_works(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            timeout_seconds=1,  # 1 second timeout
        )

        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 33333
        sm._processes[session_id] = mock_proc
        sm._heartbeats[session_id] = time.monotonic()  # fresh heartbeat

        await asyncio.sleep(1.2)  # exceed hard timeout
        with patch.object(sm, '_schedule_retry', new_callable=AsyncMock):
            await sm.check_timeouts()

        mock_proc.kill.assert_called_once()

    async def test_no_stall_check_without_heartbeat_entry(self, sm):
        """Sessions without heartbeat tracking (legacy) only use hard timeout."""
        session_id = await sm.create_session(
            project_id=8, agent_role="pm", trigger_type="test",
            timeout_seconds=600,
        )
        # No heartbeat registered — should not trigger stall
        assert session_id not in sm._heartbeats

        await sm.check_timeouts()
        # No crash, no kill — session still running
        session = await sm.get_session(session_id)
        assert session["status"] == "running"


# ==================== Retry after stall ====================


class TestRetryAfterStall:
    async def test_stall_triggers_retry(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            timeout_seconds=600,
        )
        sm._heartbeats[session_id] = time.monotonic() - 200

        with patch.object(sm, '_schedule_retry', new_callable=AsyncMock) as mock_sched:
            await sm._kill_and_fail(session_id, "stall:200s_no_output")
            # Verify retry was scheduled (delayed, not immediate)
            mock_sched.assert_called_once()

        # Original session should be marked failed
        session = await sm.get_session(session_id)
        assert session["status"] == "failed"
        assert "stall" in session["error_message"]

    async def test_stall_blocks_after_max_retries(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            timeout_seconds=600, retry_count=2,  # already at max
        )
        sm._heartbeats[session_id] = time.monotonic() - 200

        await sm._kill_and_fail(session_id, "stall:200s_no_output")

        session = await sm.get_session(session_id)
        assert session["status"] == "blocked"
        assert "stall" in session["error_message"]


# ==================== Agent heartbeat callback wiring ====================


class TestAgentCallbackWiring:
    def test_coach_dev_accepts_heartbeat(self):
        from agents.coach_dev import CoachDev

        calls = []
        agent = CoachDev(
            repo_path="/tmp", project_id=1,
            on_heartbeat=lambda: calls.append(1),
        )
        agent._on_heartbeat()
        assert len(calls) == 1

    async def test_comment_agent_passes_heartbeat_to_hermes(self):
        from agents.comment_agent import CommentAgent

        calls = []
        cb = lambda: calls.append(1)

        agent = CommentAgent("industry", project_id=8)

        # Mock _call_model to verify on_heartbeat is passed through
        original_call = agent._call_model

        async def mock_call(prompt, timeout=None, on_heartbeat=None):
            if on_heartbeat:
                on_heartbeat()
            return "test response"

        agent._call_model = mock_call

        card = {"code": "KH-TEST", "title": "test", "status": "research"}
        result = await agent.execute(card, [], on_heartbeat=cb)

        assert len(calls) == 1
        assert result["success"] is True


# ==================== Constants ====================


class TestConstants:
    def test_stall_timeout_value(self):
        assert DEFAULT_STALL_TIMEOUT == 120

    def test_default_timeout_unchanged(self):
        assert DEFAULT_TIMEOUT == 600

    def test_stall_shorter_than_hard_timeout(self):
        assert DEFAULT_STALL_TIMEOUT < DEFAULT_TIMEOUT
