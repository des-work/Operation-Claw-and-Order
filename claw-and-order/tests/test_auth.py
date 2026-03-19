"""A3 gate: valid token → 201, invalid token → 401."""

import pytest
from httpx import ASGITransport, AsyncClient
from backend.main import app
from tests.conftest import VALID_TOKEN

EVENT_PAYLOAD = {
    "team_id": "team-alpha",
    "command": "nmap -sV 10.50.1.100",
    "technique": "service_fingerprint",
    "target_ip": "10.50.1.100",
    "result": "success",
}


@pytest.mark.asyncio
async def test_valid_token_returns_201():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/events",
            json=EVENT_PAYLOAD,
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
    assert r.status_code == 201
    assert r.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_invalid_token_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/events",
            json=EVENT_PAYLOAD,
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", json=EVENT_PAYLOAD)
    assert r.status_code == 401
