# Operation Open Claw — Claw & Order

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
    |   |-- reporter.py          # Telemetry tailer + POST to backend
    |   |-- technique_map.json   # Command-to-technique mapping
    |   +-- technique_normalizer.py
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

## Project Tracker

### Build Progress (Completed)

| Phase | What | Status |
|-------|------|--------|
| A | DB schema, auth (bcrypt + team_id hint), CIDR validation | Done |
| B | SSE streaming with bounded queues, keepalive | Done |
| C | Phase enforcement from phases.json, milestone detection, time-weighted scoring | Done |
| D | Discord bot integration, queue-based dispatch, reconnect guard | Done |
| Hardening | WAL mode, rate limiting, CORS, exception handler, PhaseLog seed, watchdog, reporter fix | Done |
| Reorg | Project structure cleanup, .gitignore, .env.example, docs | Done |

### Daily Log

| Date | Day | Work Done |
|------|-----|-----------|
| 2026-03-19 | Wed | Built entire backend pipeline (phases A-D), wrote all 38 tests, ran 2 audit rounds, hardened for pen test week (WAL mode, rate limiting, CORS, global exception handler, PhaseLog seeding, VM watchdog, reporter is_unknown fix), reorganized project, wrote handoff doc, pushed to GitHub |
| 2026-03-20 | Thu | *Dashboard integration testing — coordinate with dashboard team on SSE stream format and CORS* |
| 2026-03-21 | Fri | *Reporter deployment dry run — provision sample teams, deploy to test Kali VM, validate end-to-end event flow* |
| 2026-03-22 | Sat | *Buffer day — stress test with 4 concurrent reporters, fix anything that breaks under load* |
| 2026-03-23 | Sun | *Final review — verify Discord alerts, walk through competition-day runbook, backup procedures* |
| 2026-03-24 | Mon | *Pen test week begins — Phase 1 (WAN and DMZ)* |
| 2026-03-25 | Tue | *Phase 1 continues — monitor health endpoint, watch for reporter disconnects* |
| 2026-03-26 | Wed | *Phase 2 transition (Internal Pivot) — POST /api/admin/phase with phase: 2* |
| 2026-03-27 | Thu | *Phase 2 continues — watch for lateral_movement milestones* |
| 2026-03-28 | Fri | *Phase 3 transition (LAN and Crown Jewels) — POST /api/admin/phase with phase: 3* |
| 2026-03-29 | Sat | *Phase 3 continues — watch for domain_compromise milestones* |
| 2026-03-30 | Sun | *Competition ends — pull final report CSV, debrief* |

> *Italicized entries are planned, not yet completed.*

### Weekly Overview

| Week | Focus | Deliverables |
|------|-------|-------------|
| **Week 1** (Mar 16–22) | Backend build + hardening | FastAPI server, all endpoints, auth, phase enforcement, milestones, SSE, Discord, reporter, scripts, 38 tests, hardened for competition |
| **Week 2** (Mar 23–29) | Competition week | Live monitoring, phase transitions, score tracking, incident response |
| **Week 3** (Mar 30–) | Wrap-up | Final report, debrief, post-mortem |

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

---

## Tests

```bash
cd claw-and-order
python -m pytest tests/ -v
```

38 tests covering: auth, CIDR validation, DB models, phase gating, milestone detection, SSE streaming, Discord messaging, admin endpoints, input validation, and error handling.
