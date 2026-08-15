"""Discord scheduled-event watcher -> Telegram.

Read-only. Polls GET /guilds/{id}/scheduled-events on an interval, diffs the
result against a persisted snapshot, and pushes anything new/changed/cancelled
to a Telegram chat.

Uses a *user* token (self-bot territory, against Discord ToS). Kept deliberately
low-rate and read-only. Never writes to Discord.
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any

import requests

log = logging.getLogger("lacerda")

API = "https://discord.com/api/v10"
TG_API = "https://api.telegram.org"

# Discord event status codes
STATUS = {1: "Scheduled", 2: "Active", 3: "Completed", 4: "Cancelled"}
ENTITY = {1: "Stage", 2: "Voice", 3: "External"}


class Config:
    def __init__(self) -> None:
        self.discord_token = _req("DISCORD_TOKEN")
        self.guild_id = _req("GUILD_ID")

        # Telegram is optional. Without both values the watcher still runs and
        # prints notifications to the log instead - useful for testing the
        # Discord half before BotFather is set up.
        self.tg_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.tg_chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.tg_enabled = bool(self.tg_token and self.tg_chat)

        self.interval = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
        self.jitter = float(os.getenv("POLL_JITTER", "0.25"))
        self.state_path = Path(os.getenv("STATE_PATH", "/data/state.json"))
        self.notify_updates = _flag("NOTIFY_UPDATES", True)
        self.notify_cancellations = _flag("NOTIFY_CANCELLATIONS", True)
        # First run has no baseline. Defaults to announcing everything it finds
        # so a fresh setup proves itself; set false to seed silently instead.
        self.announce_on_first_run = _flag("ANNOUNCE_ON_FIRST_RUN", True)
        self.user_agent = os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        )


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        sys.exit(f"Missing required env var: {name}")
    return v


def _flag(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.warning("State file unreadable (%s); treating as first run.", e)
        return None


def save_state(path: Path, events: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"events": events, "saved_at": time.time()}, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)  # atomic-ish; survives a kill mid-write


# --------------------------------------------------------------------------
# discord
# --------------------------------------------------------------------------

class Discord:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": cfg.discord_token,  # user token: no "Bot " prefix
            "User-Agent": cfg.user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def whoami(self) -> dict:
        """Confirms the token authenticates and shows which account it is."""
        r = self.s.get(f"{API}/users/@me", timeout=30)
        if r.status_code == 401:
            raise FatalError(
                "401 on /users/@me - the token is invalid or expired. Re-grab "
                "it from the web client (README step 1)."
            )
        r.raise_for_status()
        return r.json()

    def fetch_events(self) -> list[dict]:
        url = f"{API}/guilds/{self.cfg.guild_id}/scheduled-events"
        r = self.s.get(url, timeout=30)

        if r.status_code == 401:
            raise FatalError(
                "401 Unauthorized - token is invalid or expired. Re-grab it "
                "from the web client (see README) and restart."
            )
        if r.status_code == 403:
            raise FatalError(
                "403 Forbidden - this account cannot see that guild. Confirm "
                "the account is a member and GUILD_ID is correct."
            )
        if r.status_code == 404:
            raise FatalError(f"404 - guild {self.cfg.guild_id} not found.")
        if r.status_code == 429:
            retry = float(r.headers.get("Retry-After", 60))
            log.warning("Rate limited; sleeping %.0fs", retry)
            time.sleep(retry + 1)
            return self.fetch_events()

        r.raise_for_status()
        return r.json()


class FatalError(RuntimeError):
    """Config/auth problem that retrying will not fix."""


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------

# Fields worth waking someone up for. Ignores volatile stuff like user_count.
WATCHED = ("name", "description", "scheduled_start_time",
           "scheduled_end_time", "status", "entity_type", "channel_id")


def summarize(ev: dict) -> dict:
    """Reduce an API event to the fields we persist and compare."""
    meta = ev.get("entity_metadata") or {}
    return {
        "id": ev.get("id"),
        "name": ev.get("name"),
        "description": ev.get("description"),
        "scheduled_start_time": ev.get("scheduled_start_time"),
        "scheduled_end_time": ev.get("scheduled_end_time"),
        "status": ev.get("status"),
        "entity_type": ev.get("entity_type"),
        "channel_id": ev.get("channel_id"),
        "location": meta.get("location"),
        "creator": (ev.get("creator") or {}).get("username"),
    }


def diff(old: dict[str, dict], new: dict[str, dict]) -> dict[str, list]:
    created = [new[k] for k in new if k not in old]
    cancelled = [old[k] for k in old if k not in new]

    updated = []
    for k in new:
        if k not in old:
            continue
        changes = [
            (f, old[k].get(f), new[k].get(f))
            for f in WATCHED
            if old[k].get(f) != new[k].get(f)
        ]
        if changes:
            updated.append((new[k], changes))

    return {"created": created, "updated": updated, "cancelled": cancelled}


# --------------------------------------------------------------------------
# telegram
# --------------------------------------------------------------------------

class Telegram:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.s = requests.Session()

    def send(self, text: str) -> None:
        if not self.cfg.tg_enabled:
            # Console fallback: strip tags so the log stays readable.
            import re
            plain = re.sub(r"<[^>]+>", "", text)
            plain = (plain.replace("&amp;", "&")
                          .replace("&lt;", "<").replace("&gt;", ">"))
            log.info("[telegram disabled] would send:\n%s\n%s",
                     "-" * 46, plain)
            return

        url = f"{TG_API}/bot{self.cfg.tg_token}/sendMessage"
        payload = {
            "chat_id": self.cfg.tg_chat,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(3):
            try:
                r = self.s.post(url, json=payload, timeout=30)
                if r.status_code == 429:
                    wait = r.json().get("parameters", {}).get("retry_after", 5)
                    time.sleep(wait + 1)
                    continue
                r.raise_for_status()
                return
            except requests.RequestException as e:
                log.warning("Telegram send failed (attempt %d): %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        log.error("Giving up on Telegram message after 3 attempts.")


def esc(s: Any) -> str:
    t = "" if s is None else str(s)
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    # ISO8601 from Discord; render as a Telegram-friendly UTC string.
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%a %d %b %Y, %H:%M UTC")
    except (ValueError, TypeError):
        return iso


def where(ev: dict) -> str:
    if ev.get("location"):
        return esc(ev["location"])
    if ev.get("channel_id"):
        return f"{ENTITY.get(ev.get('entity_type'), 'Channel')} channel"
    return "—"


def render_created(ev: dict, guild_id: str) -> str:
    link = f"https://discord.com/events/{guild_id}/{ev['id']}"
    lines = [
        "🆕 <b>New Discord event</b>",
        "",
        f"<b>{esc(ev.get('name'))}</b>",
        f"🕒 {fmt_time(ev.get('scheduled_start_time'))}",
        f"📍 {where(ev)}",
    ]
    if ev.get("creator"):
        lines.append(f"👤 {esc(ev['creator'])}")
    if ev.get("description"):
        d = ev["description"]
        d = d[:400] + "…" if len(d) > 400 else d
        lines += ["", esc(d)]
    lines += ["", f'<a href="{link}">Open in Discord</a>']
    return "\n".join(lines)


LABELS = {
    "name": "Title",
    "description": "Description",
    "scheduled_start_time": "Start",
    "scheduled_end_time": "End",
    "status": "Status",
    "entity_type": "Type",
    "channel_id": "Channel",
}


def render_updated(ev: dict, changes: list, guild_id: str) -> str:
    link = f"https://discord.com/events/{guild_id}/{ev['id']}"
    lines = ["✏️ <b>Event updated</b>", "", f"<b>{esc(ev.get('name'))}</b>", ""]
    for field, before, after in changes:
        label = LABELS.get(field, field)
        if field == "status":
            before, after = STATUS.get(before, before), STATUS.get(after, after)
        elif field == "entity_type":
            before, after = ENTITY.get(before, before), ENTITY.get(after, after)
        elif field in ("scheduled_start_time", "scheduled_end_time"):
            before, after = fmt_time(before), fmt_time(after)
        elif field == "description":
            before, after = "(changed)", "(changed)"
        lines.append(f"• {label}: {esc(before)} → <b>{esc(after)}</b>")
    lines += ["", f'<a href="{link}">Open in Discord</a>']
    return "\n".join(lines)


def render_gone(ev: dict) -> str:
    return "\n".join([
        "❌ <b>Event removed or cancelled</b>",
        "",
        f"<b>{esc(ev.get('name'))}</b>",
        f"🕒 was {fmt_time(ev.get('scheduled_start_time'))}",
    ])


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

_stop = False


def _handle_signal(signum, _frame):
    global _stop
    log.info("Signal %s received; finishing current cycle and exiting.", signum)
    _stop = True


def fatal_marker(cfg: Config) -> Path:
    return cfg.state_path.with_name("last_fatal.txt")


def alert_fatal(cfg: Config, tg: Telegram, err: Exception) -> None:
    """Push a fatal error to Telegram, once per distinct cause.

    A FatalError exits the process and docker restarts us, so without the
    marker a bad token would fire the same alert on every restart forever.
    """
    msg = str(err)
    marker = fatal_marker(cfg)
    try:
        if marker.read_text("utf-8") == msg:
            log.info("Already alerted on this error; not re-sending.")
            return
    except OSError:
        pass  # no marker yet, or unreadable - alert and try to write it

    tg.send(f"⚠️ <b>Watcher stopped</b>\n\n{esc(msg)}")
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(msg, encoding="utf-8")
    except OSError as e:
        log.warning("Could not record fatal marker: %s", e)


def clear_fatal(cfg: Config) -> None:
    """A good cycle means the next failure is news again."""
    fatal_marker(cfg).unlink(missing_ok=True)


def run_cycle(cfg: Config, dc: Discord, tg: Telegram) -> None:
    raw = dc.fetch_events()
    current = {e["id"]: summarize(e) for e in raw}

    prior = load_state(cfg.state_path)

    if prior is None:
        log.info("First run: seeding baseline with %d event(s).", len(current))
        if cfg.announce_on_first_run:
            for ev in current.values():
                tg.send(render_created(ev, cfg.guild_id))
        save_state(cfg.state_path, current)
        return

    d = diff(prior.get("events", {}), current)

    for ev in d["created"]:
        log.info("NEW: %s", ev.get("name"))
        tg.send(render_created(ev, cfg.guild_id))

    if cfg.notify_updates:
        for ev, changes in d["updated"]:
            log.info("UPDATED: %s (%s)", ev.get("name"),
                     ", ".join(c[0] for c in changes))
            tg.send(render_updated(ev, changes, cfg.guild_id))

    if cfg.notify_cancellations:
        for ev in d["cancelled"]:
            log.info("GONE: %s", ev.get("name"))
            tg.send(render_gone(ev))

    if not any((d["created"], d["updated"], d["cancelled"])):
        log.info("No changes (%d event(s) tracked).", len(current))

    # Saved only after sends succeed, so a crash mid-notify replays rather
    # than silently swallowing an event.
    save_state(cfg.state_path, current)


def do_check(cfg: Config, dc: Discord, tg: Telegram) -> int:
    """--check: prove the token works and show what's currently visible."""
    print("=" * 60)
    me = dc.whoami()
    print(f"Authenticated as : {me.get('username')}  (id {me.get('id')})")
    print(f"Email            : {me.get('email') or 'n/a'}")
    print(f"Watching guild   : {cfg.guild_id}")
    print(f"Telegram         : {'enabled' if cfg.tg_enabled else 'DISABLED (console only)'}")
    print("-" * 60)

    events = dc.fetch_events()
    if not events:
        print("No scheduled events currently visible in this guild.")
        print("Create one in Discord, then run --check again.")
    else:
        print(f"{len(events)} event(s) visible:\n")
        for e in events:
            s = summarize(e)
            print(f"  [{s['id']}] {s['name']}")
            print(f"      start  : {fmt_time(s['scheduled_start_time'])}")
            print(f"      status : {STATUS.get(s['status'], s['status'])}")
            print(f"      where  : {s.get('location') or ENTITY.get(s['entity_type'], '?')}")
            print()

    print("-" * 60)
    print(f"State file: {cfg.state_path} "
          f"({'exists' if cfg.state_path.exists() else 'not yet created'})")
    print("=" * 60)

    if tg.cfg.tg_enabled:
        tg.send("✅ <b>Watcher self-check passed</b>\n\n"
                f"Authenticated as {esc(me.get('username'))}\n"
                f"{len(events)} event(s) currently visible.")
        print("Test message sent to Telegram.")
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    args = set(sys.argv[1:])
    cfg = Config()
    dc, tg = Discord(cfg), Telegram(cfg)

    if not cfg.tg_enabled:
        log.warning("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - "
                    "notifications will print to this log instead.")

    try:
        if "--check" in args:
            return do_check(cfg, dc, tg)
        if "--once" in args:
            run_cycle(cfg, dc, tg)
            return 0
    except FatalError as e:
        log.error("%s", e)
        alert_fatal(cfg, tg, e)
        return 1

    log.info("Watching guild %s every %ds.", cfg.guild_id, cfg.interval)

    consecutive_failures = 0
    while not _stop:
        try:
            run_cycle(cfg, dc, tg)
            if consecutive_failures:
                tg.send("✅ <b>Watcher recovered</b> - polling again.")
            consecutive_failures = 0
            clear_fatal(cfg)
        except FatalError as e:
            log.error("%s", e)
            alert_fatal(cfg, tg, e)
            return 1
        except Exception as e:  # noqa: BLE001 - keep the loop alive
            consecutive_failures += 1
            log.exception("Cycle failed (%d in a row): %s", consecutive_failures, e)
            if consecutive_failures == 5:
                tg.send(f"⚠️ Watcher has failed 5 cycles in a row.\n{esc(e)}")

        # Jitter so requests don't land on a perfectly regular cadence.
        delay = cfg.interval * (1 + random.uniform(-cfg.jitter, cfg.jitter))
        slept = 0.0
        while slept < delay and not _stop:
            time.sleep(min(1.0, delay - slept))
            slept += 1.0

    log.info("Exited cleanly.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
