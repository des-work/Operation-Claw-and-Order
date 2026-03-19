"""
reporter.py  — runs on each Kali VM.
Dependencies: requests (pre-installed on Kali), standard library only.

Responsibilities:
  1. Poll /api/phase at startup and every PHASE_POLL_INTERVAL seconds.
     Write current phase to current_phase.json for OpenClaw playbook to read.
  2. Drain retry buffer on startup (replay any events from previous outages).
  3. Tail the OpenClaw telemetry JSONL for tool.end events.
  4. Normalize technique, build payload, POST to /api/events.
  5. On POST failure: append to retry buffer, back off exponentially.
  6. Send a heartbeat event every HEARTBEAT_INTERVAL seconds regardless of activity.
"""

import json
import os
import signal
import sys
import time
import logging
from pathlib import Path
from typing import Optional

import requests

from technique_normalizer import TechniqueNormalizer

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("reporter")


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config(config_path: str | None = None) -> dict:
    """Load reporter config from JSON file. Returns the raw config dict."""
    path = Path(config_path) if config_path else Path(__file__).parent / "config.json"
    if not path.exists():
        log.error("Config file not found: %s", path)
        sys.exit(1)
    return json.loads(path.read_text())


# ── Backoff tracker ───────────────────────────────────────────────────────────

class BackoffTracker:
    """Exponential backoff for POST failures. Resets on success."""

    def __init__(self, base: float = 1.0, cap: float = 60.0, multiplier: float = 2.0):
        self._base = base
        self._cap = cap
        self._multiplier = multiplier
        self._current = 0.0            # 0 means "not in backoff"
        self._next_retry_at = 0.0      # timestamp when we can try again
        self._consecutive_failures = 0

    @property
    def in_backoff(self) -> bool:
        return self._current > 0 and time.time() < self._next_retry_at

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def record_failure(self) -> float:
        """Record a failure. Returns the backoff delay in seconds."""
        self._consecutive_failures += 1
        if self._current == 0:
            self._current = self._base
        else:
            self._current = min(self._current * self._multiplier, self._cap)
        self._next_retry_at = time.time() + self._current
        return self._current

    def record_success(self) -> bool:
        """Record a success. Returns True if we were previously in backoff (i.e. reconnected)."""
        was_in_backoff = self._consecutive_failures > 0
        self._current = 0.0
        self._next_retry_at = 0.0
        self._consecutive_failures = 0
        return was_in_backoff


# ── Retry buffer ──────────────────────────────────────────────────────────────

class RetryBuffer:
    """Append-only JSONL buffer for events that failed to POST."""

    def __init__(self, path: str, max_events: int = 500):
        self._path = Path(path)
        self._max = max_events
        # Count existing lines once at init instead of re-reading every append
        self._count = 0
        if self._path.exists():
            self._count = sum(1 for _ in self._path.open("r"))

    @property
    def has_content(self) -> bool:
        return self._count > 0

    def append(self, payload: dict) -> None:
        """Append a failed event. Trims oldest if over max."""
        if self._count >= self._max:
            # Trim: keep the newest (max - 1) lines, drop the oldest
            lines = self._path.read_text().splitlines()
            lines = lines[-(self._max - 1):]
            self._path.write_text("\n".join(lines) + "\n")
            self._count = len(lines)
            log.warning("Retry buffer at limit (%d), dropped oldest event", self._max)

        with self._path.open("a") as f:
            f.write(json.dumps(payload) + "\n")
        self._count += 1

    def drain(self, post_fn) -> int:
        """Replay buffered events in order. Returns count of successfully drained events."""
        if not self._path.exists():
            return 0
        lines = self._path.read_text().splitlines()
        if not lines:
            return 0

        log.info("Draining retry buffer: %d events", len(lines))
        drained = 0
        remaining = []

        for line in lines:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue  # corrupt line, discard
            if post_fn(payload):
                drained += 1
            else:
                remaining.append(line)
                # Stop draining — backend still down, keep the rest intact
                remaining.extend(lines[drained + len(remaining):])
                break

        if remaining:
            self._path.write_text("\n".join(remaining) + "\n")
            self._count = len(remaining)
            log.warning("Drain stopped — backend unreachable, %d events remain", len(remaining))
        else:
            self._path.unlink(missing_ok=True)
            self._count = 0
            log.info("Retry buffer cleared (%d events replayed)", drained)

        return drained


