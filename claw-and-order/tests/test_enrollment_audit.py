"""Threat-boundary and human review regressions; synthetic identities only."""

import hashlib
import json
import ssl
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from backend.database import verified_database_settings
from backend.enrollment_policy import parse_policy, EnrollmentError
from backend.enrollment_reports import change_report, history_report, cell
from backend.main import app, restore_competition_phase
from backend.models import EnrollmentAudit, MembershipSync, PhaseLog, RegistrationInvitation
from backend.services.enrollment import preview_import, apply_import, reconcile_preview, rollback_preview, claim_registration, issue_invitations
from backend.services.enrollment_review import batch_review
from backend.services.membership_sync import sync_once, MembershipError
from backend.services.phase import set_current_phase, get_current_phase
from tests.conftest import _session_factory, TEST_ADMIN_SECRET
from tests.test_enrollment import enrollment_setup, row, policy_data, apply_rows, register, FakeDiscord, GUILD, USER, CLASS_A, TEAM_A

CONTEXT = {"requested_by": "hermes_planner", "reason": "Faculty approved team placement request", "ticket": "CLAW-101"}


@pytest.mark.asyncio
async def test_removed_class_is_included_in_review_and_access_is_revoked(enrollment_setup):
    await apply_rows([row()], enrollment_setup)
    await register(enrollment_setup)
    discord = FakeDiscord()
    await sync_once(_session_factory, enrollment_setup, discord)
    changed = policy_data()
    changed["classes"] = changed["classes"][1:]
    policy = parse_policy(changed)
    async with _session_factory() as session, session.begin():
        preview = await reconcile_preview(session, policy, CONTEXT)
        assert preview["changes"] == []
        assert set(preview["role_preview"][0]["planned_remove_role_ids"]) == {CLASS_A, TEAM_A}
    async with _session_factory() as session, session.begin():
        await apply_import(session, preview["batch_id"], preview["digest"], policy, "lab_admin")
    await sync_once(_session_factory, policy, discord)
    assert discord.roles[USER] == {"990000000000000001"}


@pytest.mark.asyncio
async def test_rollback_refuses_to_overwrite_later_assignment(enrollment_setup):
    first = await apply_rows([row()], enrollment_setup)
    await apply_rows([row(team="team-gamma")], enrollment_setup)
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="later change"):
            await rollback_preview(session, first["batch_id"], enrollment_setup, CONTEXT)


