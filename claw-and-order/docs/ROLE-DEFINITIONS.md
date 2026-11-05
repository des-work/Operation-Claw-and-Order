# Faculty student role definitions

**Current classes (owner report, November 5):** Cassandra and Netsler each have blue teams; the red-team pool, coverage policy and stable class IDs require faculty confirmation. Existing Discord roles are observations, not the approved responsibility catalog. Review HERMES-DISCORD-ROSTER-READINESS.md before balancing students. Red-team coverage of a target does not make the attacker a member of that target's blue team, and responsibility roles do not change network/scoring scope.

The repository defines development responsibilities for a Backend Team, Dashboard Team and VM Team. Those are not an authoritative catalog of student exercise roles. Red/blue competition context likewise does not define each student's duties or privileges. Faculty must supply the final catalog below.

The shipped policy deliberately starts with `"role_definitions": {}` and empty `student_roles` mappings. Leave the roster's `role_key` blank while a student's responsibility is undecided. Class and team membership are separate from the responsibility key.

## Complete one entry per responsibility

| Field | Faculty decision |
| --- | --- |
| Stable `role_key` | Pending; use 1–64 letters, digits, underscores or hyphens |
| Student-facing label | Pending |
| Duties and expected outputs | Pending |
| Class or team scope | Pending |
| Discord channels/capabilities required | Pending |
| Any Claw API/scoring or VM privileges required | Pending; these are not inferred from a Discord role |
| Who may assign/change this role | Pending |
| Approving instructor and date | Pending |

Repeat the entry for each actual role. Do not use spreadsheet free text or student ideas as permission instructions.

## Enter approved definitions

After faculty has defined a responsibility, add its stable key under `role_definitions` with exactly `label`, `purpose`, and boolean `approved`. Add the matching Discord role ID under each applicable class's `student_roles`. Set `approved` to true only after review. Keep distinct Discord role IDs for different class contexts.

The current worker requires all roles it manages to have zero server-level permission bits. Use reviewed channel overwrites for access. A team role opens its listed private team channels; class or responsibility roles must not open those private channels. Staff/admin roles stay above the bot and outside its managed mappings. The worker will reject a mapping that violates these boundaries.

If a role needs a new backend or VM capability, document that explicitly before adding the authorization behavior. Changing a label or assigning a Discord badge does not change scoring rules, allowed techniques, team bearer tokens, or VM accounts.

## Approve a catalog update

1. Edit the private policy and review the exact role IDs and duties.
2. Run `python -m backend.enrollment_bot --check` against the selected server.
3. Restart the enrollment worker with the same policy file as the API.
4. Generate a new reconciliation preview, review its desired role IDs and blocked reasons, and apply its digest.

A policy hash change invalidates earlier import approvals and old worker jobs. There is no automatic permission expansion from editing a file alone.
