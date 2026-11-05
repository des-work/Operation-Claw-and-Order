# Cyberlabs administrator: enrollment and change management

This is the daily operating guide. The database records the approved roster; Discord delivery is a separate operation. A successful write means **saved**, while a verified report means the worker subsequently read back roles and checked effective access on the listed private channels. Neither result proves future permissions cannot drift.

## People, credentials and files

| Person/process | Allowed work | Credential and storage |
| --- | --- | --- |
| Faculty | Define responsibilities, approve placement and requested changes | Institutional request/ticket; no bot token required |
| Hermes planner | Validate approved rosters, prepare previews, explain failures, read status/history/reports | Separate `ENROLLMENT_PLANNER_SECRET`; dedicated OS account with restricted roster/report directories |
| Human administrator | Review authoritative report; apply the exact digest, issue invitations, approve mail delivery, resolve failures | `ADMIN_SECRET` in a separate human-controlled session; never give it to Hermes |
| Enrollment worker | Reconcile approved jobs; inspect actual Discord roles and listed channels | Separate bot token and backend runtime DB access; no roster guessing |
| Dashboard | Read team display metadata and scoring events | Export-only credential; no student records or invitation codes |

Planner credentials can read private student records, including ideas, to prepare a change. Treat them as sensitive. They cannot apply an import, issue/check invitation credentials, change competition phases, or call the export-only feed. Enforce the OS separation as well as the API boundary: Hermes must not read API/worker/admin/SMTP environment files, private invitation exports, mail spools, database credentials, or service controls. Give it read-only application code and only the approved input/output directories. Run the application from administrator-owned source. A shared OS account with unrestricted shell access would defeat credential separation.

`requested_by` and `approved_by` are recorded assertions under credentials, not institutional identity verification. Use separate staff sessions, ticket approvals and institutional access logs to corroborate them. Database administrators can alter audit tables; keep approved reports and periodic audit exports in the institution's protected retention system if independent integrity is required. Do not call these database rows a tamper-proof audit log.

## The five-step process

| Step | Admin-visible evidence | Success outcome | Failure action |
| --- | --- | --- | --- |
| 1. Check | Enrollment status JSON/Markdown, service health, read-only bot preflight from the worker operator | Correct server/policy, current worker and known permissions; backups available | Pause application/invitations, identify the failing dependency and owner |
| 2. Prepare | Private source, JSON preview and readable change report | Every change has requester, reason, reference, before/after assignments and a bound digest | Correct source errors or resolve faculty role definitions; do not infer privileges |
| 3. Approve | Human reviews the server-stored report and staff ticket | Named administrator confirms the exact digest with the admin credential | Reject/revise; changed roster/policy/revision requires a new preview |
| 4. Verify | Apply receipt and subsequent verification report | Worker read-back matches approved roles and listed effective channel access | Keep waiting/blocked work visible; repair and prepare a reconciliation |
| 5. Record | JSON, Markdown, student history and institutional ticket | Team can explain who requested/approved what, when, and what actually completed | Preserve partial evidence; unknown outcomes stay unconfirmed until inspected |

All examples run from the installed backend `claw-and-order` directory with the correct private environment. Keep output names unique. Reports/JSON are created with mode 0600; they contain administrative student IDs and permission mappings even when identity fields are omitted. Do not put real reports into Git or public dashboard directories. The examples use synthetic identifiers.

### 1. Check readiness

```sh
python -m backend.enrollment_cli status --out private/status-001.json
```

Read both the JSON and `private/status-001.json.md`. The worker operator separately runs `python -m backend.enrollment_bot --check` with its protected environment. This check does not grant roles. Public health exposes aggregates only. Production requires distinct random admin/export/planner credentials of at least 32 characters. Remote backend PostgreSQL connections enforce hostname/certificate verification; set `PG_CA_CERT_FILE` for a private CA. The dashboard requires HTTPS for a remote export origin and refuses credential-bearing redirects.

