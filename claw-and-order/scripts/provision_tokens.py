"""
provision_tokens.py — Generate Bearer tokens for each team and write to DB.

Usage:
    python scripts/provision_tokens.py config/sample_teams.json

teams.json format:
[
  {
    "id": "team-alpha",
    "display_name": "Team Alpha",
    "wan_ip": "203.0.113.10",
    "dmz_cidr": "10.50.1.0/24",
    "lan_cidr": "10.50.2.0/24"
  },
  ...
]

Outputs:
  - Adds new teams with hashed tokens; preserves existing team tokens
  - Updates existing teams' display names and network metadata
  - Prints new plaintext tokens for secure delivery
  - Generates mode-0600 reporter configs for new teams in scripts/output/
"""

import asyncio
import json
import os
import re
import secrets
import sys
from pathlib import Path

import hashlib

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from backend.models import Base, Team
from backend.config import DATABASE_URL


def generate_token() -> tuple[str, str]:
    """Return a random 256-bit token and its fast, constant-time verifier."""
    token = secrets.token_urlsafe(32)
    hashed = f"sha256:{hashlib.sha256(token.encode()).hexdigest()}"
    return token, hashed


async def main(teams_file: str, db_url: str | None = None, destination: Path | None = None):
    db_url = db_url or DATABASE_URL
    connect_args = {}
    if db_url.startswith("sqlite"):
        connect_args = {"timeout": 30}

    engine = create_async_engine(db_url, connect_args=connect_args)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    teams = json.loads(Path(teams_file).read_text())
    if not isinstance(teams, list) or not teams:
        raise ValueError("Team definitions must be a nonempty JSON array")
    ids = [team["id"] for team in teams]
    if len(ids) != len(set(ids)) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value) for value in ids):
        raise ValueError("Team IDs must be unique and contain only letters, numbers, _ or -")
    output_dir = destination or Path(__file__).parent / "output"
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)

    print(f"\n{'='*60}")
    print(f"  Provisioning {len(teams)} teams")
    print(f"{'='*60}\n")

    new_configs = []
    async with session_factory() as session:
        for team_def in teams:
            team = await session.get(Team, team_def["id"])
            if team is not None:
                team.display_name = team_def["display_name"]
                team.wan_ip = team_def["wan_ip"]
                team.dmz_cidr = team_def["dmz_cidr"]
                team.lan_cidr = team_def["lan_cidr"]
                print(f"  Team: {team.id} — existing token preserved, metadata updated")
                continue

            token, token_hash = generate_token()
            session.add(Team(
                id=team_def["id"], display_name=team_def["display_name"],
                wan_ip=team_def["wan_ip"], dmz_cidr=team_def["dmz_cidr"],
                lan_cidr=team_def["lan_cidr"], bearer_token_hash=token_hash,
            ))

            # Generate reporter config for this team
            config = {
                "backend_url": os.getenv("BACKEND_URL", "http://10.50.0.1:8000"),
                "bearer_token": token,
                "team_id": team_def["id"],
                "telemetry_log": "/root/.openclaw/logs/telemetry.jsonl",
                "retry_buffer": f"/var/lib/claw/retry_{team_def['id']}.jsonl",
                "cursor_file": f"/var/lib/claw/cursor_{team_def['id']}.json",
                "phase_file": "/opt/claw/current_phase.json",
                "technique_map": "/opt/claw/technique_map.json",
                "heartbeat_interval_seconds": 60,
                "phase_poll_interval_seconds": 300,
                "retry_buffer_max_events": 500,
                "post_timeout_seconds": 10,
            }
            config_path = output_dir / f"config_{team_def['id']}.json"
            new_configs.append((config_path, config, token))

        await session.commit()

    for config_path, config, token in new_configs:
        descriptor = os.open(config_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(config, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        print(f"  Team: {config['team_id']}")
        print(f"  Token: {token}")
        print(f"  Config: {config_path}")
        print()

    await engine.dispose()
    print(f"{'='*60}")
    print(f"  Done. Configs written to {output_dir}/")
    print(f"  Deliver each config.json securely to its Kali VM.")
    print(f"{'='*60}")
    return len(new_configs)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/provision_tokens.py config/sample_teams.json")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
