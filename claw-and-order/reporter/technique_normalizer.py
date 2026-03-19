"""
technique_normalizer.py — single source of truth for command-to-technique mapping.

Translates raw OpenClaw tool commands into normalized technique names.
Loaded once at reporter startup. No imports beyond stdlib.

Used by: reporter.py (imports this module directly)
"""

import json
from pathlib import Path


class TechniqueNormalizer:
    """
    Loads technique_map.json and maps raw command strings to normalized names.

    Matching rules:
      1. Sort patterns by length of match string descending (longest wins).
         This ensures "nmap -sV" matches as service_fingerprint before
         the shorter "nmap" matches as port_scan.
      2. Check each pattern's 'matches' list against the command string
         (case-insensitive substring).
      3. First hit wins. Return fallback ('unknown') if nothing matches.
    """

    def __init__(self, map_path: str = "technique_map.json"):
        raw = json.loads(Path(map_path).read_text())
        self._version = raw.get("_version", "0")
        self._fallback = raw["fallback"]

        # Build a flat list of (match_string, technique_id) sorted longest-first
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
        """Return True if the technique is the fallback (unknown) value."""
        return technique == self._fallback

    @property
    def fallback(self) -> str:
        """The fallback technique name (usually 'unknown')."""
        return self._fallback

    @property
    def rule_count(self) -> int:
        """Number of match rules loaded."""
        return len(self._rules)


# ── Self-tests (run directly: python technique_normalizer.py) ─────────────────

if __name__ == "__main__":
    import sys

    norm = TechniqueNormalizer("technique_map.json")
    print(f"Loaded {norm.rule_count} rules, fallback='{norm.fallback}'\n")

    cases = [
        # (command, expected_technique)
        ("nmap -sV 10.0.1.100",                             "service_fingerprint"),
        ("nmap 10.0.1.100",                                  "port_scan"),
        ("nmap -sV -p 22,80 10.0.1.100",                    "service_fingerprint"),
        ("sqlmap -u http://10.0.1.100/login",                "sqli"),
        ("hydra -L users.txt -P pass.txt ssh://10.0.1.100", "ssh_brute"),
        ("ssh admin@10.0.1.100",                             "default_creds"),
        ("impacket-secretsdump domain/admin:pass@10.0.2.10", "hash_dump"),
        ("bloodhound-python -d domain.local",                "ad_escalation"),
        ("cat /flag.txt",                                    "data_exfil"),
        ("some_unknown_tool --flag",                         "unknown"),
        ("NMAP -SV 10.0.1.100",                             "service_fingerprint"),  # case insensitive
        ("crackmapexec smb 10.0.2.0/24 -u admin",           "smb_enum"),  # "crackmapexec smb" is longer than "crackmapexec"
        ("smbclient //10.0.2.50/share",                      "smb_enum"),
        ("",                                                  "unknown"),  # empty command
    ]

    passed = 0
    failed = 0
    for cmd, expected in cases:
        result = norm.normalize(cmd)
        ok = result == expected
        status = "PASS" if ok else "FAIL"
        passed += ok
        failed += (not ok)
        label = cmd[:55] if cmd else "(empty)"
        print(f"  {status}  '{label}' -> {result}" + (f" (expected {expected})" if not ok else ""))

    # Also test is_unknown
    assert norm.is_unknown("unknown") is True
    assert norm.is_unknown("port_scan") is False
    print(f"\n  PASS  is_unknown('unknown') -> True")
    print(f"  PASS  is_unknown('port_scan') -> False")

    total = passed + failed + 2  # +2 for is_unknown tests
    print(f"\n{total}/{total} tests passed")
    sys.exit(0 if failed == 0 else 1)
