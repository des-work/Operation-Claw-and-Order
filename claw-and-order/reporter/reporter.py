"""
reporter.py  — runs on each Kali VM.
Dependencies: requests (pre-installed on Kali), standard library only.

Responsibilities:
  1. Poll /api/phase at startup and every PHASE_POLL_INTERVAL seconds.
     Write current phase to current_phase.json for OpenClaw playbook to read.
  2. Drain retry buffer on startup (replay any events from previous outages).
  3. Tail the OpenClaw telemetry JSONL for tool.end events.
  4. Normalize technique, build payload, POST to /api/events.
  5. On POST failure: append to retry buffer, retry on next startup or reconnect.
  6. Send a heartbeat event every HEARTBEAT_INTERVAL seconds regardless of activity.
"""

import json
import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional

import requests

# ── Config (loaded from config.json alongside this script) ───────────────────
_cfg_path = Path(__file__).parent / "config.json"
_cfg = json.loads(_cfg_path.read_text())

BACKEND_URL:        str   = _cfg["backend_url"].rstrip("/")
BEARER_TOKEN:       str   = _cfg["bearer_token"]
TEAM_ID:            str   = _cfg["team_id"]
TELEMETRY_LOG:      str   = _cfg.get("telemetry_log", os.path.expanduser("~/.openclaw/logs/telemetry.jsonl"))
RETRY_BUFFER_PATH:  str   = _cfg.get("retry_buffer", "/tmp/claw_retry_buffer.jsonl")
PHASE_FILE_PATH:    str   = _cfg.get("phase_file", str(Path(__file__).parent / "current_phase.json"))
TECHNIQUE_MAP_PATH: str   = _cfg.get("technique_map", str(Path(__file__).parent / "technique_map.json"))

HEARTBEAT_INTERVAL:  int  = _cfg.get("heartbeat_interval_seconds", 60)
PHASE_POLL_INTERVAL: int  = _cfg.get("phase_poll_interval_seconds", 300)
RETRY_BUFFER_MAX:    int  = _cfg.get("retry_buffer_max_events", 500)
POST_TIMEOUT:        int  = _cfg.get("post_timeout_seconds", 10)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("reporter")

# ── Technique normalizer (inline — no separate import on Kali) ────────────────
class TechniqueNormalizer:
    def __init__(self, map_path: str):
        raw = json.loads(Path(map_path).read_text())
        self._rules: list[tuple[str, str]] = []
        for pattern in raw["patterns"]:
            for match_str in pattern["matches"]:
                self._rules.append((match_str.lower(), pattern["id"]))
        self._rules.sort(key=lambda r: len(r[0]), reverse=True)
        self._fallback = raw["fallback"]

    def normalize(self, command: str) -> str:
        cmd_lower = command.lower().strip()
        for match_str, tech_id in self._rules:
            if match_str in cmd_lower:
                return tech_id
        return self._fallback

    def is_unknown(self, technique: str) -> bool:
        """Return True if the technique is the fallback (unknown) value."""
        return technique == self._fallback

# ── HTTP helpers ──────────────────────────────────────────────────────────────
_session = requests.Session()
_session.headers.update({
    "Authorization": f"Bearer {BEARER_TOKEN}",
    "Content-Type": "application/json",
})

def _post(endpoint: str, payload: dict) -> bool:
    """POST to backend. Returns True on 2xx, False on any failure."""
    try:
        r = _session.post(f"{BACKEND_URL}{endpoint}", json=payload, timeout=POST_TIMEOUT)
        if r.status_code in (200, 201, 202):
            return True
        log.warning("POST %s returned %d: %s", endpoint, r.status_code, r.text[:200])
        return False
    except requests.exceptions.RequestException as e:
        log.warning("POST %s failed: %s", endpoint, e)
        return False

# ── Phase sync ────────────────────────────────────────────────────────────────
_current_phase: int = 1

