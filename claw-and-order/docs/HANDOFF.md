# Claw & Order — Backend Handoff Document

**Date:** March 19, 2026
**Author:** Backend Pipeline Team
**Status:** Feature-complete, hardened, reporter v2 shipped, 38/38 backend tests + 16/16 normalizer tests passing

---

## 1. What This System Does

Claw & Order is an automated scoring backend for a university red-team/blue-team cybersecurity competition. It ingests real-time telemetry from Kali Linux VMs running penetration tests, enforces competition rules (phase gating, CIDR ranges), detects milestones (DMZ compromise, LAN pivot, domain takeover), calculates time-weighted scores, and pushes live updates to a web dashboard (via SSE) and a Discord channel.

**Our team built:** The entire backend communication pipeline — FastAPI server, API endpoints, authentication, phase enforcement, milestone detection, scoring engine, SSE streaming, Discord integration, reporter v2 deployment package (class-based with exponential backoff, smart retry buffer, graceful shutdown), operational scripts, and the full test suite.

**Another team handles:** The web dashboard frontend and Kali VM configuration.

---

## 2. Architecture Overview

```
Kali VM (reporter.py)
    |
    |  POST /api/events (Bearer token auth)
    v
+----------------------------------------------+
|  FastAPI Backend (main.py)                    |
|                                              |
|  +- Auth ---------------------------------+ |
|  |  Bearer token + team_id hint (O(1))     | |
|  +-----------------------------------------+ |
|  +- Validation ----------------------------+ |
|  |  CIDR check -> Phase gating -> Pydantic  | |
|  +-----------------------------------------+ |
|  +- DB Transaction ------------------------+ |
|  |  Insert Event -> Milestone Detection ->  | |
|  |  Time-weighted Scoring -> Commit         | |
|  +-----------------------------------------+ |
|  +- Post-commit (fire-and-forget) ---------+ |
|  |  SSE Broadcast -> Discord Enqueue        | |
|  +-----------------------------------------+ |
|                                              |
|  Background Tasks:                           |
|  - Discord bot (asyncio.create_task)         |
|  - VM offline watchdog (60s interval)        |
+----------------------------------------------+
    |                |
    v                v
Dashboard (SSE)   Discord Channel
```

---

## 3. Project Structure

```
claw-and-order/
|-- backend/                    # FastAPI application
|   |-- __init__.py
|   |-- main.py                 # App entry point, lifespan, middleware
|   |-- config.py               # Centralized env var config
|   |-- database.py             # SQLAlchemy engine, session, WAL mode
|   |-- models.py               # DB models (Team, Event, Milestone, PhaseLog)
|   |-- rate_limit.py           # Shared slowapi limiter instance
|   |-- routers/                # API endpoint handlers
|   |   |-- admin.py            # Health, phase, teams, reports, admin
|   |   |-- events.py           # POST /api/events (main ingest)
|   |   +-- stream.py           # GET /api/stream (SSE)
|   +-- services/               # Business logic
|       |-- auth.py             # Bearer token + bcrypt validation
|       |-- phase.py            # Phase config loader, technique gating
|       |-- sse.py              # SSE fan-out with bounded queues
|       |-- discord_worker.py   # Discord bot + message dispatch
|       |-- milestone_detector.py  # Milestone rules + scoring (DO NOT MODIFY)
|       +-- watchdog.py         # VM offline alerting
|
|-- config/                     # Competition configuration
|   |-- phases.json             # 3-phase rules with allowed techniques
|   +-- sample_teams.json       # Example team definitions for dev/test
|
|-- reporter/                   # Kali VM deployment package (v2)
|   |-- reporter.py             # Reporter class: backoff, retry buffer, graceful shutdown
|   |-- technique_map.json      # Command-to-technique mapping rules (98 rules)
|   +-- technique_normalizer.py # Single source of truth normalizer (16 self-tests)
|
|-- scripts/                    # Operational scripts
|   |-- provision_tokens.py     # Seed teams + generate bearer tokens
|   |-- reset_competition.py    # Reset data (full, phase, scoring)
|   +-- output/                 # Generated per-team config.json files
|
|-- tests/                      # Pytest async test suite (38 tests)
|   |-- conftest.py             # Shared fixtures, in-memory DB
|   |-- test_auth.py
|   |-- test_cidr.py
|   |-- test_d_discord.py
|   |-- test_hardening.py
|   |-- test_models.py
|   |-- test_phase_and_milestone.py
|   +-- test_sse.py
|
|-- docs/                       # Documentation
|   +-- HANDOFF.md              # This file
|
|-- .env.example                # Template for environment variables
|-- .gitignore
|-- requirements.txt            # Production dependencies
|-- requirements-dev.txt        # Test/dev dependencies (httpx, pytest-asyncio)
+-- requirements-reporter.txt   # Reporter dependencies (requests only)
```

