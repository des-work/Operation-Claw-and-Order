"""Run before starting a production web process: python -m backend.migrate."""

import asyncio
from sqlalchemy import text

from backend.database import engine
from backend.main import _migrate_event_source_id, _backfill_event_counts
from backend.models import Base


async def migrate() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(lambda sync: _migrate_event_source_id(sync, create_index=False))
        await conn.run_sync(_backfill_event_counts)
        await conn.execute(text("INSERT INTO enrollment_state (id, revision) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"))
        if engine.url.get_backend_name() == "sqlite":
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_enrollment_audit_subject ON enrollment_audit (subject_id, id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_enrollment_audit_revision ON enrollment_audit (action, revision)"))
        await conn.execute(text(
            "INSERT INTO dashboard_outbox (event_id, created_at) "
            "SELECT id, timestamp FROM events WHERE 1=1 ON CONFLICT (event_id) DO NOTHING"
        ))

    # The historical events table may be large. Build the dedupe index without
    # holding a write-blocking table lock during the release migration.
    if engine.url.get_backend_name() == "postgresql":
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            valid = await conn.scalar(text(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('uq_events_team_source')"
            ))
            if valid is False:
                await conn.execute(text("DROP INDEX CONCURRENTLY uq_events_team_source"))
            await conn.execute(text(
                "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_events_team_source "
                "ON events (team_id, source_id)"
            ))
            await conn.execute(text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_dashboard_outbox_age "
                "ON dashboard_outbox (delivered_at, created_at)"
            ))
            await conn.execute(text(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_discord_outbox_age "
                "ON discord_outbox (sent_at, created_at)"
            ))
            await conn.execute(text("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_enrollment_audit_subject ON enrollment_audit (subject_id, id)"))
            await conn.execute(text("CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_enrollment_audit_revision ON enrollment_audit (action, revision)"))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(migrate())
