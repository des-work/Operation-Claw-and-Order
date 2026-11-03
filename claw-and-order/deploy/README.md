# Cyberlabs deployment templates

These examples target Linux. Read `../docs/STUDENT-ENROLLMENT.md` before installing. They do not create a server, database, Discord roles, channels, or credentials. Pin the reviewed source/image commit in your deployment records.

## Native services

Install the backend repository at `/opt/claw/backend` and create its Python 3.12 environment at `/opt/claw/backend/claw-and-order/.venv`. Install `requirements.lock`. Create a dedicated `claw` system user, `/etc/claw` and `/var/lib/claw`; keep rosters and email plans in owner-only subdirectories of `/var/lib/claw`. Make source code read-only to the service account. Place the selected policy at `/etc/claw/enrollment-policy.json`, readable by `claw` and writable only by the deployment administrator.

Copy `backend.env.example` and `enrollment.env.example` to private files in `/etc/claw`, replace placeholders and set mode 0600. Systemd reads these as root before changing service user. The API and worker share the backend PostgreSQL database and policy. Only the worker needs its enrollment bot token. Both use separate bounded database pools; include all processes in the connection budget. Use database-side TLS and separate migration-owner/runtime credentials as required by institutional policy.

Use a third, distinct planner credential for Hermes and a separate restricted `claw-planner` OS account. `planner.env.example` contains only its API origin and planner secret. Keep the administrator, worker, database and SMTP environments unreadable by that account. Do not deploy Hermes with the API/worker UID or unrestricted access to their files. See HUMAN-OPERATIONS.md and HERMES-INSTRUCTIONS.md for the enforceable credential boundary and human approval sequence. Remote production PostgreSQL connections enforce verified TLS; mount the private CA read-only and set `PG_CA_CERT_FILE` in API/worker environments when needed.

Run the migration with the database owner's environment, then the read-only bot check with the worker environment. Install the two unit files in `/etc/systemd/system`, run `systemctl daemon-reload`, and start them after the checks pass. The API binds loopback; put a trusted TLS reverse proxy with restricted staff-route access in front of it. The worker must reach Discord HTTPS and its gateway; neither process needs incoming public bot webhooks. Enable units at boot only after the staff pilot passes.

The mailer is an operator command with a reviewed digest, not an automatically scheduled service. Run it as the `claw` operator with the correct staff API/SMTP environment and persistent owner-only spool. See the operating guide for plan, send and resume commands.

## Containers

From this directory, create `../private/backend.env` and `../private/enrollment.env` from the examples. In these container copies, use `ENROLLMENT_POLICY_FILE=/run/claw/enrollment-policy.json`. Create the actual `../config/enrollment-policy.json`. Set the files' ownership/read permissions so container UID 10001 can read the mounted policy; never make secret files world-readable. Docker Compose reads env files on the host.

Build with `docker compose build`. Run migrations explicitly before startup:

```sh
docker compose run --rm api python -m backend.migrate
docker compose run --rm enrollment python -m backend.enrollment_bot --check
docker compose up -d
```

The database is external and must already exist. The API is exposed only on host loopback port 8000; the worker exposes no port. The dashboard is deployed from the companion `c3claw` repository with its own PostgreSQL database and release migration. This example mounts no student files into either long-running container; run the operator CLI/mail sender separately with controlled access to the API and private storage. If alert routing uses a private webhook file, add an explicit read-only mount and set its path; webhook JSON can instead be injected through the deployment secret store.

## Promotion and fallback

Record source SHAs, image digest, policy hash, tested backups and pilot results. Configure external monitoring of both health endpoints, enrollment worker freshness and queue age. On failure, stop invitation sending, preserve queue/audit/spool state, fix and reconcile with a reviewed preview. Stop the worker before policy/server or database restoration. Restore compatible app, database, policy and mail receipts together when required; never delete a queue to clear an alarm. A container restart policy is process recovery, not proof that Discord permissions or student delivery succeeded.
