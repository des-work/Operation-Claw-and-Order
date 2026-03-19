"""GET /api/stream — SSE endpoint for dashboard browsers."""

import asyncio
import logging
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from backend.services.sse import subscribe, unsubscribe

log = logging.getLogger("stream")

router = APIRouter()

SSE_KEEPALIVE_SECONDS = 30


async def _event_generator(q: asyncio.Queue):
    """Yield SSE-formatted messages. Sends keepalive ping every 30s."""
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=SSE_KEEPALIVE_SECONDS)
                event = msg.get("event", "message")
                data = msg.get("data", "{}")
                yield f"event: {event}\ndata: {data}\n\n"
            except asyncio.TimeoutError:
                # SSE keepalive comment to prevent proxy/browser disconnect
                yield ": keepalive\n\n"
    except Exception as e:
        log.error("SSE generator error: %s", e)
    finally:
        unsubscribe(q)


@router.get("/api/stream")
async def stream():
    q = await subscribe()
    return StreamingResponse(
        _event_generator(q),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
