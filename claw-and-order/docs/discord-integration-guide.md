# Discord handoff — current semester

The owner-confirmed existing Kali connection uses a webhook/script on Kali. Preserve and inventory it using [the infrastructure guide](OPERATION-OPEN-CLAW-INFRASTRUCTURE.md). The following backend configuration is the implemented central reporter/alert route; it is not evidence that this route is already installed on Cyberlabs.

The preferred backend alert path uses Discord incoming webhook URLs. A bot token is unnecessary for webhook delivery. This backend route receives telemetry directly from authenticated team reporters and exports it to the dashboard. Incoming Discord webhooks post into Discord; they cannot read channel history, assign student roles or supply machine health.

## What to provide

For a shared instructor channel, provide its webhook URL. For separate class channels, provide one webhook URL per class, labeled with its class and the exact team IDs or team prefix that belongs there. Example: `class-a`, prefix `ClassA_`. Matching is case sensitive. Add further routes for additional classes without changing code.

An administrator with **Manage Webhooks** in the destination channel can create each URL under Channel Settings → Integrations → Webhooks. Treat the full URL as a secret. Store it in the deployment secret store or the ignored private configuration file, rather than Git, screenshots, or command history. Creating and controlling the hook supplies posting access to that channel; webhook delivery does not need Message Content intent or a reader bot.

## Configure one shared instructor channel

Set `DISCORD_WEBHOOK_URL` in the backend secret store. Leave `DISCORD_TOKEN`, `DISCORD_WEBHOOKS_FILE`, and `DISCORD_WEBHOOKS_JSON` empty. This route explicitly receives all teams and phase announcements.

## Configure separate class channels

Copy `config/discord-webhooks.example.json` to the ignored `config/discord-webhooks.json`. Replace the URLs and team prefixes with the new class values, make the file readable only by the service account, and set `DISCORD_WEBHOOKS_FILE` to its path. Alternatively supply the same JSON array in `DISCORD_WEBHOOKS_JSON`. Use exactly one configuration source and leave `DISCORD_TOKEN` empty.

Each route requires a unique stable `name`, a Discord `url`, and either `team_ids`, `team_prefixes`, or explicit `all_teams: true`. Team milestone and VM-offline alerts go only to matching routes. Phase announcements go to every configured route. An unmatched team's alert stays queued for routing repair; it is never silently posted to another class. Overlapping selectors intentionally deliver to each matching destination. Keep destination names stable when rotating a hook so pending messages retain their destination.

## Start and verify

1. Stop the previous sender. Back up the production database and run `python -m backend.migrate` before starting this release; two new tables store webhook delivery receipts and destination cooldowns. Run one backend web worker.
2. Run `python -m backend.check_discord_webhooks` to validate the configured URLs against Discord without posting messages. Output contains destination names and channel IDs, never full URLs. Every destination must pass. URL validation alone does not prove that a real message has been delivered.
3. In staging, send one controlled milestone per class and one phase announcement. Confirm the original team, milestone, phase and score are correct in the intended channel. Health must show `discord=webhook_ready`, `discord_pending=0`, and `discord_webhook_failures=0` after delivery. `webhook_ready` means the configured worker is running; the test message and its persisted receipt establish end-to-end delivery.
4. Interrupt one destination and restart the backend. Its alert must remain queued while other destinations continue. After repair, it must deliver without repeating the destinations already recorded as successful.

The sender requests a Discord message receipt (`wait=true`), persists a receipt independently per destination, suppresses automatic mentions, and follows server-specified per-webhook and global rate-limit delays. Cooldowns survive restarts. HTTP 401/403/404 quarantines the destination until its URL is replaced; fix access and provide a new webhook URL. Health reports outstanding failed deliveries. Network failures retry with backoff. A crash after Discord accepts a post but before the receipt commits can still repeat that post: delivery is at least once.

## Clean semester and fallback

The active dashboard has no automatic mock data, default legacy channel IDs, or seed endpoint. Old synthetic fixtures are retained under its `archive/legacy-preview/` directory. The local SQLite database was backed up and reset; no production database was connected or reset. Before an actual semester cutover, back up and archive the production dashboard and scoring databases together, then use fresh semester databases and new reporter registrations so historical outboxes cannot repopulate the gallery.

If Discord fails, preserve the database queues and repair the hook; scoring and dashboard export continue. If a destination was revoked, rotate its URL using the same destination name. If routing is wrong, stop the sender before editing selectors. Existing deliveries retain their destination names. Reverting the sender can repeat alerts whose webhook receipts the older bot version does not understand; keep delivery paused while reconciling them.

## Optional Discord telemetry reader

Use the dashboard reader for the existing Kali webhook feed if it posts the exact `[OCRT] team-id | Tool | Target | Result | Details` contract. Do not also export the same logical events from the scoring backend; the two source ID namespaces cannot deduplicate each other. The reader requires `DISCORD_BOT_TOKEN`, an explicit `ALLOWED_CHANNELS` list, `DISCORD_SOURCE_POLICY_FILE`, View Channel, Read Message History, and Message Content intent. The source policy binds exact channel/webhook IDs to exact team IDs (see dashboard `config/discord-sources.example.json`). Missing mappings stop startup; ordinary user/bot messages and cross-team claims are rejected. Imported timestamps retain Discord posting time. `DISCORD_INITIAL_BACKFILL=false` begins at new messages on first connection. Old channel IDs are never selected automatically. If the source exists only as Discord messages, this reader is necessary; posting webhook URLs do not replace it.

## Sources

[Discord webhooks](https://docs.discord.com/developers/resources/webhook) and [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits).
