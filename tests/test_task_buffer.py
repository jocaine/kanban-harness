"""Layer 2 unit tests: core/task_buffer.py — TaskBufferManager

Pure unit tests, no DB, no network. Validates:
- State transitions (create → append → mark_done)
- Concurrent subscriber notification
- TTL eviction
- Max task cap
"""

import asyncio
import time
import pytest

from core.task_buffer import TaskBufferManager, TaskState, TaskChunk


@pytest.fixture
def buf():
    return TaskBufferManager(max_tasks=5, ttl_seconds=1)


class TestBasicLifecycle:
    def test_create_returns_state(self, buf):
        state = buf.create("t1")
        assert isinstance(state, TaskState)
        assert state.task_id == "t1"
        assert state.done is False
        assert state.chunks == []

    def test_get_existing(self, buf):
        buf.create("t1")
        assert buf.get("t1") is not None
        assert buf.get("t1").task_id == "t1"

    def test_get_nonexistent(self, buf):
        assert buf.get("nope") is None

    def test_append_chunk(self, buf):
        buf.create("t1")
        buf.append_chunk("t1", 'data: {"type": "text"}\n\n')
        state = buf.get("t1")
        assert len(state.chunks) == 1
        assert state.chunks[0].index == 0
        assert state.chunks[0].data == 'data: {"type": "text"}\n\n'

    def test_append_to_nonexistent_is_noop(self, buf):
        buf.append_chunk("ghost", "data")  # should not raise

    def test_mark_done(self, buf):
        buf.create("t1")
        buf.append_chunk("t1", "chunk1")
        buf.mark_done("t1")
        assert buf.get("t1").done is True

    def test_chunk_index_increments(self, buf):
        buf.create("t1")
        for i in range(5):
            buf.append_chunk("t1", f"chunk{i}")
        state = buf.get("t1")
        assert [c.index for c in state.chunks] == [0, 1, 2, 3, 4]


class TestSubscriberNotification:
    @pytest.mark.asyncio
    async def test_subscribe_gets_notified_on_append(self, buf):
        buf.create("t1")
        event = buf.subscribe("t1")
        assert event is not None
        assert not event.is_set()

        buf.append_chunk("t1", "data")
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_subscribe_gets_notified_on_done(self, buf):
        buf.create("t1")
        event = buf.subscribe("t1")
        buf.mark_done("t1")
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, buf):
        buf.create("t1")
        e1 = buf.subscribe("t1")
        e2 = buf.subscribe("t1")
        buf.append_chunk("t1", "data")
        assert e1.is_set()
        assert e2.is_set()

    def test_subscribe_nonexistent_returns_none(self, buf):
        assert buf.subscribe("ghost") is None

    def test_unsubscribe(self, buf):
        buf.create("t1")
        event = buf.subscribe("t1")
        buf.unsubscribe("t1", event)
        assert event not in buf.get("t1").subscribers

    @pytest.mark.asyncio
    async def test_subscriber_can_wait_for_chunks(self, buf):
        buf.create("t1")
        event = buf.subscribe("t1")
        received = []

        async def consumer():
            while True:
                event.clear()
                state = buf.get("t1")
                if state.done:
                    break
                await asyncio.wait_for(event.wait(), timeout=1)
                received.append(len(state.chunks))

        async def producer():
            await asyncio.sleep(0.01)
            buf.append_chunk("t1", "a")
            await asyncio.sleep(0.01)
            buf.append_chunk("t1", "b")
            await asyncio.sleep(0.01)
            buf.mark_done("t1")

        await asyncio.gather(consumer(), producer())
        assert len(received) >= 2


class TestEviction:
    def test_ttl_eviction(self, buf):
        buf.create("t1")
        buf.mark_done("t1")
        # Manually backdate
        buf.get("t1").created_at = time.time() - 10
        # Trigger eviction via create
        buf.create("t2")
        assert buf.get("t1") is None
        assert buf.get("t2") is not None

    def test_max_tasks_eviction(self, buf):
        for i in range(5):
            buf.create(f"t{i}")
        # All 5 exist
        assert buf.get("t0") is not None
        # Creating 6th should evict oldest
        buf.create("t5")
        assert buf.get("t0") is None
        assert buf.get("t5") is not None

    def test_only_done_tasks_evicted_by_ttl(self, buf):
        buf.create("running")
        buf.create("done")
        buf.mark_done("done")
        # Backdate both
        buf.get("running").created_at = time.time() - 10
        buf.get("done").created_at = time.time() - 10
        buf.create("trigger")
        # done task evicted, running task kept (TTL only applies to done)
        assert buf.get("done") is None
        assert buf.get("running") is not None
