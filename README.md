# Operation Claw & Order

> Automated scoring backend for the Spring 2026 university red-team/blue-team cybersecurity competition.

Teams deploy Kali Linux VMs to penetrate a target network across three timed phases. Claw & Order captures every action in real time, enforces competition rules, detects key milestones, calculates time-weighted scores, and pushes live updates to a dashboard and Discord.

---

## Team Roles

| Member | Responsibility |
|--------|---------------|
| **Backend Team** | FastAPI server, API endpoints, auth, phase enforcement, milestone detection, scoring, SSE streaming, Discord integration, reporter package, scripts, tests |
| **Dashboard Team** | Web frontend consuming the SSE stream and REST APIs |
| **VM Team** | Kali VM images, OpenClaw tooling, network topology |

---

## Quick Start

```bash
cd claw-and-order

# Python 3.14 required (3.15 alpha cannot compile pydantic-core)
py -3.14 -m venv .venv314
.venv314\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Configure environment
copy .env.example .env
# Edit .env — set ADMIN_SECRET at minimum

# Start the backend
uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000

# Run tests
python -m pytest tests/ -v
```

---

## Project Structure

```
Operation-Open-Claw/
|
|-- Claw_and_Order_v2_Definitive_Architecture.docx   # Architecture spec
|-- Claw_and_Order_PreBuild_Gap_Analysis.docx        # Gap analysis
|
+-- claw-and-order/                                  # Main project
    |-- backend/                 # FastAPI application
    |   |-- main.py              # Entry point, lifespan, middleware
    |   |-- config.py            # Centralized env var config
    |   |-- database.py          # SQLAlchemy engine + WAL mode
    |   |-- models.py            # DB models (Team, Event, Milestone, PhaseLog)
    |   |-- rate_limit.py        # Shared rate limiter
    |   |-- routers/             # API endpoint handlers
    |   |   |-- events.py        # POST /api/events (ingest)
    |   |   |-- admin.py         # Health, phase, teams, reports
    |   |   +-- stream.py        # GET /api/stream (SSE)
    |   +-- services/            # Business logic
    |       |-- auth.py          # Bearer token + bcrypt
    |       |-- phase.py         # Phase config + technique gating
    |       |-- sse.py           # SSE fan-out
    |       |-- discord_worker.py
    |       |-- milestone_detector.py  (DO NOT MODIFY)
    |       +-- watchdog.py      # VM offline alerting
    |
    |-- config/                  # Competition configuration
    |   |-- phases.json          # Phase rules + allowed techniques
    |   +-- sample_teams.json    # Dev/test team definitions
    |
    |-- reporter/                # Kali VM deployment package
    |   |-- reporter.py          # Reporter class with backoff + retry buffer
    |   |-- technique_normalizer.py  # Single source of truth for command mapping
    |   +-- technique_map.json   # Command-to-technique mapping rules (98 rules)
    |
    |-- scripts/                 # Operational scripts
    |   |-- provision_tokens.py  # Seed teams + generate tokens
    |   |-- reset_competition.py # Reset data
    |   +-- output/              # Generated configs (gitignored)
    |
    |-- tests/                   # 38 async tests
    |-- docs/HANDOFF.md          # Detailed technical handoff
    |-- .env.example             # Environment variable template
    +-- .gitignore
```

---

## API Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/api/health` | — | Operational health check |
| GET | `/api/phase` | — | Current phase + allowed techniques |
| GET | `/api/teams` | — | All teams with scores |
| GET | `/api/report/final` | — | Ranked JSON scoreboard |
| GET | `/api/report/final/csv` | — | CSV download |
| GET | `/api/stream` | — | SSE live event stream |
| POST | `/api/events` | Bearer token | Ingest from Kali reporters |
| POST | `/api/admin/phase` | X-Admin-Secret | Transition phase |

---

## Competition Phases

