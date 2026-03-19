"""C3 gate: phase enforcement and milestone detection."""

import json
import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.services import phase as phase_svc
from backend.services import sse
from tests.conftest import VALID_TOKEN

HEADERS = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.mark.asyncio
async def test_phase1_allowed_technique_returns_201():
    phase_svc.set_current_phase(1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "nmap -sV 10.50.1.100",
            "technique": "service_fingerprint",
            "target_ip": "10.50.1.100",
            "result": "success",
        })
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_phase1_blocked_technique_returns_403():
    phase_svc.set_current_phase(1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "impacket-psexec domain/user:pass@10.50.2.50",
            "technique": "lateral_movement",
            "target_ip": "10.50.2.50",
            "result": "success",
        })
    assert r.status_code == 403
    assert "not allowed in phase 1" in r.json()["detail"]


@pytest.mark.asyncio
async def test_phase2_allows_lateral_movement():
    phase_svc.set_current_phase(2)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "impacket-psexec domain/user:pass@10.50.2.50",
            "technique": "lateral_movement",
            "target_ip": "10.50.2.50",
            "result": "success",
        })
    assert r.status_code == 201
    # Reset
    phase_svc.set_current_phase(1)


@pytest.mark.asyncio
async def test_heartbeat_allowed_in_every_phase():
    for p in (1, 2, 3):
        phase_svc.set_current_phase(p)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r = await client.post("/api/events", headers=HEADERS, json={
                "team_id": "team-alpha",
                "command": "__heartbeat__",
                "technique": "heartbeat",
                "target_ip": "0.0.0.0",
                "result": "success",
            })
        assert r.status_code == 201, f"Heartbeat rejected in phase {p}"
    phase_svc.set_current_phase(1)


@pytest.mark.asyncio
async def test_milestone_triggers_on_dmz_compromise():
    """Successful sqli in DMZ should trigger dmz_compromise milestone."""
    phase_svc.set_current_phase(1)
    transport = ASGITransport(app=app)

    # Subscribe to SSE to check milestone broadcast
    q = await sse.subscribe()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "sqlmap -u http://10.50.1.100/login",
            "technique": "sqli",
            "target_ip": "10.50.1.100",
            "result": "success",
        })
    assert r.status_code == 201
    body = r.json()
    assert body["milestone"] == "dmz_compromise"

    # Check SSE received milestone event
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    milestone_msgs = [i for i in items if i["event"] == "milestone"]
    assert len(milestone_msgs) >= 1
    m = json.loads(milestone_msgs[0]["data"])
    assert m["key"] == "dmz_compromise"
    assert m["score"] > 0

    sse.unsubscribe(q)


@pytest.mark.asyncio
async def test_recon_does_not_trigger_milestone():
    """A successful port_scan should NOT trigger any milestone."""
    phase_svc.set_current_phase(1)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "nmap 10.50.1.100",
            "technique": "port_scan",
            "target_ip": "10.50.1.100",
            "result": "success",
        })
    assert r.status_code == 201
    assert r.json()["milestone"] is None
