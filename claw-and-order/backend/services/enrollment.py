"""Reviewed roster imports, account claims, and the durable membership work queue."""

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select, or_, func

from backend.enrollment_policy import EnrollmentError, snowflake
from backend.models import (Student, Enrollment, Team, EnrollmentState, EnrollmentImport,
                            RegistrationInvitation, MembershipSync, EnrollmentAudit, ManagedDiscordGrant)
from backend.roster_input import normalize_rows


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def audit(session, action, subject, revision, **details):
    session.add(EnrollmentAudit(action=action, subject_id=subject, revision=revision,
                                details_json=json.dumps(details, sort_keys=True)))


async def state_for_update(session):
    state = (await session.execute(select(EnrollmentState).where(EnrollmentState.id == 1)
                                   .with_for_update())).scalar_one_or_none()
    if state is None:
        # Release migrations seed this singleton. Development/tests may start empty.
        state = EnrollmentState(id=1, revision=0)
        session.add(state)
        await session.flush()
    return state


def row_of(student, enrollment):
    return {"student_id": student.id, "email": student.email, "display_name": student.display_name,
            "term_id": enrollment.term_id, "class_id": enrollment.class_id,
            "team_id": enrollment.team_id or "", "role_key": enrollment.role_key or "",
            "idea": enrollment.idea, "status": enrollment.status}


async def queue_sync(session, student_id, policy, revision, linked):
    job = (await session.execute(select(MembershipSync).where(
        MembershipSync.student_id == student_id, MembershipSync.guild_id == policy.guild_id
    ).with_for_update())).scalar_one_or_none()
    if job is None:
        job = MembershipSync(student_id=student_id, guild_id=policy.guild_id)
        session.add(job)
    job.policy_hash, job.revision = policy.digest, revision
    job.status = "pending" if linked else "awaiting_registration"
    job.next_attempt, job.last_error, job.completed_at = now(), None, None
    job.queued_at = now()
    job.attempts = 0
    # Persist approved grant intent before any network operation. If Discord accepts
    # an addition just before a worker crash, a later policy can still retire it.
    try:
        intended = await desired_roles(session, student_id, policy)
    except EnrollmentError:
        intended = set()
    recorded = set((await session.execute(select(ManagedDiscordGrant.role_id).where(
        ManagedDiscordGrant.student_id == student_id, ManagedDiscordGrant.guild_id == policy.guild_id))).scalars())
    for role_id in intended - recorded:
        session.add(ManagedDiscordGrant(student_id=student_id, guild_id=policy.guild_id, role_id=role_id))


