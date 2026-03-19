"""Hardening tests — error handling, edge cases, security boundaries."""

import pytest
from httpx import ASGITransport, AsyncClient

from backend.main import app
from tests.conftest import VALID_TOKEN, TEST_ADMIN_SECRET

HEADERS = {"Authorization": f"Bearer {VALID_TOKEN}"}
ADMIN_HEADERS = {"X-Admin-Secret": TEST_ADMIN_SECRET}


# ── Admin auth tests ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_admin_phase_without_secret_returns_422():
    """Missing X-Admin-Secret header → 422 (Header is required)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/admin/phase", json={"phase": 2})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_admin_phase_with_bad_secret_returns_401():
    """Wrong X-Admin-Secret → 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/admin/phase",
            json={"phase": 2},
            headers={"X-Admin-Secret": "wrong-secret"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_admin_phase_valid_transition():
    """Valid admin request transitions phase and returns 200."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/admin/phase",
            json={"phase": 2, "activated_by": "test-prof"},
            headers=ADMIN_HEADERS,
        )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "phase_changed"
    assert body["phase"] == 2


@pytest.mark.asyncio
async def test_admin_phase_invalid_phase_number():
    """Phase 99 → 400 (out of range)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post(
            "/api/admin/phase",
            json={"phase": 99},
            headers=ADMIN_HEADERS,
        )
    assert r.status_code == 422  # Pydantic rejects ge=1,le=3


# ── Input validation tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_event_invalid_result_enum():
    """result='hacked' is not a valid Literal value → 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "nmap -sV 10.50.1.5",
            "technique": "nmap_scan",
            "target_ip": "10.50.1.5",
            "result": "hacked",
        })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_event_empty_team_id_rejected():
    """Empty team_id → 422 (min_length=1)."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "",
            "command": "nmap",
            "technique": "nmap_scan",
            "target_ip": "10.50.1.5",
            "result": "success",
        })
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_event_missing_required_field():
    """Missing technique field → 422."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "nmap",
            "target_ip": "10.50.1.5",
            "result": "success",
        })
    assert r.status_code == 422


# ── Health endpoint ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_endpoint_returns_status():
    """GET /api/health returns status, db, phase info."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["db"] == "ok"
    assert "phase" in body
    assert "sse_clients" in body


# ── Duplicate milestone protection ────────────────────────────────────────

@pytest.mark.asyncio
async def test_duplicate_milestone_returns_none_second_time():
    """Second DMZ compromise event should NOT produce a duplicate milestone."""
    from backend.services.phase import set_current_phase
    set_current_phase(1)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # First event triggers milestone
        r1 = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "exploit http",
            "technique": "http_exploit",
            "target_ip": "10.50.1.5",
            "result": "success",
        })
        assert r1.status_code == 201
        body1 = r1.json()
        assert body1["milestone"] == "dmz_compromise"

        # Second event — same technique, same zone — should NOT trigger again
        r2 = await client.post("/api/events", headers=HEADERS, json={
            "team_id": "team-alpha",
            "command": "exploit http again",
            "technique": "http_exploit",
            "target_ip": "10.50.1.10",
            "result": "success",
        })
        assert r2.status_code == 201
        body2 = r2.json()
        assert body2["milestone"] is None


# ── Phase enforcement edge cases ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_phase_endpoint_returns_current_state():
    """GET /api/phase returns current phase info."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/phase")
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == 1
    assert "allowed_techniques" in body
    assert isinstance(body["allowed_techniques"], list)


@pytest.mark.asyncio
async def test_teams_endpoint_returns_list():
    """GET /api/teams returns team list with scores."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/teams")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert body[0]["id"] == "team-alpha"
    assert "total_score" in body[0]
    assert "milestones" in body[0]
