# Hermes, Discord access and team readiness

Prepared November 5, 2026 for Matthew, Cyberlabs and faculty. Pentest Week starts Monday, November 9. This update supersedes earlier statements that the agent or Discord server selection is undecided. The owner selected Hermes and the existing Discord server/webhooks. The installed Hermes version, deployment location and telemetry format still require confirmation.

## Immediate decisions and owners

| Establish | Owner | Required outcome |
| --- | --- | --- |
| Where Hermes runs and what it replaces | Cyberlabs | Distinguish Kali execution agents, the restricted enrollment planner, and any Discord chat gateway; record installed version and service configuration |
| Complete current Discord inventory | Server owner / authorized administrator | Server ID, categories, channel IDs/types, bot application IDs, role IDs and effective access, including channels the project owner cannot see |
| Authoritative current enrollment | Both instructors | Reconcile Excel, approved backend records and Discord observations; resolve Discord-only students and conflicting assignments |
| Exercise coverage | Faculty | Stable class IDs for Cassandra's and Netsler's blue-team classes, approved red-team pool, availability, minimum team size and rotation rules |
| Identity and change approval | Faculty + Matthew | Verified student-to-Discord links and a named human approving every placement/change |
| Live pilot and fallback | Matthew + faculty | One real Hermes event per class, verified permissions, one reviewed transfer, agreed coverage schedule and recovery evidence |

Until these pass, the deployed dashboard is available for setup; it is not evidence that Hermes telemetry, rosters or role delivery work live. This review inspected repository code, not the Cyberlabs hosts or private Discord server.

## 1. Separate the agent from the transport

Claw & Order is the exercise system's name. OpenClaw was an execution-agent reference in older infrastructure documents. Selecting Hermes does not change the existing Discord server, team IDs, scoring credentials or publisher contract automatically.

The lowest-change route for this week is:

```text
Hermes / actual tool execution on Kali
  -> inspected script or deterministic formatter
  -> existing incoming webhook in an approved telemetry channel
  -> Cloudflare reader -> D1 -> topology dashboard

Excel + faculty decisions -> reviewed backend change
  -> human digest approval -> enrollment worker -> Discord role verification

Trusted machine monitor -> authenticated machine heartbeat -> dashboard
```

The planner, tool execution agent, Discord reader and role worker have different authority. A Hermes agent on a student-controlled Kali VM must not receive enrollment administrator credentials or the shared dashboard machine-write key. The staff planner uses only its restricted planner credential and the existing HERMES-INSTRUCTIONS.md. If Hermes also provides Discord chat, treat that as another service identity; do not reuse the enrollment bot's token or role authority.

### Compatibility findings from the actual code

| Component | Verified code behavior | Hermes consequence |
| --- | --- | --- |
| Cloudflare Discord parser | Accepts plain content beginning `[OCRT]` with team, tool, target and result, plus optional details | Preserve this exact structure using an inspected publisher; natural-language agent summaries and embeds are not telemetry |
| Source authorization | Requires exact channel ID + webhook ID + permitted team ID | A normal Hermes bot message has no posting webhook ID and is rejected even if its text looks correct; retain the posting hook or review an explicit adapter change |
| Optional Python reporter | Defaults to `~/.openclaw/logs/telemetry.jsonl`; consumes `type: tool.end` with `input.command`, `input.target` and boolean `success` | A Hermes log path or transcript is not a drop-in replacement. Inspect a real sample, map the schema and configure an explicit path before use; other record types are skipped |
| Backend target validator | Validates events against the authenticated team's configured DMZ/LAN CIDRs | A red team covering a different target may be rejected. Approve source-team/target-team attribution and scope before changing backend routes; do not bypass CIDR validation |
| Enrollment worker | Uses class/team/approved-responsibility policy and specific Discord member lookups | Already agent independent; Hermes prepares previews, a human applies them. It does not discover all server members |
| Machine health | Separate authenticated observations, independent of activity | A Discord post or successful agent reply is not proof that a VM is healthy |

Example of the existing Discord contract, with synthetic values:

```text
[OCRT] ClassA_01 | Nmap | 10.50.1.100 | SUCCESS | Approved lab test
```

Capture one sanitized real Hermes tool-result sample and one emitted Discord message. Record installed version, script/service path, source log, formatter version, team binding, destination IDs, retry/spool behavior and timestamps privately. Secret-store references belong in inventory; token values and full webhook URLs do not.

