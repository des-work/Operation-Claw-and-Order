# Week 1 Recap & Weekend Plan (Mar 19 – Mar 23)

**Created:** March 19, 2026 (Wednesday)
**Competition starts:** Monday, March 24

---

## What Was Completed This Week

| # | Item | Details |
|---|------|---------|
| 1 | Full backend pipeline (Phases A–D) | FastAPI server, auth, CIDR validation, phase enforcement, milestone detection, time-weighted scoring, SSE streaming, Discord integration |
| 2 | 38 async backend tests | Auth, CIDR, models, phase/milestone, SSE, Discord, hardening — all passing |
| 3 | Hardening pass | WAL mode, rate limiting (120/min global, 60/min ingest), CORS, global exception handler, PhaseLog auto-seed, VM watchdog, corrupted-CIDR catch, corrupted-bcrypt catch, ADMIN_SECRET guard |
| 4 | Project reorganization | Config files to `config/`, docs to `docs/`, `.gitignore`, `.env.example`, handoff doc |
| 5 | Reporter v2 rewrite | Refactored to `Reporter` class with `BackoffTracker` (1s→60s), `RetryBuffer` (in-memory line count, drain-on-reconnect), single `TechniqueNormalizer`, SIGINT/SIGTERM graceful shutdown, heartbeats bypass backoff |
| 6 | 16 normalizer self-tests | Covering all technique patterns, case-insensitive matching, longest-match-first, edge cases |
| 7 | Documentation | README with project tracker, HANDOFF.md with full architecture + reporter v2 section |

**Total test count:** 54 (38 backend + 16 normalizer)

---

## What Needs to Happen This Weekend

Competition is Monday. Everything below is ordered by priority — do them in this sequence.

---

### Task 1: Confirm OpenClaw Telemetry Format with VM Team (CRITICAL — Thu morning)

**Why:** The reporter tails `telemetry.jsonl` and expects a specific JSON structure. If the fields don't match, zero events get scored.

**Steps:**

1. Contact the VM team and ask for a sample `telemetry.jsonl` snippet (3–5 lines) from an actual OpenClaw run.
2. Compare their output against what `reporter.py` expects in `build_event()` (line 253):
   ```
   entry["type"]              → must be "tool.end"
   entry["input"]["command"]  → raw command string
   entry["input"]["target"]   → target IP
   entry["success"]           → boolean (true/false)
   entry["output"]            → raw output string
   ```
3. If their field names differ (e.g. `entry["cmd"]` instead of `entry["input"]["command"]`), update `build_event()` in `reporter.py` to match.
4. If they emit event types other than `"tool.end"`, confirm those should be ignored (line 361 filters for `tool.end` only).
5. Confirm the telemetry log path — reporter defaults to `~/.openclaw/logs/telemetry.jsonl` but this is configurable in `config.json` under `"telemetry_log"`.

**Done when:** You have a real sample and `build_event()` field mapping is confirmed correct or patched.

---

### Task 2: End-to-End Integration Test — Fake Telemetry (Thu afternoon)

**Why:** Proves the entire pipeline works: reporter → backend → SSE + Discord, without needing a real Kali VM or OpenClaw.

**Steps:**

1. **Start the backend:**
   ```bash
   cd claw-and-order
   .venv314\Scripts\activate
   copy .env.example .env
   # Edit .env: set ADMIN_SECRET to any string (e.g., "test-secret-123")
   uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
   ```

2. **Provision test teams:**
   ```bash
   python scripts/provision_tokens.py config/sample_teams.json
   ```
   This creates `scripts/output/config_red-1.json` and `config_red-2.json` with bearer tokens.

3. **Open a second terminal — verify the backend is alive:**
   ```bash
   curl http://localhost:8000/api/health
   curl http://localhost:8000/api/teams
   curl http://localhost:8000/api/phase
   ```
   You should see phase 1, two teams with 0 scores, health OK.

