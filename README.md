# lacerdiscord

Watches a Discord server's **Scheduled Events** and pushes new ones to Telegram.

Read-only poller. Runs in Docker. Never writes anything to Discord.

---

## Read this first

This uses a **user token**, not a bot token. Automating a user account is a
"self-bot" and is [against Discord's ToS](https://support.discord.com/hc/en-us/articles/115002192352-Automated-User-Accounts-Self-Bots);
the stated penalty is account termination. That's why it runs on a throwaway
account. Two things that actually reduce your exposure:

- **Keep the interval slow.** 300s default. Don't drop below ~60s. The single
  biggest detection signal is request volume and perfectly regular cadence —
  hence the built-in jitter.
- **Consider separate egress.** The container shares your host's IP with your
  real Discord session by default. Same-IP is how alt accounts get linked. If
  that matters, set `HTTPS_PROXY` in `.env` (see `docker-compose.yml`).

Also: **rotate that account's password.** It was pasted into a chat log. The
script doesn't need it and never uses it.

---

## One-time setup

### 1. Get the user token

Log into <https://discord.com> **in a browser where you are not logged into your
real account** — use a private window or a separate browser profile. Log in as
the throwaway account, then:

1. `F12` → **Network** tab
2. Filter for `science` or `messages`, then click around Discord to generate a request
3. Click any request → **Headers** → **Request Headers**
4. Copy the value of the **`authorization`** header

It's a long string, no `Bot ` prefix. That's your `DISCORD_TOKEN`.

> The token invalidates if that account changes its password or logs out of all
> devices. If the watcher sends you a `401` alert, redo this step. Otherwise it
> lasts indefinitely — this is the only manual step, and it's not recurring.

### 2. Make sure the account can see the server

The throwaway account must be a **member** of the guild you're watching, and
have permission to view the events. Join it with an invite link before running.

### 3. Create the Telegram bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts
2. Copy the token he gives you → `TELEGRAM_BOT_TOKEN`
3. **Send your new bot any message** (e.g. `/start`). It cannot message you first.
4. Get your chat id — open in a browser:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Find `"chat":{"id":123456789` → that number is `TELEGRAM_CHAT_ID`.

### 4. Configure

```powershell
cd D:\Work\PetProjects\lacerdiscord
copy .env.example .env
notepad .env
```

Fill in the four required values. `GUILD_ID` is already set to the test server
(`1538204737349099613`) — swap it for the real one when you're ready.

---

## Run

```powershell
docker compose up -d --build
docker compose logs -f
```

Expected first-run log:

```
Watching guild 1538204737349099613 every 300s.
First run: seeding baseline with 2 event(s).
```

The first run is **silent by design** — it records what already exists so you
aren't spammed with every pre-existing event. Everything created after that
point notifies. Set `ANNOUNCE_ON_FIRST_RUN=true` in `.env` if you'd rather see
the current list once.

### Self-check (`--check`)

Proves the token authenticates and lists what the account can currently see,
without starting the loop:

```powershell
docker compose run --rm watcher python poller.py --check
```

Prints the authenticated username, the guild being watched, and every scheduled
event visible right now. Run this first — it fails fast and loudly if the token
or guild id is wrong.

`--once` runs exactly one poll cycle and exits, if you want to step through
manually.

### Running without Telegram

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are **optional**. Leave them blank
and the watcher runs normally but prints notifications to the docker log
instead of sending them:

```
WARNING TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - notifications will
        print to this log instead.
INFO    [telegram disabled] would send:
        ----------------------------------------------
        🆕 New Discord event
        ...
```

Useful for testing the Discord half before setting up BotFather. Fill both in
and restart to switch on real delivery.

### Verify it works end to end

Create a scheduled event in your own test server, point `GUILD_ID` at it, and
wait one interval (or set `POLL_INTERVAL_SECONDS=30` temporarily). You should
get a Telegram message. Put the interval back afterwards.

### Common commands

```powershell
docker compose logs -f          # watch it work
docker compose restart          # apply .env changes
docker compose down             # stop
del data\state.json             # reset baseline (next run re-seeds silently)
```

---

## What you get notified about

| Trigger                         | Default   | Env var                |
| ------------------------------- | --------- | ---------------------- |
| New event created               | always on | —                      |
| Time / title / status changed   | on        | `NOTIFY_UPDATES`       |
| Event cancelled or deleted      | on        | `NOTIFY_CANCELLATIONS` |
| Auth failure or 5 failed cycles | always on | —                      |

Volatile fields like RSVP count are deliberately ignored so you don't get
pinged every time someone clicks Interested.

---

## Files

```
poller.py            the watcher (single file, stdlib + requests)
test_diff.py         offline test harness - no network or tokens needed
Dockerfile           python:3.12-slim, runs as non-root
docker-compose.yml   restart policy, state volume, proxy hook
.env.example         copy to .env and fill in
data/state.json      created at runtime; the seen-events snapshot
```

Run the tests any time without touching the network:

```powershell
python test_diff.py
```

---

## Troubleshooting

**`401 Unauthorized`** — token expired or wrong. Re-grab it (step 1). Make sure
you copied the `authorization` header value only, with no `Bot ` prefix and no
quotes.

**`403 Forbidden`** — the account isn't in that guild, or can't view events
there. Join the server with that account.

**`404`** — wrong `GUILD_ID`. Enable Developer Mode in Discord settings
(Advanced → Developer Mode), then right-click the server icon → Copy Server ID.

**Telegram silent, no errors in log** — you never messaged the bot first, or the
chat id is wrong. Re-check step 3.

**Duplicate notifications after restart** — the `./data` volume isn't
persisting. Confirm `data/state.json` exists on the host after a run.
