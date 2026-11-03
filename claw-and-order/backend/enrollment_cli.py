"""Private operator workflow for Cyberlabs; no SMTP or live invitation sending."""

import argparse
import csv
import hmac
import io
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request

from backend.config import ADMIN_SECRET
from backend.roster_input import MAX_BYTES, safe_csv_cell


def request_api(path, method="GET", data=None, raw=None):
    base = os.getenv("CLAW_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed = urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or (
        parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))
    ):
        raise ValueError("CLAW_BACKEND_URL must be an HTTPS origin or a loopback HTTP origin")
    if not ADMIN_SECRET:
        raise ValueError("Set ADMIN_SECRET in the operator's private environment")
    payload = raw if raw is not None else json.dumps(data).encode() if data is not None else None
    req = Request(base + "/api/admin/enrollment" + path, data=payload, method=method,
                  headers={"X-Admin-Secret": ADMIN_SECRET,
                           "Content-Type": "application/octet-stream" if raw is not None else "application/json"})
    # Never follow a redirect carrying an admin credential to another origin.
    from urllib.request import HTTPRedirectHandler, build_opener
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    try:
        with build_opener(NoRedirect()).open(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        try:
            detail = json.loads(error.read(4096)).get("detail", "Request rejected")
        except (ValueError, TypeError):
            detail = "Request rejected"
        raise ValueError(f"HTTP {error.code}: {detail}") from None
    except (URLError, TimeoutError):
        raise ValueError("Backend connection failed") from None


def private_write(path, content):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as output:
        output.write(content)
    return target


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preview = commands.add_parser("preview", help="Read a CSV/XLSX and save a private change preview")
    preview.add_argument("file")
    preview.add_argument("--column-map", help="Private JSON source-header to canonical-header mapping")
    preview.add_argument("--out", required=True)
    apply = commands.add_parser("apply", help="Apply an explicitly reviewed preview digest")
    apply.add_argument("preview")
    apply.add_argument("--confirm-digest", required=True)
    reconcile = commands.add_parser("reconcile", help="Preview role reconciliation after policy/permission repair")
    reconcile.add_argument("--out", required=True)
    rollback = commands.add_parser("rollback", help="Prepare a reviewed restoration of a prior import")
    rollback.add_argument("--batch-id", required=True)
    rollback.add_argument("--out", required=True)
    invites = commands.add_parser("invitations", help="Export private mail-merge records; sends nothing")
    invites.add_argument("--student-id", action="append", required=True)
    invites.add_argument("--ttl-hours", type=int, default=72)
    invites.add_argument("--format", choices=("csv", "json"), default="csv")
    invites.add_argument("--out", required=True)
    for name in ("policy", "students", "audit"):
        command = commands.add_parser(name)
        command.add_argument("--out", required=True)
    commands.add_parser("status")
    args = parser.parse_args(argv)
    if hasattr(args, "out") and Path(args.out).exists():
        raise ValueError("Output already exists; choose a new private filename")
    if args.command == "preview":
        source = Path(args.file)
        if source.stat().st_size > MAX_BYTES:
            raise ValueError("Roster exceeds 10 MiB")
        params = {"filename": source.name}
        if args.column_map:
            params["column_map"] = json.dumps(json.loads(Path(args.column_map).read_text()))
        result = request_api("/imports/file?" + urlencode(params), "POST", raw=source.read_bytes())
    elif args.command == "apply":
        preview = json.loads(Path(args.preview).read_text())
        if not hmac.compare_digest(preview["digest"], args.confirm_digest):
            raise ValueError("Confirmation digest does not match the reviewed preview")
        result = request_api(f"/imports/{preview['batch_id']}/apply", "POST", {"digest": args.confirm_digest})
    elif args.command == "reconcile":
        result = request_api("/reconcile/preview", "POST")
    elif args.command == "rollback":
        result = request_api(f"/imports/{args.batch_id}/rollback-preview", "POST")
    elif args.command == "invitations":
        result = request_api("/invitations", "POST", {"student_ids": args.student_id, "ttl_hours": args.ttl_hours})
        if args.format == "csv":
            fields = ("student_id", "email", "invite_url", "registration_code", "expires_at", "instructions")
            stream = io.StringIO(newline="")
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in result["invitations"]:
                if row["email"].lstrip().startswith(("=", "+", "-", "@")):
                    raise ValueError("An email has a spreadsheet formula prefix; reissue using --format json")
                writer.writerow({key: safe_csv_cell(row[key]) for key in fields})
            private_write(args.out, stream.getvalue())
            print(f"Saved {len(result['invitations'])} private invitation records; no email was sent")
            return
    elif args.command in ("students", "audit"):
        # Read all pages; do not silently omit students after the first 100.
        result, cursor, revision = [], "", None
        while True:
            path = "/students?" + urlencode({"limit": 200, "after_id": cursor}) if args.command == "students" else "/audit?limit=200" + (f"&before_id={cursor}" if cursor else "")
            page = request_api(path)
            if args.command == "students":
                if revision is not None and revision != page["revision"]:
                    raise ValueError("Roster changed during export; retry to obtain a consistent roster")
                revision = page["revision"]
            rows = page["students"] if args.command == "students" else page
            result.extend(rows)
            if len(rows) < 200:
                break
            cursor = page["next_after_id"] if args.command == "students" else rows[-1]["id"]
    else:
        result = request_api("/" + args.command)
    if hasattr(args, "out"):
        private_write(args.out, json.dumps(result, indent=2) + "\n")
        print("Saved private operator output")
        if isinstance(result, dict) and "batch_id" in result:
            print(f"Review batch {result['batch_id']} and its digest before applying")
    else:
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(str(error)) from None
