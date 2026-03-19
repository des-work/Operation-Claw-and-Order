"""Bearer token validation via bcrypt."""

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
    """
    FastAPI dependency: validates Bearer token, returns the Team row.

    Optimisation: if the request body contains a team_id field, look up that
    single team and verify its hash (O(1) bcrypt call).  Falls back to a
    full scan only when the hint is missing.
    """
    raw_token = _extract_bearer(request)

    # ── Fast path: team_id hint from JSON body ─────────────────────────────
    try:
        body = await request.json()
        team_id_hint = body.get("team_id") if isinstance(body, dict) else None
    except Exception:
        team_id_hint = None

    if team_id_hint:
        team = (await db.execute(
            select(Team).where(Team.id == team_id_hint)
        )).scalar()
        if team is not None:
            try:
                if bcrypt.checkpw(raw_token.encode(), team.bearer_token_hash.encode()):
                    return team
            except (ValueError, TypeError) as e:
                log.error("bcrypt check failed for team %s: %s", team_id_hint, e)
        # If hint matched a team but token was wrong, fall through to reject
        log.warning("Auth failed for team_id=%s", team_id_hint)
        raise HTTPException(status_code=401, detail="Invalid token")

    # ── Slow path: no hint, scan all teams ─────────────────────────────────
    teams = (await db.execute(select(Team))).scalars().all()
    for team in teams:
        try:
            if bcrypt.checkpw(raw_token.encode(), team.bearer_token_hash.encode()):
                return team
        except (ValueError, TypeError) as e:
            log.error("bcrypt check failed for team %s: %s", team.id, e)
            continue

    log.warning("Auth failed — no matching token (scanned %d teams)", len(teams))
    raise HTTPException(status_code=401, detail="Invalid token")
