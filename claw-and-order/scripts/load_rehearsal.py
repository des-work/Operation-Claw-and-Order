"""Write synthetic, non-scoring events to a staging backend at a fixed rate.

Example: python scripts/load_rehearsal.py --url http://127.0.0.1:8000 \
  --team-id test-team --token test-token --target-ip 10.50.1.100 \
  --rate 20 --duration 60 --staging-write
"""

import argparse
import json
import math
import sys
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit


def send(url, token, team_id, target_ip, source_id):
    origin = urlsplit(url)
    if origin.username or origin.password or origin.query or origin.fragment or origin.path not in ("", "/") or (
        origin.scheme != "https" and not (origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1", "::1"))
    ):
        raise ValueError("Rehearsal target must be an HTTPS origin or loopback HTTP origin")
    payload = json.dumps({
        "team_id": team_id, "command": f"load-rehearsal {target_ip}",
        "technique": "port_scan", "target_ip": target_ip,
        "result": "failure", "source_id": source_id,
    }).encode()
    request = Request(f"{url.rstrip('/')}/api/events", data=payload, method="POST",
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    start = time.monotonic()
    try:
        with urlopen(request, timeout=10) as response:
            return response.status, time.monotonic() - start
    except HTTPError as error:
        return error.code, time.monotonic() - start
    except (URLError, TimeoutError):
        return 0, time.monotonic() - start


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1)] * 1000, 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--team-id", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--target-ip", required=True, help="IP within this team's allowed CIDR")
    parser.add_argument("--rate", type=float, required=True, help="events per second")
    parser.add_argument("--duration", type=float, required=True, help="seconds")
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--staging-write", action="store_true", help="required acknowledgement of database writes")
    args = parser.parse_args()
    if not args.staging_write or args.rate <= 0 or args.duration <= 0 or args.workers < 1:
        parser.error("positive rate/duration/workers and --staging-write are required")

    count = math.ceil(args.rate * args.duration)
    run_id = uuid.uuid4().hex[:12]
    results = []
    in_flight = set()
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        for index in range(count):
            due = start + index / args.rate
            if due > time.monotonic():
                time.sleep(due - time.monotonic())
            while len(in_flight) >= args.workers * 2:
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                results.extend(future.result() for future in done)
            in_flight.add(executor.submit(send, args.url, args.token, args.team_id,
                                          args.target_ip, f"load:{run_id}:{index}"))
        for future in in_flight:
            results.append(future.result())

    latencies = [elapsed for status, elapsed in results if status == 201]
    statuses = {}
    for status, _ in results:
        statuses[str(status)] = statuses.get(str(status), 0) + 1
    summary = {
        "run_id": run_id, "attempted": count, "accepted": len(latencies),
        "status_counts": statuses, "p95_ms": percentile(latencies, .95),
        "p99_ms": percentile(latencies, .99),
        "wall_seconds": round(time.monotonic() - start, 2),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if len(latencies) == count else 1


if __name__ == "__main__":
    sys.exit(main())