@pytest.mark.asyncio
async def test_planner_cannot_apply_or_issue_invites_and_private_errors_never_echo_roster(enrollment_setup, monkeypatch):
    monkeypatch.setenv("ENROLLMENT_PLANNER_SECRET", "separate-test-planner")
    planner = {"X-Enrollment-Planner-Secret": "separate-test-planner"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        bad = await client.post("/api/admin/enrollment/imports", headers=planner, json={"rows": [row()]})
        assert bad.status_code == 422 and bad.headers["cache-control"] == "no-store"
        assert "college.example" not in bad.text
        result = await client.post("/api/admin/enrollment/imports", headers=planner, json={"rows": [row()], "context": CONTEXT})
        assert result.status_code == 200
        batch = result.json()
        assert (await client.get(f"/api/admin/enrollment/imports/{batch['batch_id']}/review", headers=planner)).status_code == 200
        for path, body in [(f"/imports/{batch['batch_id']}/apply", {"digest": batch["digest"], "approved_by": "lab_admin"}), ("/invitations", {"student_ids": ["s1"]}), ("/invitations/check", {"entries": []})]:
            denied = await client.post("/api/admin/enrollment" + path, headers=planner, json=body)
            assert denied.status_code in (401, 422) and denied.headers["cache-control"] == "no-store"
        assert (await client.post(f"/api/admin/enrollment/imports/{batch['batch_id']}/apply", headers={"X-Admin-Secret": TEST_ADMIN_SECRET}, json={"digest": batch["digest"], "approved_by": "lab_admin"})).status_code == 200


@pytest.mark.asyncio
async def test_verified_reports_history_and_superseded_changes(enrollment_setup):
    context = {**CONTEXT, "reason": "Move request <script>alert(1)</script> [data](https://example.invalid)"}
    async with _session_factory() as session, session.begin():
        preview = await preview_import(session, [row()], enrollment_setup, context)
    async with _session_factory() as session, session.begin():
        await apply_import(session, preview["batch_id"], preview["digest"], enrollment_setup, "lab_admin")
    await register(enrollment_setup)
    await sync_once(_session_factory, enrollment_setup, FakeDiscord())
    async with _session_factory() as session:
        review = await batch_review(session, preview["batch_id"])
        assert review["verification"]["outcome"] == "VERIFIED"
        report = change_report(review)
        assert cell("lab_admin") in report and "&lt;script&gt;" in report and "<script>" not in report
        assert "@college" not in report and "private idea" not in report
        events = [{"id": a.id, "action": a.action, "revision": a.revision, "created_at": a.created_at.isoformat(), "details": json.loads(a.details_json)} for a in (await session.execute(select(EnrollmentAudit).where(EnrollmentAudit.subject_id == "s1"))).scalars()]
        history = history_report("s1", events)
        assert "CLAW" in history and "team" in history and cell("lab_admin") in history
    await apply_rows([row(team="team-gamma")], enrollment_setup)
    async with _session_factory() as session:
        assert (await batch_review(session, preview["batch_id"]))["verification"]["outcome"] == "SUPERSEDED"


@pytest.mark.asyncio
async def test_no_success_when_discord_did_not_apply_roles_or_effective_isolation_fails(enrollment_setup):
    await apply_rows([row()], enrollment_setup)
    await register(enrollment_setup)
    discord = FakeDiscord()
    async def ignored(*_):
        pass
    discord.add_role = ignored
    await sync_once(_session_factory, enrollment_setup, discord)
    async with _session_factory() as session:
        assert (await session.get(MembershipSync, ("s1", GUILD))).status == "retry"
    await apply_rows([row()], enrollment_setup)
    discord = FakeDiscord()
    async def leak(*_):
        raise MembershipError("Unmanaged role exposes the wrong team", "blocked_isolation")
    discord.verify_access = leak
    await sync_once(_session_factory, enrollment_setup, discord)
    async with _session_factory() as session:
        assert (await session.get(MembershipSync, ("s1", GUILD))).status == "blocked_isolation"


@pytest.mark.asyncio
async def test_registration_cannot_expand_a_changed_policy_without_review(enrollment_setup):
    await apply_rows([row()], enrollment_setup)
    async with _session_factory() as session, session.begin():
        invite = (await issue_invitations(session, ["s1"], 72, enrollment_setup))[0]
    changed = policy_data()
    changed["invite_url"] = "https://discord.gg/changed"
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="since approval"):
            await claim_registration(session, invite["registration_code"], USER, GUILD, parse_policy(changed))
        invitation = await session.get(RegistrationInvitation, hashlib.sha256(invite["registration_code"].encode()).hexdigest())
        assert invitation.used_at is None


@pytest.mark.asyncio
async def test_backend_restart_restores_phase_and_rejects_invalid_stored_phase():
    async with _session_factory() as session, session.begin():
        session.add(PhaseLog(phase=3, activated_by="synthetic_admin"))
    set_current_phase(1)
    async with _session_factory() as session:
        await restore_competition_phase(session)
    assert get_current_phase() == 3
    async with _session_factory() as session, session.begin():
        session.add(PhaseLog(phase=99, activated_by="synthetic_corrupt_fixture"))
    async with _session_factory() as session:
        with pytest.raises(RuntimeError, match="Stored competition phase"):
            await restore_competition_phase(session)


def test_remote_database_requires_verified_tls_even_if_url_requests_disable():
    url, options = verified_database_settings("postgresql+asyncpg://claw:placeholder@db.example/claw?sslmode=disable", True)
    assert "sslmode" not in url.query
    assert options["ssl"].verify_mode == ssl.CERT_REQUIRED and options["ssl"].check_hostname
    _, options = verified_database_settings("postgresql+asyncpg://claw:placeholder@127.0.0.1/claw", True)
    assert not options


