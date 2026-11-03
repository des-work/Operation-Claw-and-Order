# Sources, certainty and installed-inventory record

## What is known and what must be checked onsite

| Statement | Evidence | Status |
| --- | --- | --- |
| The existing Kali connection uses a webhook/script on Kali | Project owner clarification, November 3, 2026 | Owner-confirmed transport; installed script/configuration not inspected |
| The reporter normalizes tool.end and authenticates to the backend | reporter/reporter.py and reporter_config_template.json | Code verified; actual HTTP method passed the isolated PostgreSQL rehearsal |
| Backend alerts and dashboard export have durable delivery queues | backend/services/discord_worker.py, discord_webhooks.py and routers/admin.py; dashboard/server/backend-sync.js | Unit tests and isolated database rehearsal; no live Discord proof |
| The dashboard reader binds a webhook/channel to permitted team IDs and preserves Discord creation time | dashboard/server/bot.js, discord-sources.js and events.js; tests/discord-sources.test.js | Local tests/lint/build; real Discord pilot pending |
| Enrollment supports two classes, unassigned students and reviewed transfers | backend enrollment modules and scripts/rehearse_enrollment.py | Shipped Excel upload, separate planner/human CLI and dual-class transfer passed with synthetic data |
| Portal access uses the institutional VPN/Proxmox flow | 2026 Proxmox Cyberlab Documentation - Student.pdf, page 8 | Local reference reviewed; current access policy remains Cyberlabs' authority |
| A management-network backend and Ollama service are part of the original design | Claw_and_Order_v2_Definitive_Architecture.docx and historical Dogpark playbook appendices | Design context; addresses, model endpoint and live deployment unverified |

Exact delivered repository commits, functional CI links and the package timestamp are in HANDOFF-MANIFEST.json. The earlier audit and its scan dispositions remain included; enrollment-validation.json contains the latest functional recheck. Passing synthetic tests is not a substitute for the live signoff checklist.

## Primary product references

Reviewed November 3, 2026:

- [Discord incoming webhooks](https://docs.discord.com/developers/resources/webhook): channel posting via a generated URL/token. Posting hooks do not assign roles or read history.
- [Discord Gateway](https://docs.discord.com/developers/events/gateway): bot event connections, intents and session behavior.
- [Discord permissions](https://docs.discord.com/developers/topics/permissions): role hierarchy and channel/member overrides. Actual effective access must pass the pilot.
- [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits): retry delays and global/per-route limits.
- [OpenClaw Discord adapter](https://docs.openclaw.ai/channels/discord): a supported optional product adapter. No such native adapter is asserted to be installed in this lab; the owner identified the Kali webhook/script path.

The original student guide and Dogpark playbook are not redistributed here because the handoff only needs their relevant infrastructure context. Historical example addresses are not promoted into production configuration.

## Installed publisher inventory - complete privately

Record one row per distinct deployed publisher, then expand for additional teams. Do not paste webhook URL tokens, bot tokens, passwords, raw student activity or invitation codes into this document.

| Field | Operator fills in |
| --- | --- |
| Inventory date, operator, change reference | Pending |
| Kali VM/host and exact team/class ID | Pending |
| Existing script path/version and service/trigger | Pending |
| Observed payload shape and formatter requirement | Pending |
| Discord server, channel ID and webhook ID | Pending |
| Secret-store reference for posting URL (no value) | Pending |
| Local log, retry buffer and cursor locations | Pending |
| Existing retry/rate-limit/restart behavior observed | Pending |
| Selected dashboard feed and source policy revision | Pending |
| Optional reporter backend origin/team/log path | Pending if adopted |
| Trusted machine-health integration and owner | Pending |
| Pilot event IDs, timestamps and stored receipts | Pending |
| Cutover boundary and fallback routing | Pending if changed |
| Human acceptance and remaining blockers | Pending |

Keep this completed record in restricted institutional storage. The blank handoff is intentionally free of live infrastructure secrets and student records.
