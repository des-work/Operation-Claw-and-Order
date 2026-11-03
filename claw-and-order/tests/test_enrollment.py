"""Enrollment pipeline acceptance tests; no real student data or Discord requests."""

import copy
import hashlib
import io
import json
import zipfile
from datetime import timedelta
from xml.sax.saxutils import escape

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, func

from backend.main import app
from backend.enrollment_policy import parse_policy, EnrollmentError
from backend.models import (Student, Enrollment, EnrollmentImport, RegistrationInvitation,
                            MembershipSync, Team, Event, DiscordOutbox, EnrollmentState)
from backend.roster_input import read_roster, normalize_rows, FIELDS
from backend.services.enrollment import (preview_import, apply_import, issue_invitations,
    claim_registration, rollback_preview, now, export_teams)
from backend.services.membership_sync import sync_once, MembershipError, heartbeat
from tests.conftest import _session_factory, TEST_ADMIN_SECRET, TEST_EXPORT_SECRET

GUILD = "910000000000000001"
USER = "920000000000000001"
CLASS_A, CLASS_B = "930000000000000001", "930000000000000002"
TEAM_A, TEAM_B, TEAM_C = "940000000000000001", "940000000000000002", "940000000000000003"


def policy_data():
    return {"version": 1, "guild_id": GUILD, "invite_url": "https://discord.gg/test-only",
        "role_definitions": {}, "classes": [
        {"term_id": "2026F", "class_id": "A", "discord_role_id": CLASS_A,
         "teams": {"team-alpha": {"discord_role_id": TEAM_A, "channel_ids": ["950000000000000001"]},
                   "team-gamma": {"discord_role_id": TEAM_C, "channel_ids": ["950000000000000003"]}}, "student_roles": {}},
        {"term_id": "2026F", "class_id": "B", "discord_role_id": CLASS_B,
         "teams": {"team-beta": {"discord_role_id": TEAM_B, "channel_ids": ["950000000000000002"]}}, "student_roles": {}}]}


def row(student="s1", cls="A", team="team-alpha", **extra):
    return dict(student_id=student, email=f"{student}@college.example", display_name=student,
                term_id="2026F", class_id=cls, team_id=team, role_key="", idea="private idea", status="active", **extra)


@pytest_asyncio.fixture
async def enrollment_setup(tmp_path, monkeypatch):
    data = policy_data()
    file = tmp_path / "policy.json"
    file.write_text(json.dumps(data))
    monkeypatch.setenv("ENROLLMENT_POLICY_FILE", str(file))
    async with _session_factory() as session, session.begin():
        for name in ("team-beta", "team-gamma"):
            session.add(Team(id=name, display_name=name, wan_ip="192.0.2.1", dmz_cidr="10.2.1.0/24", lan_cidr="10.2.2.0/24", bearer_token_hash="test"))
    return parse_policy(data)


async def apply_rows(rows, policy):
    async with _session_factory() as session, session.begin():
        preview = await preview_import(session, rows, policy)
    async with _session_factory() as session, session.begin():
        await apply_import(session, preview["batch_id"], preview["digest"], policy)
    return preview


async def register(policy, student="s1", user=USER):
    async with _session_factory() as session, session.begin():
        invitations = await issue_invitations(session, [student], 72, policy)
    async with _session_factory() as session, session.begin():
        await claim_registration(session, invitations[0]["registration_code"], user, GUILD, policy)
    return invitations[0]["registration_code"]


class FakeDiscord:
    def __init__(self):
        self.roles = {USER: {"990000000000000001"}}  # unrelated role must survive
        self.operations = []
        self.fail_add_once = False
    async def validate(self, *_):
        pass
    async def member_roles(self, user):
        if user not in self.roles:
            raise MembershipError("not joined", "awaiting_member")
        return set(self.roles[user])
    async def add_role(self, user, role, reason):
        if self.fail_add_once:
            self.fail_add_once = False
            raise MembershipError("temporary outage")
        self.operations.append(("add", role))
        self.roles[user].add(role)
    async def remove_role(self, user, role, reason):
        self.operations.append(("remove", role))
        self.roles[user].discard(role)