async def preview_import(session, records, policy, context=None, *, kind="roster", sync_student_ids=None, source_batch_id=None, expected_revision=None):
    domains = tuple(d.strip().casefold() for d in os.getenv("ENROLLMENT_EMAIL_DOMAINS", "").split(",") if d.strip())
    rows = normalize_rows(records, domains) if kind != "reconcile" else []
    state = await state_for_update(session)
    if expected_revision is not None and state.revision != expected_revision:
        raise EnrollmentError("Roster changed while preparing this targeted change; read the student again", 409)
    targets = sorted(set(sync_student_ids or ()) | {r["student_id"] for r in rows})
    if len(targets) > 5000:
        raise EnrollmentError("Review at most 5,000 students in one change; use scoped roster batches")
    students = {s.id: s for s in (await session.execute(select(Student).where(
        or_(Student.id.in_(targets), Student.email.in_([r["email"] for r in rows]))))).scalars()}
    emails = {s.email: s.id for s in students.values()}
    existing = {(e.student_id, e.term_id, e.class_id): e for e in (await session.execute(select(Enrollment).where(Enrollment.student_id.in_(targets)))).scalars()}
    known_teams = set((await session.execute(select(Team.id))).scalars())
    before, changes = [], []
    for row in rows:
        c = policy.classes.get((row["term_id"], row["class_id"]))
        if c is None:
            raise EnrollmentError("Roster references a term/class absent from the selected policy")
        if row["team_id"] and (row["team_id"] not in c["teams"] or row["team_id"] not in known_teams):
            raise EnrollmentError("Team is not mapped to this class or not provisioned in Claw & Order")
        if row["role_key"] and row["role_key"] not in policy.data["role_definitions"]:
            raise EnrollmentError("Unknown student role key; define it explicitly or leave it blank")
        if row["email"] in emails and emails[row["email"]] != row["student_id"]:
            raise EnrollmentError("Email already belongs to another student; resolve the identity conflict")
        student = students.get(row["student_id"])
        previous = existing.get((row["student_id"], row["term_id"], row["class_id"]))
        old = row_of(student, previous) if previous else None
        before.append({"row": row, "previous": old, "previous_student": {
            "email": student.email, "display_name": student.display_name} if student else None})
        changes.append({"student_id": row["student_id"], "term_id": row["term_id"], "class_id": row["class_id"],
                        "operation": "unchanged" if old == row else "update" if previous else "create",
                        "before": old, "after": row})
    from types import SimpleNamespace
    prospective = dict(existing)
    for row in rows:
        prospective[(row["student_id"], row["term_id"], row["class_id"])] = SimpleNamespace(**row)
    enrollments_by_student, grants_by_student = {}, {}
    for key, enrollment in prospective.items():
        enrollments_by_student.setdefault(key[0], []).append(enrollment)
    for grant in (await session.execute(select(ManagedDiscordGrant).where(
        ManagedDiscordGrant.guild_id == policy.guild_id, ManagedDiscordGrant.student_id.in_(targets)))).scalars():
        grants_by_student.setdefault(grant.student_id, set()).add(grant.role_id)
    role_preview = []
    for student_id in targets:
        owned = grants_by_student.get(student_id, set())
        try:
            planned = roles_for_enrollments(enrollments_by_student.get(student_id, []), policy)
            blocked = None
        except EnrollmentError as error:
            planned, blocked = set(), str(error)
        role_preview.append({"student_id": student_id, "discord_linked": bool(students.get(student_id) and students[student_id].discord_user_id),
                             "recorded_managed_role_ids": sorted(owned), "desired_role_ids": sorted(planned),
                             "planned_add_role_ids": sorted(planned - owned) if not blocked else [], "planned_remove_role_ids": sorted(owned - planned) if not blocked else [],
                             "blocked_reason": blocked})
    labels = {}
    for c in policy.data["classes"]:
        labels[c["discord_role_id"]] = f"Class {c['class_id']} / {c['term_id']}"
        for team_id, team in c["teams"].items():
            labels[team["discord_role_id"]] = f"Team {team_id}"
        for key, role_id in c["student_roles"].items():
            labels[role_id] = f"{policy.data['role_definitions'][key]['label']} / {c['class_id']}"
    review = {"kind": kind, "context": context or {}, "source_batch_id": source_batch_id,
            "guild_id": policy.guild_id, "role_labels": labels, "changes": changes, "role_preview": role_preview,
            "students": len(targets), "enrollments": len(rows),
            "unassigned": sum(r["status"] == "active" and not r["team_id"] for r in rows),
            "pending_role_definitions": sorted({r["role_key"] for r in rows if r["role_key"] and (
                not policy.data["role_definitions"][r["role_key"]]["approved"] or
                r["role_key"] not in policy.classes[(r["term_id"], r["class_id"])]["student_roles"])})}
    payload = {"rows": rows, "before": before, "sync_student_ids": targets, "review": review}
    batch_id = str(uuid4())
    digest = hashlib.sha256(json.dumps({"id": batch_id, "payload": payload,
        "revision": state.revision, "policy": policy.digest}, sort_keys=True).encode()).hexdigest()
    session.add(EnrollmentImport(id=batch_id, payload_json=json.dumps(payload), digest=digest,
                                policy_hash=policy.digest, base_revision=state.revision))
    audit(session, "change_preview_created", batch_id, state.revision,
          kind=kind, context=context or {}, students=len(targets), policy_hash=policy.digest)
    return {"batch_id": batch_id, "digest": digest, "base_revision": state.revision, "policy_hash": policy.digest, **review}