The Discord route preserves posting time, not independently verified tool execution time. It deduplicates the same Discord message ID; two webhook posts for one operation are two messages. Do not run two publishers for the same event. If reliable original execution timestamps or producer-level deduplication are required, review a versioned contract/adapter with real fixtures first. Do not infer success or timestamps from an LLM narrative.

**Acceptance:** one controlled real execution in each class produces the intended source record, exactly one attributed dashboard event and matching fields; wrong-source/team messages are rejected; interruption/restart preserves pending work. Normal idle-source visibility is within 12 minutes only for the tested two-channel standard profile.

**Failure:** preserve source logs, spools, Discord history and reader checkpoints. Hold the incompatible feed and review a formatter/adapter using real samples. Roll back only to a previously verified publisher if it is still available and faculty approves; otherwise retain manual source evidence and label dashboard monitoring unavailable. Do not claim OpenClaw remains installed as a fallback.

## 2. Repair Discord visibility with the server administrator

One server can have many categories and channels. If a genuinely separate server is involved, its owner must authorize the bot there as well. A channel the project owner cannot see requires an authorized server administrator to inventory and review it. Bot access and the human administrator's own visibility are separate decisions.

Create a private access inventory with: server/category/channel IDs and type, purpose, class/team, human owner, intended bot identity, effective current permissions, desired permissions, telemetry source mapping and test result. Check category inheritance, unsynced channel overwrites and member-specific overwrites. Do not make private channels public to fix bot visibility.

| Identity | Access required | Boundary |
| --- | --- | --- |
| Telemetry reader | View Channel + Read Message History on each selected telemetry channel; Message Content intent enabled for its application | No Administrator or Manage Roles needed; all imported sources still require explicit webhook/team mapping |
| Enrollment worker | Manage Roles; visibility into every listed private team channel; bot role above all managed student roles | No Administrator; managed student roles have zero server permission bits and reviewed channel overwrites |
| Hermes chat gateway, if used | Only the faculty-approved operational channels and users needed for that gateway | Separate from telemetry import and role assignment; access to a channel does not authorize enrollment changes |
| Human operator | Faculty-approved oversight visibility | Grant separately using a reviewed staff role; bot grants do not repair human visibility |

An authorized admin makes and records the permission changes. The worker operator runs `python -m backend.enrollment_bot --check` with the protected worker environment. This is a read-only configured-policy check, not an exhaustive audit of every channel on the server. Effective access must then be checked with ordinary student pilot accounts, including a dual-class student.

The current reader accepts server text and announcement channels; it rejects thread/forum/voice/DM channels as telemetry sources. Do not assume private threads are covered by parent-channel access.

### Preserve the low-cost collection limits

Granting visibility does not mean importing every chat channel. Keep discussion/team-private channels separate from the small set of telemetry channels. Prefer the two existing class telemetry destinations where their existing webhook routing permits it. Many team hooks may be explicitly mapped in those channels. Moving an existing hook to another channel is a reviewed routing change, not a permission fix.

The deployed reader handles two channels per five-minute run. With N idle channels, a collection round needs approximately `5 × ceil(N / 2)` minutes, plus cache/browser timing. More than two channels invalidates the existing 12-minute visibility acceptance assumption. A single large backlog can also exceed it. Measure expected traffic and approve channel capacity/freshness settings before expansion; do not silently increase polling or move hooks solely to make health appear current.

**Acceptance:** the actual reader can inspect every allowlisted telemetry channel, configured roles pass preflight, approved students can see only their permitted private channels, and an event from each class imports under its mapped source.

**Failure:** stop the affected grants/feed; administrator repairs the exact overwrite or hierarchy. Preserve blocked jobs and reports. Do not grant Administrator to finish the pilot.

## 3. Reconcile Discord and the roster before balancing

Excel is an approved input. The backend stores the approved enrollment state. Discord is the observed delivery/membership state. Joining Discord or choosing a nickname/team role does not establish approved enrollment.

