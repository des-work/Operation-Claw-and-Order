"""Webhook routing, partial failures, persistence, and credential-safe transport."""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.models import DiscordOutbox, DiscordWebhookDelivery, DiscordWebhookDestination
from backend.services import discord_worker
from backend.services.discord_webhooks import (
    parse_routes, load_routes, DeliveryError, WebhookSender, deliver_once,
)
from tests.conftest import _session_factory

URL_A = "https://discord.com/api/webhooks/123456789012345678/secret-a"
URL_B = "https://discord.com/api/webhooks/223456789012345678/secret-b"


def routes():
    return parse_routes(json.dumps([
        {"name": "class-a", "url": URL_A, "team_prefixes": ["A_"]},
        {"name": "class-b", "url": URL_B, "team_prefixes": ["B_"]},
    ]))


async def queue(message, key="test:1"):
    async with _session_factory() as session:
        discord_worker.queue_notification(session, message, key)
        await session.commit()


def test_class_routing_and_validation():
    a, b = routes()
    assert a.matches({"type": "vm_offline", "data": {"team_id": "A_01"}})
    assert not b.matches({"type": "vm_offline", "data": {"team_id": "A_01"}})
    assert a.matches({"type": "phase_change"}) and b.matches({"type": "phase_change"})
    assert "secret-a" not in repr(a)
    for url in ["http://127.0.0.1/private", URL_A + "?redirect=bad", URL_A.replace("discord.com", "discord.com.evil.test")]:
        with pytest.raises(ValueError, match="valid Discord webhook URL") as error:
            parse_routes(json.dumps([{"name": "a", "url": url, "all_teams": True}]))
        assert "secret-a" not in str(error.value)
    with pytest.raises(ValueError, match="selectors"):
        parse_routes(json.dumps([{"name": "a", "url": URL_A}]))
    with pytest.raises(ValueError, match="same webhook"):
        parse_routes(json.dumps([{"name": n, "url": URL_A, "all_teams": True} for n in ["a", "b"]]))


@pytest.mark.asyncio
async def test_partial_failure_retries_only_failed_destination():
    await queue({"type": "phase_change", "data": {"phase": 2, "label": "Pivot"}})
    class Sender:
        sent = []
        async def send(self, route, content):
            if route.name == "class-b" and not self.sent.count("class-b"):
                self.sent.append("class-b")
                raise DeliveryError("Discord webhook rate limited", 120)
            self.sent.append(route.name)
            return "123"
    sender = Sender()
    for _ in range(2):
        assert await deliver_once(_session_factory, routes(), sender, discord_worker._format_message) == 1
    async with _session_factory() as session:
        rows = (await session.execute(select(DiscordWebhookDelivery).order_by(DiscordWebhookDelivery.destination))).scalars().all()
        assert rows[0].sent_at and rows[0].message_id == "123"
        assert rows[1].sent_at is None and rows[1].attempts == 1
        assert rows[1].next_attempt.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) + timedelta(seconds=110)
        assert (await session.execute(select(DiscordOutbox))).scalar_one().sent_at is None
        rows[1].next_attempt = datetime.now(timezone.utc) - timedelta(seconds=1)
        (await session.get(DiscordWebhookDestination, "class-b")).retry_after = rows[1].next_attempt
        await session.commit()
    # A new worker reads persisted destination receipts after a restart.
    assert await deliver_once(_session_factory, routes(), sender, discord_worker._format_message) == 1
    assert await deliver_once(_session_factory, routes(), sender, discord_worker._format_message) == 0
    assert sender.sent.count("class-a") == 1
    async with _session_factory() as session:
        assert (await session.execute(select(DiscordOutbox))).scalar_one().sent_at


@pytest.mark.asyncio
async def test_unmatched_team_is_retained_without_cross_class_delivery():
    await queue({"type": "vm_offline", "data": {"team_id": "C_01"}})
    class NeverSend:
        async def send(self, *_):
            pytest.fail("Unmatched team's alert must not go to another class")
    assert await deliver_once(_session_factory, routes(), NeverSend(), discord_worker._format_message) == 1
    async with _session_factory() as session:
        assert not (await session.execute(select(DiscordWebhookDelivery))).scalars().all()
        row = (await session.execute(select(DiscordOutbox))).scalar_one()
        assert row.sent_at is None and row.attempts == 1