@pytest.mark.asyncio
async def test_preview_apply_dual_class_registration_and_export(enrollment_setup):
    policy = enrollment_setup
    async with _session_factory() as session, session.begin():
        preview = await preview_import(session, [row(), row(cls="B", team="team-beta"), row("s2", team="")], policy)
        assert await session.scalar(select(func.count()).select_from(Student)) == 0
        assert preview["students"] == 2 and preview["unassigned"] == 1
        assert set(preview["role_preview"][0]["desired_role_ids"]) == {CLASS_A, TEAM_A, CLASS_B, TEAM_B}
    async with _session_factory() as session, session.begin():
        first = await apply_import(session, preview["batch_id"], preview["digest"], policy)
        assert not first["already_applied"]
    async with _session_factory() as session, session.begin():
        assert (await apply_import(session, preview["batch_id"], preview["digest"], policy))["already_applied"]
        assert await session.scalar(select(func.count()).select_from(Student)) == 2
        assert await session.scalar(select(func.count()).select_from(Enrollment)) == 3
        assert await session.scalar(select(func.count()).select_from(Event)) == 0
        assert await session.scalar(select(func.count()).select_from(DiscordOutbox)) == 0
        exported = await export_teams(session, policy)
        assert exported["teams"][0]["member_count"] == 1
        assert "email" not in json.dumps(exported) and "private idea" not in json.dumps(exported)
    await register(policy)
    adapter = FakeDiscord()
    assert await sync_once(_session_factory, policy, adapter) == 1
    assert adapter.roles[USER] == {"990000000000000001", CLASS_A, TEAM_A, CLASS_B, TEAM_B}
    assert await sync_once(_session_factory, policy, adapter) == 0


@pytest.mark.asyncio
async def test_omitted_student_stays_active_and_unassigned_is_supported(enrollment_setup):
    await apply_rows([row(), row("s2", team="")], enrollment_setup)
    await apply_rows([row()], enrollment_setup)
    async with _session_factory() as session:
        enrollment = await session.get(Enrollment, ("s2", "2026F", "A"))
        assert enrollment.status == "active" and enrollment.team_id is None
    await register(enrollment_setup, "s2")
    adapter = FakeDiscord()
    await sync_once(_session_factory, enrollment_setup, adapter)
    assert CLASS_A in adapter.roles[USER] and TEAM_A not in adapter.roles[USER]


@pytest.mark.asyncio
async def test_stale_and_changed_policy_previews_are_rejected(enrollment_setup):
    async with _session_factory() as session, session.begin():
        a = await preview_import(session, [row()], enrollment_setup)
        b = await preview_import(session, [row("s2")], enrollment_setup)
    async with _session_factory() as session, session.begin():
        await apply_import(session, a["batch_id"], a["digest"], enrollment_setup)
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="changed"):
            await apply_import(session, b["batch_id"], b["digest"], enrollment_setup)
    async with _session_factory() as session, session.begin():
        c = await preview_import(session, [row()], enrollment_setup)
    changed = policy_data()
    changed["invite_url"] = "https://discord.gg/new-test-invite"
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="changed"):
            await apply_import(session, c["batch_id"], c["digest"], parse_policy(changed))


@pytest.mark.asyncio
async def test_email_correction_revokes_invites_and_prevents_old_recipient_delivery(enrollment_setup):
    await apply_rows([row()], enrollment_setup)
    async with _session_factory() as session, session.begin():
        invite = (await issue_invitations(session, ["s1"], 72, enrollment_setup))[0]
    code_hash = hashlib.sha256(invite["registration_code"].encode()).hexdigest()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        headers = {"X-Admin-Secret": TEST_ADMIN_SECRET}
        checks = {"entries": [{"code_hash": code_hash, "email": "wrong@college.example"}]}
        assert (await client.post("/api/admin/enrollment/invitations/check", headers=headers, json=checks)).json()[code_hash] == "recipient_or_enrollment_changed"
        checks["entries"][0]["email"] = invite["email"]
        assert (await client.post("/api/admin/enrollment/invitations/check", headers=headers, json=checks)).json()[code_hash] == "valid"
        changed = row()
        changed["email"] = "corrected@college.example"
        await apply_rows([changed], enrollment_setup)
        assert (await client.post("/api/admin/enrollment/invitations/check", headers=headers, json=checks)).json()[code_hash] == "revoked_used_or_expired"
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError):
            await claim_registration(session, invite["registration_code"], USER, GUILD, enrollment_setup)


@pytest.mark.asyncio
async def test_registration_codes_are_hashed_single_use_rotatable_and_server_bound(enrollment_setup):
    await apply_rows([row(), row("s2")], enrollment_setup)
    async with _session_factory() as session, session.begin():
        first = (await issue_invitations(session, ["s1"], 1, enrollment_setup))[0]
    async with _session_factory() as session, session.begin():
        second = (await issue_invitations(session, ["s1"], 1, enrollment_setup))[0]
        assert (await session.get(RegistrationInvitation, hashlib.sha256(first["registration_code"].encode()).hexdigest())).revoked
    for code, guild in [(first["registration_code"], GUILD), (second["registration_code"], "910000000000000002")]:
        async with _session_factory() as session:
            with pytest.raises(EnrollmentError):
                await claim_registration(session, code, USER, guild, enrollment_setup)
    async with _session_factory() as session, session.begin():
        await claim_registration(session, second["registration_code"], USER, GUILD, enrollment_setup)
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError):
            await claim_registration(session, second["registration_code"], USER, GUILD, enrollment_setup)
        records = (await session.execute(select(RegistrationInvitation))).scalars().all()
        assert all(record.code_hash != second["registration_code"] for record in records)
    async with _session_factory() as session, session.begin():
        expired = (await issue_invitations(session, ["s2"], 1, enrollment_setup))[0]
        invitation = await session.get(RegistrationInvitation, hashlib.sha256(expired["registration_code"].encode()).hexdigest())
        invitation.expires_at = now() - timedelta(seconds=1)
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="expired"):
            await claim_registration(session, expired["registration_code"], "920000000000000002", GUILD, enrollment_setup)


