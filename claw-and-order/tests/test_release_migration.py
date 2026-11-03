"""Release migration must also work on a cleared or historical local database."""

import pytest
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from backend import migrate
from backend.models import Team, Event, DashboardOutbox, DiscordWebhookDelivery


@pytest.mark.asyncio
async def test_empty_migration_and_repeat_preserve_delivery_backfill(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'semester.db'}")
    monkeypatch.setattr(migrate, "engine", engine)
    await migrate.migrate()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(DiscordWebhookDelivery)) == 0
        session.add(Team(id="A_01", display_name="A 01", wan_ip="192.0.2.1",
                         dmz_cidr="10.1.1.0/24", lan_cidr="10.1.2.0/24", bearer_token_hash="test"))
        await session.flush()
        session.add(Event(team_id="A_01", phase=1, command="nmap", technique="port_scan",
                          target_ip="10.1.1.2", result="success"))
        await session.commit()
    await migrate.migrate()
    await migrate.migrate()
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Team)) == 1
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(func.count()).select_from(DashboardOutbox)) == 1
    await engine.dispose()