---

## 4. Database Schema

**Team** — One row per competing team
- `id` (PK, text) — e.g. `"team-alpha"`
- `display_name`, `wan_ip`, `dmz_cidr`, `lan_cidr`
- `bearer_token_hash` — bcrypt hash for API auth
- `last_seen` — updated on each heartbeat (used by watchdog)

**Event** — Every telemetry event from every reporter
- `id` (PK, auto), `team_id` (FK), `phase`, `timestamp`
- `command`, `technique`, `target_ip`, `result`
- `milestone` — set if this event triggered a milestone
- `raw_output` — truncated to 2000 chars

**Milestone** — One per team per milestone type (idempotent)
- `team_id` + `key` has a UniqueConstraint
- `triggering_event_id` (FK), `score`, `recorded_at`
- Keys: `dmz_compromise`, `lan_pivot`, `wan_to_lan_direct`, `domain_compromise`

**PhaseLog** — Audit trail of phase transitions
- `phase`, `activated_at`, `activated_by`
- First row auto-seeded on startup for scoring baseline

---

## 5. API Reference

### Public Endpoints (no auth)
| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | Operational health check (DB, phase, SSE, Discord) |
| GET | `/api/phase` | Current phase number + allowed techniques |
| GET | `/api/teams` | All teams with scores and milestones |
| GET | `/api/report/final` | Ranked JSON scoreboard |
| GET | `/api/report/final/csv` | Downloadable CSV scoreboard |
| GET | `/api/stream` | SSE event stream for dashboard |

### Authenticated Endpoints
| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/events` | Bearer token | Ingest telemetry from Kali reporters |
| POST | `/api/admin/phase` | X-Admin-Secret header | Transition competition phase |

### Rate Limits
- Global: 120 requests/minute per IP
- `/api/events`: 60 requests/minute per IP

---

## 6. Environment Variables

See `.env.example` for a ready-to-copy template.

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `DATABASE_URL` | No | `sqlite+aiosqlite:///./claw_and_order.db` | DB connection string |
| `ADMIN_SECRET` | **Yes** | `""` (rejects all) | Shared secret for admin endpoints |
| `DISCORD_TOKEN` | No | `""` (disabled) | Discord bot token |
| `DISCORD_CHANNEL_ID` | No | `"0"` | Channel for competition alerts |
| `LOG_LEVEL` | No | `"INFO"` | Python logging level |
| `SSE_MAX_QUEUE_SIZE` | No | `"256"` | Max queued SSE messages per client |
| `RAW_OUTPUT_MAX_LENGTH` | No | `"2000"` | Max chars for raw_output field |

---

## 7. How to Run

### Development (SQLite)
```bash
# Create venv with Python 3.14 (3.15 alpha can't compile pydantic-core)
py -3.14 -m venv .venv314
.venv314\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Copy and fill in .env
copy .env.example .env
# Edit .env — set ADMIN_SECRET at minimum

# Run server
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
```

### Production (PostgreSQL)
```bash
# Set env vars (or use .env file)
set DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/claw_and_order
set ADMIN_SECRET=<strong-random-secret>
set DISCORD_TOKEN=<bot-token>
set DISCORD_CHANNEL_ID=<channel-id>

uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
```

> **Important:** Use `--workers 1` because the Discord bot and watchdog are in-process background tasks. Multiple workers would spawn duplicate bots.

### Provision Teams
```bash
python scripts/provision_tokens.py config/sample_teams.json
```
Seeds teams into the DB and generates per-team `config.json` files in `scripts/output/` with bearer tokens for each Kali VM.

### Deploy Reporter to Kali VMs
Copy to each Kali VM:
- `reporter/reporter.py`
- `reporter/technique_normalizer.py`
- `reporter/technique_map.json`
- The team-specific `config.json` from `scripts/output/`