| Phase | Label | Days | Key Techniques |
|-------|-------|------|----------------|
| 1 | WAN and DMZ | 1–2 | port_scan, http_exploit, sqli, ssh_brute, default_creds, cve_exploit_public |
| 2 | Internal Pivot | 3–4 | lateral_movement, smb_enum, credential_reuse, internal_scan, firewall_bypass |
| 3 | LAN and Crown Jewels | 5–6 | hash_dump, password_spray, ad_escalation, data_exfil, persistence |

Scores decay from 100% at phase start to 30% at phase end.

---

## Milestones

| Milestone | Points | Trigger |
|-----------|--------|---------|
| `wan_to_lan_direct` | 100 | LAN hit without prior DMZ compromise |
| `dmz_compromise` | 70 | First successful compromise in DMZ |
| `lan_pivot` | 50 | LAN compromise after DMZ compromise |
| `domain_compromise` | 40 | AD escalation in LAN |

---

## Reporter (Kali VM Agent)

The reporter runs on each Kali VM and is the bridge between OpenClaw tool output and the scoring backend.

**Key features:**
- **`Reporter` class** — all state encapsulated, config passed as dict (testable, no module-level globals)
- **Exponential backoff** — on POST failures, backs off 1s -> 2s -> 4s -> ... capped at 60s. Resets on success. Prevents log spam and connection flooding when backend is down
- **Smart retry buffer** — failed events saved to disk JSONL. Buffer drain only triggers on reconnect (not every POST). Line count tracked in memory, no full file re-read on append
- **Single TechniqueNormalizer** — imported from `technique_normalizer.py` (one class, one place to maintain, 98 match rules, 16 self-tests)
- **Graceful shutdown** — SIGINT/SIGTERM handled cleanly with summary stats (events sent vs buffered)
- **Heartbeats bypass backoff** — health signal always attempts to reach backend; successful heartbeat during outage triggers buffer drain

**Usage:**
```bash
# Default (reads config.json from same directory)
python reporter.py

# Custom config path
python reporter.py /path/to/config.json
```

---

## Project Tracker

### Build Progress

| Phase | What | Status |
|-------|------|--------|
| A | DB schema, auth (bcrypt + team_id hint), CIDR validation | Done |
| B | SSE streaming with bounded queues, keepalive | Done |
| C | Phase enforcement from phases.json, milestone detection, time-weighted scoring | Done |
| D | Discord bot integration, queue-based dispatch, reconnect guard | Done |
| Hardening | WAL mode, rate limiting, CORS, exception handler, PhaseLog seed, watchdog | Done |
| Reorg | Project structure cleanup, .gitignore, .env.example, docs | Done |
| Reporter v2 | Rewritten as Reporter class with backoff, smart buffer, single normalizer, graceful shutdown | Done |

### Daily Log

| Date | Day | Work Done |
|------|-----|-----------|
| 2026-03-19 | Wed | Built entire backend pipeline (phases A-D), wrote all 38 tests, ran 2 audit rounds, hardened for pen test week (WAL mode, rate limiting, CORS, global exception handler, PhaseLog seeding, VM watchdog, reporter is_unknown fix), reorganized project, wrote handoff doc, pushed to GitHub |
| 2026-03-19 | Wed | Rewrote reporter.py: refactored from globals to Reporter class, added BackoffTracker (exponential 1s-60s cap), RetryBuffer class (in-memory line count, drain-on-reconnect only), consolidated duplicate TechniqueNormalizer into single source of truth, added SIGINT/SIGTERM graceful shutdown, heartbeats bypass backoff for fastest recovery. 16/16 normalizer tests + 38/38 backend tests passing |

