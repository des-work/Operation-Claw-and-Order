"""Plain Markdown reports: stable headings, explicit outcomes, escaped source data."""

import html
import re


def cell(value):
    if value is None or value == "":
        return "—"
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    text = html.escape(text, quote=True)
    return re.sub(r"([\\`*_[\]{}()#+.!|>~-])", r"\\\1", text)


GUIDANCE = {
    "not_applied": "Review the proposed changes. A human administrator must approve the digest before application.",
    "pending": "The change is saved. Wait for the worker, then run verify again.",
    "retry": "Discord delivery failed temporarily. Keep the job; repair the connection if retries persist.",
    "awaiting_registration": "The student has not linked an account. An administrator can issue a private invitation.",
    "awaiting_member": "The linked account is absent from this server. Ask the student to join, then allow the next retry.",
    "complete": "The worker read back the managed roles and checked the listed private channel access. This is a recorded observation, not continuous monitoring.",
    "superseded": "A later approved change replaced this job. Review the student's latest history; this batch cannot certify their current state.",
    "blocked_policy": "Restart with the reviewed policy and prepare a new reconciliation preview.",
    "blocked_role_definition": "Faculty must approve the responsibility and map it before a new reconciliation.",
    "blocked_permissions": "Repair the bot hierarchy/role/channel permissions, run preflight, then review reconciliation.",
    "blocked_isolation": "An effective member permission exposes the wrong team or hides the intended team. Staff must inspect unmanaged roles and member overrides.",
    "missing_job": "The expected job is missing. Stop promotion and inspect the database/audit; do not assume success.",
    "unverified": "A completion flag has no matching read-back evidence. Prepare a reviewed reconciliation before claiming success.",
}


def change_report(review):
    verification = review.get("verification", {"outcome": "NOT APPLIED", "students": []})
    context = review.get("context", {})
    approval = review.get("approval") or {}
    lines = ["# Claw & Order change report", "", f"**Outcome: {cell(verification['outcome'])}**", "",
             "## 1. Request and approval", "", "| Item | Recorded value |", "| --- | --- |",
             f"| Change ID | {cell(review['batch_id'])} |", f"| Type | {cell(review.get('kind'))} |",
             f"| Requested by | {cell(context.get('requested_by'))} |", f"| Reason (provided text) | {cell(context.get('reason'))} |",
             f"| Ticket/reference | {cell(context.get('ticket'))} |", f"| Approved by | {cell(approval.get('approved_by'))} |",
             f"| Selected Discord server | {cell(review.get('guild_id'))} |",
             f"| Roster revision reviewed | {cell(review['base_revision'])} |", f"| Policy hash | {cell(review['policy_hash'])} |",
             f"| Confirmation digest | {cell(review['digest'])} |", "",
             "Names, email addresses, student ideas and registration codes are omitted from this report. Student IDs and permission mappings remain private administrative data.", "",
             "## 2. Proposed roster changes", "", "| Student ID | Term / class | Field | Before | After |", "| --- | --- | --- | --- | --- |"]
    count = 0
    for change in review.get("changes", []):
        before, after = change.get("before") or {}, change["after"]
        for field in ("team_id", "role_key", "status"):
            if before.get(field) != after.get(field):
                lines.append(f"| {cell(change['student_id'])} | {cell(change['term_id'])} / {cell(change['class_id'])} | {field} | {cell(before.get(field))} | {cell(after.get(field))} |")
                count += 1
        if any(before.get(f) != after.get(f) for f in ("email", "display_name", "idea")):
            lines.append(f"| {cell(change['student_id'])} | {cell(change['term_id'])} / {cell(change['class_id'])} | Private identity/idea fields | Values omitted | Changed; review restricted JSON |")
            count += 1
    if not count:
        lines.append("| — | — | No roster fields change | — | — |")
    labels = review.get("role_labels", {})
    def roles(values):
        return "; ".join(f"{cell(labels.get(role, 'Retired managed role'))} ({cell(role)})" for role in values) or "—"
    lines += ["", "## 3. Discord permission plan", "", "This compares the proposed roles with the recorded grant ledger. The ledger includes approved grant intent after uncertain network acceptance; this plan is not a live Discord inspection. Actual roles are read and reconciled by the worker after human approval.", "",
              "| Student ID | Roles to add if absent | Roles to remove if present | Pending definition |",
              "| --- | --- | --- | --- |"]
    for row in review.get("role_preview", []):
        lines.append(f"| {cell(row['student_id'])} | {roles(row.get('planned_add_role_ids', []))} | {roles(row.get('planned_remove_role_ids', []))} | {cell(row.get('blocked_reason'))} |")
    lines += ["", "## 4. Recorded execution and verification", "", "| Student ID | State | Last verified (UTC) | Next action |", "| --- | --- | --- | --- |"]
    for row in verification.get("students", []):
        lines.append(f"| {cell(row['student_id'])} | {cell(row['status'])} | {cell(row.get('last_verified_at'))} | {cell(GUIDANCE.get(row['status'], 'Stop and ask the administrator to investigate this state.'))} |")
    if not verification.get("students"):
        lines.append("| — | Not applied | — | Review the plan before approval |")
    lines += ["", "## 5. Human decision and recovery", "",
              "- Confirm the request, class/team scope, role removals and policy hash against the staff record.",
              "- Keep blocked or waiting students visible. A saved import is not proof of completed Discord delivery.",
              "- Approve with the administrator credential and a recorded approver; Hermes receives only the separate planner credential.",
              "- Run verify after application. Record the report with the change ticket and inspect any waiting, blocked or superseded student.",
              "- For an incorrect assignment, prepare a reviewed rollback or targeted change. Rollback refuses to overwrite later student changes. Preserve the audit, jobs and mail receipts.", ""]
    return "\n".join(lines)


