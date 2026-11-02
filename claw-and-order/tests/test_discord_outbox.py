"""Alerts remain durable when Discord is unavailable and retry after recovery."""

from datetime import datetime, timezone, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.main import app
from backend.models import DiscordOutbox
from backend.models import Team
from backend.services import discord_worker, watchdog
from tests.conftest import VALID_TOKEN, _session_factory


@pytest.mark.asyncio
async def test_milestone_alert_retries_without_losing_payload(monkeypatch):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/events", headers={"Authorization": f"Bearer {VALID_TOKEN}"}, json={
            "team_id": "team-alpha", "command": "sqlmap -u http://10.50.1.100/login",
            "technique": "sqli", "target_ip": "10.50.1.100", "result": "success",
        })
    assert response.status_code == 201

    monkeypatch.setattr(discord_worker, "async_session", _session_factory)
    async with _session_factory() as session:
        alert = (await session.execute(select(DiscordOutbox))).scalar_one()
        assert alert.message_type == "milestone"
        assert "team-alpha" in alert.payload_json
        assert alert.sent_at is None

    class FlakyChannel:
        def __init__(self):
            self.calls = 0
            self.sent = []

        async def send(self, content):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Discord unavailable")
            self.sent.append(content)

    channel = FlakyChannel()
    monkeypatch.setattr(discord_worker, "_channel", channel)
    assert await discord_worker._outbox_once() == 1

    async with _session_factory() as session:
        alert = (await session.execute(select(DiscordOutbox))).scalar_one()
        assert alert.attempts == 1
        assert alert.sent_at is None
        alert.next_attempt = datetime.now(timezone.utc) - timedelta(seconds=1)
        await session.commit()

    assert await discord_worker._outbox_once() == 1
    assert await discord_worker._outbox_once() == 0
    assert len(channel.sent) == 1
    assert "team-alpha" in channel.sent[0]

    async with _session_factory() as session:
        alert = (await session.execute(select(DiscordOutbox))).scalar_one()
        assert alert.sent_at is not None


@pytest.mark.asyncio
async def test_unavailable_channel_keeps_alert_pending(monkeypatch):
    monkeypatch.setattr(discord_worker, "async_session", _session_factory)
    async with _session_factory() as session:
        discord_worker.queue_notification(session, {
            "type": "vm_offline", "data": {"team_id": "team-alpha"},
        }, "offline:team-alpha:1")
        await session.commit()
    monkeypatch.setattr(discord_worker, "_channel", None)
    assert await discord_worker._outbox_once() == 0
    async with _session_factory() as session:
        assert (await session.execute(select(DiscordOutbox))).scalar_one().sent_at is None


@pytest.mark.asyncio
async def test_watchdog_persists_one_alert_per_outage_after_restart(monkeypatch):
    monkeypatch.setattr(watchdog, "async_session", _session_factory)
    watchdog._alerted.clear()
    async with _session_factory() as session:
        team = await session.get(Team, "team-alpha")
        team.last_seen = datetime.now(timezone.utc) - timedelta(minutes=6)
        await session.commit()

    await watchdog._check_once()
    watchdog._alerted.clear()  # simulate process restart
    await watchdog._check_once()
    async with _session_factory() as session:
        alerts = (await session.execute(select(DiscordOutbox))).scalars().all()
        assert len(alerts) == 1
        assert alerts[0].message_type == "vm_offline"
    watchdog._alerted.clear()
