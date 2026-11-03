"""Database engine and session factory."""

import logging
import os
import ssl
from sqlalchemy.engine import make_url
from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from backend.config import DATABASE_URL

log = logging.getLogger("database")

# ── Engine configuration ──────────────────────────────────────────────────
connect_args = {}
engine_kwargs = {}
_is_sqlite = DATABASE_URL.startswith("sqlite")

if _is_sqlite:
    connect_args = {"timeout": 30}
    log.info("Using SQLite: %s", DATABASE_URL)
else:
    # PostgreSQL pool tuning for competition load
    engine_kwargs = {
        "pool_size": int(os.getenv("DB_POOL_SIZE", "5")),
        "max_overflow": int(os.getenv("DB_MAX_OVERFLOW", "2")),
        "pool_pre_ping": True,       # detect stale connections
        "pool_recycle": 1800,        # recycle connections every 30 min
    }
    # Log the DB host without credentials
    safe_url = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    log.info("Using PostgreSQL: ...@%s", safe_url)

def verified_database_settings(database_url, production=False, ca_file=None):
    url = make_url(database_url)
    options = {}
    if production and url.get_backend_name() == "postgresql" and url.host not in ("localhost", "127.0.0.1", "::1"):
        url = url.difference_update_query(("ssl", "sslmode", "sslrootcert", "sslcert", "sslkey"))
        options["ssl"] = ssl.create_default_context(cafile=ca_file or None)
    return url, options


database_url, tls_options = verified_database_settings(DATABASE_URL, os.getenv("APP_ENV") == "production", os.getenv("PG_CA_CERT_FILE"))
engine = create_async_engine(database_url, connect_args={**connect_args, **tls_options}, **engine_kwargs)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# ── SQLite WAL mode — critical for concurrent reporter writes ─────────────
if _is_sqlite:
    @sa_event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_wal(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def get_db():
    """FastAPI dependency — yields one AsyncSession per request."""
    async with async_session() as session:
        yield session
