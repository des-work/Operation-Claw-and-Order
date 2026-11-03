"""Invitation preparation and retry behavior; SMTP is always replaced with a fake."""

import copy
import json
import os
import smtplib
from datetime import datetime, timedelta, timezone

import pytest

from backend.enrollment_cli import private_write
from backend.enrollment_mailer import build_plan, plan_digest, send_pass, private_read, smtp_config


def test_mailer_template_defaults_and_private_file_validation(monkeypatch, tmp_path):
    monkeypatch.setenv("ENROLLMENT_SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("ENROLLMENT_SMTP_FROM", "instructor@college.example")
    monkeypatch.setenv("ENROLLMENT_SMTP_PORT", "")
    monkeypatch.setenv("ENROLLMENT_SMTP_TLS", "ssl")
    assert smtp_config()["port"] == 465
    monkeypatch.setenv("ENROLLMENT_SMTP_TLS", "starttls")
    assert smtp_config()["port"] == 587
    monkeypatch.setenv("ENROLLMENT_SMTP_TLS", "plain")
    with pytest.raises(ValueError, match="transport"):
        smtp_config()
    private_dir = tmp_path / "directory"
    private_dir.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="regular private"):
        private_read(private_dir)


def plan():
    return build_plan([{"email": "student@college.example", "registration_code": "CLAW-" + "a" * 43,
        "invite_url": "https://discord.gg/test-only", "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}],
        {"host": "smtp.example.invalid", "port": 465, "mode": "ssl", "from": "instructor@college.example"})


class SMTP:
    def __init__(self):
        self.messages = []
        self.fail = False
    def send_message(self, message):
        if self.fail:
            raise OSError("synthetic outage")
        self.messages.append(message)
        return {}
    def close(self):
        pass


def test_private_mail_plan_preparation_receipts_and_no_repeat(tmp_path):
    value = plan()
    target = private_write(tmp_path / "invitations.json", json.dumps(value))
    assert target.stat().st_mode & 0o777 == 0o600
    assert json.loads(private_read(target))["digest"] == plan_digest(value)
    smtp, saves = SMTP(), []
    assert send_pass(value, lambda v: saves.append(copy.deepcopy(v)), verify=lambda _: True, connector=lambda _: smtp) == 1
    assert value["records"][0]["status"] == "sent" and saves
    assert send_pass(value, lambda _: None, verify=lambda _: True, connector=lambda _: smtp) == 0
    assert len(smtp.messages) == 1
    assert smtp.messages[0]["Message-ID"] == value["records"][0]["message_id"]


def test_mail_outage_retains_plan_and_retry_succeeds():
    value, smtp = plan(), SMTP()
    smtp.fail = True
    send_pass(value, lambda _: None, verify=lambda _: True, connector=lambda _: smtp)
    assert value["records"][0]["status"] == "retry"
    smtp.fail = False
    value["records"][0]["next_attempt"] = 0
    send_pass(value, lambda _: None, verify=lambda _: True, connector=lambda _: smtp)
    assert value["records"][0]["status"] == "sent" and len(smtp.messages) == 1


def test_invalid_or_expired_codes_never_reach_smtp():
    for expired in (True, False):
        value = plan()
        if expired:
            value["records"][0]["expires_at"] = 0
        def forbidden(_):
            pytest.fail("Invalid invitation must not open an SMTP connection")
        send_pass(value, lambda _: None, verify=lambda _: False, connector=forbidden)
        assert value["records"][0]["status"] in ("expired", "cancelled")


def test_authentication_or_backend_failure_pauses_and_tampering_changes_digest():
    value = plan()
    def auth_failure(_):
        raise smtplib.SMTPAuthenticationError(535, b"test-only")
    send_pass(value, lambda _: None, verify=lambda _: True, connector=auth_failure)
    assert value["paused_reason"] and value["records"][0]["status"] == "pending"
    altered = copy.deepcopy(value)
    altered["records"][0]["to"] = "another@college.example"
    assert plan_digest(altered) != value["digest"]
    value["paused_reason"] = None
    def backend_failure(_):
        raise OSError("unavailable")
    send_pass(value, lambda _: None, verify=backend_failure, connector=auth_failure)
    assert value["paused_reason"] == "backend_verification_unavailable"
