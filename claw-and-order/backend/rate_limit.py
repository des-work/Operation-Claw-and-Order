"""Shared rate limiter instance for all routers."""

from slowapi import Limiter
from slowapi.util import get_remote_address


def team_or_ip(request):
    """Give every authenticated reporter its own allowance, even behind one NAT."""
    team_id = getattr(request.state, "authenticated_team_id", None)
    return f"team:{team_id}" if team_id else f"ip:{get_remote_address(request)}"

limiter = Limiter(key_func=get_remote_address, default_limits=["120/minute"])