### 2. Prepare an initial roster

```sh
python -m backend.enrollment_cli preview private/roster.xlsx --requested-by hermes --reason "Faculty approved semester enrollment" --ticket CLAW-101 --out private/change-101.json
```

Read `private/change-101.json.md`. The frozen report shows student IDs, term/class, current and proposed team/responsibility/status, planned role additions/removals, blocked definitions, server, policy hash, revision and digest. Roster names/emails/ideas are omitted from the Markdown; changes to those fields are flagged for review in the restricted JSON. Treat requester/reason text as provided data. No command is executed from it.

The permission plan compares approved intent with the managed-grant ledger; it is not a fresh Discord query. A blocked responsibility causes no Discord changes, and existing grants remain until repaired or explicitly withdrawn. For urgent removal, a human should prepare an explicit withdrawal with cleared team/responsibility, review it and verify revocation.

### 3. Approve as the human administrator

Use a separate human-controlled session with `ADMIN_SECRET`; unset `ENROLLMENT_PLANNER_SECRET` there. Retrieve the authoritative report before approval:

```sh
python -m backend.enrollment_cli report --batch-id CHANGE_ID --out private/change-101-server.md
python -m backend.enrollment_cli apply private/change-101.json --approved-by lab_admin --confirm-digest DIGEST_FROM_REVIEWED_REPORT --out private/change-101-applied.json
```

The CLI compares the local reviewed fields to the immutable server copy. The API rejects stale revision/policy approvals. Repeat application of the same batch is idempotent. A timeout or lost response may occur after a commit; inspect that change ID before retrying or making another change. Approval queues work; it does not certify Discord delivery. The application contract now requires change context and approver fields; older scripts/previews must be regenerated or updated.

### 4. Verify the actual result

```sh
python -m backend.enrollment_cli verify --batch-id CHANGE_ID --out private/change-101-verified.md
```

| Report state | Meaning | Next action |
| --- | --- | --- |
| NOT APPLIED | Only a preview exists | Review and obtain human approval |
| WAITING | Saved; waiting for registration, membership or retry | Follow each student's explanation; preserve jobs |
| BLOCKED | Definition, permission, isolation, policy or evidence problem | Stop promotion for affected students; repair and review reconciliation |
| VERIFIED | Every target's matching job has recorded role read-back and listed-channel access evidence | Record the report/time; it is an observation, not continuous monitoring |
| SUPERSEDED | Later approved changes replaced all jobs in this batch | Read the latest student history; do not certify old state |

Mixed superseded/pending batches remain WAITING; inspect the per-student rows. Students who never registered have no account to verify and must not be marked complete. An old completion flag without matching verification evidence is blocked as unverified.

`verify` exits 0 only for VERIFIED, 3 for waiting/not applied, and 4 for blocked/superseded. `status` exits 3 when not healthy. Read-only `report` exits 0 when the report was retrieved, regardless of the outcome printed inside it. Apply exit 0 confirms the stored/queued change, not Discord completion. The mail sender exits 3 if messages remain unaccepted or paused; its exit 0 means SMTP acceptance only. Tool errors exit nonzero and save an unconfirmed/failure report when an output path is available.

### 5. Preserve the record

```sh
python -m backend.enrollment_cli history --student-id student_001 --out private/student-001-history.md
python -m backend.enrollment_cli audit --out private/audit-001.json
```

History links approved before/after team, responsibility and status changes to the requester, approver, reason, reference, time, batch and revision. It includes registration, deferred delivery and completed role observations. Audit exports are newest first and paginate without changing records. Store the evidence alongside the ticket under the school's retention policy. Synchronize host clocks and use UTC for comparison.

## Move, unassign, change responsibility or withdraw a student

These commands create previews. They do not directly mutate Discord. Other classes for the same student are preserved. A targeted change carries the read revision; a concurrent roster edit requires rereading the student rather than silently overwriting it.

