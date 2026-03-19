"""Centralized configuration — all env vars read here."""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Database ───────────────────────────────────────────────────────────────
DATABASE_URL: str = os.getenv(
    "DATABASE_URL",
    "sqlite+aiosqlite:///./claw_and_order.db",
)

# ── Auth ───────────────────────────────────────────────────────────────────
ADMIN_SECRET: str = os.getenv("ADMIN_SECRET", "")

# ── Discord ────────────────────────────────────────────────────────────────
DISCORD_TOKEN: str = os.getenv("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID: str = os.getenv("DISCORD_CHANNEL_ID", "0")

# ── Logging ────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Tuning ─────────────────────────────────────────────────────────────────
SSE_MAX_QUEUE_SIZE: int = int(os.getenv("SSE_MAX_QUEUE_SIZE", "256"))
RAW_OUTPUT_MAX_LENGTH: int = int(os.getenv("RAW_OUTPUT_MAX_LENGTH", "2000"))
