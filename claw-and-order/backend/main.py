"""Claw & Order — FastAPI entry point."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from sqlalchemy import inspect, text

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from backend.config import LOG_LEVEL
from backend.database import engine, async_session
from backend.models import Base, PhaseLog
from backend.routers import events, stream, admin
from backend.services import discord_worker
from backend.services.phase import get_current_phase
from backend.services.watchdog import run_watchdog

# ── Logging setup ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(name)-16s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


def _migrate_event_source_id(conn, create_index=True):
    """Add retry IDs to existing databases; create_all only handles fresh tables."""
    columns = {column["name"] for column in inspect(conn).get_columns("events")}
    if "source_id" not in columns:
        conn.execute(text("ALTER TABLE events ADD COLUMN source_id TEXT"))
    if create_index:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_events_team_source ON events (team_id, source_id)"))


def _backfill_event_counts(conn):
    if conn.execute(text("SELECT 1 FROM team_event_counts LIMIT 1")).first() is None:
        conn.execute(text("INSERT INTO team_event_counts (team_id, event_count) "
                          "SELECT team_id, COUNT(*) FROM events GROUP BY team_id "
                          "ON CONFLICT (team_id) DO NOTHING"))

from backend.rate_limit import limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    production = os.getenv("APP_ENV") == "production"
    if production and (not os.getenv("DATABASE_URL", "").startswith("postgresql+asyncpg://")
                       or not os.getenv("ADMIN_SECRET")):
        raise RuntimeError("Production requires a PostgreSQL asyncpg DATABASE_URL and ADMIN_SECRET")
    log.info("Checking database schema..." if production else "Creating development database tables...")
    try:
        if production:
            async with engine.connect() as conn:
                for table in ("teams", "events", "team_event_counts", "discord_outbox",
                              "dashboard_outbox", "phase_log"):
                    await conn.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
                valid = await conn.scalar(text(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('uq_events_team_source')"
                ))
                if valid is not True:
                    raise RuntimeError("Missing or invalid uq_events_team_source index; run python -m backend.migrate")
        else:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.run_sync(_migrate_event_source_id)
                await conn.run_sync(_backfill_event_counts)
        log.info("Database tables ready")
    except Exception as e:
        log.error("Database initialization failed: %s", e)
        raise

    # Seed an initial PhaseLog row if none exists (accurate scoring baseline)
    try:
        async with async_session() as session:
            from sqlalchemy import select, func
            count = await session.scalar(select(func.count()).select_from(PhaseLog))
            if count == 0:
                session.add(PhaseLog(
                    phase=get_current_phase(),
                    activated_by="system_startup",
                ))
                await session.commit()
                log.info("Seeded initial PhaseLog for phase %d", get_current_phase())
    except Exception as e:
        log.warning("Could not seed initial PhaseLog: %s", e)

    # Start Discord bot as background task (never bot.run())
    discord_task = await discord_worker.start()

    # Start VM offline watchdog
    watchdog_task = asyncio.create_task(run_watchdog())

    log.info("Claw & Order backend started")
    yield

    # Shutdown: cleanly close Discord and watchdog
    log.info("Shutting down...")
    watchdog_task.cancel()
    try:
        await watchdog_task
    except asyncio.CancelledError:
        pass
    await discord_worker.stop(discord_task)


app = FastAPI(title="Claw & Order", lifespan=lifespan)

# ── Rate limiting middleware ──────────────────────────────────────────────
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS middleware — dashboard may be on a different origin ──────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("CORS_ORIGINS", "").split(",") if origin.strip()],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type", "X-Admin-Secret"],
)

# ── Global exception handler — never leak raw tracebacks to clients ──────
@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )

app.include_router(events.router)
app.include_router(stream.router)
app.include_router(admin.router)
