"""Private operator workflow for Cyberlabs; no SMTP or live invitation sending."""

import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from backend.config import ADMIN_SECRET


def request_api(path, method="GET", data=None, raw=None, headers=None):
    base = os.getenv("CLAW_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
    parsed = urlsplit(base)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/") or (
        parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1"))
    ):
        raise ValueError("CLAW_BACKEND_URL must be an HTTPS origin or a loopback HTTP origin")
    planner = os.getenv("ENROLLMENT_PLANNER_SECRET", "")
    if not ADMIN_SECRET and not planner:
        raise ValueError("Set the planner credential for Hermes or ADMIN_SECRET for the human administrator")
    payload = raw if raw is not None else json.dumps(data).encode() if data is not None else None
    req = Request(base + "/api/admin/enrollment" + path, data=payload, method=method,
                  headers={**({"X-Enrollment-Planner-Secret": planner} if planner else {"X-Admin-Secret": ADMIN_SECRET}),
                           **(headers or {}),
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
        output.flush()
        os.fsync(output.fileno())
    return target


def main(argv=None):
    from backend.enrollment_ops import main as operations
    return operations(argv)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, KeyError) as error:
        raise SystemExit(str(error)) from None