> *See [Weekly Updates](#weekly-updates) for detailed plans per week.*

### Weekly Overview

| Week | Dates | Focus | Deliverables |
|------|-------|-------|-------------|
| **Week 1** | Mar 16–22 | Backend build + hardening + reporter rewrite | FastAPI server, all endpoints, auth, phase enforcement, milestones, SSE, Discord, reporter v2 (backoff, smart buffer, graceful shutdown), scripts, 38 backend tests + 16 normalizer tests, hardened for competition |
| **Week 2** | Mar 23–29 | Integration testing + cross-team sync | End-to-end integration test, confirm telemetry format with VM team, dashboard SSE payload sync |
| **Week 3** | Mar 30 – Apr 5 | Reporter deployment + Discord setup | Deploy reporter to test Kali VM, Discord bot token + channel, real team definitions from instructor |
| **Week 4** | Apr 6–12 | Stress testing + production hardening | Stress test with 4 concurrent reporters, tighten CORS, production .env, automated DB backup |
| **Week 5** | Apr 13–19 | Dry run + polish | Full dress rehearsal (all teams, all phases), fix anything that breaks, finalize runbook |
| **Week 6** | Apr 20–26 | Pre-competition lockdown | Final checklist, team config delivery, freeze code, backup procedures |
| **Competition** | Apr 27 – May 3 | Pen test week | Phase 1 (days 1–2), Phase 2 (days 3–4), Phase 3 (days 5–6), final report + debrief |

### What's Left Before Competition

| # | Item | Owner | Priority | Target Week | Status |
|---|------|-------|----------|-------------|--------|
| 1 | Confirm OpenClaw telemetry JSONL format matches reporter's expected fields | Us + VM team | **Critical** | Week 2 | Not started |
| 2 | End-to-end integration test (fake telemetry -> reporter -> backend -> SSE + Discord) | Us | **Critical** | Week 2 | Not started |
| 3 | Dashboard team sync on SSE event payloads | Us + Dashboard team | **High** | Week 2–3 | Not started |
| 4 | Real team definitions (IPs, CIDRs) from instructor | Us | **High** | Week 3 | Waiting |
| 5 | Discord bot token + channel setup | Us | **High** | Week 3 | Not started |
| 6 | Reporter deployment dry run on test Kali VM | Us + VM team | **High** | Week 3 | Not started |
| 7 | Stress test with 4 concurrent reporters | Us | Medium | Week 4 | Not started |
| 8 | Tighten CORS to dashboard origin | Us | Medium | Week 4 | Not started |
| 9 | Full dress rehearsal (all teams, all phases, end-to-end) | Everyone | **High** | Week 5 | Not started |
| 10 | Automated DB backup script | Us | Medium | Week 4–5 | Not started |
| 11 | Competition-day runbook walkthrough with team | Everyone | Medium | Week 5–6 | Not started |

---

## Competition Day Cheat Sheet

```bash
# Check system health
curl http://localhost:8000/api/health

# Transition to next phase
curl -X POST http://localhost:8000/api/admin/phase \
  -H "Content-Type: application/json" \
  -H "X-Admin-Secret: $ADMIN_SECRET" \
  -d '{"phase": 2, "activated_by": "instructor_name"}'

# View live scoreboard
curl http://localhost:8000/api/report/final

# Download results CSV
curl -O http://localhost:8000/api/report/final/csv

# Emergency reset (scoring only — keeps event history)
python scripts/reset_competition.py scoring

# Full reset (nuclear option)
python scripts/reset_competition.py full
```

---

## Documentation

- **[Technical Handoff](claw-and-order/docs/HANDOFF.md)** — Full architecture details, DB schema, hardening measures, critical rules
- **[Architecture Spec](Claw_and_Order_v2_Definitive_Architecture.docx)** — Original design document
- **[Gap Analysis](Claw_and_Order_PreBuild_Gap_Analysis.docx)** — Pre-build analysis

### Weekly Updates

| Week | Document | Summary |
|------|----------|---------|
| **Week 1** (Mar 16–22) | **[Week 1 Recap & Plan](claw-and-order/docs/week1-recap-and-weekend-plan.md)** | Backend build complete, reporter v2 shipped, integration testing plan for weeks 2–6 |

---

## Tests

```bash
cd claw-and-order

# Backend tests (38 tests)
python -m pytest tests/ -v

# Reporter normalizer self-tests (16 tests)
cd reporter && python technique_normalizer.py
```

54 total tests covering: auth, CIDR validation, DB models, phase gating, milestone detection, SSE streaming, Discord messaging, admin endpoints, input validation, error handling, and technique normalization.
