"""Reconciliation Loop tests: scheduler/engine.py — _reconcile_running_sessions

Validates:
- Running agent cancelled when card moved to done
- Running agent cancelled when card archived
- Running agent cancelled when card moved to unexpected status
- Agent NOT cancelled when card is in expected status
- Agent NOT cancelled when DB query fails (safe default)
- Agent NOT cancelled when input_context has no requirement_id
- cancel_session marks session failed with reason, no retry
- _extract_requirement_id handles valid/invalid JSON
- Reconciliation runs before dispatch in _tick
"""

import asyncio
import json
import os
import tempfile
import time

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

_test_db = tempfile.mktemp(suffix=".db")
os.environ["DB_PATH"] = _test_db

from core.database import init_db, DB_PATH
from core.session_manager import SessionManager

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


# ==================== cancel_session ====================


class TestCancelSession:
    async def test_cancel_marks_failed_with_reason(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            input_context='{"requirement_id": 100}',
        )

        result = await sm.cancel_session(session_id, "card_status:done")

        assert result["status"] == "failed"
        assert "cancelled:card_status:done" in result["error_message"]

    async def test_cancel_does_not_trigger_retry(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="industry", trigger_type="test",
            input_context='{"requirement_id": 100}',
        )

        await sm.cancel_session(session_id, "card_moved:done")

        # No retry session should be created
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM agent_sessions WHERE parent_session_id=?",
                (session_id,),
            )
            retry = await cursor.fetchone()
            assert retry is None

    async def test_cancel_kills_tracked_process(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="coach_dev", trigger_type="test",
            input_context='{"requirement_id": 200}',
        )
        mock_proc = MagicMock()
        mock_proc.returncode = None
        mock_proc.pid = 55555
        sm.register_process(session_id, mock_proc)

        await sm.cancel_session(session_id, "reconciliation")

        mock_proc.kill.assert_called_once()
        assert session_id not in sm._processes
        assert session_id not in sm._heartbeats

    async def test_cancel_idempotent_on_already_completed(self, sm):
        session_id = await sm.create_session(
            project_id=8, agent_role="pm", trigger_type="test",
        )
        await sm.complete_session(session_id, "done normally")

        # Cancel on already-completed session — should not crash
        result = await sm.cancel_session(session_id, "late_reconcile")
        # Status stays completed (WHERE status='running' won't match)
        assert result["status"] == "completed"


# ==================== _extract_requirement_id ====================


