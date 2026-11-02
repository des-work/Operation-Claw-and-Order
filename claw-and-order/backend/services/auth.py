"""Bearer token validation for high entropy team secrets and legacy bcrypt hashes."""

import asyncio
import hashlib
import hmac
import logging

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models import Team

log = logging.getLogger("auth")


def _extract_bearer(request: Request) -> str:
    """Pull the raw token from the Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed Authorization header")
    return auth[7:]


async def validate_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Team:
    """Validate a team-scoped token without scanning or blocking the event loop."""
    raw_token = _extract_bearer(request)

    try:
        body = await request.json()
        team_id_hint = body.get("team_id") if isinstance(body, dict) else None
    except Exception:
        team_id_hint = None

    if not isinstance(team_id_hint, str) or not team_id_hint:
        raise HTTPException(status_code=422, detail="team_id is required")
    team = (await db.execute(select(Team).where(Team.id == team_id_hint))).scalar()
    if team is None:
        raise HTTPException(status_code=401, detail="Invalid token")

    digest = hashlib.sha256(raw_token.encode()).hexdigest()
    stored = team.bearer_token_hash
    if stored.startswith("sha256:"):
        valid = hmac.compare_digest(stored[7:], digest)
    else:
        try:
            valid = await asyncio.to_thread(bcrypt.checkpw, raw_token.encode(), stored.encode())
        except (ValueError, TypeError) as error:
            log.error("Legacy token check failed for team %s: %s", team_id_hint, error)
            valid = False
        if valid:
            # Existing deployments keep their token. The first valid use upgrades
            # its stored verifier so later events avoid a costly bcrypt check.
            team.bearer_token_hash = f"sha256:{digest}"
            await db.commit()
            log.info("Upgraded token verifier for team %s", team.id)

    if not valid:
        log.warning("Auth failed for team_id=%s", team_id_hint)
        raise HTTPException(status_code=401, detail="Invalid token")
    request.state.authenticated_team_id = team.id
    return team
