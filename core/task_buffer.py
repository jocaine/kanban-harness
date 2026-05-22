"""In-memory streaming buffer for background chat tasks (v0.7)."""

import asyncio
import time
from dataclasses import dataclass, field
from collections import OrderedDict


@dataclass
class TaskChunk:
    index: int
    data: str
    timestamp: float


@dataclass
class TaskState:
    task_id: str
    chunks: list[TaskChunk] = field(default_factory=list)
    done: bool = False
    subscribers: list[asyncio.Event] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


class TaskBufferManager:
    """Process-global buffer for active chat tasks.

    Stores SSE chunks in memory while task is running.
    Supports multiple concurrent subscribers per task.
    Auto-evicts completed tasks after TTL.
    """

    def __init__(self, max_tasks: int = 100, ttl_seconds: int = 3600):
        self._tasks: OrderedDict[str, TaskState] = OrderedDict()
        self._max_tasks = max_tasks
        self._ttl = ttl_seconds

    def create(self, task_id: str) -> TaskState:
        self._evict_expired()
        state = TaskState(task_id=task_id)
        self._tasks[task_id] = state
        return state

    def get(self, task_id: str) -> TaskState | None:
        return self._tasks.get(task_id)

    def append_chunk(self, task_id: str, data: str):
        state = self._tasks.get(task_id)
        if not state:
            return
        chunk = TaskChunk(index=len(state.chunks), data=data, timestamp=time.time())
        state.chunks.append(chunk)
        for event in state.subscribers:
            event.set()

    def mark_done(self, task_id: str):
        state = self._tasks.get(task_id)
        if state:
            state.done = True
            for event in state.subscribers:
                event.set()

    def subscribe(self, task_id: str) -> asyncio.Event | None:
        state = self._tasks.get(task_id)
        if not state:
            return None
        event = asyncio.Event()
        state.subscribers.append(event)
        return event

    def unsubscribe(self, task_id: str, event: asyncio.Event):
        state = self._tasks.get(task_id)
        if state and event in state.subscribers:
            state.subscribers.remove(event)

    def _evict_expired(self):
        now = time.time()
        expired = [k for k, v in self._tasks.items()
                   if v.done and (now - v.created_at) > self._ttl]
        for k in expired:
            del self._tasks[k]
        while len(self._tasks) > self._max_tasks:
            self._tasks.popitem(last=False)


task_buffers = TaskBufferManager()
