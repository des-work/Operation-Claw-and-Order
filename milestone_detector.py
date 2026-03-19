"""
milestone_detector.py  — runs server-side inside the FastAPI backend.

Called synchronously inside the POST /api/events handler, AFTER the event
is written to the database but BEFORE the SSE broadcast.

Returns a milestone key if triggered, otherwise None.
All milestones are idempotent: calling detect() twice for the same event
is safe — the EXISTS check prevents duplicate milestone rows.
"""

from __future__ import annotations
import ipaddress
from typing import Optional
from sqlalchemy import select, exists
from sqlalchemy.ext.asyncio import AsyncSession

# ── Milestone keys (also used as Discord trigger names) ───────────────────────
M_DMZ_COMPROMISE    = "dmz_compromise"
M_LAN_PIVOT         = "lan_pivot"
M_WAN_TO_LAN_DIRECT = "wan_to_lan_direct"
M_DOMAIN_COMPROMISE = "domain_compromise"

# Techniques that constitute a meaningful "compromise" vs just a scan
COMPROMISE_TECHNIQUES = {
    "http_exploit", "sqli", "default_creds", "cve_exploit_public",
    "credential_reuse", "lateral_movement", "hash_dump",
    "password_spray", "ad_escalation", "data_exfil"
}

DOMAIN_TECHNIQUES = {"ad_escalation"}


async def detect_milestone(
    session: AsyncSession,
    event_id: int,
    team_id: str,
    technique: str,
    result: str,
    target_ip: str,
    phase: int,
    team_dmz_cidr: str,
    team_lan_cidr: str,
    # import these from your models
    Event,
    Milestone,
) -> Optional[str]:
    """
    Evaluate a freshly-written event against all milestone rules.
    Returns the milestone key if one fires, None otherwise.
    Writes the milestone row inside the same session (caller commits).
    """

    # Only successful compromise-class techniques can trigger milestones
    if result != "success":
        return None
    if technique not in COMPROMISE_TECHNIQUES and technique not in DOMAIN_TECHNIQUES:
        return None

    try:
        target = ipaddress.ip_address(target_ip)
        in_dmz = target in ipaddress.ip_network(team_dmz_cidr, strict=False)
        in_lan = target in ipaddress.ip_network(team_lan_cidr, strict=False)
    except ValueError:
        return None  # malformed IP — already rejected by CIDR validator, but be safe

    milestone_key = None

    # ── Rule 1: DMZ compromise ────────────────────────────────────────────────
    if in_dmz:
        already = await session.scalar(
            select(exists().where(
                Milestone.team_id == team_id,
                Milestone.key == M_DMZ_COMPROMISE
            ))
        )
        if not already:
            milestone_key = M_DMZ_COMPROMISE

    # ── Rule 2: WAN→LAN direct (no prior DMZ compromise for this team) ────────
    elif in_lan and phase >= 1:
        has_dmz = await session.scalar(
            select(exists().where(
                Milestone.team_id == team_id,
                Milestone.key == M_DMZ_COMPROMISE
            ))
        )
        already_wl = await session.scalar(
            select(exists().where(
                Milestone.team_id == team_id,
                Milestone.key == M_WAN_TO_LAN_DIRECT
            ))
        )
        if not has_dmz and not already_wl:
            milestone_key = M_WAN_TO_LAN_DIRECT

    # ── Rule 3: LAN pivot (DMZ compromise existed, now hitting LAN) ──────────
        elif has_dmz:
            already_lp = await session.scalar(
                select(exists().where(
                    Milestone.team_id == team_id,
                    Milestone.key == M_LAN_PIVOT
                ))
            )
            if not already_lp:
                milestone_key = M_LAN_PIVOT

    # ── Rule 4: Domain compromise (AD escalation success, in LAN) ────────────
    if in_lan and technique in DOMAIN_TECHNIQUES:
        # Requires lan_pivot or wan_to_lan_direct to exist first
        has_lan_access = await session.scalar(
            select(exists().where(
                Milestone.team_id == team_id,
                Milestone.key.in_([M_LAN_PIVOT, M_WAN_TO_LAN_DIRECT])
            ))
        )
        already_dc = await session.scalar(
            select(exists().where(
                Milestone.team_id == team_id,
                Milestone.key == M_DOMAIN_COMPROMISE
            ))
        )
        if has_lan_access and not already_dc:
            milestone_key = M_DOMAIN_COMPROMISE

    # ── Write the milestone row ────────────────────────────────────────────────
    if milestone_key:
        session.add(Milestone(
            team_id=team_id,
            key=milestone_key,
            triggering_event_id=event_id,
        ))
        # Caller commits — do not commit here

    return milestone_key


# ── Scoring weights for each milestone ───────────────────────────────────────
# Used by the scoring engine, not the detector itself.
MILESTONE_BASE_SCORES = {
    M_WAN_TO_LAN_DIRECT: 100,   # catastrophic misconfiguration
    M_DMZ_COMPROMISE:     70,   # perimeter failure
    M_LAN_PIVOT:          50,   # internal segmentation failure
    M_DOMAIN_COMPROMISE:  40,   # full domain compromise
}

def calculate_score(
    milestone_key: str,
    phase_start_ts: float,   # unix timestamp when this phase began
    event_ts: float,         # unix timestamp of the triggering event
    phase_duration_hours: float = 48.0,  # each phase is 2 days
) -> int:
    """
    Time-weighted score.
    Full base score at phase start, decays linearly to 30% by phase end.
    Capped at base score, floored at 30% of base score.
    """
    base = MILESTONE_BASE_SCORES.get(milestone_key, 0)
    if base == 0:
        return 0

    elapsed_hours = (event_ts - phase_start_ts) / 3600
    fraction = max(0.0, min(1.0, elapsed_hours / phase_duration_hours))
    multiplier = 1.0 - (fraction * 0.70)   # 1.0 at start → 0.30 at end
    return round(base * multiplier)
