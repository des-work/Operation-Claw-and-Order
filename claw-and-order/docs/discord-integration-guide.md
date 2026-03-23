# Discord Integration Guide — Claw & Order

**Date:** March 22, 2026
**Status:** Code complete, awaiting bot token + channel ID configuration

---

## 1. Overview

Claw & Order uses a single Discord bot to post real-time competition alerts to one channel in your existing Discord server. The bot is fully built into the backend — no separate process, no external service, no webhook configuration needed.

**What the bot posts:**
- Milestone alerts (team compromised DMZ, pivoted to LAN, etc.)
- Phase transition announcements (Phase 1 → 2 → 3)
- VM offline warnings (team's Kali VM stopped heartbeating for 5+ minutes)

**What the bot does NOT post:**
- Individual tool runs (port scans, SQLi attempts, etc.) — these go to the SSE stream only
- Heartbeat events — processed silently by the backend
- Failed attempts — only successful milestones trigger alerts

---

## 2. Architecture

```
Backend (FastAPI)
    |
    |── events.py ──── milestone fires ──> enqueue({"type": "milestone", ...})
    |── admin.py ───── phase change ────> enqueue({"type": "phase_change", ...})
    |── watchdog.py ── VM offline ──────> enqueue({"type": "vm_offline", ...})
    |                                              |
    |                                              v
    |                                     asyncio.Queue (in-memory)
    |                                              |
    |                                              v
    |                                     _dispatch_loop() ── one message at a time
    |                                              |
    |                                              v
    |                                     discord.TextChannel.send()
    |                                              |
    |                                              v
    |                                     Your existing Discord channel
```

### Key design points:

- **One bot, one channel, one dispatch loop** — everything is serialized through a single `asyncio.Queue`
- **Fire-and-forget from the API's perspective** — the event ingest endpoint enqueues a Discord message and immediately returns. If Discord is slow or down, scoring is unaffected
- **No separate process** — the bot runs as an `asyncio.create_task()` inside the FastAPI lifespan. Starting the backend starts the bot. Stopping the backend stops the bot

---

## 3. Using Your Existing Discord Server

**You do not need a new Discord server.** You use the one your team already has.

What you need to create is a **bot application** on Discord's developer portal. This is not a server — it's a programmatic identity that can connect to any server you invite it to. Think of it like creating a service account.

### What changes in your existing server: nothing

- Your channels, roles, members, permissions — all untouched
- The bot joins as a new member with a "BOT" tag next to its name
- It only needs **Send Messages** permission in the one channel you point it at
- It cannot read your other channels, DMs, or anything else unless you explicitly grant access

### What you create (one time, takes 2 minutes)

1. A bot application on discord.com/developers
2. A bot token (the password the code uses to authenticate)
3. An invite link to add the bot to your server

---

## 4. Setup Steps

### Step 1: Create the bot application

1. Go to https://discord.com/developers/applications
2. Click **New Application** → name it "Claw & Order" (or whatever you want)
3. Go to the **Bot** tab on the left sidebar
4. Click **Reset Token** → copy and save the token somewhere secure
   - This is the `DISCORD_TOKEN` value for your `.env` file
   - You can only see it once — if you lose it, you'll need to reset it again

### Step 2: Set bot permissions

1. On the same Bot tab, scroll down to **Privileged Gateway Intents**
2. Leave everything **off** — our bot doesn't need message content, presence, or member intents
3. Go to **OAuth2 → URL Generator** on the left sidebar
4. Under **Scopes**, check only: `bot`
5. Under **Bot Permissions**, check only: `Send Messages`
6. Copy the generated URL at the bottom of the page

### Step 3: Add the bot to your server

1. Paste the URL from Step 2 into your browser
2. Select your existing competition Discord server from the dropdown
3. Click **Authorize**
4. The bot will appear in your server's member list with a "BOT" tag

### Step 4: Get the channel ID

1. In Discord, go to **User Settings → Advanced → Developer Mode** → turn it **On**
2. Right-click the channel you want alerts posted to → **Copy Channel ID**
   - Use an existing channel, or create a new one like `#claw-alerts`
   - This is the `DISCORD_CHANNEL_ID` value for your `.env` file

### Step 5: Configure the backend

Edit your `.env` file in `claw-and-order/`:

```env
DISCORD_TOKEN=MTIzNDU2Nzg5MDEy...your-bot-token-here
DISCORD_CHANNEL_ID=1234567890123456789
```

### Step 6: Verify it works

1. Start (or restart) the backend:
   ```bash
   uvicorn backend.main:app --reload --host 0.0.0.0 --port 8000
   ```
2. Check the logs for:
   ```
   Discord bot connected as Claw & Order#1234
   Discord posting to #claw-alerts
   Discord dispatch loop started
   ```
3. Trigger a test milestone (use the fake telemetry method from the Week 1 plan) and confirm the alert appears in your Discord channel

---

## 5. Frequently Asked Questions

### "Will we overload Discord with too many messages?"

**No.** Discord allows 5 messages per 5 seconds per channel. Our bot sends at most ~20 messages across the entire 6-day competition:

| Event Type | When It Fires | Max Volume |
|------------|---------------|------------|
| Milestone alert | When a team hits a milestone (4 types per team) | ~16 total (4 milestones × 4 teams) |
| Phase transition | When you manually advance the phase | Exactly 2 (phase 1→2, 2→3) |
| VM offline | When a team's reporter is silent for 5+ min | 1 per outage per team |

Even if all 4 teams triggered milestones in the same second, the `asyncio.Queue` serializes them one at a time, and discord.py's built-in rate limiter handles the rest.

### "Do we need one bot per team?"

**No.** One bot handles all teams. Every milestone, phase change, and VM alert goes through the same bot to the same channel. The message text includes the team name so you know who triggered it.

### "What if Discord goes down during competition?"

**Nothing breaks.** The bot catches `discord.HTTPException` in the dispatch loop (line 123 in `discord_worker.py`), logs the error, and moves on to the next message. Scoring, SSE streaming, and the dashboard all continue working independently. You just won't see Discord alerts until Discord comes back — and when it does, the bot auto-reconnects (the `on_ready` handler fires again, with a `_dispatch_started` guard to prevent duplicate dispatch loops).

### "What if we don't set the token before competition?"

**The backend still works.** If `DISCORD_TOKEN` is empty, the `start()` function returns `None` and logs a warning:
```
DISCORD_TOKEN not set — Discord worker disabled
```
Events still get scored, SSE still streams, dashboard still works. You just don't get Discord alerts. You can add the token later and restart the backend to enable it mid-competition.

### "Can the bot read our messages or other channels?"

**No.** We create it with `discord.Intents.default()` and zero privileged intents. It has no `on_message` handler. It literally only sends messages to the one channel ID you configure. It cannot see message content in any channel, cannot see who's online, cannot see member lists.

### "What do the alerts actually look like in Discord?"

**Milestone:**
```
🚨 red-1: DMZ compromised | Phase 1, sqli | 70 pts
```

**Phase transition:**
```
📢 Phase 2 (Internal Pivot) now active. New techniques unlocked.
```

**VM offline:**
```
⚠️ red-1 Kali VM silent for 5+ minutes — check reporter process
```

### "Can we have separate channels for different alert types?"

**Not currently**, but the code is structured to support it easily. Right now everything goes to one channel via `DISCORD_CHANNEL_ID`. To split by type, you'd add additional channel ID env vars (e.g., `DISCORD_MILESTONE_CHANNEL_ID`) and route in `_dispatch_loop()` based on `msg["type"]`. This is a nice-to-have, not a competition requirement.

### "What if the bot disconnects from Discord's gateway?"

**It reconnects automatically.** Discord.py handles gateway reconnection internally. When it reconnects, `on_ready` fires again. We have a `_dispatch_started` bool guard (line 88) that prevents a second dispatch loop from spawning. The existing loop keeps running and picks up where it left off. You'll see this in the logs:
```
Discord reconnected — dispatch loop already running
```

### "Do we need to keep the bot running 24/7 before competition?"

**No.** The bot starts when you start the backend and stops when you stop it. During development and testing, it only needs to be running when you want to verify Discord alerts work. On competition day, you start the backend once and leave it running.

---

## 6. File Reference

| File | What It Does |
|------|-------------|
| `backend/services/discord_worker.py` | Bot connection, dispatch loop, message formatting, queue, start/stop |
| `backend/config.py` | Reads `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` from `.env` |
| `backend/main.py` | Starts bot in lifespan (`discord_worker.start()`), stops on shutdown |
| `backend/routers/events.py` | Enqueues milestone alerts after a milestone fires |
| `backend/routers/admin.py` | Enqueues phase transition alert after phase change |
| `backend/services/watchdog.py` | Enqueues VM offline alert when a team goes silent |
| `tests/test_d_discord.py` | 6 tests covering message formatting and queue ordering |
| `.env` | Where you put `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` |