```sh
# Move within one class; preserve responsibility, identity and the other class.
python -m backend.enrollment_cli change --student-id student_001 --term-id 2026F --class-id ClassA --team-id ClassA_02 --requested-by hermes --reason "Instructor approved team rebalance" --ticket CLAW-102 --out private/change-102.json

# Awaiting placement: class membership remains, team assignment is removed.
python -m backend.enrollment_cli change --student-id student_001 --term-id 2026F --class-id ClassA --unassign --requested-by hermes --reason "Student awaiting a new team placement" --ticket CLAW-103 --out private/change-103.json

# Responsibility keys must already be faculty-defined and mapped.
python -m backend.enrollment_cli change --student-id student_001 --term-id 2026F --class-id ClassA --role-key FACULTY_APPROVED_KEY --requested-by hermes --reason "Faculty approved responsibility change" --ticket CLAW-104 --out private/change-104.json

# Withdraw from this class, clearing pending team/responsibility information.
python -m backend.enrollment_cli change --student-id student_001 --term-id 2026F --class-id ClassA --status withdrawn --unassign --clear-role --requested-by hermes --reason "Faculty confirmed withdrawal from class" --ticket CLAW-105 --out private/change-105.json
```

Apply and verify each approved change using steps 3–5. The worker removes obsolete managed access before adding replacement access. Unrelated Discord roles are preserved; if one grants cross-team access, effective-access verification blocks and explains the need for staff intervention. Withdrawing from ClassA does not remove the student's ClassB assignment. Removing membership also does not erase messages, historical scores or data they previously accessed; handle those separately under exercise rules.

## Repair, rollback and policy changes

After permission repair, restart/configuration change or class removal:

```sh
python -m backend.enrollment_cli reconcile --requested-by hermes --reason "Permissions repaired and policy reviewed" --ticket CLAW-106 --out private/reconcile-106.json
```

This includes prior managed grants/jobs, even if the new policy removed the entire class. A changed policy file pauses the running worker until restarted. New registrations cannot expand a policy that has not been reviewed for that student. Reconciliation changes convergence, not roster rows; another reconciliation is its recovery action.

For a large roster, use repeated `--student-id` options to review reconciliation in subsets of at most 5,000 students. Preserve the same reviewed policy and verify every subset. The import size limit is not a production capacity guarantee; test actual class sizes, Discord rate limits, initial registration bursts and database connection latency before launch.

For a wrong roster application:

```sh
python -m backend.enrollment_cli rollback --batch-id ORIGINAL_CHANGE_ID --requested-by hermes --reason "Instructor requested correction of prior assignment" --ticket CLAW-107 --out private/rollback-107.json
```

Review, apply and verify normally. A rollback refuses if later changes affected that student's prior row or identity; prepare a new targeted correction instead. New enrollments introduced by the original import become withdrawn. If old team/role mappings are no longer valid, staff must review the intended target configuration rather than force an obsolete snapshot back into service. No recovery command wipes audit history, event outboxes or mail receipts.

## Invitations and email

Only a human administrator can issue private invitations. The optional mailer has separate reviewed plan/digest and readable delivery counts. See STUDENT-ENROLLMENT.md. SMTP acceptance is not inbox delivery. Codes, exact recipients and message bodies remain in restricted JSON/spool/email, never the public dashboard. A crash after SMTP acceptance can repeat an invitation; reconcile deterministic Message-IDs and durable receipts. Recreate older mail plans after this audit release because invite identity is now bound into their digest and revalidated against the selected server.

## Stop conditions

Stop and explain the affected students, failed check, retained evidence, next safe action and responsible owner when: student identity conflicts; unknown roles; unmapped teams; mismatched server/policy; bad or missing approval; unexpected access; uncertain write outcome; missing read-back evidence; no verified backup; failed source/TLS/authentication; or a report no longer corresponds to the latest change. Preserve existing evidence and use a new filename for every operator report. Never convert an unknown result into a success message.
