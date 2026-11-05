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
import hashlib
import os
import signal
import sys
import time
import logging
import socket
from pathlib import Path
from typing import Optional

import requests

try:
    from .technique_normalizer import TechniqueNormalizer
except ImportError:  # direct execution on Kali VM
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

class PermanentEventError(Exception):
    """The backend rejected this event; retrying the same payload cannot help."""

    def __init__(self, status_code: int, response: str):
        self.status_code = status_code
        self.response = response[:500]
        super().__init__(f"HTTP {status_code}: {self.response}")


class RetryBuffer:
    """Append-only JSONL buffer for events that failed to POST."""

    def __init__(self, path: str, max_events: int = 500):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._max = max_events
        # Count existing lines once at init instead of re-reading every append
        self._count = 0
        if self._path.exists():
            self._count = sum(1 for _ in self._path.open("r"))
        self._warned = False

    @property
    def has_content(self) -> bool:
        return self._count > 0

    def append(self, payload: dict) -> None:
        """Durably append failed telemetry; max_events is an alert threshold, not a drop limit."""
        if self._count >= self._max:
            if not self._warned:
                log.error("Retry buffer exceeded %d events; inspect backend and free disk space", self._max)
                self._warned = True

        with self._path.open("a") as f:
            f.write(json.dumps(payload) + "\n")
            f.flush()
            os.fsync(f.fileno())
        self._count += 1

    def reject(self, payload: dict, error: PermanentEventError) -> None:
        """Keep rejected telemetry for operator review without blocking later events."""
        rejected = self._path.with_suffix(self._path.suffix + ".rejected.jsonl")
        with rejected.open("a") as out:
            out.write(json.dumps({"payload": payload, "status_code": error.status_code,
                                  "response": error.response, "rejected_at": time.time()}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        log.error("Event permanently rejected (%s); saved to %s", error, rejected)

    def drain(self, post_fn) -> int:
        """Replay buffered events in order. Returns count of successfully drained events."""
        if not self._path.exists():
            return 0
        log.info("Draining retry buffer: %d events", self._count)
        drained = 0
        remaining = 0
        failed = False
        replacement = self._path.with_suffix(self._path.suffix + ".new")
        with self._path.open("r") as source, replacement.open("w") as target:
            for line in source:
                if failed:
                    target.write(line)
                    remaining += 1
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    with self._path.with_suffix(self._path.suffix + ".corrupt").open("a") as bad:
                        bad.write(line)
                        bad.flush()
                        os.fsync(bad.fileno())
                    log.error("Moved corrupt retry record to %s.corrupt", self._path)
                    continue
                try:
                    accepted = post_fn(payload)
                except PermanentEventError as error:
                    self.reject(payload, error)
                    continue
                if accepted:
                    drained += 1
                else:
                    failed = True
                    target.write(line)
                    remaining += 1
            target.flush()
            os.fsync(target.fileno())

        if remaining:
            os.replace(replacement, self._path)
            self._count = remaining
            log.warning("Drain stopped — backend unreachable, %d events remain", remaining)
        else:
            replacement.unlink(missing_ok=True)
            self._path.unlink(missing_ok=True)
            self._count = 0
            self._warned = False
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
        self.cursor_file = Path(cfg.get("cursor_file", self.telemetry_log + ".cursor.json"))
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
            path=cfg.get("retry_buffer", os.path.expanduser(
                f"~/.local/state/claw/retry_{self.team_id}.jsonl")),
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
        """POST an event; raise PermanentEventError for non-retryable responses."""
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
            if r.status_code in (400, 403, 409, 413, 422):
                raise PermanentEventError(r.status_code, r.text)
            if r.status_code == 401:
                log.error("Reporter credential rejected (HTTP 401); verify this team's token")
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

    def build_event(self, tool_end_line: dict, technique: str, source_id: str | None = None) -> dict:
        event = {
            "team_id": self.team_id,
            "command": tool_end_line.get("input", {}).get("command", ""),
            "technique": technique,
            "target_ip": tool_end_line.get("input", {}).get("target", ""),
            "result": "success" if tool_end_line.get("success") else "failure",
            "raw_output": str(tool_end_line.get("output") or "")[:2000],
        }
        if source_id:
            event["source_id"] = source_id
        return event

    def _source_id(self, file, offset: int, line: str) -> str:
        identity = os.fstat(file.fileno())
        material = f"{self.team_id}:{socket.gethostname()}:{identity.st_dev}:{identity.st_ino}:{offset}:{line}"
        return hashlib.sha256(material.encode()).hexdigest()

    def _reject_line(self, line: str, reason: str) -> None:
        rejected = self.cursor_file.with_suffix(self.cursor_file.suffix + ".rejected.jsonl")
        rejected.parent.mkdir(parents=True, exist_ok=True)
        with rejected.open("a") as out:
            out.write(json.dumps({"reason": reason, "line": line}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        log.error("Telemetry %s; saved to %s", reason, rejected)

    def _save_cursor(self, file) -> None:
        identity = os.fstat(file.fileno())
        self.cursor_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cursor_file.with_suffix(self.cursor_file.suffix + ".new")
        with temporary.open("w") as out:
            json.dump({"device": identity.st_dev, "inode": identity.st_ino,
                       "offset": file.tell()}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.cursor_file)

    def _open_telemetry(self):
        file = open(self.telemetry_log, "r")
        identity = os.fstat(file.fileno())
        try:
            cursor = json.loads(self.cursor_file.read_text())
            if (cursor["device"], cursor["inode"]) == (identity.st_dev, identity.st_ino):
                file.seek(cursor["offset"] if cursor["offset"] <= identity.st_size else 0)
            else:
                file.seek(0)
        except (OSError, ValueError, KeyError):
            # First installation begins at the live tail; subsequent runs resume.
            file.seek(0, 2)
            self._save_cursor(file)
        return file

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

        if self.buffer.has_content:
            self.buffer.append(payload)
            self._events_buffered += 1
            drained = self.buffer.drain(self.post_event)
            if self.buffer.has_content:
                self.backoff.record_failure()
                return False
            self._events_sent += drained
            self.backoff.record_success()
            return True

        try:
            accepted = self.post_event(payload)
        except PermanentEventError as error:
            self.buffer.reject(payload, error)
            return False
        if accepted:
            self._events_sent += 1
            self.backoff.record_success()
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
            self._events_sent += self.buffer.drain(self.post_event)
            if self.buffer.has_content:
                self.backoff.record_failure()

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

        f = self._open_telemetry()
        try:
            while self._running:
                now = time.time()

                # Send health reports even during a flood of malformed or unrelated lines.
                if now - last_heartbeat >= self.heartbeat_interval:
                    hb = self.build_heartbeat()
                    try:
                        heartbeat_ok = self.post_event(hb)
                    except PermanentEventError as error:
                        self.buffer.reject(hb, error)
                        heartbeat_ok = None
                    if heartbeat_ok is False:
                        if not self.backoff.in_backoff:
                            self.backoff.record_failure()
                        log.warning("Heartbeat POST failed — backend may be down")
                    elif heartbeat_ok:
                        reconnected = self.backoff.record_success()
                        if reconnected and self.buffer.has_content:
                            log.info("Heartbeat succeeded after outage — draining buffer")
                            self._events_sent += self.buffer.drain(self.post_event)
                    last_heartbeat = now

                # ── Phase poll ────────────────────────────────────────────
                if now - last_phase_poll >= self.phase_poll_interval:
                    self.poll_phase()
                    last_phase_poll = now

                # ── Read new lines ────────────────────────────────────────
                offset = f.tell()
                line = f.readline()
                if line:
                    if not line.endswith("\n"):
                        f.seek(offset)
                        time.sleep(0.1)
                        continue
                    line = line.strip()
                    if not line:
                        self._save_cursor(f)
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        self._reject_line(line, "invalid JSON")
                        self._save_cursor(f)
                        continue

                    # Only process tool.end events
                    if entry.get("type") != "tool.end":
                        self._save_cursor(f)
                        continue

                    inputs = entry.get("input")
                    if (not isinstance(inputs, dict) or
                            not isinstance(inputs.get("command"), str) or not inputs["command"] or
                            not isinstance(inputs.get("target"), str) or not inputs["target"] or
                            not isinstance(entry.get("success"), bool)):
                        self._reject_line(line, "incomplete tool.end telemetry")
                        self._save_cursor(f)
                        continue

                    command = entry.get("input", {}).get("command", "")
                    technique = self.normalizer.normalize(command)

                    if self.normalizer.is_unknown(technique):
                        log.info("Unknown technique for command: %s", command[:80])

                    payload = self.build_event(entry, technique, self._source_id(f, offset, line))
                    self.send_event(payload)
                    self._save_cursor(f)

                else:
                    try:
                        current = os.stat(self.telemetry_log)
                        opened = os.fstat(f.fileno())
                        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or current.st_size < f.tell():
                            f.close()
                            f = open(self.telemetry_log, "r")
                            self._save_cursor(f)
                    except OSError:
                        pass
                    # No new line — brief sleep to avoid busy-wait
                    time.sleep(0.1)

        finally:
            f.close()

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
