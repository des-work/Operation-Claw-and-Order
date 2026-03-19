"""Shared test fixtures — single in-memory DB for all tests."""

import os
import bcrypt
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.models import Base, Team
from backend.database import get_db
from backend.main import app
from backend.services.phase import set_current_phase, load_phases

VALID_TOKEN = "test-token-alpha"
VALID_HASH = bcrypt.hashpw(VALID_TOKEN.encode(), bcrypt.gensalt()).decode()

# Set a known admin secret for tests
TEST_ADMIN_SECRET = "test-admin-secret"
os.environ["ADMIN_SECRET"] = TEST_ADMIN_SECRET

_engine = create_async_engine("sqlite+aiosqlite://", connect_args={"timeout": 30})
_session_factory = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with _session_factory() as s:
        yield s


# Override once at module level
app.dependency_overrides[get_db] = _override_get_db


@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Create tables, seed a test team, reset phase state, and tear down after each test."""
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with _session_factory() as s:
        s.add(Team(
            id="team-alpha",
            display_name="Team Alpha",
            wan_ip="203.0.113.10",
            dmz_cidr="10.50.1.0/24",
            lan_cidr="10.50.2.0/24",
            bearer_token_hash=VALID_HASH,
        ))
        await s.commit()

    # Reset phase to 1 before each test
    set_current_phase(1)
    load_phases()

    yield

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
