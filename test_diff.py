"""Dry-run harness. No network, no tokens - exercises the diff/render logic.

Run:  python test_diff.py
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Satisfy Config() before importing anything that reads env.
os.environ.setdefault("DISCORD_TOKEN", "x")
os.environ.setdefault("GUILD_ID", "1538204737349099613")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")

import poller  # noqa: E402

GUILD = "1538204737349099613"
FAILS = []


def check(label, cond):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILS.append(label)


def ev(eid, name, start, status=1, desc=None, loc="The Bar"):
    return {
        "id": eid,
        "guild_id": GUILD,
        "name": name,
        "description": desc,
        "scheduled_start_time": start,
        "scheduled_end_time": None,
        "status": status,
        "entity_type": 3,
        "channel_id": None,
        "entity_metadata": {"location": loc},
        "creator": {"username": "someone"},
        "user_count": 12,
    }


A = ev("111", "Lacerda Live Set", "2026-09-01T20:00:00+00:00", desc="Doors at 7.")
B = ev("222", "Open Decks", "2026-09-08T21:00:00+00:00")
C = ev("333", "Afterparty", "2026-09-09T02:00:00+00:00")


def s(*events):
    return {e["id"]: poller.summarize(e) for e in events}


print("\n[1] baseline: no changes between identical snapshots")
d = poller.diff(s(A, B), s(A, B))
check("nothing created", d["created"] == [])
check("nothing updated", d["updated"] == [])
check("nothing cancelled", d["cancelled"] == [])

print("\n[2] a new event appears")
d = poller.diff(s(A, B), s(A, B, C))
check("one created", len(d["created"]) == 1)
check("it's the right one", d["created"][0]["name"] == "Afterparty")
check("no false updates", d["updated"] == [])

print("\n[3] volatile field (user_count) must NOT trigger an update")
B_hot = dict(B, user_count=9999)
d = poller.diff(s(A, B), s(A, B_hot))
check("user_count ignored", d["updated"] == [])

print("\n[4] start time moved")
B_moved = dict(B, scheduled_start_time="2026-09-08T23:30:00+00:00")
d = poller.diff(s(A, B), s(A, B_moved))
check("one update", len(d["updated"]) == 1)
changed = [c[0] for c in d["updated"][0][1]]
check("start time flagged", changed == ["scheduled_start_time"])

print("\n[5] event cancelled in place (status 1 -> 4)")
B_cx = dict(B, status=4)
d = poller.diff(s(A, B), s(A, B_cx))
check("flagged as update", len(d["updated"]) == 1)
check("status field flagged", "status" in [c[0] for c in d["updated"][0][1]])

print("\n[6] event vanishes from the list entirely")
d = poller.diff(s(A, B), s(A))
check("one cancelled", len(d["cancelled"]) == 1)
check("it's Open Decks", d["cancelled"][0]["name"] == "Open Decks")

print("\n[7] renderers produce non-empty HTML and escape correctly")
nasty = ev("444", "Rock & <b>Roll</b>", "2026-10-01T20:00:00+00:00")
msg = poller.render_created(poller.summarize(nasty), GUILD)
check("ampersand escaped", "&amp;" in msg)
# The title is legitimately wrapped in our own <b>…</b>; what must NOT survive
# is the user-supplied markup inside it.
check("user markup escaped", "&lt;b&gt;Roll&lt;/b&gt;" in msg)
check("no raw injected tag", "<b>Roll</b>" not in msg)
check("event link present", f"discord.com/events/{GUILD}/444" in msg)
check("time humanized", "Thu 01 Oct 2026" in msg)

upd = poller.render_updated(
    poller.summarize(B_cx), [("status", 1, 4)], GUILD)
check("status names rendered", "Scheduled" in upd and "Cancelled" in upd)
check("gone renderer works", "Open Decks" in poller.render_gone(poller.summarize(B)))

print("\n[8] state round-trips and first run is detected")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "nested" / "state.json"
    check("missing file -> first run", poller.load_state(p) is None)
    poller.save_state(p, s(A, B))
    loaded = poller.load_state(p)
    check("file created", p.exists())
    check("round-trips intact", loaded["events"] == s(A, B))
    d = poller.diff(loaded["events"], s(A, B))
    check("reload causes no replay", not any(d.values()))
    p.write_text("{ broken json")
    check("corrupt file -> first run", poller.load_state(p) is None)

print("\n[9] no-op cycle after a restart with 3 events")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "state.json"
    poller.save_state(p, s(A, B, C))
    d = poller.diff(poller.load_state(p)["events"], s(A, B, C))
    check("zero notifications", sum(len(v) for v in d.values()) == 0)

print("\n[10] fatal alerts reach Telegram once, not once per docker restart")
with tempfile.TemporaryDirectory() as td:
    sent = []
    cfg = SimpleNamespace(state_path=Path(td) / "state.json")
    tg = SimpleNamespace(send=sent.append)

    poller.alert_fatal(cfg, tg, poller.FatalError("401 - token expired"))
    check("first fatal alerts", len(sent) == 1)
    check("message carries the cause", "401 - token expired" in sent[0])

    poller.alert_fatal(cfg, tg, poller.FatalError("401 - token expired"))
    check("same cause not re-sent", len(sent) == 1)

    poller.alert_fatal(cfg, tg, poller.FatalError("403 - not a member"))
    check("a different cause does alert", len(sent) == 2)

    poller.clear_fatal(cfg)
    poller.alert_fatal(cfg, tg, poller.FatalError("403 - not a member"))
    check("alerts again after recovery", len(sent) == 3)

print("\n" + "=" * 52)
if FAILS:
    print(f"{len(FAILS)} FAILURE(S):")
    for f in FAILS:
        print("  -", f)
    sys.exit(1)
print("All checks passed.")
