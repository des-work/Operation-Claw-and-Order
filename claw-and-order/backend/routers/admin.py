"""Admin and read-only endpoints."""

import csv
import hmac
import io
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

import os

from backend.database import get_db
from backend.models import Team, Event, TeamEventCount, Milestone, PhaseLog, DiscordOutbox, DashboardOutbox
from backend.services.phase import (
    get_current_phase,
    get_phase_config,
    get_all_phases,
    set_current_phase,
    load_phases,
)
from backend.services.sse import broadcast
from backend.services import discord_worker
from backend.services.milestone_detector import MILESTONE_BASE_SCORES

log = logging.getLogger("admin")

router = APIRouter()


# ── Admin auth dependency ─────────────────────────────────────────────────

async def require_admin(x_admin_secret: str = Header(...)) -> None:
    """Validate the X-Admin-Secret header against env var ADMIN_SECRET."""
    admin_secret = os.getenv("ADMIN_SECRET", "")
    if not admin_secret:
        log.error("ADMIN_SECRET env var not set — all admin requests will be rejected")
        raise HTTPException(status_code=503, detail="Admin auth not configured")
    if not hmac.compare_digest(x_admin_secret, admin_secret):
        log.warning("Admin auth failed — bad secret")
        raise HTTPException(status_code=401, detail="Invalid admin secret")


async def require_export(x_export_secret: str = Header(...)) -> None:
    """Use a read-only secret that cannot authorize phase transitions."""
    export_secret = os.getenv("EVENT_EXPORT_SECRET", "")
    if not export_secret:
        raise HTTPException(status_code=503, detail="Event export not configured")
    if not hmac.compare_digest(x_export_secret, export_secret):
        raise HTTPException(status_code=401, detail="Invalid export secret")


# ── Pydantic models for admin input ──────────────────────────────────────

class PhaseTransitionIn(BaseModel):
    phase: int = Field(..., ge=1, le=3, description="Phase number (1, 2, or 3)")
    activated_by: str = Field(default="admin", max_length=100)


class ExportAckIn(BaseModel):
    event_ids: list[int] = Field(..., min_length=1, max_length=500)


# ── GET /api/health ─────────────────────────────────────────────────────────

