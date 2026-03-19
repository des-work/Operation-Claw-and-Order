"""
reset_competition.py — Reset competition data.

Three modes:
  full    — Clear ALL data: events, milestones, phase_log. Teams kept.
  phase   — Clear events/milestones from a specific phase onward.
  scoring — Zero out milestone scores but keep all event history for debrief.

Usage:
    python scripts/reset_competition.py full
    python scripts/reset_competition.py phase 2
    python scripts/reset_competition.py scoring
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from backend.models import Base, Event, Milestone, PhaseLog, Team
from backend.config import DATABASE_URL


async def reset_full(session: AsyncSession):
    """Delete all events, milestones, and phase_log. Keep teams."""
    await session.execute(delete(Milestone))
    await session.execute(delete(Event))
    await session.execute(delete(PhaseLog))
    # Reset last_seen on all teams
    await session.execute(update(Team).values(last_seen=None))
    await session.commit()
    print("Full reset complete. All events, milestones, and phase logs deleted.")
    print("Teams preserved. Run provision_tokens.py to re-provision if needed.")


async def reset_phase(session: AsyncSession, from_phase: int):
    """Delete events and milestones from the given phase onward."""
    # Delete milestones whose triggering event is in the affected phases
    from sqlalchemy import select
    affected_event_ids = select(Event.id).where(Event.phase >= from_phase)
    await session.execute(
        delete(Milestone).where(Milestone.triggering_event_id.in_(affected_event_ids))
    )
    await session.execute(delete(Event).where(Event.phase >= from_phase))
    await session.execute(delete(PhaseLog).where(PhaseLog.phase >= from_phase))
    await session.commit()
    print(f"Phase reset complete. Cleared data from phase {from_phase} onward.")


async def reset_scoring(session: AsyncSession):
    """Zero out milestone scores. Keep all events and milestones for debrief."""
    await session.execute(update(Milestone).values(score=0))
    await session.commit()
    print("Scoring reset complete. All milestone scores set to 0.")
    print("Event history and milestone records preserved for debrief.")


async def main(mode: str, phase: int | None = None):
    connect_args = {}
    if DATABASE_URL.startswith("sqlite"):
        connect_args = {"timeout": 30}

    engine = create_async_engine(DATABASE_URL, connect_args=connect_args)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as session:
        if mode == "full":
            await reset_full(session)
        elif mode == "phase":
            if phase is None:
                print("Error: phase mode requires a phase number")
                sys.exit(1)
            await reset_phase(session, phase)
        elif mode == "scoring":
            await reset_scoring(session)
        else:
            print(f"Unknown mode: {mode}")
            print("Usage: python scripts/reset_competition.py [full|phase N|scoring]")
            sys.exit(1)

    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/reset_competition.py [full|phase N|scoring]")
        sys.exit(1)

    mode = sys.argv[1]
    phase_num = int(sys.argv[2]) if len(sys.argv) > 2 else None
    asyncio.run(main(mode, phase_num))
