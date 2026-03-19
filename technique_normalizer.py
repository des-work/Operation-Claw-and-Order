"""
technique_normalizer.py
Translates raw OpenClaw tool commands into normalized technique names.
Loaded once at reporter startup. No imports beyond stdlib.
"""

import json
import re
from pathlib import Path


class TechniqueNormalizer:
    """
    Loads technique_map.json and maps raw command strings to normalized names.
    Matching rules:
      1. Sort patterns by length of match string descending (longest wins).
      2. Check each pattern's 'matches' list against the command string (case-insensitive substring).
      3. First hit wins. Return 'unknown' if nothing matches.
    """

    def __init__(self, map_path: str = "technique_map.json"):
        raw = json.loads(Path(map_path).read_text())
        self._version = raw["_version"]
        self._fallback = raw["fallback"]

        # Build a flat list of (match_string, technique_id) sorted longest-first
        # so more specific matches like "nmap -sV" beat generic "nmap"
        self._rules: list[tuple[str, str]] = []
        for pattern in raw["patterns"]:
            for match_str in pattern["matches"]:
                self._rules.append((match_str.lower(), pattern["id"]))

        self._rules.sort(key=lambda r: len(r[0]), reverse=True)

    def normalize(self, command: str) -> str:
        """Return the normalized technique id for a raw command string."""
        cmd_lower = command.lower().strip()
        for match_str, technique_id in self._rules:
            if match_str in cmd_lower:
                return technique_id
        return self._fallback

    def is_unknown(self, technique: str) -> bool:
        return technique == self._fallback


# ── Tests (run directly: python technique_normalizer.py) ─────────────────────

if __name__ == "__main__":
    import sys

    norm = TechniqueNormalizer("technique_map.json")

    cases = [
        ("nmap -sV 10.0.1.100",                        "service_fingerprint"),
        ("nmap 10.0.1.100",                             "port_scan"),
        ("nmap -sV -p 22,80 10.0.1.100",               "service_fingerprint"),
        ("sqlmap -u http://10.0.1.100/login",           "sqli"),
        ("hydra -L users.txt -P pass.txt ssh://10.0.1.100", "ssh_brute"),
        ("ssh admin@10.0.1.100",                        "default_creds"),
        ("impacket-secretsdump domain/admin:pass@10.0.2.10", "hash_dump"),
        ("bloodhound-python -d domain.local",           "ad_escalation"),
        ("cat /flag.txt",                               "data_exfil"),
        ("some_unknown_tool --flag",                    "unknown"),
        ("NMAP -SV 10.0.1.100",                        "service_fingerprint"),  # case insensitive
    ]

    passed = 0
    failed = 0
    for cmd, expected in cases:
        result = norm.normalize(cmd)
        status = "PASS" if result == expected else "FAIL"
        if status == "PASS":
            passed += 1
        else:
            failed += 1
        print(f"  {status}  '{cmd[:50]}' -> {result} (expected {expected})")

    print(f"\n{passed}/{passed+failed} tests passed")
    sys.exit(0 if failed == 0 else 1)
