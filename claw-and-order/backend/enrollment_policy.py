"""Explicit server, class, team and student-role mappings. No inferred privileges."""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
SNOWFLAKE = re.compile(r"[0-9]{15,22}\Z")


class EnrollmentError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def identifier(value):
    return isinstance(value, str) and bool(ID.fullmatch(value))


def snowflake(value):
    if not isinstance(value, str) or not SNOWFLAKE.fullmatch(value):
        raise EnrollmentError("Discord IDs must be text containing 15–22 digits")
    return value


@dataclass(frozen=True)
class EnrollmentPolicy:
    data: dict
    digest: str

    @property
    def guild_id(self):
        return self.data["guild_id"]

    @property
    def classes(self):
        return {(c["term_id"], c["class_id"]): c for c in self.data["classes"]}

    @property
    def managed_roles(self):
        result = set()
        for c in self.data["classes"]:
            result.add(c["discord_role_id"])
            result.update(t["discord_role_id"] for t in c["teams"].values())
            result.update(c["student_roles"].values())
        return result


def parse_policy(data):
    if not isinstance(data, dict) or set(data) != {"version", "guild_id", "invite_url", "classes", "role_definitions"} or data["version"] != 1:
        raise EnrollmentError("Enrollment policy requires version 1, guild_id, invite_url, classes and role_definitions")
    snowflake(data["guild_id"])
    try:
        invite = urlsplit(data["invite_url"]) if isinstance(data["invite_url"], str) else None
        valid_invite = invite and invite.scheme == "https" and invite.hostname in ("discord.gg", "discord.com") and not (
            invite.query or invite.fragment or invite.username or invite.password or invite.port)
    except ValueError:
        valid_invite = False
    if not valid_invite:
        raise EnrollmentError("Enrollment invite_url must be a Discord HTTPS invite without query parameters")
    if not re.fullmatch(r"/[A-Za-z0-9_-]+", invite.path) and not (invite.hostname == "discord.com" and re.fullmatch(r"/invite/[A-Za-z0-9_-]+", invite.path)):
        raise EnrollmentError("Invalid Discord invite path")
    definitions = data["role_definitions"]
    if not isinstance(definitions, dict):
        raise EnrollmentError("role_definitions must be an object; leave it empty until roles are defined")
    for key, definition in definitions.items():
        if not identifier(key) or not isinstance(definition, dict) or set(definition) != {"label", "purpose", "approved"}:
            raise EnrollmentError("Each student role needs a stable key, label, purpose and approved flag")
        if not isinstance(definition["approved"], bool) or any(not isinstance(definition[k], str) or not definition[k].strip() or len(definition[k]) > 1000 for k in ("label", "purpose")):
            raise EnrollmentError("Invalid student role definition")
    if not isinstance(data["classes"], list) or not data["classes"]:
        raise EnrollmentError("At least one class mapping is required")
    classes, teams, roles, channels = set(), set(), set(), set()
    def role(value):
        snowflake(value)
        if value == data["guild_id"] or value in roles:
            raise EnrollmentError("Every managed class/team/student role needs a distinct ID; @everyone is forbidden")
        roles.add(value)
    for c in data["classes"]:
        if not isinstance(c, dict) or set(c) != {"term_id", "class_id", "discord_role_id", "teams", "student_roles"}:
            raise EnrollmentError("Invalid class mapping fields")
        if not identifier(c["term_id"]) or not identifier(c["class_id"]):
            raise EnrollmentError("Class and term IDs must contain 1–64 letters, numbers, _ or -")
        key = (c["term_id"], c["class_id"])
        if key in classes:
            raise EnrollmentError("Duplicate term/class mapping")
        classes.add(key)
        role(c["discord_role_id"])
        if not isinstance(c["teams"], dict) or not isinstance(c["student_roles"], dict):
            raise EnrollmentError("teams and student_roles must be objects")
        for team_id, t in c["teams"].items():
            if not identifier(team_id) or team_id in teams or not isinstance(t, dict) or set(t) != {"discord_role_id", "channel_ids"}:
                raise EnrollmentError("A scoring team must belong to exactly one class and have a role and private channel_ids")
            teams.add(team_id)
            role(t["discord_role_id"])
            if not isinstance(t["channel_ids"], list) or not t["channel_ids"]:
                raise EnrollmentError("Every team needs at least one private channel for isolation verification")
            for channel in t["channel_ids"]:
                snowflake(channel)
                if channel in channels:
                    raise EnrollmentError("A private channel cannot belong to multiple teams")
                channels.add(channel)
        for key, value in c["student_roles"].items():
            if key not in definitions:
                raise EnrollmentError("student_roles contains a key absent from role_definitions")
            role(value)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return EnrollmentPolicy(json.loads(canonical), hashlib.sha256(canonical.encode()).hexdigest())


def load_policy():
    path = os.getenv("ENROLLMENT_POLICY_FILE")
    if not path:
        raise EnrollmentError("Set ENROLLMENT_POLICY_FILE after selecting the server and role mappings", 503)
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise EnrollmentError("Enrollment policy file is unreadable or invalid JSON", 503) from None
    return parse_policy(data)
