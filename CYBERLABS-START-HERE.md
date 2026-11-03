# Claw & Order handoff to Cyberlabs

This release connects student rosters, reviewed team/role assignments, private Discord registration, optional invitation email delivery, and the existing Claw & Order dashboard/scoring pipelines.

## Start with these files

For Matthew's current delivery, begin with [implementation and setup](claw-and-order/docs/MATTHEW-IMPLEMENTATION.md), [existing Open Claw infrastructure](claw-and-order/docs/OPERATION-OPEN-CLAW-INFRASTRUCTURE.md), [launch checklist](claw-and-order/docs/MATTHEW-LAUNCH-CHECKLIST.md) and [sources/installed-inventory record](claw-and-order/docs/MATTHEW-SOURCES.md). The owner confirmed the existing Kali connection uses a webhook/script on Kali. Preserve it and verify its format; the reporter/backend route is an implemented option, not proof of the live installation.

Start daily administration with [Human operations and change management](claw-and-order/docs/HUMAN-OPERATIONS.md). Give the future Cyberlabs agent the [Hermes planner instructions](claw-and-order/docs/HERMES-INSTRUCTIONS.md), with a separate planner OS account and credential. The [security/logic audit](claw-and-order/docs/AUDIT-2026-11-03.md) records findings, fixes, tests and remaining pilot gates.

1. [Student enrollment operating guide](claw-and-order/docs/STUDENT-ENROLLMENT.md): architecture, configuration, exact commands, privacy boundaries, recovery, and acceptance checks.
2. [Role definitions to complete](claw-and-order/docs/ROLE-DEFINITIONS.md): the faculty's student role definitions are still required. No student responsibility roles are guessed or enabled by default.
3. `claw-and-order/templates/roster.xlsx` or `roster.csv`: blank import templates. One row represents one student in one class for one term.
4. `claw-and-order/config/enrollment-policy.example.json`: choose the old or new Discord server and enter real class/team/channel/role IDs in a private copy.
5. `claw-and-order/deploy/`: Linux service examples and a two-process container deployment. Keep the API and enrollment worker separate.
6. [Whole exercise deployment gates](claw-and-order/docs/DEPLOYMENT.md): machine reporters, database backups, access policy, load/freshness, and production promotion.

## What is implemented

- Bounded CSV and XLSX imports, explicit header mapping, identity/conflict validation, a stored change preview, and digest-confirmed application.
- One student can be enrolled in multiple classes, with a different team and responsibility in each. Blank team assignments remain awaiting placement. Missing rows never imply withdrawal.
- Configurable faculty-approved responsibility keys. Pending definitions block that student's Discord grants until repaired and reviewed.
- Private, expiring, single-use registration codes. Only code hashes are stored in the application database. The student's Discord ID comes from their private `/register` interaction.
- Durable role reconciliation, removal of obsolete managed access before replacement access, preserved unrelated roles, bounded retries, private audit records, and reviewed import rollback.
- Live preflight verifies server identity, role hierarchy, nonprivileged managed roles, and isolation of every listed private team channel. No Message Content or Members privileged intent is requested.
- Private invitation exports and optional reviewed SMTP delivery with verified TLS, durable private receipts, expiration/revocation/recipient checks, and retry/pause behavior. Nothing sends email during import or export.
- A separate export-only team inventory feed into `c3claw`, using existing scoring team IDs and the existing export secret. It excludes student emails, names, ideas, registration codes and Discord user IDs. It never creates machine health reports.

## Configuration still needed from the school

| Input | Owner | Completion condition |
| --- | --- | --- |
| Student responsibility definitions | Faculty | Stable role keys, labels, duties, scope and approvals recorded in ROLE-DEFINITIONS.md |
| Old/new Discord selection | Discord owner | One selected server ID and an invite pointing to that server |
| Class/team permissions | Discord owner | Role IDs and private channel IDs supplied; live preflight passes |
| Roster and team network inventory | Faculty / lab staff | Stable student, term, class and scoring-team IDs agree across the roster, policy, reporter provisioning and topology |
| Hosting and secrets | Cyberlabs | PostgreSQL, trusted TLS, restricted staff API access, service account, bot token, and separate admin/export secrets configured |
| Email destination, if automated | Cyberlabs | Approved sender, SMTP host, TLS mode, credentials, and reviewed email plan |
| Machine monitoring | Lab staff | Real reporters/probes populate health independently of Discord enrollment |

Selecting the server and filling these mappings requires no code changes. The implementation controls Discord membership; any future role that must change scoring authorization or VM operating-system access requires a separately defined capability change. The existing scoring pipeline continues to authenticate by team token.

## Evidence and release boundary

The synthetic acceptance suite and isolated PostgreSQL rehearsal exercise both repositories. The rehearsal uses fake Discord role operations and no SMTP connection; it reports two synthetic students, three class enrollments, three exported teams, one stored scoring event, no duplicate event on replay, preserved unrelated roles, and healthy backend state. The Docker image builds with pinned dependencies and runs as UID 10001. The blank workbook is rendered and read back through the actual XLSX reader.

See `claw-and-order/docs/enrollment-validation.json` for the recorded results. Live Discord permissions, real institutional email delivery, actual student data, production hosting/restore/load, and final role duties still require the school's pilot acceptance. This package contains no student roster, live credentials, registration codes, private policy, or production database backup.

## First deployment sequence

Back up the existing production databases and configuration. Deploy the backend migration and API, configure the selected Discord policy, run the read-only Discord preflight, start the enrollment worker, and deploy the dashboard migration/code. Rehearse with a small staff-controlled roster before importing the full classes. Generate an import preview, review and apply it, then prepare invitations. Send mail only after reviewing and confirming an email plan. Students join the selected server and register privately; the worker applies approved roles and the dashboard receives team inventory automatically.

The service examples are templates for Cyberlabs to install on its hosts. No agent has been installed on Cyberlabs and no student invitations have been sent from this workspace.
