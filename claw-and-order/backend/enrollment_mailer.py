"""Reviewed invitation emails with private receipts. No mail is sent by default."""

import argparse
import csv
import fcntl
import hashlib
import hmac
import json
import os
import re
import smtplib
import ssl
import stat
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path

from backend.enrollment_cli import private_write, request_api
from backend.enrollment_reports import mail_report

IMMUTABLE = ("id", "to", "subject", "body", "expires_at", "message_id", "invite_url")


def smtp_config():
    host = os.getenv("ENROLLMENT_SMTP_HOST", "")
    sender = os.getenv("ENROLLMENT_SMTP_FROM", "")
    mode = os.getenv("ENROLLMENT_SMTP_TLS", "ssl")
    if not host or any(c.isspace() for c in host) or any(c in sender for c in "\r\n") or parseaddr(sender)[1] != sender or "@" not in sender:
        raise ValueError("Configure ENROLLMENT_SMTP_HOST and a plain ENROLLMENT_SMTP_FROM email address")
    if mode not in ("ssl", "starttls"):
        raise ValueError("SMTP transport must use ssl or starttls with certificate verification")
    port = int(os.getenv("ENROLLMENT_SMTP_PORT") or ("465" if mode == "ssl" else "587"))
    if not 1 <= port <= 65535:
        raise ValueError("Invalid SMTP port")
    return {"host": host, "port": port, "mode": mode, "from": sender}


def private_read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Invitation files must be regular private files owned by this operator with mode 0600")
        if info.st_size > 20 * 1024 * 1024:
            raise ValueError("Invitation file exceeds 20 MiB")
        source = os.fdopen(fd, "r", encoding="utf-8")
        fd = None
        with source:
            return source.read()
    finally:
        if fd is not None:
            os.close(fd)


def plan_digest(plan):
    immutable = {"version": plan["version"], "smtp": plan["smtp"],
                 "records": [{key: row[key] for key in IMMUTABLE} for row in plan["records"]]}
    return hashlib.sha256(json.dumps(immutable, sort_keys=True).encode()).hexdigest()


def build_plan(records, config):
    if not isinstance(records, list) or not 1 <= len(records) <= 5000:
        raise ValueError("Invitation export must have 1–5,000 rows")
    plan = {"version": 1, "smtp": config, "records": [], "paused_reason": None}
    seen = set()
    for row in records:
        address, code = row["email"], row["registration_code"]
        if not isinstance(address, str) or any(c.isspace() for c in address) or parseaddr(address)[1] != address or "@" not in address:
            raise ValueError("Invitation export contains an invalid recipient")
        if not isinstance(code, str) or not re.fullmatch(r"CLAW-[A-Za-z0-9_-]{43}", code):
            raise ValueError("Invitation export contains an invalid registration code")
        if any(c in str(row["invite_url"]) for c in "\r\n") or not str(row["invite_url"]).startswith(("https://discord.gg/", "https://discord.com/invite/")):
            raise ValueError("Invitation export contains an invalid invite URL")
        expiration = datetime.fromisoformat(row["expires_at"])
        if expiration.tzinfo is None:
            raise ValueError("Invitation expiration must include its time zone")
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        if code_hash in seen:
            raise ValueError("Duplicate invitation code in export")
        seen.add(code_hash)
        body = (f"You are invited to Claw & Order.\n\nJoin the Discord server: {row['invite_url']}\n\n"
                f"In that server, run /register and enter your personal code: {code}\n"
                f"This code expires at {expiration.astimezone(timezone.utc).isoformat()}.\n\n"
                "Keep the code private. Your approved class and team assignments will be applied after registration. "
                "If you are awaiting team placement, an instructor can update your assignment later.\n")
        plan["records"].append({"id": code_hash, "to": address, "invite_url": row["invite_url"], "subject": "Your Claw & Order Discord invitation",
            "body": body, "expires_at": expiration.timestamp(),
            "message_id": f"<claw-invite-{code_hash}@{config['from'].split('@', 1)[1]}>",
            "status": "pending", "attempts": 0, "next_attempt": 0, "sent_at": None, "last_error": None})
    plan["digest"] = plan_digest(plan)
    return plan


def connect(config):
    context = ssl.create_default_context()
    client = smtplib.SMTP_SSL(config["host"], config["port"], timeout=15, context=context) if config["mode"] == "ssl" else smtplib.SMTP(config["host"], config["port"], timeout=15)
    try:
        if config["mode"] == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        user, password = os.getenv("ENROLLMENT_SMTP_USER"), os.getenv("ENROLLMENT_SMTP_PASSWORD")
        if bool(user) != bool(password):
            raise ValueError("SMTP user and password must be configured together")
        if user:
            client.login(user, password)
        return client
    except Exception:
        client.close()
        raise


