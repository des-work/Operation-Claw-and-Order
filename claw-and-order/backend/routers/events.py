"""POST /api/events — Kali reporter ingest endpoint."""

import ipaddress
import logging
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.rate_limit import limiter, team_or_ip
from backend.models import Team, Event, Milestone, PhaseLog
from backend.services.auth import validate_token
from backend.services.milestone_detector import detect_milestone, calculate_score
from backend.services.phase import get_current_phase, is_technique_allowed
from backend.services.sse import broadcast
from backend.services import discord_worker

log = logging.getLogger("events")

router = APIRouter()

# ── Constants ──────────────────────────────────────────────────────────────
MAX_RAW_OUTPUT = 2000


# ── Input validation ──────────────────────────────────────────────────────

class EventIn(BaseModel):
    team_id: str = Field(..., min_length=1, max_length=64)
    command: str = Field(..., min_length=1, max_length=2000)
    technique: str = Field(..., min_length=1, max_length=100)
    target_ip: str = Field(..., min_length=1, max_length=45)   # max IPv6 length
    result: Literal["success", "failure", "blocked", "timeout", "heartbeat"] = Field(...)
    raw_output: str | None = Field(default=None, max_length=MAX_RAW_OUTPUT)
    source_id: str | None = Field(default=None, min_length=1, max_length=100)


def _duplicate_receipt(existing: Event, payload: EventIn):
    if (existing.command, existing.technique, existing.target_ip, existing.result,
            existing.raw_output or "") != (payload.command, payload.technique, payload.target_ip,
                                        payload.result, payload.raw_output or ""):
        raise HTTPException(status_code=409, detail="source_id already used for different telemetry")
    return JSONResponse(status_code=200, content={
        "status": "duplicate", "event_id": existing.id, "milestone": existing.milestone,
    })


def _validate_cidr(target_ip: str, team: Team, technique: str) -> None:
    """Reject target_ip outside team's DMZ and LAN CIDRs. Heartbeat bypasses."""
    if technique == "heartbeat":
        return

    try:
        target = ipaddress.ip_address(target_ip)
    except ValueError:
        raise HTTPException(status_code=403, detail="Invalid target IP")

    try:
        in_dmz = target in ipaddress.ip_network(team.dmz_cidr, strict=False)
        in_lan = target in ipaddress.ip_network(team.lan_cidr, strict=False)
    except ValueError as e:
        # Corrupted CIDR in team row — log loudly, don't crash
        log.error("Team %s has invalid CIDR config: %s", team.id, e)
        raise HTTPException(status_code=500, detail="Team CIDR configuration error")

    if not in_dmz and not in_lan:
        raise HTTPException(status_code=403, detail="Target IP outside team's allowed CIDR ranges")


@router.post("/api/events", status_code=201)
@limiter.limit("300/minute", key_func=team_or_ip)
async def ingest_event(
    request: Request,
    payload: EventIn,
    team: Team = Depends(validate_token),
    db: AsyncSession = Depends(get_db),
):
    if payload.source_id:
        existing = await db.scalar(select(Event).where(
            Event.team_id == team.id, Event.source_id == payload.source_id))
        if existing:
            return _duplicate_receipt(existing, payload)
    # A4: CIDR validation (heartbeat bypasses)
    _validate_cidr(payload.target_ip, team, payload.technique)

    # C1: Phase enforcement — technique must be allowed in current phase
    current_phase = get_current_phase()
    if not is_technique_allowed(payload.technique):
        raise HTTPException(
            status_code=403,
            detail=f"Technique '{payload.technique}' not allowed in phase {current_phase}",
        )

    # ── DB transaction: insert event + detect milestone ──────────────────
    milestone_key = None
    milestone_score = None

    try:
        event = Event(
            team_id=team.id,
            phase=current_phase,
            timestamp=datetime.now(timezone.utc),
            command=payload.command,
            technique=payload.technique,
            target_ip=payload.target_ip,
            result=payload.result,
            raw_output=payload.raw_output[:MAX_RAW_OUTPUT] if payload.raw_output else None,
            source_id=payload.source_id,
        )
        db.add(event)
        await db.flush()  # get event.id for milestone FK
        await db.execute(text("INSERT INTO team_event_counts (team_id, event_count) VALUES (:team_id, 1) "
                              "ON CONFLICT (team_id) DO UPDATE SET "
                              "event_count = team_event_counts.event_count + 1"),
                         {"team_id": team.id})

        # Update last_seen on heartbeat
        if payload.technique == "heartbeat":
            team.last_seen = datetime.now(timezone.utc)

        # C2: Milestone detection (in same transaction)
        milestone_key = await detect_milestone(
            session=db,
            event_id=event.id,
            team_id=team.id,
            technique=payload.technique,
            result=payload.result,
            target_ip=payload.target_ip,
            phase=current_phase,
            team_dmz_cidr=team.dmz_cidr,
            team_lan_cidr=team.lan_cidr,
            Event=Event,
            Milestone=Milestone,
        )

        # If milestone fired, calculate score and update event
        if milestone_key:
            phase_log = (await db.execute(
                select(PhaseLog)
                .where(PhaseLog.phase == current_phase)
                .order_by(PhaseLog.activated_at.desc())
            )).scalar()
            phase_start_ts = phase_log.activated_at.timestamp() if phase_log else event.timestamp.timestamp()
            milestone_score = calculate_score(milestone_key, phase_start_ts, event.timestamp.timestamp())

            await db.execute(
                update(Milestone)
                .where(Milestone.triggering_event_id == event.id)
                .values(score=milestone_score)
            )
            event.milestone = milestone_key
            log.info("Milestone %s fired for %s — %d pts", milestone_key, team.id, milestone_score)
            discord_worker.queue_notification(db, {
                "type": "milestone",
                "data": {
                    "team_id": team.id, "key": milestone_key, "score": milestone_score,
                    "phase": current_phase, "technique": event.technique,
                    "timestamp": event.timestamp.isoformat(),
                },
            }, f"milestone:{team.id}:{milestone_key}")

        await db.commit()

    except IntegrityError as e:
        await db.rollback()
        if payload.source_id:
            existing = await db.scalar(select(Event).where(
                Event.team_id == team.id, Event.source_id == payload.source_id))
            if existing:
                return _duplicate_receipt(existing, payload)
        log.error("Event integrity failure for team %s: %s", team.id, e)
        raise HTTPException(status_code=500, detail="Event integrity failure")
    except HTTPException:
        raise  # re-raise FastAPI errors as-is
    except Exception as e:
        await db.rollback()
        log.error("DB transaction failed for team %s: %s", team.id, e, exc_info=True)
        raise HTTPException(status_code=500, detail="Event processing failed")

    # ── Post-commit notifications (fire-and-forget, never fail the request) ──
    event_data = {
        "event_id": event.id,
        "team_id": team.id,
        "phase": event.phase,
        "timestamp": event.timestamp.isoformat(),
        "technique": event.technique,
        "target_ip": event.target_ip,
        "result": event.result,
        "milestone": milestone_key,
    }
    try:
        await broadcast("new_event", event_data)
    except Exception as e:
        log.error("SSE broadcast failed for event %d: %s", event.id, e)

    if milestone_key:
        milestone_data = {
            "team_id": team.id,
            "key": milestone_key,
            "score": milestone_score,
            "phase": current_phase,
            "technique": event.technique,
            "timestamp": event.timestamp.isoformat(),
        }
        try:
            await broadcast("milestone", milestone_data)
        except Exception as e:
            log.error("SSE milestone broadcast failed: %s", e)

    return {"status": "accepted", "event_id": event.id, "milestone": milestone_key}
