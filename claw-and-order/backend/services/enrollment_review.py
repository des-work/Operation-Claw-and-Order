"""Read-only change verification. An accepted roster write is not Discord success."""

import json
from sqlalchemy import select

from backend.enrollment_policy import EnrollmentError
from backend.models import EnrollmentImport, EnrollmentAudit, MembershipSync
from backend.services.enrollment import now


async def batch_review(session, batch_id):
    batch = await session.get(EnrollmentImport, batch_id)
    if not batch:
        raise EnrollmentError("Change batch not found", 404)
    payload = json.loads(batch.payload_json)
    review = payload.get("review")
    if not review:
        raise EnrollmentError("This legacy batch has no immutable human review; create a fresh preview", 409)
    applied_revision = batch.base_revision + 1
    jobs = {j.student_id: j for j in (await session.execute(select(MembershipSync).where(
        MembershipSync.guild_id == review["guild_id"],
        MembershipSync.student_id.in_(payload["sync_student_ids"])))).scalars()}
    observations = {}
    for event in (await session.execute(select(EnrollmentAudit).where(
        EnrollmentAudit.action == "discord_roles_reconciled", EnrollmentAudit.revision == applied_revision,
        EnrollmentAudit.subject_id.in_(payload["sync_student_ids"])).order_by(EnrollmentAudit.id))).scalars():
        data = json.loads(event.details_json)
        if data.get("guild_id") == review["guild_id"] and data.get("policy_hash") == batch.policy_hash and data.get("verified"):
            observations[event.subject_id] = set(data.get("verified_role_ids", []))
    approvals = (await session.execute(select(EnrollmentAudit).where(
        EnrollmentAudit.subject_id == batch_id, EnrollmentAudit.action == "import_applied")
        .order_by(EnrollmentAudit.id.desc()).limit(1))).scalar_one_or_none()
    students = []
    for entry in review["role_preview"]:
        job = jobs.get(entry["student_id"])
        status = "not_applied"
        if batch.applied_at:
            status = "missing_job" if job is None else "superseded" if job.revision != applied_revision or job.policy_hash != batch.policy_hash else job.status
            if status == "complete" and observations.get(entry["student_id"]) != set(entry["desired_role_ids"]):
                status = "unverified"
        students.append({"student_id": entry["student_id"], "status": status,
                         "last_verified_at": job.completed_at.isoformat() if job and status == "complete" and job.completed_at else None,
                         "attempts": job.attempts if job else 0, "explanation": job.last_error if job and status not in ("not_applied", "superseded") else None})
    statuses = {row["status"] for row in students}
    outcome = "NOT APPLIED" if not batch.applied_at else "VERIFIED" if statuses == {"complete"} else "SUPERSEDED" if statuses == {"superseded"} else "BLOCKED" if any(s.startswith("blocked") or s in ("missing_job", "unverified") for s in statuses) else "WAITING"
    return {"batch_id": batch.id, "digest": batch.digest, "base_revision": batch.base_revision,
            "policy_hash": batch.policy_hash, "created_at": batch.created_at.isoformat(),
            "applied_at": batch.applied_at.isoformat() if batch.applied_at else None,
            "approval": json.loads(approvals.details_json) if approvals else None,
            **review, "verification": {"outcome": outcome, "checked_at": now().isoformat(), "students": students}}
