"""A2 smoke test: insert one row per table, read it back."""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import select

from backend.models import Base, Team, Event, Milestone, PhaseLog


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",  # in-memory
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.mark.asyncio
async def test_insert_and_read_team(session: AsyncSession):
    team = Team(
        id="team-alpha",
        display_name="Team Alpha",
        wan_ip="203.0.113.10",
        dmz_cidr="10.50.1.0/24",
        lan_cidr="10.50.2.0/24",
        bearer_token_hash="$2b$12$fakehashvalue",
    )
    session.add(team)
    await session.commit()

    row = await session.get(Team, "team-alpha")
    assert row is not None
    assert row.display_name == "Team Alpha"
    assert row.dmz_cidr == "10.50.1.0/24"
    assert row.last_seen is None
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_insert_and_read_event(session: AsyncSession):
    # Need a team first (FK)
    session.add(Team(
        id="team-beta",
        display_name="Team Beta",
        wan_ip="203.0.113.20",
        dmz_cidr="10.50.3.0/24",
        lan_cidr="10.50.4.0/24",
        bearer_token_hash="$2b$12$fakehash2",
    ))
    await session.flush()

    event = Event(
        team_id="team-beta",
        phase=1,
        command="nmap -sV 10.50.3.100",
        technique="service_fingerprint",
        target_ip="10.50.3.100",
        result="success",
        raw_output="PORT 22/tcp open ssh",
    )
    session.add(event)
    await session.commit()

    row = (await session.execute(select(Event).where(Event.team_id == "team-beta"))).scalar_one()
    assert row.technique == "service_fingerprint"
    assert row.result == "success"
    assert row.timestamp is not None
    assert row.milestone is None


@pytest.mark.asyncio
async def test_insert_and_read_milestone(session: AsyncSession):
    session.add(Team(
        id="team-gamma",
        display_name="Team Gamma",
        wan_ip="203.0.113.30",
        dmz_cidr="10.50.5.0/24",
        lan_cidr="10.50.6.0/24",
        bearer_token_hash="$2b$12$fakehash3",
    ))
    await session.flush()

    event = Event(
        team_id="team-gamma",
        phase=1,
        command="sqlmap -u http://10.50.5.100/login",
        technique="sqli",
        target_ip="10.50.5.100",
        result="success",
    )
    session.add(event)
    await session.flush()

    milestone = Milestone(
        team_id="team-gamma",
        key="dmz_compromise",
        triggering_event_id=event.id,
        score=70,
    )
    session.add(milestone)
    await session.commit()

    row = (await session.execute(select(Milestone).where(Milestone.team_id == "team-gamma"))).scalar_one()
    assert row.key == "dmz_compromise"
    assert row.score == 70
    assert row.recorded_at is not None


@pytest.mark.asyncio
async def test_insert_and_read_phase_log(session: AsyncSession):
    log = PhaseLog(phase=1, activated_by="admin")
    session.add(log)
    await session.commit()

    row = (await session.execute(select(PhaseLog).where(PhaseLog.phase == 1))).scalar_one()
    assert row.activated_by == "admin"
    assert row.activated_at is not None
