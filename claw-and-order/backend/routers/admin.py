"""Admin and read-only endpoints."""

import csv
import io
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

import os

from backend.database import get_db
from backend.models import Team, Event, Milestone, PhaseLog
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
    if x_admin_secret != admin_secret:
        log.warning("Admin auth failed — bad secret")
        raise HTTPException(status_code=401, detail="Invalid admin secret")


# ── Pydantic models for admin input ──────────────────────────────────────

class PhaseTransitionIn(BaseModel):
    phase: int = Field(..., ge=1, le=3, description="Phase number (1, 2, or 3)")
    activated_by: str = Field(default="admin", max_length=100)


# ── GET /api/health ─────────────────────────────────────────────────────────

@router.get("/api/health")
async def health_check(db: AsyncSession = Depends(get_db)):
    """Quick operational health check — DB connectivity + current state."""
    from backend.services.sse import client_count
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        log.error("Health check DB query failed: %s", e)
        db_ok = False

    phase = get_current_phase()
    cfg = get_phase_config(phase)

    status = "healthy" if db_ok else "degraded"
    result = {
        "status": status,
        "db": "ok" if db_ok else "error",
        "phase": phase,
        "phase_label": cfg["label"] if cfg else "Unknown",
        "sse_clients": client_count(),
        "discord": "configured" if os.getenv("DISCORD_TOKEN") else "not configured",
    }
    if not db_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(content=result, status_code=503)
    return result


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
    result = []
    for t in teams:
        # Event count
        event_count = await db.scalar(
            select(func.count()).select_from(Event).where(Event.team_id == t.id)
        )
        # Milestones
        milestones = (await db.execute(
            select(Milestone).where(Milestone.team_id == t.id)
        )).scalars().all()

        total_score = sum(m.score for m in milestones)

        result.append({
            "id": t.id,
            "display_name": t.display_name,
            "last_seen": t.last_seen.isoformat() if t.last_seen else None,
            "event_count": event_count,
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

    set_current_phase(body.phase)
    load_phases()  # reload in case file was edited

    log_entry = PhaseLog(
        phase=body.phase,
        activated_by=body.activated_by,
    )
    db.add(log_entry)
    await db.commit()
    log.info("Phase transitioned to %d by %s", body.phase, body.activated_by)

    cfg = get_phase_config(body.phase)
    phase_data = {
        "phase": body.phase,
        "label": cfg["label"] if cfg else "Unknown",
        "activated_at": log_entry.activated_at.isoformat(),
    }
    try:
        await broadcast("phase_change", phase_data)
    except Exception as e:
        log.error("SSE broadcast failed on phase change: %s", e)

    # D2: Notify Discord of phase transition
    try:
        await discord_worker.enqueue({"type": "phase_change", "data": phase_data})
    except Exception as e:
        log.error("Discord enqueue failed on phase change: %s", e)

    return {"status": "phase_changed", "phase": body.phase}
