"""Human reviewed enrollment operations. No command infers a role from free text."""

import argparse
import csv
import hmac
import io
import json
from pathlib import Path
from urllib.parse import urlencode

from backend.enrollment_cli import request_api, private_write
from backend.enrollment_reports import change_report, history_report, cell
from backend.roster_input import MAX_BYTES, safe_csv_cell


def context_options(command):
    command.add_argument("--requested-by", required=True, help="Staff or agent identifier recorded with this request")
    command.add_argument("--reason", required=True, help="Plain explanation of why the assignment changes")
    command.add_argument("--ticket", default="", help="Optional institutional change reference")


def read_pages(kind, student_id=None):
    result, cursor, revision = [], "", None
    while True:
        params = {"limit": 200}
        if student_id:
            params["student_id"] = student_id
        if cursor:
            params["after_id" if kind == "students" else "before_id"] = cursor
        page = request_api("/" + kind + "?" + urlencode(params))
        if kind == "students":
            if revision is not None and revision != page["revision"]:
                raise ValueError("Roster changed during export; retry for a consistent view")
            revision = page["revision"]
        rows = page["students"] if kind == "students" else page
        result.extend(rows)
        if len(rows) < 200:
            return result
        cursor = page["next_after_id"] if kind == "students" else rows[-1]["id"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Validate CSV/XLSX and prepare JSON plus human report")
    preview.add_argument("file")
    preview.add_argument("--column-map")
    change = commands.add_parser("change", help="Preview one student's team, responsibility or enrollment status change")
    change.add_argument("--student-id", required=True)
    change.add_argument("--term-id", required=True)
    change.add_argument("--class-id", required=True)
    team = change.add_mutually_exclusive_group()
    team.add_argument("--team-id")
    team.add_argument("--unassign", action="store_true")
    duty = change.add_mutually_exclusive_group()
    duty.add_argument("--role-key")
    duty.add_argument("--clear-role", action="store_true")
    change.add_argument("--status", choices=("active", "withdrawn"))
    reconcile = commands.add_parser("reconcile", help="Preview all current and formerly managed access")
    reconcile.add_argument("--student-id", action="append", help="Optional reviewed subset; repeat to batch a large reconciliation")
    rollback = commands.add_parser("rollback", help="Preview restoration; refuses later conflicting changes")
    rollback.add_argument("--batch-id", required=True)
    for cmd in (preview, change, reconcile, rollback):
        context_options(cmd)
        cmd.add_argument("--out", required=True)
    apply = commands.add_parser("apply", help="Human administrator approves the exact server-stored preview")
    apply.add_argument("preview")
    apply.add_argument("--confirm-digest", required=True)
    apply.add_argument("--approved-by", required=True)
    apply.add_argument("--out", required=True)
    for name in ("report", "verify"):
        cmd = commands.add_parser(name, help="Save an authoritative Markdown change/verification report")
        cmd.add_argument("--batch-id", required=True)
        cmd.add_argument("--out", required=True)
    history = commands.add_parser("history", help="Show one student's human-readable change history")
    history.add_argument("--student-id", required=True)
    history.add_argument("--out", required=True)
    invites = commands.add_parser("invitations", help="Human administrator exports private codes; sends no email")
    invites.add_argument("--student-id", action="append", required=True)
    invites.add_argument("--ttl-hours", type=int, default=72)
    invites.add_argument("--format", choices=("csv", "json"), default="json")
    invites.add_argument("--out", required=True)
    for name in ("policy", "students", "audit", "status"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if Path(args.out).exists() or Path(args.out + ".md").exists():
        raise ValueError("Output/report already exists; choose a new private filename")
    context = {k: getattr(args, k) for k in ("requested_by", "reason", "ticket")} if hasattr(args, "requested_by") else None
    try:
        if args.command == "preview":
            source = Path(args.file)
            if source.stat().st_size > MAX_BYTES:
                raise ValueError("Roster exceeds 10 MiB")
            params = {"filename": source.name}
            if args.column_map:
                params["column_map"] = json.dumps(json.loads(Path(args.column_map).read_text()))
            result = request_api("/imports/file?" + urlencode(params), "POST", raw=source.read_bytes(),
                                 headers={"X-Enrollment-Change": json.dumps(context)})
        elif args.command == "change":
            if args.team_id is None and not args.unassign and args.role_key is None and not args.clear_role and args.status is None:
                raise ValueError("Select a team, unassign, responsibility or status change")
            source_page = request_api("/students?" + urlencode({"student_id": args.student_id, "limit": 1}))
            people = source_page["students"]
            if len(people) != 1:
                raise ValueError("Student not found; import the reviewed enrollment first")
            student = people[0]
            matches = [e for e in student["enrollments"] if e["term_id"] == args.term_id and e["class_id"] == args.class_id]
            if len(matches) != 1:
                raise ValueError("Student has no enrollment in that term/class")
            row = {k: matches[0][k] for k in ("term_id", "class_id", "team_id", "role_key", "idea", "status")}
            row.update({k: student[k] for k in ("student_id", "email", "display_name")})
            if args.team_id is not None or args.unassign:
                row["team_id"] = args.team_id or ""
            if args.role_key is not None or args.clear_role:
                row["role_key"] = args.role_key or ""
            if args.status:
                row["status"] = args.status
            result = request_api("/imports", "POST", {"rows": [row], "context": context, "expected_revision": source_page["revision"]})
        elif args.command == "apply":
            preview = json.loads(Path(args.preview).read_text())
            server = request_api(f"/imports/{preview['batch_id']}/review")
            if not hmac.compare_digest(server["digest"], args.confirm_digest) or not hmac.compare_digest(preview["digest"], args.confirm_digest):
                raise ValueError("Digest differs from the immutable server preview; save and review a fresh report")
            for key in ("changes", "role_preview", "context", "policy_hash", "base_revision"):
                if preview.get(key) != server.get(key):
                    raise ValueError("Local preview differs from server; fetch and review the authoritative report")
            request_api(f"/imports/{preview['batch_id']}/apply", "POST", {"digest": args.confirm_digest, "approved_by": args.approved_by})
            result = request_api(f"/imports/{preview['batch_id']}/review")
        elif args.command in ("reconcile", "rollback"):
            path = "/reconcile/preview" if args.command == "reconcile" else f"/imports/{args.batch_id}/rollback-preview"
            result = request_api(path, "POST", {**context, **({"student_ids": args.student_id} if args.command == "reconcile" and args.student_id else {})})
        elif args.command in ("report", "verify"):
            result = request_api(f"/imports/{args.batch_id}/review")
            private_write(args.out, change_report(result))
            print(f"Saved private report. Outcome: {result['verification']['outcome']}")
            if args.command == "report" or result["verification"]["outcome"] == "VERIFIED":
                return 0
            return 3 if result["verification"]["outcome"] in ("WAITING", "NOT APPLIED") else 4
        elif args.command == "history":
            private_write(args.out, history_report(args.student_id, read_pages("audit", args.student_id)))
            print("Saved private student change history")
            return
        elif args.command == "invitations":
            result = request_api("/invitations", "POST", {"student_ids": args.student_id, "ttl_hours": args.ttl_hours})
            if args.format == "csv":
                fields = ("student_id", "email", "invite_url", "registration_code", "expires_at", "instructions")
                stream = io.StringIO(newline="")
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                for row in result["invitations"]:
                    if row["email"].lstrip().startswith(("=", "+", "-", "@")):
                        raise ValueError("Email has a formula prefix; reissue with JSON")
                    writer.writerow({key: safe_csv_cell(row[key]) for key in fields})
                private_write(args.out, stream.getvalue())
                print(f"Saved {len(result['invitations'])} private invitations; no email sent")
                return
        elif args.command in ("students", "audit"):
            result = read_pages(args.command)
        else:
            result = request_api("/" + args.command)
        private_write(args.out, json.dumps(result, indent=2) + "\n")
        if isinstance(result, dict) and "batch_id" in result:
            private_write(args.out + ".md", change_report(result))
            print(f"Saved private JSON and Markdown for change {result['batch_id']}. Read the report before approval; application does not prove Discord completion.")
        elif args.command == "status":
            lines = ["# Claw & Order enrollment status", "", f"**State: {cell(result['status'])}**", "", f"Worker: {cell(result.get('worker'))}", "", "| Queue state | Students |", "| --- | --- |"]
            lines.extend(f"| {cell(k)} | {v} |" for k, v in result.get("queue", {}).items())
            lines += ["", "For a waiting or blocked student, inspect their history and change verification report. Do not delete queued jobs to clear an alarm.", ""]
            private_write(args.out + ".md", "\n".join(lines))
            print(f"Enrollment state: {result['status']}. Saved private JSON and readable report.")
            return 0 if result["status"] == "healthy" else 3
        else:
            print("Saved private operator output; no email sent")
    except (ValueError, OSError, KeyError) as error:
        # Never place invitation data or secrets in failure reports.
        message = str(error) if isinstance(error, ValueError) and args.command != "invitations" else "Operation failed; inspect private inputs, permissions and connectivity. No success is claimed."
        failure = "# Claw & Order operation report\n\n**Outcome: FAILED OR UNCONFIRMED**\n\n" + cell(message) + "\n\nPreserve the source, change ID, queues and receipts. If a request may have reached the server, inspect the authoritative change report before retrying.\n"
        target = args.out + ".failure.md"
        if not Path(target).exists():
            private_write(target, failure)
        raise ValueError("Operation failed or could not be confirmed; read the private failure report") from None
