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
    |── events.py ──── milestone fires ──> queue_notification()
    |── admin.py ───── phase change ────> queue_notification()
    |── watchdog.py ── VM offline ──────> queue_notification()
    |                                              |
    |                                              v
    |                                     discord_outbox (database)
    |                                              |
    |                                              v
    |                                     _outbox_loop() ── retry until delivered
    |                                              |
    |                                              v
    |                                     discord.TextChannel.send()
    |                                              |
    |                                              v
    |                                     Your existing Discord channel
```

### Key design points:

- **One bot and one alert channel** — each alert is persisted in the same transaction as the scoring or phase change
- **Retryable delivery** — the worker processes pending rows in order, with exponential backoff after a send failure
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

Alert volume grows with the number of teams. This bot sends milestones, phase changes, and offline notices rather than every tool event:

| Event Type | When It Fires | Max Volume |
|------------|---------------|------------|
| Milestone alert | When a team hits a milestone (4 types per team) | Up to 4 per team |
| Phase transition | When you manually advance the phase | Exactly 2 (phase 1→2, 2→3) |
| VM offline | When a team's reporter is silent for 5+ min | 1 per outage per team |

The database outbox sends alerts in order and retries failures with exponential backoff; discord.py also handles Discord rate limits.

### "Do we need one bot per team?"

**No.** One bot handles all teams. Every milestone, phase change, and VM alert goes through the same bot to the same channel. The message text includes the team name so you know who triggered it.

### "What if Discord goes down during competition?"

Scoring continues. Alerts stay in `discord_outbox` and retry after Discord returns or the backend restarts. Check `discord_pending` in `/api/health`; a growing value means delivery is delayed. Delivery is at least once: a crash after Discord accepts a message but before the database records success can cause a repeated alert.

### "What if we don't set the token before competition?"

**The backend still works.** If `DISCORD_TOKEN` is empty, the `start()` function returns `None` and logs a warning:
```
DISCORD_TOKEN not set — Discord worker disabled
```
Events still get scored and alerts remain pending in the database. Add the token later and restart the backend to deliver them.

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

**Not currently.** Everything goes to one alert channel via `DISCORD_CHANNEL_ID`.

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
| `backend/services/discord_worker.py` | Bot connection, durable outbox dispatch, retry, message formatting, start/stop |
| `backend/config.py` | Reads `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` from `.env` |
| `backend/main.py` | Starts bot in lifespan (`discord_worker.start()`), stops on shutdown |
| `backend/routers/events.py` | Saves milestone alerts with the event transaction |
| `backend/routers/admin.py` | Saves phase alerts with the phase transition |
| `backend/services/watchdog.py` | Saves VM offline alerts when a team goes silent |
| `tests/test_d_discord.py`, `tests/test_discord_outbox.py` | Formatting and retry coverage |
| `.env` | Where you put `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` |
