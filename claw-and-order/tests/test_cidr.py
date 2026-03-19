"""A4 gate: IP in range → 201, IP out of range → 403, heartbeat → 201."""

import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from backend.services import phase as phase_svc
from tests.conftest import VALID_TOKEN

HEADERS = {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.mark.asyncio
async def test_ip_in_dmz_returns_201():
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
async def test_ip_in_lan_returns_201():
    phase_svc.set_current_phase(2)  # internal_scan is Phase 2
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "nmap 10.50.2.50",
            "technique": "internal_scan",
            "target_ip": "10.50.2.50",
            "result": "success",
        })
    phase_svc.set_current_phase(1)
    assert r.status_code == 201


@pytest.mark.asyncio
async def test_ip_outside_range_returns_403():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "nmap 192.168.1.1",
            "technique": "port_scan",
            "target_ip": "192.168.1.1",
            "result": "success",
        })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_heartbeat_bypasses_cidr_returns_201():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "__heartbeat__",
            "technique": "heartbeat",
            "target_ip": "0.0.0.0",
            "result": "success",
        })
    assert r.status_code == 201
