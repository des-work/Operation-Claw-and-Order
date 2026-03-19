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
  - Writes hashed tokens to the database (teams table)
  - Prints each team's plaintext token to stdout for secure delivery
  - Generates a reporter config.json per team in scripts/output/
"""

import asyncio
import json
import os
import secrets
import sys
from pathlib import Path

import bcrypt

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from backend.models import Base, Team
from backend.config import DATABASE_URL


def generate_token() -> tuple[str, str]:
    """Return (plaintext_token, bcrypt_hash)."""
    token = secrets.token_urlsafe(32)
    hashed = bcrypt.hashpw(token.encode(), bcrypt.gensalt()).decode()
    return token, hashed


async def main(teams_file: str):
    connect_args = {}
    if DATABASE_URL.startswith("sqlite"):
        connect_args = {"timeout": 30}

    engine = create_async_engine(DATABASE_URL, connect_args=connect_args)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    teams = json.loads(Path(teams_file).read_text())
    output_dir = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)

    print(f"\n{'='*60}")
    print(f"  Provisioning {len(teams)} teams")
    print(f"{'='*60}\n")

    async with session_factory() as session:
        for team_def in teams:
            token, token_hash = generate_token()

            team = Team(
                id=team_def["id"],
                display_name=team_def["display_name"],
                wan_ip=team_def["wan_ip"],
                dmz_cidr=team_def["dmz_cidr"],
                lan_cidr=team_def["lan_cidr"],
                bearer_token_hash=token_hash,
            )
            session.add(team)

            # Generate reporter config for this team
            config = {
                "backend_url": os.getenv("BACKEND_URL", "http://10.50.0.1:8000"),
                "bearer_token": token,
                "team_id": team_def["id"],
                "telemetry_log": "/root/.openclaw/logs/telemetry.jsonl",
                "retry_buffer": "/tmp/claw_retry_buffer.jsonl",
                "phase_file": "/opt/claw/current_phase.json",
                "technique_map": "/opt/claw/technique_map.json",
                "heartbeat_interval_seconds": 60,
                "phase_poll_interval_seconds": 300,
                "retry_buffer_max_events": 500,
                "post_timeout_seconds": 10,
            }
            config_path = output_dir / f"config_{team_def['id']}.json"
            config_path.write_text(json.dumps(config, indent=2))

            print(f"  Team: {team_def['id']}")
            print(f"  Token: {token}")
            print(f"  Config: {config_path}")
            print()

        await session.commit()

    await engine.dispose()
    print(f"{'='*60}")
    print(f"  Done. Configs written to {output_dir}/")
    print(f"  Deliver each config.json securely to its Kali VM.")
    print(f"{'='*60}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python scripts/provision_tokens.py config/sample_teams.json")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