def poll_phase() -> None:
    global _current_phase
    try:
        r = _session.get(f"{BACKEND_URL}/api/phase", timeout=POST_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            new_phase = int(data["phase"])
            if new_phase != _current_phase:
                log.info("Phase changed: %d -> %d", _current_phase, new_phase)
                _current_phase = new_phase
            Path(PHASE_FILE_PATH).write_text(json.dumps({
                "phase": _current_phase,
                "updated_at": time.time()
            }))
    except Exception as e:
        log.warning("Phase poll failed: %s", e)

# ── Retry buffer ──────────────────────────────────────────────────────────────
def buffer_append(payload: dict) -> None:
    """Append a failed event to the local retry buffer."""
    buf = Path(RETRY_BUFFER_PATH)
    # Enforce max size: count lines, trim oldest if over limit
    lines = buf.read_text().splitlines() if buf.exists() else []
    if len(lines) >= RETRY_BUFFER_MAX:
        log.warning("Retry buffer at limit (%d), dropping oldest event", RETRY_BUFFER_MAX)
        lines = lines[-(RETRY_BUFFER_MAX - 1):]
        buf.write_text("\n".join(lines) + "\n")
    with buf.open("a") as f:
        f.write(json.dumps(payload) + "\n")

def drain_buffer() -> None:
    """On startup (or reconnect): replay buffered events in order."""
    buf = Path(RETRY_BUFFER_PATH)
    if not buf.exists():
        return
    lines = buf.read_text().splitlines()
    if not lines:
        return

    log.info("Draining retry buffer: %d events", len(lines))
    remaining = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue  # corrupt line, discard
        if _post("/api/events", payload):
            log.debug("Replayed buffered event for team %s", payload.get("team_id"))
        else:
            remaining.append(line)
            log.warning("Backend still unreachable during drain, %d events remain", len(remaining))
            break  # stop draining if backend is still down; keep remaining intact

    if remaining:
        buf.write_text("\n".join(remaining) + "\n")
    else:
        buf.unlink()
        log.info("Retry buffer cleared")

# ── Event builder ─────────────────────────────────────────────────────────────
def build_event(tool_end_line: dict, technique: str) -> dict:
    """Build the POST payload from a tool.end JSONL line."""
    return {
        "team_id":   TEAM_ID,
        "command":   tool_end_line.get("input", {}).get("command", ""),
        "technique": technique,
        "target_ip": tool_end_line.get("input", {}).get("target", ""),
        "result":    "success" if tool_end_line.get("success") else "failure",
        "raw_output": str(tool_end_line.get("output", ""))[:2000],  # truncate
    }

def build_heartbeat() -> dict:
    return {
        "team_id":   TEAM_ID,
        "command":   "__heartbeat__",
        "technique": "heartbeat",
        "target_ip": "0.0.0.0",
        "result":    "success",
        "raw_output": "",
    }

# ── Main loop ─────────────────────────────────────────────────────────────────
def run() -> None:
    log.info("Reporter starting for team %s", TEAM_ID)
    normalizer = TechniqueNormalizer(TECHNIQUE_MAP_PATH)

    # Step 1: sync phase
    poll_phase()

    # Step 2: drain any buffered events from previous run
    drain_buffer()

    # Step 3: wait for telemetry log to exist
    telemetry_path = Path(TELEMETRY_LOG)
    while not telemetry_path.exists():
        log.info("Waiting for telemetry log at %s ...", TELEMETRY_LOG)
        time.sleep(5)

    log.info("Tailing %s", TELEMETRY_LOG)

    last_heartbeat  = time.time()
    last_phase_poll = time.time()

    with open(TELEMETRY_LOG, "r") as f:
        # Seek to end — we only want new events from this point
        f.seek(0, 2)

        while True:
            now = time.time()

            # ── Phase poll ────────────────────────────────────────────────────
            if now - last_phase_poll >= PHASE_POLL_INTERVAL:
                poll_phase()
                last_phase_poll = now

            # ── Read new lines ─────────────────────────────────────────────────
            line = f.readline()
            if line:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("Skipping non-JSON line")
                    continue

                # Only process tool.end events
                if entry.get("type") != "tool.end":
                    continue

                command = entry.get("input", {}).get("command", "")
                technique = normalizer.normalize(command)

                if normalizer.is_unknown(technique):
                    log.info("Unknown technique for command: %s", command[:80])

                payload = build_event(entry, technique)

                if not _post("/api/events", payload):
                    buffer_append(payload)
                    log.warning("Event buffered (POST failed). Buffer drain on next reconnect.")
                else:
                    # Successful POST: try to drain buffer if it has content
                    if Path(RETRY_BUFFER_PATH).exists():
                        drain_buffer()

            else:
                # No new line — check heartbeat timer
                time.sleep(0.1)

            # ── Heartbeat ──────────────────────────────────────────────────────
            if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                hb = build_heartbeat()
                if not _post("/api/events", hb):
                    log.warning("Heartbeat POST failed — backend may be down")
                last_heartbeat = now


if __name__ == "__main__":
    run()