```bash
pip install requests
python reporter.py              # reads config.json from same directory
python reporter.py /path/to/config.json  # custom config path
```

### Run Tests
```bash
python -m pytest tests/ -v
```

---

## 8. Competition Day Operations

### Phase Transitions
```bash
curl -X POST http://localhost:8000/api/admin/phase \
  -H "Content-Type: application/json" \
  -H "X-Admin-Secret: $ADMIN_SECRET" \
  -d '{"phase": 2, "activated_by": "professor_smith"}'
```

### Check Health
```bash
curl http://localhost:8000/api/health
```
Returns: DB status, current phase, SSE client count, Discord config status.

### Monitor Scores
- JSON: `GET /api/report/final`
- CSV download: `GET /api/report/final/csv`
- Live dashboard: Connect to `GET /api/stream` for real-time SSE events

### Reset Competition
```bash
python scripts/reset_competition.py full     # wipe everything
python scripts/reset_competition.py phase 1  # reset to phase 1
python scripts/reset_competition.py scoring  # clear milestones only
```

---

## 9. Hardening Applied

These resilience measures are already built in:

| Issue | Fix |
|-------|-----|
| SQLite locks under concurrent reporters | WAL mode + 5s busy_timeout on every connection |
| Dashboard CORS blocked | CORSMiddleware with wildcard origins |
| Rate limiting | slowapi: 120/min global, 60/min on event ingest |
| Raw tracebacks leaked to clients | Global exception handler returns generic 500 |
| No scoring baseline without PhaseLog | Auto-seeds PhaseLog row on first startup |
| VM goes silent, nobody notices | Watchdog checks `last_seen` every 60s, alerts Discord after 5min |
| Reporter crashes on unknown technique | Added `is_unknown()` method to TechniqueNormalizer |
| Discord bot spawns duplicate dispatch loops | `_dispatch_started` bool guard on `on_ready` |
| SSE dead clients accumulate | Bounded queues (256), `QueueFull` eviction |
| Post-commit notification failure kills request | All notifications are fire-and-forget with try/except |
| DB transaction failure leaks partial state | Explicit rollback in except block |
| Corrupted team CIDR config | Caught with error logging, returns 500 not crash |
| Corrupted bcrypt hash in DB | Caught ValueError/TypeError per team |
| ADMIN_SECRET not set | Returns 503 with clear error, not silent pass |

---

## 10. Reporter v2 Architecture

The reporter runs on each Kali VM and bridges OpenClaw tool output to the scoring backend. It was rewritten from module-level globals into a fully encapsulated `Reporter` class for testability and reliability.

### Classes

| Class | Purpose |
|-------|---------|
| `Reporter` | Main class — all state encapsulated, config passed as dict, no module-level globals |
| `BackoffTracker` | Exponential backoff on POST failures (1s → 2s → 4s → ... capped at 60s). Resets on success |
| `RetryBuffer` | Append-only JSONL buffer for failed events. In-memory line count (no full re-read on append). Drain only triggers on reconnect, not every POST |
| `TechniqueNormalizer` | Single source of truth for command-to-technique mapping (imported from `technique_normalizer.py`). Longest-match-first sorting, 98 rules, 16 self-tests |

### Key Design Decisions

1. **Heartbeats bypass backoff** — The heartbeat POST always attempts regardless of backoff state. This is the fastest recovery path: if the backend comes back, the next heartbeat detects it and triggers a buffer drain immediately.
2. **Buffer drain only on reconnect** — `drain()` is only called when `record_success()` returns `True` (meaning we transitioned from failing to succeeding). This avoids the previous bug where every successful POST would re-read and replay the entire buffer file.
3. **Single TechniqueNormalizer** — Previously duplicated inline in reporter.py. Now imported from `technique_normalizer.py` so there's one class, one place to maintain, one set of tests.
4. **Graceful shutdown** — SIGINT/SIGTERM handlers set `_running = False`, allowing the main loop to exit cleanly and print summary stats (events sent vs buffered).
5. **Log throttling** — POST failure warnings log the first 3 failures, then every 10th, to avoid flooding stdout during extended outages.

### Reporter Config Format

Each Kali VM gets a `config.json` (generated by `provision_tokens.py`):