1. Save dated private copies of the current Excel inputs and approved backend export. An authorized administrator records Discord user IDs and relevant role IDs, server ID, snapshot time and scope. Distinguish bots/staff/last-year members from this semester's students; do not remove them in bulk.
2. Match using the existing verified student-to-Discord binding or private registration. Do not guess by nickname, email-like username, self-assigned role or chat claims. Faculty resolves Discord-only students against institutional records before adding an enrollment. Discord normally does not supply a member's college email to the bot.
3. Report the categories below, with affected stable IDs, counts, owner and resolution. Compare per-class totals and distinct-person totals separately so dual-class students are not counted twice as available red-team capacity.
4. Faculty approves corrections/placements. Hermes prepares deterministic previews; the human reviews the server-stored report and exact digest, applies it, and verifies actual roles/channel access. Unknown students remain unresolved; missing rows never mean withdrawal.
5. Preserve input snapshots, approved before/after records, verification evidence and the institutional change reference. Existing members should use their existing Discord accounts; no need to leave/rejoin the server to register.

| Reconciliation category | Resolution |
| --- | --- |
| Approved record + linked account + matching roles | Retain; record verified observation time |
| Approved record, no Discord account/join | Private invitation/registration follow-up; remain waiting |
| Already joined, no approved roster record | Faculty verifies identity/class/team, then adds a reviewed enrollment and private registration |
| Roles disagree with approved record | Investigate unauthorized/manual/self-assigning role tools; preview reconciliation; verify effective access |
| Duplicate/ambiguous identity | Human identity review; do not merge automatically |
| Dual-class student | Preserve both enrollments; check availability and separation before red/blue placement |
| Withdrawn/previous-term/staff/bot | Explicit owner disposition; no inference from absence in one workbook |

The current worker does not bulk-enumerate server membership. Use an authorized administrator's private inventory first. If a complete API-based discovery tool is commissioned, Discord's List Guild Members endpoint requires the privileged GUILD_MEMBERS intent; implement pagination, protected reports and completeness checks separately. Do not claim the existing no-Members-intent worker discovers Discord-only students, and do not enable that intent merely for normal registration.

Legacy roles need special review: the worker preserves unrelated roles and blocks when they open another listed team's private channel. Removing old unmanaged roles/overwrites requires an administrator's explicit decision. Other self-role bots and manual grants can reintroduce drift after a successful observation.

**Acceptance:** every participating person has a resolved identity and approved class enrollment; every participant is assigned or explicitly awaiting placement; team/class totals reconcile; unresolved exceptions have a named owner. No roster-wide destructive cleanup is implied.

**Failure:** hold affected placement/grants, resolve the exception privately, regenerate a preview and retain history. Do not manufacture student emails or import Discord nicknames as authoritative identities.

## 4. Balance coverage across Cassandra and Netsler

These are instructor/class display labels supplied by the owner, not verified stable IDs. Keep the existing class/team IDs if already provisioned. Do not rename scoring teams, reuse a previous-term roster or populate production mappings from these labels alone.

Before moving students, faculty records:

- Blue-team count and size in each class, target inventory and required test duration per team.
- Verified red-team participants, agreed minimum/maximum red-team size, availability and relevant experience.
- Whether a shared red-team pool can serve both classes; class credit/attendance constraints and approved volunteers.
- Concurrent exercise slots, supervision capacity, availability overlaps and reserve/makeup time.
- Whether students may serve both colors, with conflict rules: no attacking their own blue team or exposing its private information.
- Written target scope, source-team attribution, permitted windows and consistent grading/evidence rules.

### Recommended approach if faculty permits it

Use a shared pool of viable red teams with scheduled rotations across both classes. Preserve blue-team membership initially; equal headcounts are not the objective. Give every blue team an agreed amount of supervised testing and reporting. Spread experienced red students where faculty approves, avoid double-booking dual-class students, and keep a reserve for absences.

For B blue teams and R available red teams, one concurrent target per red team requires at least `ceil(B / R)` waves, assuming equal duration and compatible availability. If R is zero, no covered schedule exists. These are planning bounds, not measured capacity. Example: 12 blue teams and 4 red teams require at least 3 waves. The example is not the actual roster.

If cross-class coverage is not allowed, calculate coverage separately for each class. If time is insufficient, faculty can reduce concurrent blue-team windows, approve volunteers or team moves, or adopt a supervised standard test plus reporting exercise. Any reduced coverage or grading change must be explicit; do not quietly label an untested blue team covered. Avoid making undersized red teams just to obtain one red team per blue team.

### Record a separate coverage schedule

For each scheduled test record a stable exercise ID, red team ID, blue class/team ID, approved targets, start/end/timezone, responsible instructor, planned test/report duration, status, evidence/report reference and fallback slot. Change reports include the prior/new assignment, requester, approver, reason, effective time and notification owner.

