"""SSE broadcast for board state changes (card moved, created, updated)."""
import asyncio
import json
import logging
import time
from fastapi import APIRouter, Request
from starlette.responses import StreamingResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_subscribers: list[asyncio.Queue] = []


def broadcast(event_type: str, data: dict):
    msg = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        _subscribers.remove(q)


async def _event_stream(req: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=64)
    _subscribers.append(q)
    try:
        yield "event: connected\ndata: {}\n\n"
        while True:
            if await req.is_disconnected():
                break
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25)
                yield msg
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
    finally:
        if q in _subscribers:
            _subscribers.remove(q)


@router.get("/board/events")
async def board_sse(req: Request):
    return StreamingResponse(
        _event_stream(req),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
