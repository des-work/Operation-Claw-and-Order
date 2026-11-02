"""
Discord worker — single bot, asyncio task, queued dispatch.

RULE: Never use bot.run(). Always asyncio.create_task(bot.start()) in lifespan.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Any

import discord
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import DISCORD_TOKEN, DISCORD_CHANNEL_ID as _CHANNEL_ID_STR
from backend.database import async_session
from backend.models import DiscordOutbox

log = logging.getLogger("discord_worker")

# ── Configuration ───────────────────────────────────────────────────────────

try:
    DISCORD_CHANNEL_ID: int = int(_CHANNEL_ID_STR)
except ValueError:
    log.error("DISCORD_CHANNEL_ID is not a valid integer, defaulting to 0")
    DISCORD_CHANNEL_ID = 0

# ── Internal queue ──────────────────────────────────────────────────────────

_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()


async def enqueue(message: dict[str, Any]) -> None:
    """Legacy transient notifications. Scoring alerts use queue_notification."""
    await _queue.put(message)


def queue_notification(session: AsyncSession, message: dict[str, Any], dedupe_key: str) -> None:
    """Add a Discord alert to the caller's transaction so it survives restarts."""
    session.add(DiscordOutbox(
        dedupe_key=dedupe_key,
        message_type=message["type"],
        payload_json=json.dumps(message["data"]),
    ))


# ── Message formatters ─────────────────────────────────────────────────────

_MILESTONE_LABELS = {
    "dmz_compromise": "DMZ compromised",
    "lan_pivot": "LAN pivot achieved",
    "wan_to_lan_direct": "Direct WAN→LAN access (perimeter bypass!)",
    "domain_compromise": "Domain compromised",
}


def _format_milestone(data: dict) -> str:
    label = _MILESTONE_LABELS.get(data["key"], data["key"])
    return (
        f"🚨 **{data['team_id']}**: {label} "
        f"| Phase {data.get('phase', '?')}, {data.get('technique', 'unknown')} "
        f"| **{data.get('score', 0)} pts**"
    )


def _format_phase_change(data: dict) -> str:
    return (
        f"📢 **Phase {data['phase']}** ({data.get('label', '')}) now active. "
        f"New techniques unlocked."
    )


def _format_vm_offline(data: dict) -> str:
    return (
        f"⚠️ **{data['team_id']}** Kali VM silent for 5+ minutes "
        f"— check reporter process"
    )


def _format_message(msg: dict) -> str:
    msg_type = msg.get("type", "")
    data = msg.get("data", {})

    if msg_type == "milestone":
        return _format_milestone(data)
    elif msg_type == "phase_change":
        return _format_phase_change(data)
    elif msg_type == "vm_offline":
        return _format_vm_offline(data)
    else:
        return f"[Claw & Order] {msg_type}: {data}"


# ── Bot and dispatch loop ──────────────────────────────────────────────────

intents = discord.Intents.default()
bot = discord.Client(intents=intents)

_channel: discord.TextChannel | None = None
_dispatch_started: bool = False  # guard against duplicate loops on reconnect
_worker_tasks: list[asyncio.Task] = []


@bot.event
async def on_ready():
    global _channel, _dispatch_started
    log.info("Discord bot connected as %s", bot.user)
    if DISCORD_CHANNEL_ID:
        _channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if _channel is None:
            try:
                _channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
            except discord.HTTPException as e:
                log.error("Discord channel %d unavailable: %s", DISCORD_CHANNEL_ID, e)
        if _channel:
            log.info("Discord posting to #%s", _channel.name)
    # Start the dispatch loop ONCE — on_ready fires on every gateway reconnect
    if not _dispatch_started:
        _dispatch_started = True
        _worker_tasks.extend([asyncio.create_task(_dispatch_loop()), asyncio.create_task(_outbox_loop())])
        log.info("Discord dispatch loop started")
    else:
        log.info("Discord reconnected — dispatch loop already running")


async def _dispatch_loop():
    """Consume the internal queue and send messages to Discord."""
    while True:
        msg = await _queue.get()
        delay = 1
        while True:
            try:
                if _channel is None:
                    raise RuntimeError("Discord channel unavailable")
                await _channel.send(_format_message(msg))
                _queue.task_done()
                break
            except Exception as e:
                log.error("Discord transient dispatch failed; retrying: %s", e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)


async def _outbox_once() -> int:
    """Dispatch one due alert under a database row lock; returns 1 if attempted."""
    if _channel is None:
        return 0
    async with async_session() as session:
        async with session.begin():
            now = datetime.now(timezone.utc)
            row = (await session.execute(
                select(DiscordOutbox)
                .where(DiscordOutbox.sent_at.is_(None), DiscordOutbox.next_attempt <= now)
                .order_by(DiscordOutbox.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if row is None:
                return 0
            try:
                await _channel.send(_format_message({
                    "type": row.message_type, "data": json.loads(row.payload_json),
                }))
                row.sent_at = datetime.now(timezone.utc)
            except Exception as e:
                row.attempts += 1
                row.next_attempt = datetime.now(timezone.utc) + timedelta(
                    seconds=min(2 ** min(row.attempts, 6), 60))
                log.error("Discord alert %d failed (attempt %d): %s", row.id, row.attempts, e)
            return 1


async def _outbox_loop():
    while True:
        try:
            attempted = await _outbox_once()
            if not attempted:
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("Discord outbox unavailable; retrying: %s", e)
            await asyncio.sleep(5)


# ── Lifespan helpers (called from main.py) ──────────────────────────────────

async def start() -> asyncio.Task | None:
    """Start the bot. Returns the task for cancellation on shutdown."""
    if not DISCORD_TOKEN:
        log.warning("DISCORD_TOKEN not set — Discord worker disabled")
        return None
    task = asyncio.create_task(bot.start(DISCORD_TOKEN))
    return task


async def stop(task: asyncio.Task | None) -> None:
    """Cleanly shut down the bot."""
    if task is None:
        return
    await bot.close()
    for worker in _worker_tasks:
        worker.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks.clear()
    global _dispatch_started, _channel
    _dispatch_started = False
    _channel = None
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