# ── Reporter ──────────────────────────────────────────────────────────────────

class Reporter:
    """Encapsulates all reporter state and logic."""

    def __init__(self, cfg: dict):
        # Config
        self.backend_url = cfg["backend_url"].rstrip("/")
        self.bearer_token = cfg["bearer_token"]
        self.team_id = cfg["team_id"]
        self.telemetry_log = cfg.get(
            "telemetry_log", os.path.expanduser("~/.openclaw/logs/telemetry.jsonl")
        )
        self.phase_file = cfg.get(
            "phase_file", str(Path(__file__).parent / "current_phase.json")
        )
        technique_map = cfg.get(
            "technique_map", str(Path(__file__).parent / "technique_map.json")
        )
        self.heartbeat_interval = cfg.get("heartbeat_interval_seconds", 60)
        self.phase_poll_interval = cfg.get("phase_poll_interval_seconds", 300)
        self.post_timeout = cfg.get("post_timeout_seconds", 10)

        # Components
        self.normalizer = TechniqueNormalizer(technique_map)
        self.backoff = BackoffTracker(base=1.0, cap=60.0)
        self.buffer = RetryBuffer(
            path=cfg.get("retry_buffer", "/tmp/claw_retry_buffer.jsonl"),
            max_events=cfg.get("retry_buffer_max_events", 500),
        )

        # HTTP session
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
        })

        # State
        self._current_phase = 1
        self._running = True
        self._events_sent = 0
        self._events_buffered = 0

    # ── Signal handling ──────────────────────────────────────────────────────

    def _handle_shutdown(self, signum, frame):
        sig_name = signal.Signals(signum).name
        log.info("Received %s — shutting down gracefully", sig_name)
        self._running = False

    def _install_signal_handlers(self):
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

    # ── HTTP ─────────────────────────────────────────────────────────────────

    def post_event(self, payload: dict) -> bool:
        """POST to /api/events. Returns True on 2xx, False on failure."""
        return self._post("/api/events", payload)

    def _post(self, endpoint: str, payload: dict) -> bool:
        try:
            r = self._session.post(
                f"{self.backend_url}{endpoint}",
                json=payload,
                timeout=self.post_timeout,
            )
            if r.status_code in (200, 201, 202):
                return True
            log.warning("POST %s returned %d: %s", endpoint, r.status_code, r.text[:200])
            return False
        except requests.exceptions.RequestException as e:
            log.warning("POST %s failed: %s", endpoint, e)
            return False

    # ── Phase sync ───────────────────────────────────────────────────────────

    def poll_phase(self) -> None:
        try:
            r = self._session.get(
                f"{self.backend_url}/api/phase", timeout=self.post_timeout
            )
            if r.status_code == 200:
                data = r.json()
                new_phase = int(data["phase"])
                if new_phase != self._current_phase:
                    log.info("Phase changed: %d -> %d", self._current_phase, new_phase)
                    self._current_phase = new_phase
                Path(self.phase_file).write_text(
                    json.dumps({"phase": self._current_phase, "updated_at": time.time()})
                )
        except Exception as e:
            log.warning("Phase poll failed: %s", e)

    # ── Event builders ───────────────────────────────────────────────────────

    def build_event(self, tool_end_line: dict, technique: str) -> dict:
        return {
            "team_id": self.team_id,
            "command": tool_end_line.get("input", {}).get("command", ""),
            "technique": technique,
            "target_ip": tool_end_line.get("input", {}).get("target", ""),
            "result": "success" if tool_end_line.get("success") else "failure",
            "raw_output": str(tool_end_line.get("output", ""))[:2000],
        }

    def build_heartbeat(self) -> dict:
        return {
            "team_id": self.team_id,
            "command": "__heartbeat__",
            "technique": "heartbeat",
            "target_ip": "0.0.0.0",
            "result": "success",
            "raw_output": "",
        }

    # ── Core: send with backoff ──────────────────────────────────────────────

    def send_event(self, payload: dict) -> bool:
        """
        Try to POST an event. On failure, buffer it and engage backoff.
        On success after backoff, drain the buffer.
        """
        if self.backoff.in_backoff:
            # Don't even try — we're waiting for backoff to expire
            self.buffer.append(payload)
            self._events_buffered += 1
            return False

        if self.post_event(payload):
            self._events_sent += 1
            reconnected = self.backoff.record_success()
            # Only drain buffer on reconnect (transition from failing to succeeding)
            if reconnected and self.buffer.has_content:
                log.info("Backend reconnected — draining buffer")
                self.buffer.drain(self.post_event)
            return True
        else:
            delay = self.backoff.record_failure()
            self.buffer.append(payload)
            self._events_buffered += 1
            failures = self.backoff.consecutive_failures
            if failures <= 3 or failures % 10 == 0:
                # Log first few failures, then every 10th to avoid spam
                log.warning(
                    "POST failed (attempt %d) — backing off %.1fs, event buffered",
                    failures, delay,
                )
            return False

    # ── Main loop ────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._install_signal_handlers()
        log.info("Reporter starting for team %s", self.team_id)
        log.info("Backend: %s", self.backend_url)

        # Step 1: sync phase
        self.poll_phase()

        # Step 2: drain any buffered events from previous run
        if self.buffer.has_content:
            self.buffer.drain(self.post_event)

        # Step 3: wait for telemetry log to exist
        telemetry_path = Path(self.telemetry_log)
        while not telemetry_path.exists() and self._running:
            log.info("Waiting for telemetry log at %s ...", self.telemetry_log)
            time.sleep(5)

        if not self._running:
            log.info("Shutdown before telemetry log appeared")
            return

        log.info("Tailing %s", self.telemetry_log)

        last_heartbeat = time.time()
        last_phase_poll = time.time()

        with open(self.telemetry_log, "r") as f:
            # Seek to end — only process new events from this point
            f.seek(0, 2)

            while self._running:
                now = time.time()

                # ── Phase poll ────────────────────────────────────────────
                if now - last_phase_poll >= self.phase_poll_interval:
                    self.poll_phase()
                    last_phase_poll = now

                # ── Read new lines ────────────────────────────────────────
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
                    technique = self.normalizer.normalize(command)

                    if self.normalizer.is_unknown(technique):
                        log.info("Unknown technique for command: %s", command[:80])

                    payload = self.build_event(entry, technique)
                    self.send_event(payload)

                else:
                    # No new line — brief sleep to avoid busy-wait
                    time.sleep(0.1)

                # ── Heartbeat ─────────────────────────────────────────────
                if now - last_heartbeat >= self.heartbeat_interval:
                    hb = self.build_heartbeat()
                    # Heartbeats bypass backoff — they're the health signal
                    if not self.post_event(hb):
                        if not self.backoff.in_backoff:
                            self.backoff.record_failure()
                        log.warning("Heartbeat POST failed — backend may be down")
                    else:
                        reconnected = self.backoff.record_success()
                        if reconnected and self.buffer.has_content:
                            log.info("Heartbeat succeeded after outage — draining buffer")
                            self.buffer.drain(self.post_event)
                    last_heartbeat = now

        # ── Shutdown summary ──────────────────────────────────────────────
        log.info(
            "Reporter stopped. Sent: %d events, Buffered: %d events",
            self._events_sent, self._events_buffered,
        )
        if self.buffer.has_content:
            log.info("Retry buffer has pending events — will drain on next startup")


# ── Entry point ───────────────────────────────────────────────────────────────

def main(config_path: str | None = None) -> None:
    cfg = load_config(config_path)
    reporter = Reporter(cfg)
    reporter.run()


if __name__ == "__main__":
    config_arg = sys.argv[1] if len(sys.argv) > 1 else None
    main(config_arg)
