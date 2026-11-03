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
            ENROLLMENT_DISCORD_BOT_TOKEN="")
        import httpx
        from sqlalchemy import select, func
        from backend.database import async_session, engine
        from backend.models import Team, Student
        from backend.migrate import migrate
        from backend.enrollment_policy import load_policy
        from backend.services.enrollment import claim_registration
        from backend.services.membership_sync import sync_once, heartbeat
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
                    preview = client.post("/api/admin/enrollment/imports", headers=admin, json={"rows": rows})
                    preview.raise_for_status()
                    batch = preview.json()
                    applied = client.post(f"/api/admin/enrollment/imports/{batch['batch_id']}/apply", headers=admin, json={"digest": batch["digest"]})
                    applied.raise_for_status()
                    again = client.post(f"/api/admin/enrollment/imports/{batch['batch_id']}/apply", headers=admin, json={"digest": batch["digest"]})
                    assert again.json()["already_applied"]
                    invitations = client.post("/api/admin/enrollment/invitations", headers=admin, json={"student_ids": ["synthetic-1", "synthetic-2"]})
                    invitations.raise_for_status()
                    code = invitations.json()["invitations"][0]["registration_code"]
                    class FakeDiscord:
                        roles = {"990000000000000001"}
                        async def validate(self, *_): pass
                        async def member_roles(self, _): return set(self.roles)
                        async def add_role(self, user, role, reason): self.roles.add(role)
                        async def remove_role(self, user, role, reason): self.roles.discard(role)
                    async def claim_and_sync():
                        selected = load_policy()
                        async with async_session() as session, session.begin():
                            await claim_registration(session, code, "920000000000000001", selected.guild_id, selected)
                        adapter = FakeDiscord()
                        assert await sync_once(async_session, selected, adapter) == 1
                        assert adapter.roles == {"990000000000000001", "930000000000000001", "930000000000000002", "940000000000000001", "940000000000000003"}
                        await heartbeat(async_session, selected)
                        await engine.dispose()
                    asyncio.run(claim_and_sync())
                    event = client.post("/api/events", headers={"Authorization": "Bearer " + token}, json={"team_id": "ClassA_01",
                        "command": "nmap -sV 10.50.1.100", "technique": "port_scan", "target_ip": "10.50.1.100", "result": "success", "source_id": "synthetic:enrollment:1"})
                    event.raise_for_status()
                    env = os.environ.copy()
                    env.update(DATABASE_URL=dashboard, BACKEND_EXPORT_URL="http://127.0.0.1:18080", BACKEND_EXPORT_SECRET=os.environ["EVENT_EXPORT_SECRET"])
                    result = subprocess.run(["node", str(ROOT / "scripts/verify_dashboard_enrollment.mjs")], env=env, cwd=ROOT, capture_output=True, text=True, check=True)
                    print(result.stdout.strip())
                    health = client.get("/api/health")
                    assert health.status_code == 200, health.text
                    assert health.json()["enrollment"]["status"] == "healthy"
                    print(json.dumps({"postgresql": "migration_rerun_passed", "synthetic_students": 2, "enrollments": 3,
                        "registered_students": 1, "unassigned_students": 1, "preserved_unrelated_roles": True,
                        "real_discord_requests": 0, "real_emails_sent": 0, "backend_health": "healthy"}))
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
