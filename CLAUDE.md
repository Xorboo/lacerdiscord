# CLAUDE.md

Context for Claude Code working in this repo. Written 2026-08-15.

## What this is

A read-only watcher that polls a Discord server's **Scheduled Events** and
pushes new/changed/cancelled ones to Telegram. Single Python file, runs in
Docker on the user's Windows host (`D:\Work\PetProjects\lacerdiscord`).

**Why it exists:** the user wants notifications for events in a server they are
_not_ an admin of and _cannot_ add a bot to.

## Current status

Live as of 2026-08-15, pointed at the **real** guild `...`.
`--check` passes (authenticated as `yuutsu_re`, 1 event visible), Telegram is
configured and sending, and the container runs under `docker compose up -d`.

Poll cadence is `POLL_INTERVAL_SECONDS=120` with `POLL_JITTER=0.5`, i.e. a real
gap of 60-180s. `ANNOUNCE_ON_FIRST_RUN=true`.

**Switching `GUILD_ID` requires deleting `data/state.json`.** Otherwise every
event from the old guild is absent from the new one and gets reported as
cancelled.

### Open items

- [x] Run `--check` and confirm the token authenticates
- [x] Telegram configured - bot `@lacerda_games_bot`, chat id set in `.env`
- [x] Pointed at the real guild
- [ ] Confirm a newly-created event actually triggers a notification
- [ ] 120s is well below the 300s the ToS-risk notes call for. Fine while
      watching it; consider raising it for the long run
- [ ] `GUILD_ID` is still the throwaway test server; the real one is TBD
- [ ] Not a git repo yet. If initializing: `.gitignore` already excludes `.env`
      — verify that holds before any first commit

## Security — read before touching `.env`

`.env` contains a **live Discord user token**. Treat it as a password.

- Never `cat`, `echo`, print, or paste the contents of `.env` into chat output
- Never copy the token into any other file, including docs or examples
- It is gitignored; keep it that way
- The account is a disposable alt with nothing important on it, but the token
  still grants full account access

## The big constraint: this is a self-bot

The script authenticates with a **user token**, not a bot token (no `Bot `
prefix on the `Authorization` header). This is the only way to read a guild's
scheduled events without adding a bot to it — there is no unauthenticated or
OAuth route. `GET /guilds/{id}/scheduled-events` requires a member.

Discord's ToS forbids automating user accounts and the stated penalty is
account termination. The user knows and accepted this; a throwaway account was
created for it. Design rules that follow from this, **do not violate them**:

- **Read-only. Never write to Discord.** No message sends, no reactions, no
  event creation, no joins. The only calls are `GET /users/@me` and
  `GET /guilds/{id}/scheduled-events`.
- **Keep the poll rate low.** 300s in production, floor around 60s. Request
  volume and perfectly regular cadence are the main detection signals — hence
  `POLL_JITTER`.
- **Never use email/password login.** Automated credential login triggers
  CAPTCHA and new-location flags. Token only. (The user pasted the account
  password in chat early on; it was deliberately not used and they were told to
  rotate it.)
- Datacenter IPs are heavily flagged — this must run from the user's host, not
  a cloud sandbox.

## Architecture

```
poller.py            everything: config, Discord client, diff, Telegram, loop
test_diff.py         offline harness, no network or real tokens needed
Dockerfile           python:3.12-slim, non-root user, state at /data
docker-compose.yml   restart policy, ./data volume, HTTPS_PROXY hook
.env                 live config (gitignored)
.env.example         template
data/state.json      runtime snapshot of seen events
```

Only third-party dependency is `requests`.

### Flow

`fetch_events()` → `summarize()` each event down to watched fields →
`diff()` against `data/state.json` → render → `Telegram.send()` →
`save_state()`.

### Deliberate design choices — don't "fix" these

- **State is saved only _after_ notifications are sent.** A crash mid-notify
  replays on restart rather than silently swallowing an event. Losing an event
  is worse than a duplicate.
- **First run has no baseline.** `ANNOUNCE_ON_FIRST_RUN` now defaults to `true`
  so a fresh setup proves itself. Set it `false` on the real server if being
  messaged about every pre-existing event would be noise.
- **`diff()` compares only `WATCHED` fields.** Volatile fields like
  `user_count` are excluded on purpose — otherwise every RSVP click fires a
  notification.
- **`save_state()` writes to `.tmp` then `replace()`** so a kill mid-write
  can't corrupt state. A corrupt/missing state file degrades to "first run"
  rather than crashing.
- **Telegram is optional.** Missing creds → console fallback, not a hard exit.
- **`FatalError`** is for auth/config problems retrying won't fix; it alerts and
  exits. Everything else keeps the loop alive with a failure counter.
- **Fatal alerts are deduped via `data/last_fatal.txt`.** The process exits on
  `FatalError` and docker restarts it, so without the marker a dead token would
  fire the same Telegram message every restart, forever. A successful cycle
  clears the marker and sends a "recovered" note.

## Commands

```powershell
docker compose run --rm watcher python poller.py --check   # validate + list
docker compose run --rm watcher python poller.py --once    # one cycle, exit
docker compose up -d --build
docker compose logs -f
docker compose restart          # after .env changes
del data\state.json             # reset baseline
python test_diff.py             # offline tests, no network needed
```

## Gotchas

- **Token rotates** when that account changes password or logs out everywhere.
  Symptom is a `401` and a Telegram alert; fix is re-grabbing it from DevTools
  (README step 1). Expected, not a bug.
- **Telegram bots cannot message a user first.** If sends silently fail, the
  user probably never sent the bot a `/start`.
- **Docker shares the host IP** with the user's real Discord session, which is
  how alt accounts get correlated. `HTTPS_PROXY` in `.env` routes around it;
  `requests` picks it up with no code change.
- Snowflake IDs decode to creation timestamps: `(id >> 22) + 1420070400000` ms.
  Useful for sanity-checking a token's account id offline.

## Testing

`test_diff.py` covers the diff and render logic with mock payloads — no network,
no credentials. Run it after any change to `diff()`, `summarize()`, `WATCHED`,
or the renderers. It checks HTML escaping of user-supplied event titles, that
volatile fields don't trigger updates, and that a restart doesn't replay.

There is no test coverage of the live API path; that can only be verified by
running `--check`.

---

**Note:** this file was written by hand with full project context. `/init`
scans the code but won't recover the ToS constraints, the rationale above, or
the open items — if you run it, merge rather than overwrite.