@pytest.mark.asyncio
async def test_route_cooldown_survives_restart_and_other_class_keeps_delivering():
    await queue({"type": "vm_offline", "data": {"team_id": "A_01"}}, "a:1")
    await queue({"type": "vm_offline", "data": {"team_id": "A_02"}}, "a:2")
    await queue({"type": "vm_offline", "data": {"team_id": "B_01"}}, "b:1")
    class Sender:
        calls = []
        async def send(self, route, content):
            self.calls.append(route.name)
            if route.name == "class-a":
                raise DeliveryError("rate limited", 120)
            return "456"
    sender = Sender()
    for _ in range(4):
        await deliver_once(_session_factory, routes(), sender, discord_worker._format_message)
    assert sender.calls == ["class-a", "class-b"]
    async with _session_factory() as session:
        assert (await session.get(DiscordWebhookDestination, "class-a")).retry_after.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
        sent = (await session.execute(select(DiscordWebhookDelivery).where(DiscordWebhookDelivery.sent_at.is_not(None)))).scalars().all()
        assert [row.destination for row in sent] == ["class-b"]


@pytest.mark.asyncio
async def test_revoked_hook_waits_for_rotation():
    await queue({"type": "vm_offline", "data": {"team_id": "A_01"}})
    class Revoked:
        async def send(self, *_):
            raise DeliveryError("Discord webhook HTTP 404", blocked=True)
    await deliver_once(_session_factory, routes(), Revoked(), discord_worker._format_message)
    async with _session_factory() as session:
        destination = await session.get(DiscordWebhookDestination, "class-a")
        assert destination.blocked
        delivery = (await session.execute(select(DiscordWebhookDelivery))).scalar_one()
        delivery.next_attempt = datetime.now(timezone.utc) - timedelta(seconds=1)
        destination.retry_after = delivery.next_attempt
        await session.commit()
    assert await deliver_once(_session_factory, routes(), Revoked(), discord_worker._format_message) == 0
    repaired = parse_routes(json.dumps([{"name": "class-a", "url": URL_A + "-rotated", "team_prefixes": ["A_"]}]))
    class Working:
        async def send(self, *_):
            return "789"
    assert await deliver_once(_session_factory, repaired, Working(), discord_worker._format_message) == 1
    async with _session_factory() as session:
        assert (await session.execute(select(DiscordOutbox))).scalar_one().sent_at


@pytest.mark.asyncio
async def test_global_limit_stops_all_destinations():
    await queue({"type": "phase_change", "data": {"phase": 2}})
    class Limited:
        calls = 0
        async def send(self, *_):
            self.calls += 1
            raise DeliveryError("global rate limited", 60, global_limit=True)
    sender = Limited()
    await deliver_once(_session_factory, routes(), sender, discord_worker._format_message)
    assert await deliver_once(_session_factory, routes(), sender, discord_worker._format_message) == 0
    assert sender.calls == 1


class Response:
    status = 200
    headers = {}
    body = {"id": "456"}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass
    async def json(self):
        return self.body


@pytest.mark.asyncio
async def test_http_receipt_mentions_rate_limits_and_safe_errors():
    sender = WebhookSender()
    await sender.close()
    response = Response()
    class Session:
        def post(self, url, **options):
            assert options["params"] == {"wait": "true"}
            assert options["json"]["allowed_mentions"] == {"parse": []}
            assert options["allow_redirects"] is False
            return response
    sender.session = Session()
    assert await sender.send(routes()[0], "@everyone") == "456"
    response.headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "20.5"}
    assert await sender.send(routes()[0], "test") == "456"
    assert sender.cooldowns["class-a"] == 20.5
    response.headers = {"Retry-After": "80"}
    response.status, response.body = 429, {"retry_after": 75.5}
    with pytest.raises(DeliveryError) as error:
        await sender.send(routes()[0], "test")
    assert error.value.retry_after == 80
    response.status = 403
    with pytest.raises(DeliveryError, match="HTTP 403") as error:
        await sender.send(routes()[0], "test")
    assert error.value.blocked is True
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_webhook_worker_starts_without_bot_token(monkeypatch):
    for key in ("DISCORD_WEBHOOKS_JSON", "DISCORD_WEBHOOKS_FILE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", URL_A)
    monkeypatch.setattr(discord_worker, "DISCORD_TOKEN", "")
    async def idle():
        await asyncio.Event().wait()
    monkeypatch.setattr(discord_worker, "_outbox_loop", idle)
    task = await discord_worker.start()
    assert discord_worker.connection_status() == "webhook_ready"
    await discord_worker.stop(task)
    assert discord_worker.connection_status() == "disabled"
    assert load_routes()[0].all_teams