This schedule is an operational record to establish; it is not a shipped scheduler or a currently supported import format. The enrollment schema allows one team and optional responsibility per student/term/class. Red/blue duty definitions are still faculty-owned. Do not add invented `side` or `opponent` columns to the production roster and assume the importer understands them. Do not enroll a red student as a member of a target's blue team to grant target access. Lab authorization, scoring attribution, network scope and Discord membership are different mappings.

The current event contract has one `team_id`, not separate attacker/defender IDs, and no enforced exercise-window schedule. Faculty must decide what that ID credits before cross-team coverage begins. Review the backend CIDR check and scoreboard mapping against real source/target fixtures; a Discord role or an operational schedule does not update them. If first-class matchups or time-window authorization are required, that is a separately reviewed code change.

**Acceptance:** every blue team in both classes has a qualified, available red team, approved targets, an achievable slot and reporting owner; nobody is double-booked; attribution is unambiguous; faculty approves coverage and exceptions.

**Failure:** reschedule, use approved reserve staff/volunteers, or adopt faculty-approved reduced coverage. Keep uncovered teams visible and record the impact. Do not grant cross-team private-channel access or expand target CIDRs as a workaround.

## 5. Reports and launch order

Use the reporting headings in HERMES-INSTRUCTIONS.md: Step, Outcome, What I checked, What this means, Affected students/classes, Proposed or observed changes, Evidence, and Next action and owner. Each report needs a timestamp, source/policy revision and completeness limits. Use stable IDs and counts in general summaries; keep emails, Discord identity mappings, private ideas and source payloads in protected institutional storage.

Required readable reports:

1. **Compatibility:** installed Hermes/publisher version, real fixture comparison, selected feed and remaining adapter gaps.
2. **Access:** required channels versus effective reader/worker/student visibility, unsupported types and preflight evidence.
3. **Roster reconciliation:** matched, Discord-only, roster-only, conflicting, unassigned, withdrawn and dual-class counts with explicit resolutions.
4. **Coverage:** each blue team's planned red coverage, available capacity, conflict checks and uncovered/makeup work.
5. **Change and verification:** before/after approved assignments, human approver, exact change/digest, actual role read-back and effective channel evidence.
6. **Launch decision:** gates passed/failed/waiting, evidence, owners and approved fallback; no blanket VERIFIED if private channels or participants were omitted.

**Thursday Nov 5:** inventory Hermes/publishers and channels, collect both approved rosters plus Discord observations, decide the red-pool policy.

**Friday Nov 6:** resolve identities and placements, approve coverage, run one real Hermes-to-dashboard event from each class, registration/transfer/access pilot and interruption/recovery/load checks.

**Sunday Nov 8:** freeze the approved roster/policy/source mappings and schedule, preserve backups and verification reports, name the launch and incident operators.

**Monday Nov 9:** preflight actual feeds, heartbeat age, worker state and attendance; apply only reviewed exception changes. If gates fail, faculty explicitly selects the documented manual/reduced operation and communicates the monitoring limitation.

## Code evidence and current references

- Dashboard: `shared/discord.js`, `cloudflare/discord.js`, `docs/CLOUDFLARE.md` and `docs/COST-CONTROLS.md` in des-work/c3claw. Runtime inspected at source `984a06807b6178dc02a0bfd3a7a36f479c7ad669`; this document does not change that deployed runtime.
- Backend: `reporter/reporter.py`, `backend/enrollment_bot.py`, `backend/enrollment_policy.py` and `backend/routers/events.py` under claw-and-order in des-work/Operation-Claw-and-Order. Runtime inspected at source `6eaf268315e4fdb0a3dec3acbe615ed2fb896b97`.
- [Discord permissions](https://docs.discord.com/developers/topics/permissions): channel overwrites, effective permissions and role hierarchy.
- [Discord guild members](https://docs.discord.com/developers/resources/guild#list-guild-members): privileged intent required for bulk listing; specific-member lookup is a separate endpoint.
- [Hermes Discord integration](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord): native Hermes chat uses a bot/gateway. This does not assert that the Cyberlabs installation uses that route or emits Claw telemetry.
- [Hermes agent importers](https://hermes-agent.nousresearch.com/marketplace/agent-importers): configuration import exists; it is not evidence that tool telemetry, authorization or publisher services migrated correctly. Do not run an automatic secret migration to repair this pipeline.

Product references checked November 5, 2026. No live Discord permissions, student assignments, agent installation or production feed configuration were changed by this documentation review.