def history_report(student_id, entries):
    lines = ["# Claw & Order student change history", "", f"Student ID: {cell(student_id)}", "",
             "| Time (UTC) | Revision | Event | Requested by / approved by | Reason / reference | Change ID |", "| --- | --- | --- | --- | --- | --- |"]
    changes = []
    for entry in entries:
        data = entry["details"]
        context = data.get("context", {})
        lines.append(f"| {cell(entry['created_at'])} | {cell(entry['revision'])} | {cell(entry['action'])} | {cell(context.get('requested_by'))} / {cell(data.get('approved_by'))} | {cell(context.get('reason'))} / {cell(context.get('ticket'))} | {cell(data.get('batch_id'))} |")
        for change in data.get("changes", []):
            for field in ("team_id", "role_key", "status"):
                old, new = change["before"].get(field), change["after"].get(field)
                if old != new:
                    changes.append(f"- Revision {cell(entry['revision'])}, {cell(change['term_id'])} / {cell(change['class_id'])}: {field} {cell(old)} → {cell(new)}.")
    if not entries:
        lines.append("| — | — | No recorded history | — | — | — |")
    lines += ["", "## Assignment changes", "", *(changes or ["No assignment differences recorded."]), "", "This history contains student IDs and approved assignment changes, not private identity fields or invitation credentials. Operator names are recorded assertions under a shared credential; use institutional access logs for independent attribution.", ""]
    return "\n".join(lines)


def mail_report(plan):
    lines = ["# Claw & Order invitation delivery report", "", f"Plan digest: {cell(plan['digest'])}", "",
             f"SMTP destination: {cell(plan['smtp']['host'])} / {cell(plan['smtp']['mode'])}", "",
             f"Pause reason: {cell(plan.get('paused_reason'))}", "", "| State | Messages |", "| --- | --- |"]
    for state in ("pending", "retry", "sent", "failed", "expired", "cancelled"):
        lines.append(f"| {state} | {sum(r['status'] == state for r in plan['records'])} |")
    lines += ["", "Sent means accepted by SMTP, not confirmed inbox delivery. The private JSON contains exact recipients and the template body for human review. This report omits addresses and registration codes. Check institutional delivery/bounce logs during the pilot.", "",
              "On a pause, repair the dependency and explicitly resume the same reviewed digest. A crash after SMTP acceptance can produce a repeat; reconcile deterministic Message-IDs and saved receipts before resending.", ""]
    return "\n".join(lines)