async def apply_import(session, batch_id, digest, policy, approved_by=None):
    state = await state_for_update(session)
    batch = (await session.execute(select(EnrollmentImport).where(EnrollmentImport.id == batch_id)
                                   .with_for_update())).scalar_one_or_none()
    if not batch or not hmac.compare_digest(batch.digest, digest):
        raise EnrollmentError("Import preview or confirmation digest is invalid", 409)
    if batch.applied_at:
        return {"batch_id": batch.id, "revision": batch.base_revision + 1, "already_applied": True}
    if utc(batch.created_at) < now() - timedelta(hours=24):
        raise EnrollmentError("Import preview expired; generate a new preview", 409)
    if state.revision != batch.base_revision or policy.digest != batch.policy_hash:
        raise EnrollmentError("Roster or policy changed since preview; review a new preview", 409)
    payload = json.loads(batch.payload_json)
    rows = payload["rows"]
    state.revision += 1
    touched = {}
    for row in rows:
        student = touched.get(row["student_id"]) or await session.get(Student, row["student_id"])
        if student is None:
            student = Student(id=row["student_id"])
            session.add(student)
        elif student.email != row["email"]:
            old_codes = (await session.execute(select(RegistrationInvitation).where(
                RegistrationInvitation.student_id == student.id, RegistrationInvitation.used_at.is_(None)))).scalars().all()
            for invitation in old_codes:
                invitation.revoked = True
        student.email, student.display_name = row["email"], row["display_name"]
        touched[student.id] = student
        await session.flush()
        key = (student.id, row["term_id"], row["class_id"])
        enrollment = await session.get(Enrollment, key)
        if enrollment is None:
            enrollment = Enrollment(student_id=student.id, term_id=row["term_id"], class_id=row["class_id"])
            session.add(enrollment)
        enrollment.team_id, enrollment.role_key = row["team_id"] or None, row["role_key"] or None
        enrollment.idea, enrollment.status = row["idea"], row["status"]
    for student_id in payload.get("sync_student_ids", []):
        if student_id not in touched:
            student = await session.get(Student, student_id)
            if student:
                touched[student_id] = student
    for student in touched.values():
        await queue_sync(session, student.id, policy, state.revision, bool(student.discord_user_id))
    batch.applied_at = now()
    context = payload.get("review", {}).get("context", {})
    audit(session, "import_applied", batch.id, state.revision, students=len(touched), enrollments=len(rows),
          policy_hash=policy.digest, context=context, approved_by=approved_by)
    for student_id in touched:
        diffs = []
        for entry in payload["before"]:
            if entry["row"]["student_id"] != student_id:
                continue
            previous, proposed = entry["previous"] or {}, entry["row"]
            diffs.append({"term_id": proposed["term_id"], "class_id": proposed["class_id"],
                          "before": {f: previous.get(f) for f in ("team_id", "role_key", "status")},
                          "after": {f: proposed.get(f) for f in ("team_id", "role_key", "status")},
                          "identity_fields_changed": any(previous.get(f) != proposed.get(f) for f in ("email", "display_name"))})
        audit(session, "student_change_approved", student_id, state.revision, batch_id=batch.id,
              context=context, approved_by=approved_by, changes=diffs)
    return {"batch_id": batch.id, "revision": state.revision, "already_applied": False}


