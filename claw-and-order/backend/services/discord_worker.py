"""
Discord worker — single bot, asyncio task, queued dispatch.

RULE: Never use bot.run(). Always asyncio.create_task(bot.start()) in lifespan.
"""

import asyncio
import logging
from typing import Any

import discord

from backend.config import DISCORD_TOKEN, DISCORD_CHANNEL_ID as _CHANNEL_ID_STR

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
    """Place a message on the Discord dispatch queue."""
    await _queue.put(message)


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


@bot.event
async def on_ready():
    global _channel, _dispatch_started
    log.info("Discord bot connected as %s", bot.user)
    if DISCORD_CHANNEL_ID:
        _channel = bot.get_channel(DISCORD_CHANNEL_ID)
        if _channel is None:
            try:
                _channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)
            except discord.NotFound:
                log.error("Discord channel %d not found", DISCORD_CHANNEL_ID)
        if _channel:
            log.info("Discord posting to #%s", _channel.name)
    # Start the dispatch loop ONCE — on_ready fires on every gateway reconnect
    if not _dispatch_started:
        _dispatch_started = True
        asyncio.create_task(_dispatch_loop())
        log.info("Discord dispatch loop started")
    else:
        log.info("Discord reconnected — dispatch loop already running")


async def _dispatch_loop():
    """Consume the internal queue and send messages to Discord."""
    while True:
        msg = await _queue.get()
        if _channel is None:
            log.warning("No Discord channel configured, dropping message: %s", msg)
            continue
        try:
            text = _format_message(msg)
            await _channel.send(text)
        except discord.HTTPException as e:
            log.error("Discord send failed: %s", e)
        except Exception as e:
            log.error("Discord dispatch error: %s", e)


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
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