def send_pass(plan, save, verify=None, connector=connect, max_messages=50):
    if plan.get("paused_reason"):
        return 0
    verify = verify or (lambda row: request_api("/invitations/check", "POST", {"entries": [{"code_hash": row["id"], "email": row["to"], "invite_url": row["invite_url"]}]}).get(row["id"]) == "valid")
    attempted = 0
    client = None
    try:
        for row in plan["records"]:
            if attempted >= max_messages:
                break
            if row["status"] not in ("pending", "retry") or row["next_attempt"] > time.time():
                continue
            if row["expires_at"] <= time.time():
                row["status"], row["last_error"] = "expired", "registration_code_expired"
                save(plan)
                continue
            try:
                valid = verify(row)
            except Exception:
                plan["paused_reason"] = "backend_verification_unavailable"
                save(plan)
                break
            if not valid:
                row["status"], row["last_error"] = "cancelled", "recipient_or_code_no_longer_valid"
                save(plan)
                continue
            attempted += 1
            try:
                if client is None:
                    client = connector(plan["smtp"])
                message = EmailMessage()
                message["From"], message["To"], message["Subject"] = plan["smtp"]["from"], row["to"], row["subject"]
                message["Message-ID"] = row["message_id"]
                message.set_content(row["body"])
                refused = client.send_message(message)
                if refused:
                    raise smtplib.SMTPRecipientsRefused(refused)
                row["status"], row["last_error"], row["sent_at"] = "sent", None, time.time()
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPNotSupportedError, ssl.SSLCertVerificationError, ValueError):
                plan["paused_reason"] = "smtp_authentication_tls_or_configuration_failed"
                save(plan)
                break
            except smtplib.SMTPRecipientsRefused as error:
                row["attempts"] += 1
                transient = all(400 <= value[0] < 500 for value in error.recipients.values())
                row["status"], row["last_error"] = ("retry" if transient else "failed"), "smtp_recipient_rejected"
                row["next_attempt"] = time.time() + min(2 ** min(row["attempts"], 8), 300)
            except smtplib.SMTPResponseException as error:
                row["attempts"] += 1
                row["status"] = "failed" if error.smtp_code >= 500 else "retry"
                row["last_error"] = f"smtp_{error.smtp_code}"
                row["next_attempt"] = time.time() + min(2 ** min(row["attempts"], 8), 300)
            except (OSError, smtplib.SMTPException):
                row["attempts"] += 1
                row["status"], row["last_error"] = "retry", "smtp_transport_unavailable"
                row["next_attempt"] = time.time() + min(2 ** min(row["attempts"], 8), 300)
                if client:
                    client.close()
                client = None
                save(plan)
                break  # A dead transport must not be hammered for every recipient.
            save(plan)
    finally:
        if client:
            client.close()
    return attempted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("plan")
    prepare.add_argument("invitations")
    prepare.add_argument("--out", required=True)
    send = commands.add_parser("send")
    send.add_argument("plan")
    send.add_argument("--confirm-digest", required=True)
    send.add_argument("--run", action="store_true", help="Retry pending messages until drained or paused")
    send.add_argument("--resume", action="store_true", help="Explicitly resume after repairing a paused dependency")
    args = parser.parse_args()
    if args.command == "plan":
        source = private_read(args.invitations)
        records = list(csv.DictReader(source.splitlines())) if args.invitations.lower().endswith(".csv") else json.loads(source)["invitations"]
        plan = build_plan(records, smtp_config())
        private_write(args.out, json.dumps(plan, indent=2) + "\n")
        private_write(args.out + ".md", mail_report(plan))
        print(f"Prepared {len(records)} emails; none sent. Review the private plan. Confirmation digest: {plan['digest']}")
        return
    plan_path = Path(args.plan)
    plan = json.loads(private_read(plan_path))
    if not hmac.compare_digest(plan_digest(plan), args.confirm_digest) or plan["smtp"] != smtp_config():
        raise ValueError("Plan contents, confirmation digest or SMTP destination changed; review a new plan")
    lock = os.open(str(plan_path) + ".lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("Delivery lock must be a private regular file owned by this operator")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Re-read after taking the lock so two processes cannot resend old state.
        plan = json.loads(private_read(plan_path))
        if not hmac.compare_digest(plan_digest(plan), args.confirm_digest):
            raise ValueError("Plan changed while waiting for the delivery lock")
        if args.resume:
            plan["paused_reason"] = None
        def save(value):
            temporary = plan_path.with_name(plan_path.name + f".tmp.{os.getpid()}")
            private_write(temporary, json.dumps(value, indent=2) + "\n")
            os.replace(temporary, plan_path)
            directory = os.open(plan_path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        while True:
            send_pass(plan, save)
            report_path = str(plan_path) + ".md"
            report_tmp = report_path + f".tmp.{os.getpid()}"
            private_write(report_tmp, mail_report(plan))
            os.replace(report_tmp, report_path)
            print(json.dumps({"counts": {state: sum(row["status"] == state for row in plan["records"]) for state in ("pending", "retry", "sent", "failed", "expired", "cancelled")}, "paused": plan["paused_reason"]}))
            if not args.run or plan["paused_reason"] or not any(row["status"] in ("pending", "retry") for row in plan["records"]):
                break
            time.sleep(5)
    finally:
        os.close(lock)
    return 0 if all(row["status"] == "sent" for row in plan["records"]) else 3


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(type(error).__name__ + ": invitation operation could not be completed; check private configuration and file permissions") from None
