"""B5 gate: SSE fan-out, disconnect cleanup, no leaked queues."""

import asyncio
import json
import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.services import sse
from tests.conftest import VALID_TOKEN

HEADERS = {"Authorization": f"Bearer {VALID_TOKEN}"}


def _make_event(n: int) -> dict:
    return {
        "team_id": "team-alpha",
        "command": f"nmap -sV 10.50.1.{n}",
        "technique": "service_fingerprint",
        "target_ip": f"10.50.1.{n}",
        "result": "success",
    }


@pytest.mark.asyncio
async def test_sse_fanout_to_multiple_clients():
    """3 clients subscribe, 5 events broadcast, each client gets all 5."""
    q1 = await sse.subscribe()
    q2 = await sse.subscribe()
    q3 = await sse.subscribe()

    assert sse.client_count() >= 3

    for i in range(5):
        await sse.broadcast("new_event", {"n": i})

    for q in (q1, q2, q3):
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert len(items) == 5
        assert all(item["event"] == "new_event" for item in items)
        parsed = [json.loads(item["data"]) for item in items]
        assert [p["n"] for p in parsed] == [0, 1, 2, 3, 4]

    # Cleanup
    sse.unsubscribe(q1)
    sse.unsubscribe(q2)
    sse.unsubscribe(q3)


@pytest.mark.asyncio
async def test_no_leaked_queues_after_disconnect():
    """After all clients disconnect, client set should be empty."""
    q1 = await sse.subscribe()
    q2 = await sse.subscribe()
    initial = sse.client_count()
    assert initial >= 2

    sse.unsubscribe(q1)
    sse.unsubscribe(q2)

    assert sse.client_count() == initial - 2


@pytest.mark.asyncio
async def test_events_trigger_sse_broadcast():
    """POST /api/events actually places message on SSE queues."""
    q = await sse.subscribe()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for i in range(1, 4):
            r = await client.post("/api/events", headers=HEADERS, json=_make_event(i))
            assert r.status_code == 201

    items = []
    while not q.empty():
        items.append(q.get_nowait())
    assert len(items) == 3

    events = [json.loads(item["data"]) for item in items]
    ips = {e["target_ip"] for e in events}
    assert ips == {"10.50.1.1", "10.50.1.2", "10.50.1.3"}

    sse.unsubscribe(q)


@pytest.mark.asyncio
async def test_disconnected_client_does_not_receive():
    """After unsubscribe, broadcast does not put messages on that queue."""
    q = await sse.subscribe()
    sse.unsubscribe(q)

    await sse.broadcast("new_event", {"test": True})
    assert q.empty()
