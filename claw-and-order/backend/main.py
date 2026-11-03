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
from backend.routers import events, stream, admin, enrollment
from backend.enrollment_policy import EnrollmentError
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


async def restore_competition_phase(session):
    from sqlalchemy import select
    from backend.services.phase import get_phase_config, set_current_phase
    latest = (await session.execute(select(PhaseLog).order_by(PhaseLog.activated_at.desc(), PhaseLog.id.desc()).limit(1))).scalar_one_or_none()
    if latest is None:
        latest = PhaseLog(phase=1, activated_by="system_startup")
        session.add(latest)
        await session.commit()
    if get_phase_config(latest.phase) is None:
        raise RuntimeError("Stored competition phase has no valid configuration; repair before startup")
    set_current_phase(latest.phase)


@asynccontextmanager
async def lifespan(app: FastAPI):
    production = os.getenv("APP_ENV") == "production"
    if production and (not os.getenv("DATABASE_URL", "").startswith("postgresql+asyncpg://")
                       or not os.getenv("ADMIN_SECRET")):
        raise RuntimeError("Production requires a PostgreSQL asyncpg DATABASE_URL and ADMIN_SECRET")
    if production:
        credentials = [os.getenv(k) for k in ("ADMIN_SECRET", "EVENT_EXPORT_SECRET", "ENROLLMENT_PLANNER_SECRET") if os.getenv(k)]
        if len(credentials) != len(set(credentials)) or any(len(value) < 32 for value in credentials):
            raise RuntimeError("Production admin/export/planner credentials must be distinct random values of at least 32 characters")
    log.info("Checking database schema..." if production else "Creating development database tables...")
    try:
        if production:
            async with engine.connect() as conn:
                for table in ("teams", "events", "team_event_counts", "discord_outbox",
                              "dashboard_outbox", "discord_webhook_deliveries", "discord_webhook_destinations", "phase_log",
                              "students", "enrollments", "enrollment_state", "enrollment_imports", "registration_invitations",
                              "membership_sync", "managed_discord_grants", "enrollment_audit", "enrollment_workers"):
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
                await conn.execute(text("INSERT INTO enrollment_state (id, revision) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"))
        log.info("Database tables ready")
    except Exception as e:
        log.error("Database initialization failed: %s", e)
        raise

    async with async_session() as session:
        await restore_competition_phase(session)
    log.info("Restored competition phase %d", get_current_phase())

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


class EnrollmentBodyLimit:
    """Bound request bytes before FastAPI parses JSON or workbook uploads."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/api/admin/enrollment"):
            return await self.app(scope, receive, send)
        started = False
        async def private_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in (b"cache-control", b"pragma")]
                message = {**message, "headers": headers + [(b"cache-control", b"no-store"), (b"pragma", b"no-cache")]}
            await send(message)
        try:
            if scope.get("method") != "POST":
                return await self.app(scope, receive, private_send)
            async with asyncio.timeout(30):
                return await self._bounded(scope, receive, private_send)
        except TimeoutError:
            if not started:
                await JSONResponse({"detail": "Enrollment request timed out; inspect the change before retrying"}, status_code=408)(scope, receive, private_send)
        except Exception as error:
            # SQLAlchemy errors can embed roster values. Contain them here before
            # the outer ASGI server logs a traceback with bound SQL parameters.
            log.error("Private enrollment request failed: %s", type(error).__name__)
            if not started:
                await JSONResponse({"detail": "Private enrollment operation failed; inspect change status before retrying"}, status_code=500)(scope, receive, private_send)

    async def _bounded(self, scope, receive, send):
        from collections import deque
        from backend.roster_input import MAX_BYTES
        chunks, total = deque(), 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            total += len(message.get("body", b""))
            if total > MAX_BYTES:
                return await JSONResponse({"detail": "Enrollment request exceeds 10 MiB"}, status_code=413)(scope, receive, send)
            chunks.append(message)
            if not message.get("more_body", False):
                break
        async def replay():
            return chunks.popleft() if chunks else await receive()
        await self.app(scope, replay, send)


app.add_middleware(EnrollmentBodyLimit)

from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler


@app.exception_handler(RequestValidationError)
async def _private_validation_error(request, exc):
    if request.url.path.startswith("/api/admin/enrollment"):
        return JSONResponse(status_code=422, content={"detail": "Enrollment request validation failed; check required fields and formats",
                            "fields": [list(error["loc"]) for error in exc.errors()]}, headers={"Cache-Control": "no-store"})
    return await request_validation_exception_handler(request, exc)

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
    if request.url.path.startswith("/api/admin/enrollment"):
        log.error("Private enrollment operation failed: %s", type(exc).__name__)
    else:
        log.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


@app.exception_handler(EnrollmentError)
async def _enrollment_error(request: Request, exc: EnrollmentError):
    return JSONResponse(status_code=exc.status, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"})

app.include_router(events.router)
app.include_router(stream.router)
app.include_router(admin.router)
app.include_router(enrollment.router)
app.include_router(enrollment.admin_router)
app.include_router(enrollment.export_router)