async def rollback_preview(session, batch_id, policy, context=None):
    await state_for_update(session)
    batch = await session.get(EnrollmentImport, batch_id)
    if not batch or not batch.applied_at:
        raise EnrollmentError("Only an applied import can have a rollback preview", 404)
    payload = json.loads(batch.payload_json)
    if payload.get("review", {}).get("kind") == "reconcile":
        raise EnrollmentError("A reconciliation changes Discord convergence, not roster rows; preview another reconciliation", 409)
    rows = []
    for entry in payload["before"]:
        student = await session.get(Student, entry["row"]["student_id"])
        current = await session.get(Enrollment, (student.id, entry["row"]["term_id"], entry["row"]["class_id"])) if student else None
        if current is None or row_of(student, current) != entry["row"]:
            raise EnrollmentError("A later change affects this student; rollback would overwrite it. Prepare a targeted change instead", 409)
        row = dict(entry["previous"] or entry["row"])
        if entry["previous"] is None:
            row.update(status="withdrawn", team_id="", role_key="")
            if entry["previous_student"]:
                row.update(entry["previous_student"])
        rows.append(row)
    return await preview_import(session, rows, policy, context, kind="rollback", source_batch_id=batch_id)


async def reconcile_preview(session, policy, context=None, student_ids=None):
    await state_for_update(session)
    # Include prior jobs/grants even when the new policy removes an entire class.
    targets = set((await session.execute(select(MembershipSync.student_id).where(MembershipSync.guild_id == policy.guild_id))).scalars())
    targets.update((await session.execute(select(ManagedDiscordGrant.student_id).where(ManagedDiscordGrant.guild_id == policy.guild_id))).scalars())
    targets.update(e.student_id for e in (await session.execute(select(Enrollment))).scalars()
                   if (e.term_id, e.class_id) in policy.classes)
    if student_ids is not None:
        requested = set(student_ids)
        if len(requested) != len(student_ids) or not requested.issubset(targets):
            raise EnrollmentError("Reconciliation targets must be unique known students for this server")
        targets = requested
    if not targets:
        raise EnrollmentError("No enrollment or previously managed access exists for the selected server")
    return await preview_import(session, [], policy, context, kind="reconcile", sync_student_ids=targets)


async def issue_invitations(session, student_ids, ttl_hours, policy):
    if not 1 <= ttl_hours <= 168 or not student_ids or len(student_ids) > 5000 or len(student_ids) != len(set(student_ids)):
        raise EnrollmentError("Provide unique student IDs and a lifetime of 1–168 hours")
    state = await state_for_update(session)
    output = []
    for student_id in student_ids:
        student = await session.get(Student, student_id)
        enrollments = (await session.execute(select(Enrollment).where(
            Enrollment.student_id == student_id, Enrollment.status == "active"))).scalars().all()
        if not student or not any((e.term_id, e.class_id) in policy.classes for e in enrollments):
            raise EnrollmentError("An invitation target has no active enrollment in the selected server")
        if student.discord_user_id:
            raise EnrollmentError("An invitation target is already registered; reconcile its roles instead", 409)
        old = (await session.execute(select(RegistrationInvitation).where(
            RegistrationInvitation.student_id == student_id, RegistrationInvitation.guild_id == policy.guild_id,
            RegistrationInvitation.used_at.is_(None)))).scalars().all()
        for invitation in old:
            invitation.revoked = True
        code = "CLAW-" + secrets.token_urlsafe(32)
        expires = now() + timedelta(hours=ttl_hours)
        session.add(RegistrationInvitation(code_hash=hashlib.sha256(code.encode()).hexdigest(),
            student_id=student_id, guild_id=policy.guild_id, expires_at=expires))
        output.append({"student_id": student.id, "email": student.email, "invite_url": policy.data["invite_url"],
                       "registration_code": code, "expires_at": expires.isoformat(),
                       "instructions": "Join the server, then run /register and enter your registration code privately."})
    audit(session, "invitations_issued", policy.guild_id, state.revision, count=len(output))
    return output