4. **Create a fake telemetry log:**
   ```bash
   mkdir -p %USERPROFILE%\.openclaw\logs
   ```
   Create the file `%USERPROFILE%\.openclaw\logs\telemetry.jsonl` (empty for now).

5. **Start the reporter for red-1:**
   ```bash
   cd reporter
   python reporter.py ..\scripts\output\config_red-1.json
   ```
   It should print "Tailing ..." and sit idle.

6. **In a third terminal, append fake events to the telemetry log:**
   ```bash
   echo {"type":"tool.end","input":{"command":"nmap -sV 10.50.1.100","target":"10.50.1.100"},"success":true,"output":"22/tcp open ssh"} >> %USERPROFILE%\.openclaw\logs\telemetry.jsonl
   ```
   Watch the reporter terminal — it should normalize to `service_fingerprint` and POST to backend.

7. **Verify the event landed:**
   ```bash
   curl http://localhost:8000/api/teams
   ```
   red-1 should now have event data.

8. **Test a milestone trigger — DMZ compromise:**
   ```bash
   echo {"type":"tool.end","input":{"command":"sqlmap -u http://10.50.1.100/login","target":"10.50.1.100"},"success":true,"output":"sql injection found"} >> %USERPROFILE%\.openclaw\logs\telemetry.jsonl
   ```
   The reporter should POST this as `sqli` with `result: success` targeting a DMZ IP. Check:
   ```bash
   curl http://localhost:8000/api/report/final
   ```
   red-1 should have the `dmz_compromise` milestone with ~70 points.

9. **Test SSE stream in a fourth terminal:**
   ```bash
   curl -N http://localhost:8000/api/stream
   ```
   Append another event to the telemetry log — you should see it appear in the SSE stream in real-time.

10. **Test phase transition:**
    ```bash
    curl -X POST http://localhost:8000/api/admin/phase -H "Content-Type: application/json" -H "X-Admin-Secret: test-secret-123" -d "{\"phase\": 2, \"activated_by\": \"test\"}"
    ```
    Then check `curl http://localhost:8000/api/phase` — should show phase 2 with new allowed techniques.

11. **Test reporter backoff (optional):** Stop the backend (Ctrl+C uvicorn), append another event to the telemetry log, watch the reporter back off (1s, 2s, 4s...). Restart the backend, wait for a heartbeat (~60s), and the buffer should drain.

**Done when:** Events flow from telemetry log → reporter → backend → SSE stream, milestones trigger, phase transitions work.

---

### Task 3: Dashboard Team Sync on SSE Payloads (Thu/Fri)

**Why:** The dashboard team needs to know the exact JSON shape of SSE events to render them.

**Steps:**

1. Share the two SSE event types with the dashboard team:

   **`new_event` payload:**
   ```json
   {
     "event_id": 1,
     "team_id": "red-1",
     "phase": 1,
     "timestamp": "2026-03-24T10:30:00+00:00",
     "technique": "port_scan",
     "target_ip": "10.50.1.100",
     "result": "success",
     "milestone": null
   }
   ```

   **`milestone` payload:**
   ```json
   {
     "team_id": "red-1",
     "key": "dmz_compromise",
     "score": 70,
     "phase": 1,
     "technique": "sqli",
     "timestamp": "2026-03-24T10:35:00+00:00"
   }
   ```

2. Confirm the SSE stream URL: `GET http://<backend-host>:8000/api/stream`
3. Confirm the REST endpoints they need:
   - `GET /api/teams` — all teams with scores
   - `GET /api/phase` — current phase + allowed techniques
   - `GET /api/report/final` — ranked scoreboard
4. Test CORS — have the dashboard team hit the backend from their frontend dev server. It should work (CORS is `*` currently).
5. If they give you the dashboard's actual origin URL, update CORS in `backend/main.py` (replace `"*"` with `["http://their-origin"]`).

**Done when:** Dashboard team confirms they can connect to SSE and parse both event types.

