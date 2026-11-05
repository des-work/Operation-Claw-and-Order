"""Discord webhook delivery with durable, independent receipts per destination."""

import asyncio
import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from sqlalchemy import select, func, or_

from backend.models import DiscordOutbox, DiscordWebhookDelivery, DiscordWebhookDestination

log = logging.getLogger("discord_webhooks")


@dataclass(frozen=True)
class Route:
    name: str
    url: str = field(repr=False)
    team_ids: tuple[str, ...] = ()
    team_prefixes: tuple[str, ...] = ()
    all_teams: bool = False

    def matches(self, message: dict) -> bool:
        if message.get("type") == "phase_change":
            return True
        team_id = message.get("data", {}).get("team_id", "")
        return self.all_teams or team_id in self.team_ids or any(
            team_id.startswith(prefix) for prefix in self.team_prefixes)


def parse_routes(value: str) -> list[Route]:
    try:
        entries = json.loads(value)
    except (ValueError, TypeError):
        raise ValueError("Discord webhook configuration must be a JSON array") from None
    if not isinstance(entries, list) or not entries:
        raise ValueError("Discord webhook configuration must be a nonempty JSON array")
    routes, names, endpoints = [], set(), set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) - {"name", "url", "team_ids", "team_prefixes", "all_teams"}:
            raise ValueError("Invalid Discord webhook route fields")
        name, url = entry.get("name"), entry.get("url", "")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name) or name in names or name == "__global__":
            raise ValueError("Webhook destination names must be unique stable identifiers")
        # Only Discord incoming webhooks; never arbitrary URLs or credential-bearing redirects.
        if not isinstance(url, str) or not re.fullmatch(
            r"https://(?:discord\.com|canary\.discord\.com|ptb\.discord\.com)/api/(?:v[0-9]+/)?webhooks/[0-9]{15,22}/[A-Za-z0-9_-]+", url
        ):
            raise ValueError(f"Destination {name} needs a valid Discord webhook URL")
        endpoint = tuple(urlsplit(url).path.split("/")[-2:])
        if endpoint in endpoints:
            raise ValueError("The same webhook cannot appear in multiple routes")
        selectors = []
        for key in ("team_ids", "team_prefixes"):
            values = entry.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v) for v in values):
                raise ValueError(f"Destination {name} has invalid {key}")
            selectors.append(tuple(values))
        all_teams = entry.get("all_teams", False)
        if not isinstance(all_teams, bool) or not (all_teams or any(selectors)):
            raise ValueError(f"Destination {name} needs team selectors or explicit all_teams=true")
        routes.append(Route(name, url, *selectors, all_teams))
        names.add(name)
        endpoints.add(endpoint)
    return routes


def load_routes() -> list[Route]:
    file = os.getenv("DISCORD_WEBHOOKS_FILE", "")
    raw = os.getenv("DISCORD_WEBHOOKS_JSON", "")
    single = os.getenv("DISCORD_WEBHOOK_URL", "")
    if sum(bool(value) for value in (file, raw, single)) > 1:
        raise ValueError("Use exactly one Discord webhook configuration source")
    if file:
        try:
            raw = Path(file).read_text()
        except OSError:
            raise ValueError("Discord webhook configuration file cannot be read") from None
    if single:
        raw = json.dumps([{"name": "instructors", "url": single, "all_teams": True}])
    return parse_routes(raw) if raw else []


