"""VM offline watchdog — alerts when a team's Kali VM stops heartbeating."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import async_session
from backend.models import Team
from backend.services import discord_worker

log = logging.getLogger("watchdog")

# How long before a team is considered offline (no heartbeat)
OFFLINE_THRESHOLD = timedelta(minutes=5)

# How often the watchdog checks
CHECK_INTERVAL_SECONDS = 60

# Track which teams we've already alerted about (avoid spam)
_alerted: set[str] = set()


async def _check_once() -> None:
    """One pass: check all teams for stale last_seen."""
    try:
        async with async_session() as session:
            teams = (await session.execute(select(Team))).scalars().all()
    except Exception as e:
        log.error("Watchdog DB query failed: %s", e)
        return

    now = datetime.now(timezone.utc)
    for team in teams:
        if team.last_seen is None:
            continue  # never seen — probably not provisioned yet

        last_seen = team.last_seen
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)  # SQLite returns naive UTC values
        elapsed = now - last_seen
        if elapsed > OFFLINE_THRESHOLD:
            if team.id not in _alerted:
                log.warning("Team %s Kali VM offline (last seen %s ago)", team.id, elapsed)
                try:
                    async with async_session() as session:
                        discord_worker.queue_notification(session, {
                            "type": "vm_offline", "data": {"team_id": team.id},
                        }, f"vm_offline:{team.id}:{last_seen.isoformat()}")
                        await session.commit()
                    _alerted.add(team.id)
                except IntegrityError:
                    # Another worker or an earlier run already persisted this outage.
                    _alerted.add(team.id)
                except Exception as e:
                    log.error("Failed to persist vm_offline alert: %s", e)
        else:
            # Team is alive — clear alert flag so we can re-alert if it goes down again
            _alerted.discard(team.id)


async def run_watchdog() -> None:
    """Long-running watchdog loop. Launch via asyncio.create_task."""
    log.info("VM offline watchdog started (threshold=%s)", OFFLINE_THRESHOLD)
    while True:
        await _check_once()
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