@pytest.mark.asyncio
async def test_targeted_change_refuses_stale_read_revision(enrollment_setup):
    await apply_rows([row()], enrollment_setup)
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="while preparing"):
            await preview_import(session, [row(team="team-gamma")], enrollment_setup, CONTEXT, expected_revision=0)


@pytest.mark.asyncio
async def test_cli_targeted_change_writes_readable_plan_and_rejects_local_tampering(enrollment_setup, monkeypatch, tmp_path):
    from backend import enrollment_ops as ops
    async with _session_factory() as session, session.begin():
        frozen = await preview_import(session, [row(team="team-gamma")], enrollment_setup, CONTEXT)
    calls = []
    def api(path, method="GET", data=None, **kwargs):
        calls.append((path, method, data))
        if path.startswith("/students?"):
            source = row()
            return {"revision": 7, "students": [{**{k: source[k] for k in ("student_id", "email", "display_name")}, "enrollments": [source, row(cls="B", team="team-beta")]}]}
        if path == "/imports":
            assert data["expected_revision"] == 7
            assert len(data["rows"]) == 1 and data["rows"][0]["class_id"] == "A"
            assert data["rows"][0]["team_id"] == "team-gamma" and data["rows"][0]["idea"] == "private idea"
            return frozen
        if path.endswith("/review"):
            return frozen
        pytest.fail("No apply request should be sent")
    monkeypatch.setattr(ops, "request_api", api)
    output = tmp_path / "change.json"
    ops.main(["change", "--student-id", "s1", "--term-id", "2026F", "--class-id", "A", "--team-id", "team-gamma", "--requested-by", "hermes_planner", "--reason", CONTEXT["reason"], "--ticket", CONTEXT["ticket"], "--out", str(output)])
    assert output.stat().st_mode & 0o777 == 0o600
    assert "Discord permission plan" in Path(str(output) + ".md").read_text()
    tampered = json.loads(output.read_text())
    tampered["role_preview"] = []
    output.write_text(json.dumps(tampered))
    with pytest.raises(ValueError, match="could not be confirmed"):
        ops.main(["apply", str(output), "--confirm-digest", frozen["digest"], "--approved-by", "lab_admin", "--out", str(tmp_path / "receipt.json")])
    assert (tmp_path / "receipt.json.failure.md").exists()
    assert not any(path.endswith("/apply") for path, _, _ in calls)


@pytest.mark.asyncio
async def test_large_preview_has_bounded_database_round_trips(enrollment_setup):
    from sqlalchemy import event
    from tests.conftest import _engine
    statements = []
    def count(_, __, statement, *args):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)
    event.listen(_engine.sync_engine, "before_cursor_execute", count)
    try:
        async with _session_factory() as session, session.begin():
            preview = await preview_import(session, [row(f"synthetic_{i}") for i in range(500)], enrollment_setup, CONTEXT)
        assert preview["students"] == 500 and len(statements) <= 10
    finally:
        event.remove(_engine.sync_engine, "before_cursor_execute", count)


@pytest.mark.asyncio
async def test_verification_exit_codes_never_report_waiting_as_success(enrollment_setup, monkeypatch, tmp_path):
    from backend import enrollment_ops as ops
    async with _session_factory() as session, session.begin():
        preview = await preview_import(session, [row()], enrollment_setup, CONTEXT)
    monkeypatch.setattr(ops, "request_api", lambda *a, **k: {**preview, "verification": {"outcome": "WAITING", "students": []}})
    assert ops.main(["verify", "--batch-id", preview["batch_id"], "--out", str(tmp_path / "waiting.md")]) == 3
    monkeypatch.setattr(ops, "request_api", lambda *a, **k: {**preview, "verification": {"outcome": "BLOCKED", "students": []}})
    assert ops.main(["verify", "--batch-id", preview["batch_id"], "--out", str(tmp_path / "blocked.md")]) == 4
