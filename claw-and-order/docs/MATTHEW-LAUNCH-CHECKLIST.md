# Matthew launch checklist

## Bring for the semester

- [ ] Values-only Excel roster with student_id, email, display_name, term_id and class_id. Optional team_id, role_key, idea and status are understood; IDs match the policy/provisioning.
- [ ] Old/new Discord server selected, server ID and student invite confirmed.
- [ ] Existing Kali posting hooks inventoried; new/replacement and backend alert hooks privately labeled by purpose, class/team and destination.
- [ ] Separate enrollment bot token and exact role/channel IDs; hierarchy, Manage Roles and private-channel overwrites pass read-only preflight.
- [ ] Faculty responsibility definitions/mappings approved, or responsibility fields deliberately blank for the initial class/team rollout.
- [ ] Invitation delivery chosen: approved college mail merge, or separately configured/reviewed SMTP.

## Verify the environment that already exists

- [ ] Existing Proxmox/Kali/OpenClaw webhook/script inventoried; preserve its files/configuration and record trigger, payload, logs and retry behavior.
- [ ] Selected telemetry feed confirmed: [OCRT] formatter plus reader token/channel/webhook/team mapping for direct hooks, or verified tool.end producer and reporter/backend path. No overlapping import of the same logical event.
- [ ] Backend, PostgreSQL, dashboard and enrollment worker deployed with required migrations, TLS and separate credentials.
- [ ] Team IDs and current network inventory agree across provisioning, roster, policy, reporter and dashboard.
- [ ] Trusted machine-health producers are active; unknown status is accepted when no observation exists.
- [ ] Current backups restore on a clone and production-sized class/load/freshness criteria have an operator owner.
- [ ] Hermes has a separate planner credential/OS account and cannot read administrator, bot, database or SMTP secrets.

## Pilot before the full invitation batch

- [ ] Review a real Excel preview and exact immutable server report; record requester, reason, reference and human approver.
- [ ] One ordinary test account registers privately; a two-class student receives the intended role union.
- [ ] An unassigned student remains without team access; a move/withdrawal preserves unaffected classes and produces readable history.
- [ ] Listed private channels are isolated under effective member permissions, including unrelated roles/member overrides.
- [ ] One controlled event through the selected feed appears correctly once, with the correct team/fields/time. A mapped webhook claiming another team is rejected. Intended Discord telemetry/alert destinations are checked separately.
- [ ] Outage/restart recovery preserves work and does not duplicate dashboard events. Inspect the documented at-least-once Discord/email acceptance window.
- [ ] Verification reports show the true result; waiting/blocked/superseded outcomes are not marked complete.
- [ ] Proposed security CI gate patches are activated by an authorized owner, or the documented manual audit checks are recorded for this release.

## Receiver signoff

| Item | Matthew or delegated operator records |
| --- | --- |
| Selected server and deployment hosts | Pending live setup |
| Approved roster/policy revision | Pending faculty/operator review |
| Pilot evidence and timestamp | Pending live pilot |
| Backup/restore evidence | Pending Cyberlabs validation |
| Remaining blockers and responsible owner | Record explicitly before launch |
| Go-live decision and approver | Pending human acceptance |

Excel and alert hooks complete the semester inputs only after the bot configuration and existing environment above are ready. The local package/rehearsal is not a claim that the live Cyberlabs deployment has already passed these checks.
