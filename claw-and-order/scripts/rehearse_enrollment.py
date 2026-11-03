"""Reproduce the enrollment-to-dashboard rehearsal on TWO NEW PostgreSQL databases.

Requires requirements-dev.txt, dashboard npm dependencies, and environment:
REHEARSAL_DATABASE_URL=postgresql+asyncpg://.../claw_enrollment_rehearsal_<unique>
REHEARSAL_DASHBOARD_DATABASE_URL=postgresql://.../claw_dashboard_rehearsal_<unique>
CLAW_DASHBOARD_DIR=/path/to/c3claw
Use a protected environment file; do not place database passwords on the command line.
No real Discord or SMTP connection is made. No database is dropped or reset.
"""

import asyncio
import hashlib
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    backend = os.environ.get("REHEARSAL_DATABASE_URL", "")
    dashboard = os.environ.get("REHEARSAL_DASHBOARD_DATABASE_URL", "")
    if not urlsplit(backend).path.startswith("/claw_enrollment_rehearsal_") or not urlsplit(dashboard).path.startswith("/claw_dashboard_rehearsal_"):
        raise SystemExit("Refusing: two explicitly named fresh rehearsal databases are required")
    if not os.environ.get("CLAW_DASHBOARD_DIR"):
        raise SystemExit("Set CLAW_DASHBOARD_DIR")
    with tempfile.TemporaryDirectory(prefix="claw-enrollment-rehearsal-") as work:
        policy = {"version": 1, "guild_id": "910000000000000001", "invite_url": "https://discord.gg/synthetic-only",
            "role_definitions": {}, "classes": [
                {"term_id": "2026F", "class_id": "A", "discord_role_id": "930000000000000001",
                 "teams": {"ClassA_01": {"discord_role_id": "940000000000000001", "channel_ids": ["950000000000000001"]},
                           "ClassA_02": {"discord_role_id": "940000000000000002", "channel_ids": ["950000000000000002"]}}, "student_roles": {}},
                {"term_id": "2026F", "class_id": "B", "discord_role_id": "930000000000000002",
                 "teams": {"ClassB_01": {"discord_role_id": "940000000000000003", "channel_ids": ["950000000000000003"]}}, "student_roles": {}}]}
        policy_file = Path(work) / "policy.json"
        policy_file.write_text(json.dumps(policy))
        os.environ.update(DATABASE_URL=backend, ADMIN_SECRET=secrets.token_urlsafe(32),
            EVENT_EXPORT_SECRET=secrets.token_urlsafe(32), ENROLLMENT_POLICY_FILE=str(policy_file), APP_ENV="production",
            DISCORD_TOKEN="", DISCORD_WEBHOOK_URL="", DISCORD_WEBHOOKS_FILE="", DISCORD_WEBHOOKS_JSON="",
            ENROLLMENT_DISCORD_BOT_TOKEN="", ENROLLMENT_PLANNER_SECRET=secrets.token_urlsafe(32))
        import httpx
        from sqlalchemy import select, func
        from backend.database import async_session, engine
        from backend.models import Team, Student
        from backend.migrate import migrate
        from backend.enrollment_policy import load_policy
        from backend.services.enrollment import claim_registration
        from backend.services.membership_sync import sync_once, heartbeat
        from backend.enrollment_cli import private_write
        from backend.enrollment_reports import change_report
        report_dir = Path(os.getenv("REHEARSAL_REPORT_DIR", str(Path(work) / "reports")))
        report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        asyncio.run(migrate())
        asyncio.run(migrate())
        token = secrets.token_urlsafe(32)
        async def seed():
            async with async_session() as session, session.begin():
                if await session.scalar(select(func.count()).select_from(Team)) or await session.scalar(select(func.count()).select_from(Student)):
                    raise ValueError("Rehearsal requires fresh databases; existing data is left intact")
                for team_id in ("ClassA_01", "ClassA_02", "ClassB_01"):
                    session.add(Team(id=team_id, display_name=team_id, wan_ip="192.0.2.1", dmz_cidr="10.50.1.0/24",
                        lan_cidr="10.50.2.0/24", bearer_token_hash="sha256:" + hashlib.sha256(token.encode()).hexdigest()))
            await engine.dispose()
        asyncio.run(seed())
        log_file = Path(work) / "backend.log"
        with log_file.open("w") as log:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "18080"], cwd=ROOT, env=os.environ.copy(), stdout=log, stderr=log)
            try:
                with httpx.Client(base_url="http://127.0.0.1:18080", timeout=30) as client:
                    for _ in range(100):
                        try:
                            if client.get("/api/phase").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        if process.poll() is not None:
                            raise RuntimeError("Synthetic backend failed to start; inspect local rehearsal logs")
                        time.sleep(.1)
                    else:
                        raise RuntimeError("Synthetic backend startup timed out")
                    admin = {"X-Admin-Secret": os.environ["ADMIN_SECRET"]}
                    rows = [dict(student_id="synthetic-1", email="synthetic-1@college.example", display_name="Synthetic One",
                        term_id="2026F", class_id="A", team_id="ClassA_01", role_key="", idea="private idea", status="active")]
                    rows.append({**rows[0], "class_id": "B", "team_id": "ClassB_01"})
                    rows.append({**rows[0], "student_id": "synthetic-2", "email": "synthetic-2@college.example", "display_name": "Synthetic Two", "team_id": ""})
                    from openpyxl import load_workbook
                    from backend.roster_input import FIELDS
                    workbook = load_workbook(ROOT / "templates/roster.xlsx")
                    for index, record in enumerate(rows, 2):
                        for column, field in enumerate(FIELDS, 1):
                            workbook["Roster"].cell(index, column, record[field])
                    stream = io.BytesIO()
                    workbook.save(stream)
                    workbook.close()
                    preview = client.post("/api/admin/enrollment/imports/file?filename=synthetic-roster.xlsx",
                        headers={**admin, "X-Enrollment-Change": json.dumps({"requested_by": "synthetic_operator", "reason": "Isolated PostgreSQL acceptance rehearsal"})}, content=stream.getvalue())
                    preview.raise_for_status()
                    batch = preview.json()
                    private_write(report_dir / "01-roster-proposed.md", change_report(batch))
                    applied = client.post(f"/api/admin/enrollment/imports/{batch['batch_id']}/apply", headers=admin, json={"digest": batch["digest"], "approved_by": "synthetic_admin"})
                    applied.raise_for_status()
                    again = client.post(f"/api/admin/enrollment/imports/{batch['batch_id']}/apply", headers=admin, json={"digest": batch["digest"], "approved_by": "synthetic_admin"})
                    require(again.json()["already_applied"], "Repeated application was not idempotent")
                    invitations = client.post("/api/admin/enrollment/invitations", headers=admin, json={"student_ids": ["synthetic-1", "synthetic-2"]})
                    invitations.raise_for_status()
                    code = invitations.json()["invitations"][0]["registration_code"]
                    class FakeDiscord:
                        roles = {"990000000000000001"}
                        async def validate(self, *_): pass
                        async def verify_access(self, *_): pass
                        async def member_roles(self, _): return set(self.roles)
                        async def add_role(self, user, role, reason): self.roles.add(role)
                        async def remove_role(self, user, role, reason): self.roles.discard(role)
                    adapter = FakeDiscord()
                    async def claim_and_sync():
                        selected = load_policy()
                        async with async_session() as session, session.begin():
                            await claim_registration(session, code, "920000000000000001", selected.guild_id, selected)
                        require(await sync_once(async_session, selected, adapter) == 1, "Registration did not produce a role job")
                        require(adapter.roles == {"990000000000000001", "930000000000000001", "930000000000000002", "940000000000000001", "940000000000000003"}, "Registered student's class/team role union is wrong")
                        await heartbeat(async_session, selected)
                        await engine.dispose()
                    asyncio.run(claim_and_sync())
                    review = client.get(f"/api/admin/enrollment/imports/{batch['batch_id']}/review", headers=admin)
                    review.raise_for_status()
                    require(review.json()["verification"]["outcome"] == "WAITING", "Unregistered student was incorrectly certified")
                    private_write(report_dir / "02-roster-waiting.md", change_report(review.json()))
                    planner_env = os.environ.copy()
                    for key in ("ADMIN_SECRET", "EVENT_EXPORT_SECRET", "DATABASE_URL", "ENROLLMENT_DISCORD_BOT_TOKEN"):
                        planner_env.pop(key, None)
                    planner_env["CLAW_BACKEND_URL"] = "http://127.0.0.1:18080"
                    def cli(arguments, environment):
                        subprocess.run([sys.executable, "-m", "backend.enrollment_cli", *arguments], cwd=ROOT,
                                       env=environment, check=True, capture_output=True, text=True)
                    move_file = report_dir / "03-team-move.json"
                    cli(["change", "--student-id", "synthetic-1", "--term-id", "2026F", "--class-id", "A", "--team-id", "ClassA_02",
                         "--requested-by", "synthetic_hermes", "--reason", "Faculty approved synthetic team rebalance", "--ticket", "SYNTHETIC-101", "--out", str(move_file)], planner_env)
                    move = json.loads(move_file.read_text())
                    human_env = os.environ.copy()
                    human_env.pop("ENROLLMENT_PLANNER_SECRET", None)
                    human_env["CLAW_BACKEND_URL"] = "http://127.0.0.1:18080"
                    cli(["apply", str(move_file), "--confirm-digest", move["digest"], "--approved-by", "synthetic_admin", "--out", str(report_dir / "04-team-move-applied.json")], human_env)
                    async def sync_transfer():
                        require(await sync_once(async_session, load_policy(), adapter) == 1, "Transfer job was not processed")
                        require("940000000000000001" not in adapter.roles and "940000000000000002" in adapter.roles and "940000000000000003" in adapter.roles, "Transfer did not preserve the other class")
                        await heartbeat(async_session, load_policy())
                        await engine.dispose()
                    asyncio.run(sync_transfer())
                    cli(["verify", "--batch-id", move["batch_id"], "--out", str(report_dir / "05-team-move-verified.md")], planner_env)
                    cli(["history", "--student-id", "synthetic-1", "--out", str(report_dir / "06-student-history.md")], planner_env)
                    verified = client.get(f"/api/admin/enrollment/imports/{move['batch_id']}/review", headers=admin).json()
                    require(verified["verification"]["outcome"] == "VERIFIED", "Transfer has no matching verification evidence")
                    from reporter.reporter import Reporter
                    reporter = Reporter({"backend_url": "http://127.0.0.1:18080", "team_id": "ClassA_01", "bearer_token": token,
                        "retry_buffer": str(Path(work) / "retry.jsonl"), "cursor_file": str(Path(work) / "cursor.json"), "phase_file": str(Path(work) / "phase.json")})
                    event = reporter.build_event({"input": {"command": "nmap -sV 10.50.1.100", "target": "10.50.1.100"}, "success": True, "output": "Synthetic test result"},
                        "port_scan", source_id="synthetic:enrollment:1")
                    require(reporter.post_event(event), "Actual Kali reporter HTTP event was not accepted")
                    env = os.environ.copy()
                    env.update(DATABASE_URL=dashboard, BACKEND_EXPORT_URL="http://127.0.0.1:18080", BACKEND_EXPORT_SECRET=os.environ["EVENT_EXPORT_SECRET"])
                    result = subprocess.run(["node", str(ROOT / "scripts/verify_dashboard_enrollment.mjs")], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
                    print(result.stdout.strip())
                    health = client.get("/api/health")
                    require(health.status_code == 200, "Backend health degraded during rehearsal")
                    require(health.json()["enrollment"]["status"] == "healthy", "Enrollment health degraded during rehearsal")
                    print(json.dumps({"postgresql": "migration_rerun_passed", "synthetic_students": 2, "enrollments": 3,
                        "registered_students": 1, "unassigned_students": 1, "preserved_unrelated_roles": True,
                        "real_discord_requests": 0, "real_emails_sent": 0, "backend_health": "healthy",
                        "planner_cli_human_approval_transfer": "passed", "human_reports_saved": True,
                        "shipped_excel_template_upload": "passed", "actual_reporter_http": "passed"}))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