```json
{
  "backend_url": "http://10.50.0.1:8000",
  "bearer_token": "<team-specific-token>",
  "team_id": "team-alpha",
  "telemetry_log": "/root/.openclaw/logs/telemetry.jsonl",
  "retry_buffer": "/tmp/claw_retry_buffer.jsonl",
  "phase_file": "/opt/claw/current_phase.json",
  "technique_map": "/opt/claw/technique_map.json",
  "heartbeat_interval_seconds": 60,
  "phase_poll_interval_seconds": 300,
  "retry_buffer_max_events": 500,
  "post_timeout_seconds": 10
}
```

### Reporter Lifecycle

1. Load config → initialize `Reporter` (normalizer, backoff, buffer, HTTP session)
2. Install SIGINT/SIGTERM handlers
3. Poll `/api/phase` → write `current_phase.json` for OpenClaw playbook
4. Drain any buffered events from a previous crash
5. Tail `telemetry.jsonl` (seek to end — only new events)
6. Main loop: read lines → filter `tool.end` → normalize technique → `send_event()`
7. Every 60s: send heartbeat (bypasses backoff, triggers drain on reconnect)
8. Every 300s: poll phase endpoint
9. On shutdown: print summary, note if buffer has pending events

---

## 11. Milestone Scoring Rules

| Milestone | Base Score | Trigger |
|-----------|-----------|---------|
| `wan_to_lan_direct` | 100 pts | LAN compromise without prior DMZ compromise |
| `dmz_compromise` | 70 pts | First successful compromise-class technique in DMZ |
| `lan_pivot` | 50 pts | LAN compromise after DMZ compromise |
| `domain_compromise` | 40 pts | AD escalation success in LAN (requires prior LAN access) |

Scores decay linearly from 100% at phase start to 30% at phase end (48h default duration). Each milestone can only trigger once per team (enforced at Python + DB UniqueConstraint levels).

---

## 12. Known Limitations / Future Work

1. **Single-worker deployment** — Discord bot and watchdog are in-process. Can't scale horizontally without extracting them to separate services.
2. **CORS is wide-open** — `allow_origins=["*"]` should be tightened to the dashboard's actual origin before production.
3. **No stress test suite** — Should simulate 4+ concurrent reporters with rapid-fire events to validate SQLite WAL under real load.
4. **Phase duration hardcoded** — `calculate_score()` defaults to 48 hours. If phases run longer/shorter, pass the actual duration.
5. **No automated backup** — SQLite DB file should be backed up periodically during competition.
6. **Reporter technique_map.json** — Must be populated with your actual OpenClaw tool patterns before deployment.

---

## 13. Test Coverage

54 total tests (38 backend + 16 normalizer):

### Backend Tests (38 tests, 7 files)

| File | Tests | Covers |
|------|-------|--------|
| `test_auth.py` | 3 | Bearer token validation (valid, invalid, missing) |
| `test_cidr.py` | 4 | CIDR range enforcement (DMZ, LAN, outside, heartbeat bypass) |
| `test_models.py` | 4 | All 4 DB models CRUD |
| `test_phase_and_milestone.py` | 6 | Phase gating, milestone triggers, recon non-trigger |
| `test_sse.py` | 4 | SSE fan-out, disconnect cleanup, event broadcast |
| `test_d_discord.py` | 6 | Message formatting, queue ordering |
| `test_hardening.py` | 11 | Admin auth, input validation, health, duplicate milestones |

### Reporter Normalizer Self-Tests (16 tests)

Run with `cd reporter && python technique_normalizer.py`

Covers: port_scan, service_fingerprint, sqli, ssh_brute, default_creds, hash_dump, ad_escalation, data_exfil, smb_enum, case-insensitive matching, longest-match-first priority, empty command fallback, `is_unknown()` method.

---

## 14. Critical Rules (Do Not Break)

1. **Never call `discord.Client.run()`** — always `asyncio.create_task(bot.start(token))`
2. **Never share an AsyncSession across requests** — always use `Depends(get_db)`
3. **Never modify `milestone_detector.py` logic** — tested and locked by the course staff
4. **Reporter v2 is finalized** — `Reporter`, `BackoffTracker`, `RetryBuffer` classes are tested and locked. Only config changes (intervals, buffer size) should be tweaked
5. **Never pin Pydantic version** — let it float
6. **Use Python 3.14.x** — 3.15 alpha cannot compile pydantic-core