@pytest.mark.asyncio
async def test_one_discord_account_cannot_claim_two_students(enrollment_setup):
    await apply_rows([row(), row("s2")], enrollment_setup)
    await register(enrollment_setup)
    async with _session_factory() as session, session.begin():
        code = (await issue_invitations(session, ["s2"], 72, enrollment_setup))[0]["registration_code"]
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="already linked"):
            await claim_registration(session, code, USER, GUILD, enrollment_setup)


@pytest.mark.asyncio
async def test_transfer_partial_failure_retry_withdrawal_and_reviewed_rollback(enrollment_setup):
    policy, adapter = enrollment_setup, FakeDiscord()
    await apply_rows([row(), row(cls="B", team="team-beta")], policy)
    await register(policy)
    await sync_once(_session_factory, policy, adapter)
    transfer = await apply_rows([row(team="team-gamma")], policy)
    adapter.operations.clear()
    adapter.fail_add_once = True
    await sync_once(_session_factory, policy, adapter)
    assert TEAM_A not in adapter.roles[USER] and TEAM_C not in adapter.roles[USER]
    assert CLASS_B in adapter.roles[USER] and TEAM_B in adapter.roles[USER]
    async with _session_factory() as session, session.begin():
        job = await session.get(MembershipSync, ("s1", GUILD))
        assert job.status == "retry"
        job.next_attempt = now() - timedelta(seconds=1)
    await sync_once(_session_factory, policy, adapter)
    assert adapter.operations == [("remove", TEAM_A), ("add", TEAM_C)]
    async with _session_factory() as session, session.begin():
        restore = await rollback_preview(session, transfer["batch_id"], policy)
    async with _session_factory() as session, session.begin():
        await apply_import(session, restore["batch_id"], restore["digest"], policy)
    await sync_once(_session_factory, policy, adapter)
    assert TEAM_A in adapter.roles[USER] and TEAM_C not in adapter.roles[USER]
    withdrawn = row()
    withdrawn["status"] = "withdrawn"
    await apply_rows([withdrawn], policy)
    await sync_once(_session_factory, policy, adapter)
    assert CLASS_A not in adapter.roles[USER] and TEAM_A not in adapter.roles[USER]
    assert CLASS_B in adapter.roles[USER] and "990000000000000001" in adapter.roles[USER]


@pytest.mark.asyncio
async def test_pending_role_definition_blocks_access_until_approved(enrollment_setup):
    data = policy_data()
    data["role_definitions"]["pending_duty"] = {"label": "Awaiting definition", "purpose": "Test-only pending responsibility", "approved": False}
    data["classes"][0]["student_roles"]["pending_duty"] = "960000000000000001"
    policy = parse_policy(data)
    record = row()
    record["role_key"] = "pending_duty"
    preview = await apply_rows([record], policy)
    assert preview["pending_role_definitions"] == ["pending_duty"]
    assert preview["role_preview"][0]["blocked_reason"]
    await register(policy)
    adapter = FakeDiscord()
    await sync_once(_session_factory, policy, adapter)
    assert adapter.operations == []
    async with _session_factory() as session:
        assert (await session.get(MembershipSync, ("s1", GUILD))).status == "blocked_role_definition"
    data["role_definitions"]["pending_duty"]["approved"] = True
    approved = parse_policy(data)
    await apply_rows([record], approved)
    await sync_once(_session_factory, approved, adapter)
    assert "960000000000000001" in adapter.roles[USER]


@pytest.mark.asyncio
async def test_retired_role_is_removed_after_uncertain_discord_acceptance(enrollment_setup):
    await apply_rows([row()], enrollment_setup)
    await register(enrollment_setup)
    adapter = FakeDiscord()
    async def uncertain_add(user, role, reason):
        adapter.roles[user].add(role)
        if role == TEAM_A:
            raise MembershipError("Connection lost after Discord accepted the role")
    adapter.add_role = uncertain_add
    await sync_once(_session_factory, enrollment_setup, adapter)
    assert TEAM_A in adapter.roles[USER]
    changed = policy_data()
    del changed["classes"][0]["teams"]["team-alpha"]
    policy = parse_policy(changed)
    await apply_rows([row(team="team-gamma")], policy)
    await sync_once(_session_factory, policy, adapter)
    assert TEAM_A not in adapter.roles[USER]
    assert TEAM_C in adapter.roles[USER]
    assert "990000000000000001" in adapter.roles[USER]


