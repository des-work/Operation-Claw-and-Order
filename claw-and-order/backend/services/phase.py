"""Phase configuration loader and technique validator."""

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger("phase")

_PHASES_PATH = Path(__file__).parent.parent.parent / "phase_config" / "phases.json"

_phases: list[dict[str, Any]] = []
_current_phase: int = 1


def load_phases() -> None:
    """Load (or reload) phases.json from disk."""
    global _phases
    if not _PHASES_PATH.exists():
        log.error(
            "phases.json not found at %s — phase enforcement will reject all techniques. "
            "Create phase_config/phases.json to fix this.",
            _PHASES_PATH,
        )
        _phases = []
        return
    try:
        raw = json.loads(_PHASES_PATH.read_text())
        _phases = raw["phases"]
        log.info("Loaded %d phases from %s", len(_phases), _PHASES_PATH)
    except json.JSONDecodeError as e:
        log.error("phases.json is malformed JSON: %s", e)
        _phases = []
    except KeyError:
        log.error("phases.json missing required 'phases' key")
        _phases = []


def get_current_phase() -> int:
    return _current_phase


def set_current_phase(phase: int) -> None:
    global _current_phase
    _current_phase = phase


def get_phase_config(phase: int | None = None) -> dict[str, Any] | None:
    """Return the config dict for a given phase number."""
    p = phase if phase is not None else _current_phase
    for cfg in _phases:
        if cfg["phase"] == p:
            return cfg
    return None


def get_all_phases() -> list[dict[str, Any]]:
    return list(_phases)


def is_technique_allowed(technique: str, phase: int | None = None) -> bool:
    """Check if a technique is allowed in the given (or current) phase."""
    cfg = get_phase_config(phase)
    if cfg is None:
        return False
    return technique in cfg["allowed_techniques"]


# Load on import
load_phases()
