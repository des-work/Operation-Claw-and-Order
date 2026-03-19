"""Database engine and session factory."""

import logging
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from backend.config import DATABASE_URL

log = logging.getLogger("database")

# ── Engine configuration ──────────────────────────────────────────────────
connect_args = {}
engine_kwargs = {}

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"timeout": 30}
    log.info("Using SQLite: %s", DATABASE_URL)
else:
    # PostgreSQL pool tuning for competition load
    engine_kwargs = {
        "pool_size": 10,
        "max_overflow": 20,
        "pool_pre_ping": True,       # detect stale connections
        "pool_recycle": 1800,        # recycle connections every 30 min
    }
    # Log the DB host without credentials
    safe_url = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    log.info("Using PostgreSQL: ...@%s", safe_url)

engine = create_async_engine(DATABASE_URL, connect_args=connect_args, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    """FastAPI dependency — yields one AsyncSession per request."""
    async with async_session() as session:
        yield session