async def claim_registration(session, code, discord_user_id, guild_id, policy):
    snowflake(discord_user_id)
    if guild_id != policy.guild_id or not isinstance(code, str) or not 20 <= len(code) <= 128:
        raise EnrollmentError("Registration code is invalid or unavailable")
    state = await state_for_update(session)
    invitation = (await session.execute(select(RegistrationInvitation).where(
        RegistrationInvitation.code_hash == hashlib.sha256(code.encode()).hexdigest()
    ).with_for_update())).scalar_one_or_none()
    if not invitation or invitation.guild_id != guild_id or invitation.revoked or invitation.used_at or utc(invitation.expires_at) <= now():
        raise EnrollmentError("Registration code is invalid, used, revoked or expired")
    student = await session.get(Student, invitation.student_id)
    owner = (await session.execute(select(Student.id).where(Student.discord_user_id == discord_user_id))).scalar_one_or_none()
    active = (await session.execute(select(Enrollment).where(Enrollment.student_id == student.id, Enrollment.status == "active"))).scalars().all()
    if not any((e.term_id, e.class_id) in policy.classes for e in active):
        raise EnrollmentError("No active enrollment is available for registration")
    if student.discord_user_id not in (None, discord_user_id) or owner not in (None, student.id):
        raise EnrollmentError("This student or Discord account is already linked; ask an instructor to review it", 409)
    previous_job = await session.get(MembershipSync, (student.id, guild_id))
    if previous_job is None or previous_job.policy_hash != policy.digest:
        raise EnrollmentError("Role policy changed since approval; staff must review reconciliation before registration", 409)
    approval_revision = previous_job.revision
    student.discord_user_id = discord_user_id
    invitation.used_at = now()
    state.revision += 1
    await queue_sync(session, student.id, policy, approval_revision, True)
    audit(session, "student_registered", student.id, state.revision, guild_id=guild_id)
    return {"status": "registered", "role_sync": "pending"}


async def desired_roles(session, student_id, policy):
    enrollments = (await session.execute(select(Enrollment).where(
        Enrollment.student_id == student_id, Enrollment.status == "active"))).scalars().all()
    return roles_for_enrollments(enrollments, policy)


def roles_for_enrollments(enrollments, policy):
    roles = set()
    for enrollment in enrollments:
        if enrollment.status != "active":
            continue
        c = policy.classes.get((enrollment.term_id, enrollment.class_id))
        if c is None:
            continue  # Other terms/servers do not grant access in this policy.
        roles.add(c["discord_role_id"])
        if enrollment.team_id:
            if enrollment.team_id not in c["teams"]:
                raise EnrollmentError("Assigned team is absent from the approved policy")
            roles.add(c["teams"][enrollment.team_id]["discord_role_id"])
        if enrollment.role_key:
            definition = policy.data["role_definitions"].get(enrollment.role_key)
            if not definition or not definition["approved"] or enrollment.role_key not in c["student_roles"]:
                raise EnrollmentError("Student role definition or class role mapping awaits approval")
            roles.add(c["student_roles"][enrollment.role_key])
    return roles


async def export_teams(session, policy):
    counts = {tuple(row[:3]): row[3] for row in (await session.execute(select(
        Enrollment.term_id, Enrollment.class_id, Enrollment.team_id, func.count()).where(
        Enrollment.status == "active", Enrollment.team_id.is_not(None)).group_by(
        Enrollment.term_id, Enrollment.class_id, Enrollment.team_id))).all()}
    ids = [team_id for c in policy.data["classes"] for team_id in c["teams"]]
    teams = {t.id: t for t in (await session.execute(select(Team).where(Team.id.in_(ids)))).scalars()}
    rows = []
    for c in policy.data["classes"]:
        for team_id in c["teams"]:
            if team_id not in teams:
                raise EnrollmentError("A policy team is not provisioned in Claw & Order", 503)
            team = teams[team_id]
            rows.append({"team_id": team.id, "class_id": c["class_id"], "term_id": c["term_id"],
                         "display_name": team.display_name, "member_count": counts.get((c["term_id"], c["class_id"], team.id), 0), "cidr": team.dmz_cidr})
    return {"version": 1, "teams": sorted(rows, key=lambda row: row["team_id"])}