---

### Task 4: Get Real Team Definitions from Instructor (Fri)

**Why:** `config/sample_teams.json` has placeholder IPs. Real teams need real CIDRs or milestone detection and CIDR validation will reject everything.

**Steps:**

1. Get from the instructor for each team:
   - `id` — team identifier (e.g., `"team-alpha"`)
   - `display_name` — human-readable name
   - `wan_ip` — the Kali VM's WAN-facing IP
   - `dmz_cidr` — DMZ subnet (e.g., `"10.50.1.0/24"`)
   - `lan_cidr` — LAN subnet (e.g., `"10.50.2.0/24"`)

2. Create `config/teams_production.json` with the real data:
   ```json
   [
     {
       "id": "team-alpha",
       "display_name": "Team Alpha",
       "wan_ip": "X.X.X.X",
       "dmz_cidr": "X.X.X.0/24",
       "lan_cidr": "X.X.X.0/24"
     }
   ]
   ```

3. Re-provision:
   ```bash
   python scripts/reset_competition.py full
   python scripts/provision_tokens.py config/teams_production.json
   ```

4. Securely deliver each team's `scripts/output/config_<team-id>.json` to its Kali VM.

**Done when:** Production teams are in the DB with correct CIDRs and each VM has its config.

---

### Task 5: Discord Bot Token and Channel Setup (Fri/Sat)

**Why:** Without this, milestone alerts and VM-offline warnings don't reach anyone during competition.

**Steps:**