@router.get("/api/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """Quick operational health check — DB connectivity + current state."""
    from backend.services.sse import client_count
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        pending = DiscordOutbox.sent_at.is_(None)
        discord_pending, oldest_pending = (await db.execute(
            select(func.count(DiscordOutbox.id), func.min(DiscordOutbox.created_at)).where(pending)
        )).one()
        dashboard_pending, oldest_dashboard = (await db.execute(
            select(func.count(DashboardOutbox.event_id), func.min(DashboardOutbox.created_at))
            .where(DashboardOutbox.delivered_at.is_(None))
        )).one()
        if oldest_pending is not None:
            if oldest_pending.tzinfo is None:
                oldest_pending = oldest_pending.replace(tzinfo=timezone.utc)
            discord_oldest_pending_seconds = max(0, int(
                (datetime.now(timezone.utc) - oldest_pending).total_seconds()))
        else:
            discord_oldest_pending_seconds = 0
        if oldest_dashboard is not None:
            if oldest_dashboard.tzinfo is None:
                oldest_dashboard = oldest_dashboard.replace(tzinfo=timezone.utc)
            dashboard_oldest_pending_seconds = max(0, int(
                (datetime.now(timezone.utc) - oldest_dashboard).total_seconds()))
        else:
            dashboard_oldest_pending_seconds = 0
        db_ok = True
    except Exception as e:
        log.error("Health check DB query failed: %s", e)
        db_ok = False
        discord_pending = None
        discord_oldest_pending_seconds = None
        dashboard_pending = None
        dashboard_oldest_pending_seconds = None

    phase = get_current_phase()
    cfg = get_phase_config(phase)

    discord_status = discord_worker.connection_status()
    status = "healthy" if db_ok and discord_status != "disconnected" and (
        discord_oldest_pending_seconds is None or discord_oldest_pending_seconds < 120
    ) and (not os.getenv("EVENT_EXPORT_SECRET") or dashboard_oldest_pending_seconds is None or
           dashboard_oldest_pending_seconds < 120
    ) else "degraded"
    result = {
        "status": status,
        "db": "ok" if db_ok else "error",
        "phase": phase,
        "phase_label": cfg["label"] if cfg else "Unknown",
        "sse_clients": client_count(),
        "discord": discord_status,
        "discord_pending": discord_pending,
        "discord_oldest_pending_seconds": discord_oldest_pending_seconds,
        "dashboard_export": "configured" if os.getenv("EVENT_EXPORT_SECRET") else "disabled",
        "dashboard_pending": dashboard_pending,
        "dashboard_oldest_pending_seconds": dashboard_oldest_pending_seconds,
    }
    if status != "healthy":
        from fastapi.responses import JSONResponse
        return JSONResponse(content=result, status_code=503)
    return result


# ── GET /api/phase ──────────────────────────────────────────────────────────

@router.get("/api/admin/events/export", dependencies=[Depends(require_export)])
async def export_events(limit: int = Query(default=250, ge=1, le=500),
                        db: AsyncSession = Depends(get_db)):
    """Return unacknowledged events; commit order cannot skip lower event IDs."""
    rows = (await db.execute(
        select(Event).join(DashboardOutbox, DashboardOutbox.event_id == Event.id)
        .where(DashboardOutbox.delivered_at.is_(None)).order_by(Event.id).limit(limit)
    )).scalars().all()
    head_id = await db.scalar(select(Event.id).order_by(Event.id.desc()).limit(1)) or 0
    pending = await db.scalar(select(func.count()).select_from(DashboardOutbox)
                              .where(DashboardOutbox.delivered_at.is_(None))) or 0
    return {
        "events": [{
            "id": event.id, "team_id": event.team_id,
            "timestamp": event.timestamp.isoformat(), "technique": event.technique,
            "target_ip": event.target_ip, "result": event.result,
            "phase": event.phase, "milestone": event.milestone,
        } for event in rows],
        "head_id": head_id,
        "pending": pending,
        "has_more": len(rows) == limit,
    }


@router.post("/api/admin/events/ack", dependencies=[Depends(require_export)])
async def acknowledge_events(payload: ExportAckIn, db: AsyncSession = Depends(get_db)):
    """Idempotently mark a page delivered only after dashboard storage succeeds."""
    ids = set(payload.event_ids)
    if any(event_id < 1 for event_id in ids):
        raise HTTPException(status_code=422, detail="event_ids must be positive")
    existing = await db.scalar(select(func.count()).select_from(DashboardOutbox)
                               .where(DashboardOutbox.event_id.in_(ids)))
    if existing != len(ids):
        raise HTTPException(status_code=404, detail="Unknown export event ID")
    await db.execute(update(DashboardOutbox).where(DashboardOutbox.event_id.in_(ids))
                     .values(delivered_at=datetime.now(timezone.utc)))
    await db.commit()
    return {"acknowledged": len(ids)}


# ── GET /api/phase ──────────────────────────────────────────────────────────

@router.get("/api/phase")
async def get_phase():
    phase = get_current_phase()
    cfg = get_phase_config(phase)
    return {
        "phase": phase,
        "label": cfg["label"] if cfg else "Unknown",
        "allowed_techniques": cfg["allowed_techniques"] if cfg else [],
    }


# ── GET /api/teams ──────────────────────────────────────────────────────────

@router.get("/api/teams")
async def get_teams(db: AsyncSession = Depends(get_db)):
    teams = (await db.execute(select(Team))).scalars().all()
    counts = dict((await db.execute(
        select(TeamEventCount.team_id, TeamEventCount.event_count)
    )).all())
    milestones_by_team = {}
    for milestone in (await db.execute(select(Milestone))).scalars():
        milestones_by_team.setdefault(milestone.team_id, []).append(milestone)
    result = []
    for t in teams:
        milestones = milestones_by_team.get(t.id, [])
        total_score = sum(m.score for m in milestones)

        result.append({
            "id": t.id,
            "display_name": t.display_name,
            "last_seen": t.last_seen.isoformat() if t.last_seen else None,
            "event_count": counts.get(t.id, 0),
            "total_score": total_score,
            "milestones": [
                {
                    "key": m.key,
                    "score": m.score,
                    "recorded_at": m.recorded_at.isoformat(),
                }
                for m in milestones
            ],
        })
    return result


# ── GET /api/report/final ──────────────────────────────────────────────────

@router.get("/api/report/final")
async def get_final_report(db: AsyncSession = Depends(get_db)):
    teams = (await db.execute(select(Team))).scalars().all()
    rows = []
    for t in teams:
        milestones = (await db.execute(
            select(Milestone).where(Milestone.team_id == t.id)
        )).scalars().all()

        total_score = sum(m.score for m in milestones)
        rows.append({
            "team_id": t.id,
            "display_name": t.display_name,
            "total_score": total_score,
            "milestones": {
                m.key: {"score": m.score, "recorded_at": m.recorded_at.isoformat()}
                for m in milestones
            },
        })

    # Sort by total_score descending for ranking
    rows.sort(key=lambda r: r["total_score"], reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank

    return rows


@router.get("/api/report/final/csv")
async def get_final_report_csv(db: AsyncSession = Depends(get_db)):
    """Download CSV version of final report."""
    teams = (await db.execute(select(Team))).scalars().all()
    rows = []
    for t in teams:
        milestones = (await db.execute(
            select(Milestone).where(Milestone.team_id == t.id)
        )).scalars().all()
        total_score = sum(m.score for m in milestones)
        milestone_map = {m.key: m.score for m in milestones}
        rows.append((total_score, t.id, t.display_name, milestone_map))

    rows.sort(key=lambda r: r[0], reverse=True)

    # Derive milestone columns from the scoring config — no hardcoded keys
    milestone_keys = list(MILESTONE_BASE_SCORES.keys())

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["rank", "team_id", "display_name", "total_score"] + milestone_keys)
    for rank, (score, tid, name, ms) in enumerate(rows, 1):
        writer.writerow(
            [rank, tid, name, score] + [ms.get(k, 0) for k in milestone_keys]
        )

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=claw_and_order_results.csv"},
    )


# ── POST /api/admin/phase ──────────────────────────────────────────────────

@router.post("/api/admin/phase")
async def transition_phase(
    body: PhaseTransitionIn,
    db: AsyncSession = Depends(get_db),
    _auth: None = Depends(require_admin),
):
    # Validate phase against phases.json instead of hardcoded range
    all_phases = get_all_phases()
    valid_phase_nums = {p["phase"] for p in all_phases}
    if body.phase not in valid_phase_nums:
        raise HTTPException(
            status_code=400,
            detail=f"Phase must be one of {sorted(valid_phase_nums)}",
        )

    log_entry = PhaseLog(
        phase=body.phase,
        activated_by=body.activated_by,
    )
    db.add(log_entry)
    await db.flush()
    cfg = get_phase_config(body.phase)
    phase_data = {
        "phase": body.phase,
        "label": cfg["label"] if cfg else "Unknown",
        "activated_at": log_entry.activated_at.isoformat(),
    }
    discord_worker.queue_notification(db, {"type": "phase_change", "data": phase_data},
                                      f"phase_log:{log_entry.id}")
    await db.commit()
    set_current_phase(body.phase)
    load_phases()  # reload in case file was edited
    log.info("Phase transitioned to %d by %s", body.phase, body.activated_by)
    try:
        await broadcast("phase_change", phase_data)
    except Exception as e:
        log.error("SSE broadcast failed on phase change: %s", e)

    return {"status": "phase_changed", "phase": body.phase}
