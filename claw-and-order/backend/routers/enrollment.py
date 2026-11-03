"""Private roster administration and a separate PII-free team inventory export."""

import asyncio
import json

from fastapi import APIRouter, Depends, Request, Response, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from backend.database import get_db
from backend.enrollment_policy import load_policy, EnrollmentError
from backend.models import Student, Enrollment, EnrollmentImport, EnrollmentAudit, MembershipSync, RegistrationInvitation
from backend.roster_input import read_roster, MAX_BYTES
from backend.routers.admin import require_admin, require_export
from backend.services.enrollment import (preview_import, apply_import, rollback_preview,
                                         reconcile_preview, issue_invitations, export_teams, state_for_update)
from backend.services.enrollment import now, utc
from backend.services.enrollment_health import enrollment_health


def private_response(response: Response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


router = APIRouter(prefix="/api/admin/enrollment", dependencies=[Depends(require_admin), Depends(private_response)])
export_router = APIRouter(prefix="/api/admin/enrollment", dependencies=[Depends(require_export), Depends(private_response)])


class RowsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[dict] = Field(min_length=1, max_length=5000)


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    digest: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]+$")


class InvitationsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    student_ids: list[str] = Field(min_length=1, max_length=5000)
    ttl_hours: int = Field(default=72, ge=1, le=168)


class InvitationRecipient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code_hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]+$")
    email: str = Field(max_length=254)


class InvitationChecksIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[InvitationRecipient] = Field(min_length=1, max_length=5000)


async def policy():
    return load_policy()


@router.get("/policy")
async def current_policy(selected=Depends(policy)):
    return {"policy_hash": selected.digest, "policy": selected.data}


@router.post("/imports")
async def preview(body: RowsIn, db=Depends(get_db), selected=Depends(policy)):
    result = await preview_import(db, body.rows, selected)
    await db.commit()
    return result


@router.post("/imports/file")
async def preview_file(request: Request, filename: str = Query(max_length=200), column_map: str = Query(default="", max_length=4000),
                       db=Depends(get_db), selected=Depends(policy)):
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_BYTES:
            raise EnrollmentError("Roster upload exceeds 10 MiB", 413)
    try:
        mapping = json.loads(column_map) if column_map else None
    except ValueError:
        raise EnrollmentError("column_map must be a JSON object") from None
    rows = await asyncio.to_thread(read_roster, bytes(content), filename, mapping)
    result = await preview_import(db, rows, selected)
    await db.commit()
    return result


@router.get("/imports/{batch_id}")
async def read_import(batch_id: str, db=Depends(get_db)):
    batch = await db.get(EnrollmentImport, batch_id)
    if not batch:
        raise EnrollmentError("Import not found", 404)
    return {"batch_id": batch.id, "digest": batch.digest, "base_revision": batch.base_revision,
            "policy_hash": batch.policy_hash, "applied_at": batch.applied_at,
            "payload": json.loads(batch.payload_json)}


@router.post("/imports/{batch_id}/apply")
async def apply(batch_id: str, body: ConfirmIn, db=Depends(get_db), selected=Depends(policy)):
    result = await apply_import(db, batch_id, body.digest, selected)
    await db.commit()
    return result


@router.post("/imports/{batch_id}/rollback-preview")
async def rollback(batch_id: str, db=Depends(get_db), selected=Depends(policy)):
    result = await rollback_preview(db, batch_id, selected)
    await db.commit()
    return result


@router.post("/reconcile/preview")
async def reconcile(db=Depends(get_db), selected=Depends(policy)):
    result = await reconcile_preview(db, selected)
    await db.commit()
    return result


@router.post("/invitations")
async def invitations(body: InvitationsIn, db=Depends(get_db), selected=Depends(policy)):
    rows = await issue_invitations(db, body.student_ids, body.ttl_hours, selected)
    await db.commit()
    return {"invitations": rows, "delivery": "private_export_only_no_email_sent"}


@router.post("/invitations/check")
async def check_invitations(body: InvitationChecksIn, db=Depends(get_db), selected=Depends(policy)):
    result = {}
    for entry in body.entries:
        invitation = await db.get(RegistrationInvitation, entry.code_hash)
        student = await db.get(Student, invitation.student_id) if invitation else None
        active = (await db.execute(select(Enrollment).where(Enrollment.student_id == student.id,
            Enrollment.status == "active"))).scalars().all() if student else []
        if not invitation or invitation.guild_id != selected.guild_id:
            status = "missing_or_wrong_server"
        elif invitation.revoked or invitation.used_at or utc(invitation.expires_at) <= now():
            status = "revoked_used_or_expired"
        elif not student or student.email != entry.email.casefold() or not any((e.term_id, e.class_id) in selected.classes for e in active):
            status = "recipient_or_enrollment_changed"
        else:
            status = "valid"
        result[entry.code_hash] = status
    return result


@router.get("/students")
async def students(limit: int = Query(default=100, ge=1, le=200), after_id: str = Query(default="", max_length=64), db=Depends(get_db)):
    state = await state_for_update(db)
    query = select(Student).order_by(Student.id).limit(limit)
    if after_id:
        query = query.where(Student.id > after_id)
    people = (await db.execute(query)).scalars().all()
    ids = [s.id for s in people]
    memberships = (await db.execute(select(Enrollment).where(Enrollment.student_id.in_(ids)))).scalars().all() if ids else []
    jobs = (await db.execute(select(MembershipSync).where(MembershipSync.student_id.in_(ids)))).scalars().all() if ids else []
    return {"students": [{"student_id": s.id, "email": s.email, "display_name": s.display_name,
        "discord_user_id": s.discord_user_id,
        "enrollments": [{"term_id": e.term_id, "class_id": e.class_id, "team_id": e.team_id,
                         "role_key": e.role_key, "idea": e.idea, "status": e.status,
                         "placement": "assigned" if e.team_id else "awaiting_placement"}
                        for e in memberships if e.student_id == s.id],
        "discord_sync": [{"guild_id": j.guild_id, "status": j.status, "attempts": j.attempts,
                          "last_error": j.last_error} for j in jobs if j.student_id == s.id]}
        for s in people], "limit": limit, "next_after_id": people[-1].id if len(people) == limit else None,
        "revision": state.revision}


@router.get("/audit")
async def audit_log(limit: int = Query(default=100, ge=1, le=200), before_id: int | None = Query(default=None, ge=1), db=Depends(get_db)):
    query = select(EnrollmentAudit).order_by(EnrollmentAudit.id.desc()).limit(limit)
    if before_id is not None:
        query = query.where(EnrollmentAudit.id < before_id)
    rows = (await db.execute(query)).scalars().all()
    return [{"id": r.id, "action": r.action, "subject_id": r.subject_id, "revision": r.revision,
             "details": json.loads(r.details_json), "created_at": r.created_at} for r in rows]


@router.get("/status")
async def status(db=Depends(get_db)):
    return await enrollment_health(db)


@export_router.get("/teams/export")
async def team_export(db=Depends(get_db)):
    # No policy is a valid unconfigured/empty semester, never a demo fallback.
    import os
    if not os.getenv("ENROLLMENT_POLICY_FILE"):
        return {"version": 1, "teams": []}
    return await export_teams(db, load_policy())