1. Go to https://discord.com/developers/applications
2. Create a new application (e.g., "Claw & Order Bot")
3. Go to **Bot** tab → click **Reset Token** → copy the token
4. Go to **OAuth2 → URL Generator** → check `bot` scope → check `Send Messages` permission
5. Copy the generated URL → open in browser → add bot to your competition Discord server
6. In Discord, right-click the channel you want alerts in → **Copy Channel ID** (enable Developer Mode in settings if you don't see this)
7. Update your `.env`:
   ```
   DISCORD_TOKEN=<paste-bot-token>
   DISCORD_CHANNEL_ID=<paste-channel-id>
   ```
8. Restart the backend — check logs for "Discord bot connected"
9. Test: trigger a milestone (use the fake telemetry flow from Task 2) and confirm the alert appears in the Discord channel

**Done when:** Milestone alerts and VM-offline warnings appear in the Discord channel.

---

### Task 6: Tighten CORS to Dashboard Origin (Sat)

**Why:** Production security — `allow_origins=["*"]` lets any site hit your API.

**Steps:**

1. Get the dashboard's production URL from the dashboard team (e.g., `http://192.168.1.50:3000`)
2. Edit `backend/main.py`, change:
   ```python
   allow_origins=["*"]
   ```
   to:
   ```python
   allow_origins=["http://192.168.1.50:3000"]
   ```
   Add multiple origins if needed (e.g., both a dev and prod URL).
3. Restart backend, verify dashboard still connects.

**Done when:** CORS is locked to known dashboard origin(s).

---

### Task 7: Stress Test with 4 Concurrent Reporters (Sat)

**Why:** Competition day will have 4+ Kali VMs hammering the backend simultaneously. SQLite WAL mode should handle it, but verify under real load.

**Steps:**

1. Make sure you have 4 teams provisioned (add 2 more to `sample_teams.json` or use production teams).
2. Create 4 separate telemetry log files:
   ```
   /tmp/telemetry_red-1.jsonl
   /tmp/telemetry_red-2.jsonl
   /tmp/telemetry_red-3.jsonl
   /tmp/telemetry_red-4.jsonl
   ```
3. Create 4 config files (one per team), each pointing to its own telemetry log path.
4. Open 4 terminals, start a reporter in each:
   ```bash
   python reporter.py config_red-1.json
   python reporter.py config_red-2.json
   python reporter.py config_red-3.json
   python reporter.py config_red-4.json
   ```
5. Write a quick script (or do it manually) that appends 50 events rapidly to each telemetry file:
   ```python
   import json, time
   for i in range(50):
       line = json.dumps({
           "type": "tool.end",
           "input": {"command": f"nmap 10.50.1.{i % 254 + 1}", "target": f"10.50.1.{i % 254 + 1}"},
           "success": True,
           "output": f"scan result {i}"
       })
       with open("/tmp/telemetry_red-1.jsonl", "a") as f:
           f.write(line + "\n")
       time.sleep(0.05)
   ```
6. Watch the backend logs for:
   - No `database is locked` errors
   - No 500 responses
   - All 4 reporters staying connected
7. Check `curl http://localhost:8000/api/teams` — all 4 teams should have events recorded.
8. Check `curl http://localhost:8000/api/health` — health should show OK.

**Done when:** 4 reporters posting simultaneously with no DB lock errors or dropped events.

---

### Task 8: Final Pre-Competition Checklist (Sunday)

Run through this before going to bed Sunday night:

- [ ] Backend starts cleanly: `uvicorn backend.main:app --host 0.0.0.0 --port 8000`
- [ ] `GET /api/health` returns `"status": "ok"`
- [ ] Production teams provisioned with correct CIDRs
- [ ] Each Kali VM has its `config.json`, `reporter.py`, `technique_normalizer.py`, `technique_map.json`
- [ ] Reporter starts and connects to backend on each VM (or test VM)
- [ ] Phase 1 is active: `GET /api/phase` shows phase 1
- [ ] SSE stream works: `curl -N http://localhost:8000/api/stream`
- [ ] Discord bot connected and posting to the correct channel
- [ ] CORS locked to dashboard origin (not `*`)
- [ ] `.env` has strong `ADMIN_SECRET` (not `test-secret-123`)
- [ ] Phase transition works: `POST /api/admin/phase` with phase 2, then revert to 1
- [ ] Backup plan: know where the SQLite `.db` file is, can copy it manually
- [ ] `scripts/reset_competition.py` works for emergency reset
- [ ] All 38 backend tests pass: `python -m pytest tests/ -v`
- [ ] All 16 normalizer tests pass: `cd reporter && python technique_normalizer.py`
- [ ] Team has the Competition Day Cheat Sheet from the README bookmarked

---

## Risk Register

| Risk | Impact | Mitigation |
|------|--------|------------|
| OpenClaw telemetry format doesn't match reporter expectations | **Show-stopper** — no events scored | Task 1 — get a sample ASAP |
| SQLite locks under 4+ concurrent writers | Events dropped or delayed | WAL mode + busy_timeout already in place; Task 7 validates |
| Reporter can't reach backend (network issue) | Events buffered, not lost | Backoff + retry buffer handles this; events replay on reconnect |
| Dashboard can't parse SSE events | Dashboard shows nothing | Task 3 — sync payload format before competition |
| Wrong CIDRs in team config | All events rejected with 403 | Task 4 — get real values from instructor |
| Discord bot token not set | No milestone/offline alerts | Task 5 — set up before Saturday |
| Backend crashes mid-competition | Scoring stops | Watchdog alerts Discord within 5 min; restart with `uvicorn` |

---

## Schedule

| Day | Focus | Tasks |
|-----|-------|-------|
| **Thu Mar 20** | Integration | Task 1 (telemetry format), Task 2 (end-to-end test), start Task 3 (dashboard sync) |
| **Fri Mar 21** | Cross-team | Finish Task 3, Task 4 (real team defs), Task 5 (Discord) |
| **Sat Mar 22** | Hardening | Task 6 (CORS), Task 7 (stress test), fix anything that breaks |
| **Sun Mar 23** | Final review | Task 8 (checklist), backup plan, competition-day runbook walkthrough |
| **Mon Mar 24** | **Competition begins** | Phase 1 — WAN and DMZ |
