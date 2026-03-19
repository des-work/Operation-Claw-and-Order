"""SSE broadcast — per-client asyncio.Queue fan-out with disconnect cleanup."""

import asyncio
import json
import logging
from typing import Any

log = logging.getLogger("sse")

# Max queued messages per client before we consider them dead/stuck
SSE_MAX_QUEUE_SIZE = 256

# All connected client queues
_clients: set[asyncio.Queue] = set()


def client_count() -> int:
    return len(_clients)


async def subscribe() -> asyncio.Queue:
    """Register a new SSE client. Returns its personal queue."""
    q: asyncio.Queue = asyncio.Queue(maxsize=SSE_MAX_QUEUE_SIZE)
    _clients.add(q)
    log.info("SSE client connected (%d total)", len(_clients))
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    """Remove a client queue on disconnect."""
    _clients.discard(q)
    log.info("SSE client disconnected (%d remaining)", len(_clients))


async def broadcast(event_type: str, data: dict[str, Any]) -> None:
    """Fan out an event to every connected client. Evicts dead clients."""
    payload = json.dumps(data)
    dead: list[asyncio.Queue] = []
    for q in _clients:
        try:
            q.put_nowait({"event": event_type, "data": payload})
        except asyncio.QueueFull:
            dead.append(q)
    if dead:
        for q in dead:
            _clients.discard(q)
        log.warning("Evicted %d dead SSE client(s) (queue full)", len(dead))