class TestExtractReentId:
    def test_valid_json(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        assert engine._extract_requirement_id('{"requirement_id": 42}') == 42
        assert engine._extract_requirement_id('{"requirement_id": 100, "code": "KH-050"}') == 100

    def test_empty_string(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        assert engine._extract_requirement_id("") is None
        assert engine._extract_requirement_id(None) is None

    def test_invalid_json(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        assert engine._extract_requirement_id("not json") is None
        assert engine._extract_requirement_id("{broken") is None

    def test_missing_key(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        assert engine._extract_requirement_id('{"code": "KH-001"}') is None


# ==================== _get_card_status ====================


class TestGetCardStatus:
    async def test_returns_status_for_existing_card(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO projects (id, name, prefix) VALUES (99, 'Test', 'TST')"
            )
            await db.execute(
                "INSERT OR IGNORE INTO versions (id, project_id, name) VALUES (99, 99, 'v1')"
            )
            await db.execute(
                "INSERT OR REPLACE INTO requirements (id, version_id, title, status, archived) "
                "VALUES (500, 99, 'Test Card', 'dev', 0)"
            )
            await db.commit()

        status = await engine._get_card_status(500)
        assert status == "dev"

    async def test_returns_archived_for_archived_card(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO projects (id, name, prefix) VALUES (99, 'Test', 'TST')"
            )
            await db.execute(
                "INSERT OR IGNORE INTO versions (id, project_id, name) VALUES (99, 99, 'v1')"
            )
            await db.execute(
                "INSERT OR REPLACE INTO requirements (id, version_id, title, status, archived) "
                "VALUES (501, 99, 'Archived Card', 'dev', 1)"
            )
            await db.commit()

        status = await engine._get_card_status(501)
        assert status == "archived"

    async def test_returns_archived_for_deleted_card(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        status = await engine._get_card_status(99999)
        assert status == "archived"

    async def test_returns_none_on_db_error(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        with patch("scheduler.engine.aiosqlite.connect", side_effect=Exception("DB down")):
            status = await engine._get_card_status(1)
            assert status is None


# ==================== _reconcile_running_sessions ====================


class TestReconcileRunning:
    async def _setup_card(self, req_id: int, status: str = "dev", archived: int = 0):
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR IGNORE INTO projects (id, name, prefix) VALUES (99, 'Test', 'TST')"
            )
            await db.execute(
                "INSERT OR IGNORE INTO versions (id, project_id, name) VALUES (99, 99, 'v1')"
            )
            await db.execute(
                "INSERT OR REPLACE INTO requirements (id, version_id, title, status, archived) "
                "VALUES (?, 99, 'Test', ?, ?)",
                (req_id, status, archived),
            )
            await db.commit()

    async def test_cancels_when_card_done(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        await self._setup_card(600, status="done")

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="coach_dev", trigger_type="test",
            input_context='{"requirement_id": 600}',
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "failed"
        assert "card_status:done" in session["error_message"]

    async def test_cancels_when_card_archived(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        await self._setup_card(601, status="dev", archived=1)

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="coach_dev", trigger_type="test",
            input_context='{"requirement_id": 601}',
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "failed"
        assert "card_status:archived" in session["error_message"]

    async def test_cancels_when_card_moved_to_unexpected_status(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        await self._setup_card(602, status="testing")

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="coach_dev", trigger_type="test",
            input_context='{"requirement_id": 602}',
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "failed"
        assert "card_moved:testing" in session["error_message"]

    async def test_keeps_running_when_card_in_expected_status(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        await self._setup_card(603, status="dev")

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="coach_dev", trigger_type="test",
            input_context='{"requirement_id": 603}',
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "running"

    async def test_keeps_running_when_db_query_fails(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="industry", trigger_type="test",
            input_context='{"requirement_id": 999}',
        )

        with patch.object(engine, "_get_card_status", return_value=None):
            await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "running"

    async def test_skips_session_without_requirement_id(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="pm", trigger_type="test",
            input_context="",
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "running"

    async def test_industry_cancelled_when_card_not_in_research(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        await self._setup_card(604, status="organizing")

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="industry", trigger_type="test",
            input_context='{"requirement_id": 604}',
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "failed"
        assert "card_moved:organizing" in session["error_message"]

    async def test_industry_kept_when_card_in_research(self):
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        await self._setup_card(605, status="research")

        session_id = await engine.session_manager.create_session(
            project_id=99, agent_role="industry", trigger_type="test",
            input_context='{"requirement_id": 605}',
        )

        await engine._reconcile_running_sessions()

        session = await engine.session_manager.get_session(session_id)
        assert session["status"] == "running"


# ==================== Tick ordering ====================


class TestTickOrdering:
    async def test_reconcile_runs_before_dispatch(self):
        """Verify reconciliation happens before new card dispatch."""
        from scheduler.engine import SchedulerEngine
        engine = SchedulerEngine()

        call_order = []

        original_reconcile = engine._reconcile_running_sessions
        original_find = engine._find_actionable_cards

        async def mock_reconcile():
            call_order.append("reconcile")
            await original_reconcile()

        async def mock_find():
            call_order.append("find_cards")
            return []

        engine._reconcile_running_sessions = mock_reconcile
        engine._find_actionable_cards = mock_find
        engine._peek_pending_events = AsyncMock(return_value=[])
        engine._process_events = AsyncMock()
        engine._recover_stuck_cards = AsyncMock()

        await engine._tick()

        assert call_order == ["reconcile", "find_cards"]
