"""Aggregate enrollment health; never expose roster identities on public health."""

import os
from sqlalchemy import select, func

from backend.enrollment_policy import load_policy, EnrollmentError
from backend.models import MembershipSync, EnrollmentWorker
from backend.services.enrollment import now, utc


async def enrollment_health(session):
    if not os.getenv("ENROLLMENT_POLICY_FILE"):
        return {"status": "disabled", "queue": {}}
    try:
        policy = load_policy()
    except EnrollmentError:
        return {"status": "configuration_required", "queue": {}}
    worker = await session.get(EnrollmentWorker, policy.guild_id)
    counts = dict((await session.execute(select(MembershipSync.status, func.count()).where(
        MembershipSync.guild_id == policy.guild_id).group_by(MembershipSync.status))).all())
    oldest = await session.scalar(select(func.min(MembershipSync.queued_at)).where(
        MembershipSync.guild_id == policy.guild_id, MembershipSync.status.in_(("pending", "retry"))))
    age = max(0, int((now() - utc(oldest)).total_seconds())) if oldest else 0
    ready = worker and worker.policy_hash == policy.digest and (now() - utc(worker.last_seen)).total_seconds() < 120 and not worker.last_error
    blocked = any(value and key.startswith("blocked") for key, value in counts.items())
    return {"status": "healthy" if ready and not blocked and age < 120 else "degraded",
            "worker": "ready" if ready else "missing_stale_or_wrong_policy", "queue": counts,
            "oldest_pending_seconds": age}