@pytest.mark.asyncio
async def test_roster_rejects_unknown_team_role_email_and_duplicate_enrollment(enrollment_setup):
    cases = [[row(team="not-provisioned")], [row(), row()], [row(), {**row("s2"), "email": "s1@college.example"}], [{**row(), "role_key": "administrator"}]]
    for records in cases:
        async with _session_factory() as session:
            with pytest.raises(EnrollmentError):
                await preview_import(session, records, enrollment_setup)
    async with _session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Student)) == 0


@pytest.mark.asyncio
async def test_private_api_authorization_upload_limits_and_export_scope(enrollment_setup):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        admin = {"X-Admin-Secret": TEST_ADMIN_SECRET}
        export = {"X-Export-Secret": TEST_EXPORT_SECRET}
        assert (await client.get("/api/admin/enrollment/students", headers=export)).status_code == 422
        assert (await client.post("/api/admin/enrollment/imports", headers=export, json={"rows": [row()]})).status_code == 422
        response = await client.post("/api/admin/enrollment/imports", headers=admin, json={"rows": [row()]})
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        batch = response.json()
        assert (await client.post(f"/api/admin/enrollment/imports/{batch['batch_id']}/apply", headers=admin, json={"digest": batch["digest"]})).status_code == 200
        inventory = await client.get("/api/admin/enrollment/teams/export", headers=export)
        assert inventory.status_code == 200 and "private idea" not in inventory.text and "@college" not in inventory.text
        assert (await client.get("/api/admin/enrollment/teams/export", headers=admin)).status_code == 422
        over = await client.post("/api/admin/enrollment/imports/file?filename=huge.csv", content=b"x" * (10 * 1024 * 1024 + 1), headers=admin)
        assert over.status_code == 413
        health = (await client.get("/api/health")).json()
        assert health["enrollment"]["status"] == "degraded"
        assert "s1@college.example" not in json.dumps(health)
    await heartbeat(_session_factory, enrollment_setup)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/api/health")).json()["enrollment"]["status"] == "healthy"


def minimal_xlsx(values, formula=False):
    """Small independent OOXML fixture, not the delivered operator template."""
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Roster" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        rows = []
        for r, fields in enumerate([list(FIELDS), values], 1):
            cells = ''.join(f'<c r="{chr(65+c)}{r}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>' for c, v in enumerate(fields))
            if formula and r == 2:
                cells += '<c r="J2"><f>1+1</f><v>2</v></c>'
            rows.append(f'<row r="{r}">{cells}</row>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + ''.join(rows) + '</sheetData></worksheet>')
    return stream.getvalue()


def test_csv_xlsx_column_mapping_and_formula_rejection():
    record = row()
    parsed = read_roster(minimal_xlsx([record[f] for f in FIELDS]), "roster.xlsx")
    assert normalize_rows(parsed)[0] == record
    with pytest.raises(EnrollmentError, match="formulas"):
        read_roster(minimal_xlsx([record[f] for f in FIELDS], formula=True), "roster.xlsx")
    content = b"Student ID,College Email,Name,Term,Class,Team\ns1,s1@college.example,Student,2026F,A,\n"
    mapped = read_roster(content, "roster.csv", {"College Email": "email", "Name": "display_name", "Term": "term_id", "Class": "class_id", "Team": "team_id"})
    assert normalize_rows(mapped)[0]["team_id"] == ""
    with pytest.raises(EnrollmentError, match="stored as text"):
        normalize_rows([{**row(), "student_id": 123}])


def test_policy_does_not_guess_roles_and_rejects_unsafe_mapping():
    assert parse_policy(policy_data()).data["role_definitions"] == {}
    duplicate = policy_data()
    duplicate["classes"][1]["discord_role_id"] = CLASS_A
    with pytest.raises(EnrollmentError, match="distinct ID"):
        parse_policy(duplicate)
    bad = policy_data()
    bad["invite_url"] = "https://discord.gg.evil.example/invite"
    with pytest.raises(EnrollmentError, match="Discord HTTPS"):
        parse_policy(bad)


@pytest.mark.asyncio
async def test_export_refuses_to_hide_unprovisioned_policy_team(enrollment_setup):
    data = policy_data()
    teams = data["classes"][0]["teams"]
    teams["not-provisioned"] = teams.pop("team-alpha")
    async with _session_factory() as session:
        with pytest.raises(EnrollmentError, match="not provisioned") as error:
            await export_teams(session, parse_policy(data))
        assert error.value.status == 503
