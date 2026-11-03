"""Reconcile approved enrollment roles; preserve all unrelated Discord roles."""

import asyncio
from datetime import timedelta

from sqlalchemy import select

from backend.enrollment_policy import EnrollmentError
from backend.models import Student, MembershipSync, ManagedDiscordGrant, EnrollmentWorker
from backend.services.enrollment import desired_roles, now, audit


class MembershipError(Exception):
    def __init__(self, message, status="retry"):
        super().__init__(message)
        self.status = status


async def reconcile_external(adapter, policy, controlled, user_id, desired, reason):
    try:
        async with asyncio.timeout(30):
            await adapter.validate(policy, controlled)
            actual = await adapter.member_roles(user_id)
            remove = sorted((actual & controlled) - desired)
            add = sorted(desired - actual)
            for role_id in remove:
                await adapter.remove_role(user_id, role_id, reason)
            for role_id in add:
                await adapter.add_role(user_id, role_id, reason)
            return remove, add
    except TimeoutError:
        raise MembershipError("Discord reconciliation timed out; retrying from actual membership") from None


async def sync_once(factory, policy, adapter):
    async with factory() as session, session.begin():
        job = (await session.execute(select(MembershipSync).where(
            MembershipSync.guild_id == policy.guild_id,
            MembershipSync.status.in_(("pending", "retry", "awaiting_member")),
            MembershipSync.next_attempt <= now()
        ).order_by(MembershipSync.next_attempt, MembershipSync.student_id).limit(1)
            .with_for_update(skip_locked=True))).scalar_one_or_none()
        if job is None:
            return 0
        student = await session.get(Student, job.student_id)
        try:
            if job.policy_hash != policy.digest:
                raise MembershipError("Policy changed; approve a new reconciliation preview", "blocked_policy")
            if not student.discord_user_id:
                raise MembershipError("Student has not registered a Discord account", "awaiting_registration")
            try:
                desired = await desired_roles(session, student.id, policy)
            except EnrollmentError:
                raise MembershipError("Student role definition or mapping awaits approval", "blocked_role_definition") from None
            grants = (await session.execute(select(ManagedDiscordGrant).where(
                ManagedDiscordGrant.student_id == student.id, ManagedDiscordGrant.guild_id == policy.guild_id
            ))).scalars().all()
            recorded = {grant.role_id for grant in grants}
            controlled = policy.managed_roles | recorded
            reason = f"Claw enrollment revision {job.revision}"
            # Remove obsolete team access before granting replacement access.
            remove, add = await reconcile_external(adapter, policy, controlled, student.discord_user_id, desired, reason)
            for grant in grants:
                if grant.role_id not in desired:
                    await session.delete(grant)
            for role_id in desired - recorded:
                session.add(ManagedDiscordGrant(student_id=student.id, guild_id=policy.guild_id, role_id=role_id))
            job.status, job.last_error, job.completed_at = "complete", None, now()
            audit(session, "discord_roles_reconciled", student.id, job.revision,
                  guild_id=policy.guild_id, added=add, removed=remove, policy_hash=policy.digest)
        except MembershipError as error:
            job.status, job.last_error = error.status, str(error)
            job.attempts += 1
            job.next_attempt = now() + timedelta(seconds=300 if error.status == "awaiting_member" else min(2 ** min(job.attempts, 8), 300))
        return 1


async def heartbeat(factory, policy, error=None):
    async with factory() as session, session.begin():
        worker = await session.get(EnrollmentWorker, policy.guild_id)
        if worker is None:
            worker = EnrollmentWorker(guild_id=policy.guild_id)
            session.add(worker)
        worker.policy_hash, worker.last_seen, worker.last_error = policy.digest, now(), error
