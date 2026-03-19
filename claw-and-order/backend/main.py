"""Claw & Order — FastAPI entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

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

from backend.rate_limit import limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables on startup (dev/test with SQLite)
    log.info("Creating database tables...")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
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
    allow_origins=["*"],          # tighten to dashboard origin in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
