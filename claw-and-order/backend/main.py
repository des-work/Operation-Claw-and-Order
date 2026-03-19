"""Claw & Order — FastAPI entry point."""

import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

from backend.config import LOG_LEVEL
from backend.database import engine
from backend.models import Base
from backend.routers import events, stream, admin
from backend.services import discord_worker

# ── Logging setup ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(name)-16s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("main")


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

    # Start Discord bot as background task (never bot.run())
    discord_task = await discord_worker.start()

    log.info("Claw & Order backend started")
    yield

    # Shutdown: cleanly close Discord
    log.info("Shutting down...")
    await discord_worker.stop(discord_task)


app = FastAPI(title="Claw & Order", lifespan=lifespan)
app.include_router(events.router)
app.include_router(stream.router)
app.include_router(admin.router)