class DeliveryError(Exception):
    def __init__(self, message: str, retry_after: float = 0, *, blocked=False, global_limit=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.blocked = blocked
        self.global_limit = global_limit


class WebhookSender:
    def __init__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        self.cooldowns = {}

    async def close(self):
        await self.session.close()

    async def send(self, route: Route, content: str) -> str:
        try:
            async with self.session.post(route.url, params={"wait": "true"},
                    json={"content": content, "allowed_mentions": {"parse": []}},
                    allow_redirects=False) as response:
                if response.status == 429:
                    global_limit = response.headers.get("X-RateLimit-Global") == "true"
                    try:
                        body = await response.json()
                    except (ValueError, TypeError, aiohttp.ContentTypeError):
                        body = {}
                    if not isinstance(body, dict):
                        body = {}
                    delays = [1.0]
                    for raw in (response.headers.get("Retry-After"), body.get("retry_after")):
                        try:
                            value = float(raw)
                            if math.isfinite(value) and value > 0:
                                delays.append(value)
                        except (TypeError, ValueError):
                            pass
                    global_limit = global_limit or body.get("global") is True
                    raise DeliveryError("Discord webhook rate limited", max(delays), global_limit=global_limit)
                if response.status != 200:
                    raise DeliveryError(f"Discord webhook HTTP {response.status}",
                                        blocked=response.status in (401, 403, 404))
                body = await response.json()
                if not isinstance(body, dict) or not re.fullmatch(r"[0-9]+", str(body.get("id", ""))):
                    raise DeliveryError("Discord webhook returned no message receipt")
                self.cooldowns.pop(route.name, None)
                if response.headers.get("X-RateLimit-Remaining") == "0":
                    try:
                        delay = float(response.headers.get("X-RateLimit-Reset-After", "0"))
                        if math.isfinite(delay) and delay > 0:
                            self.cooldowns[route.name] = delay
                    except (TypeError, ValueError):
                        pass
                return str(body["id"])
        except DeliveryError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            # aiohttp exceptions can contain the secret webhook URL.
            raise DeliveryError("Discord webhook connection or response failed") from None


async def deliver_once(session_factory, routes: list[Route], sender, formatter) -> int:
    """Freeze recipient names once; persist each successful receipt independently."""
    now = datetime.now(timezone.utc)
    by_name = {route.name: route for route in routes}
    async with session_factory() as session, session.begin():
        # Persist bucket cooldowns and revoked-hook quarantine across worker restarts.
        for route in routes:
            fingerprint = hashlib.sha256(route.url.encode()).hexdigest()
            state = await session.get(DiscordWebhookDestination, route.name)
            if state is None:
                session.add(DiscordWebhookDestination(name=route.name, credential_hash=fingerprint, retry_after=now))
            elif state.credential_hash != fingerprint:
                state.credential_hash, state.blocked, state.retry_after = fingerprint, False, now
        await session.flush()
        global_gate = await session.get(DiscordWebhookDestination, "__global__")
        if global_gate and global_gate.retry_after.replace(tzinfo=timezone.utc) > now:
            return 0
        unplanned = (await session.execute(select(DiscordOutbox).where(
            DiscordOutbox.sent_at.is_(None), DiscordOutbox.next_attempt <= now,
            ~select(DiscordWebhookDelivery.outbox_id).where(
                DiscordWebhookDelivery.outbox_id == DiscordOutbox.id).exists()
        ).order_by(DiscordOutbox.id).limit(1).with_for_update(skip_locked=True))).scalar_one_or_none()
        if unplanned:
            message = {"type": unplanned.message_type, "data": json.loads(unplanned.payload_json)}
            targets = [route for route in routes if route.matches(message)]
            if not targets:
                unplanned.attempts += 1
                unplanned.next_attempt = now + timedelta(seconds=60)
                log.error("Alert %d has no matching webhook destination; retained for routing repair", unplanned.id)
            for route in targets:
                session.add(DiscordWebhookDelivery(outbox_id=unplanned.id, destination=route.name))
            await session.flush()

        delivery = (await session.execute(select(DiscordWebhookDelivery).outerjoin(
            DiscordWebhookDestination, DiscordWebhookDestination.name == DiscordWebhookDelivery.destination
        ).where(
            DiscordWebhookDelivery.sent_at.is_(None), DiscordWebhookDelivery.next_attempt <= datetime.now(timezone.utc),
            or_(DiscordWebhookDestination.name.is_(None),
                (DiscordWebhookDestination.blocked.is_(False) & (DiscordWebhookDestination.retry_after <= datetime.now(timezone.utc))))
        ).order_by(DiscordWebhookDelivery.next_attempt, DiscordWebhookDelivery.outbox_id,
                   DiscordWebhookDelivery.destination).limit(1).with_for_update(of=DiscordWebhookDelivery, skip_locked=True))).scalar_one_or_none()
        if delivery is None:
            return 1 if unplanned else 0
        parent = await session.get(DiscordOutbox, delivery.outbox_id)
        route = by_name.get(delivery.destination)
        try:
            if route is None:
                raise DeliveryError("Webhook destination removed; restore its configuration", 60)
            delivery.message_id = await sender.send(route, formatter({
                "type": parent.message_type, "data": json.loads(parent.payload_json)}))
            delivery.sent_at = datetime.now(timezone.utc)
            delivery.last_error = None
            cooldown = getattr(sender, "cooldowns", {}).get(route.name, 0)
            if cooldown:
                state = await session.get(DiscordWebhookDestination, route.name)
                state.retry_after = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
            await session.flush()
            remaining = await session.scalar(select(func.count()).select_from(DiscordWebhookDelivery).where(
                DiscordWebhookDelivery.outbox_id == parent.id, DiscordWebhookDelivery.sent_at.is_(None)))
            if remaining == 0:
                parent.sent_at = datetime.now(timezone.utc)
        except DeliveryError as error:
            delivery.attempts += 1
            delivery.next_attempt = datetime.now(timezone.utc) + timedelta(
                seconds=max(error.retry_after, min(2 ** min(delivery.attempts, 6), 60)))
            delivery.last_error = str(error)
            state = await session.get(DiscordWebhookDestination, delivery.destination)
            if state:
                state.retry_after, state.blocked = delivery.next_attempt, error.blocked
            if error.global_limit:
                if global_gate is None:
                    global_gate = DiscordWebhookDestination(name="__global__", credential_hash="global")
                    session.add(global_gate)
                global_gate.retry_after = delivery.next_attempt
            log.error("Alert %d destination %s: %s", parent.id, delivery.destination, error)
        return 1
