#!/usr/bin/env python3
"""Floor now: one card at the top of the phone app with the three numbers the floor asks for most, ready before
anyone asks.

  speed   this hour's sorter pace (boxes/hr) next to normal, Vendor A and Vendor B apart (Vendor A = floor seats
          1-8 + 16-22)
  freight packages still in full gaylords on the floor and how many belt minutes that is (unsorted_board.py
          saves data/unsorted-latest.json every 10 min; stray packages are not belt freight)
  trucks  the next inbound trucks with ETA and packages, from the hourly sort report feed (posted at :16),
          read at most once per new post

Runs every 10 minutes 4 PM-8 AM. 10, not 5: each send is one KV write, and the free plan allows 1,000 a day.

    python3 floor_now.py            build and send
    python3 floor_now.py --dry-run  print it, send nothing
    python3 floor_now.py --say      one ready-to-send answer from the saved files only (1 s): floor left + next trucks.
                                    The answer to "how much is left / what's coming", with each number's read time.
"""
import argparse
import json
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
UNSORTED = DATA / "unsorted-latest.json"
TRUCKS = DATA / "floor-trucks.json"
FEEDER_APP = DATA / "feeder-app.json"
CARD = DATA / "floor-now.json"  # last built card, read by --say (speed) without calling the WMS
# The hourly sort report as text, newest post last: {"text": "..."}. Whatever publishes the report writes it here
# (in the demo, a sample file); HUBOPS_REPORT_FEED points somewhere else.
REPORT_FEED = Path(os.environ.get("HUBOPS_REPORT_FEED") or DATA / "hourly-report.json")
HUB = "HOU.H"
NORMAL_DEFAULT = 26000  # busy-hour normal if the hourly check hasn't saved one


def is_vendor_a(seat):
    return seat.isdigit() and (int(seat) <= 8 or 16 <= int(seat) <= 22)


def speed(now):
    import chute_alerts as ca
    import feeder_pace as fp
    tok = [ca.wms_token()]
    start = now.replace(hour=16, minute=0, second=0, microsecond=0) - (timedelta(days=1) if now.hour < 16 else timedelta())
    hours = fp.by_hour(fp.fetch_day(tok, now.date()))
    if start.date() != now.date():  # after midnight: tonight began yesterday
        hours.update(fp.by_hour(fp.fetch_day(tok, start.date())))
    night = sum(v.get("", (0, 0))[0] for k, v in hours.items() if k >= start.strftime("%Y-%m-%d %H"))
    key = now.strftime("%Y-%m-%d %H")
    out = {"normal": NORMAL_DEFAULT, "night": night}
    try:
        h = json.loads(FEEDER_APP.read_text())["hours"]
        out["normal"] = int(h[-1].get("normal") or NORMAL_DEFAULT)
    except Exception:
        pass
    seats = hours.get(key)
    mins = now.minute
    last_key = (now - timedelta(hours=1)).strftime("%Y-%m-%d %H")
    if not seats and mins >= 5:
        # Nothing fed this hour: the belt is stopped. Say so; the last full hour's pace is history, not "speed now"
        # (otherwise the card keeps showing a full pace for a sorter that was shut off half an hour ago).
        last = hours.get(last_key) or {}
        out.update(hour=fp.label(key), minutes=mins, per_hour=0, vendor_a=0, vendor_b=0, rej=None, stopped=True,
                   last_hour={"hour": fp.label(last_key), "fed": last.get("", (0, 0))[0]} if last else None)
        return out
    if not seats or mins < 5:  # too early in the hour: show the last full hour instead
        key, seats, mins = last_key, hours.get(last_key), 60
    if not seats:
        return out
    fed, rej = seats.get("", (0, 0))
    va = sum(v[0] for s, v in seats.items() if is_vendor_a(s))
    k = 60 / mins
    out.update(hour=fp.label(key), minutes=mins, per_hour=round(fed * k), vendor_a=round(va * k), vendor_b=round((fed - va) * k),
               rej=round(100 * rej / fed, 1) if fed else None)
    return out


BOARD_MAX_MIN = 30  # the board's count is shown only this fresh; older = "no current count"


def freight(now=None):
    """What the unsorted board says is left, split three ways: on the floor (gaylords scanned in and still full), not
    unloaded (on a truck that arrived, never scanned in), and tonight's part (trucks in before midnight).
    {"error": ...} when the board's own time is too old: a stale count must never be shown as live."""
    now = now or datetime.now()
    try:
        b = json.loads(UNSORTED.read_text())
    except Exception:
        return None
    made = datetime.strptime(str(b.get("made"))[:16], "%Y-%m-%d %H:%M")
    if now - made > timedelta(minutes=BOARD_MAX_MIN):
        return {"error": f"floor board down since {made:%-m/%-d %-I:%M %p}", "stale_since": b.get("made")}
    import live_state as ls
    trucks, waiting = [], []
    for t in b.get("trucks", []):
        noscan = t.get("noscan_in_full", 0)
        if t.get("state") == "no scans":  # unloaded without scanning: its gaylords ARE on the floor
            noscan = 0
        on_floor = t["in_full"] - noscan
        if on_floor > 0:
            trucks.append({"from": t["from"], "in_full": on_floor, "open": t["open"] - (t.get("noscan_full", 0) if noscan else 0), "arrived": t["arrived"][5:16],
                           "codes": [x["code"] for x in t["bags"] if x["full"] and not (noscan and x.get("noscan"))][:6]})
        if noscan:
            waiting.append({"from": t["from"], "pcs": noscan, "gaylords": t.get("noscan_full", 0), "arrived": t["arrived"][:16],
                            "state": t.get("state") or "missed scan"})
    carried, tonight, later = (sum(t["left"] for t in part) for part in ls.split_by_night(b.get("trucks", []), now))
    return {"updated": b.get("made"), "in_full": sum(t["in_full"] for t in trucks), "trucks": trucks,
            "not_unloaded": waiting, "not_unloaded_pcs": sum(w["pcs"] for w in waiting),
            "carried": carried, "tonight": tonight, "later": later}


def parse_trucks(text, now):
    """The newest 'Due to arrive' list in the report text: [{task, pkgs, route, eta}], soonest first."""
    i = text.rfind("Due to arrive (")
    if i < 0:
        return []
    seen = {}
    for m in re.finditer(r"(TT\d+)\s+([\d,]+)\s+(\S+?)\((\d\d:\d\d)\)\s+ETA:\s*(\d{4}-\d\d-\d\d \d\d:\d\d)", text[i:]):
        seen[m.group(1)] = {"task": m.group(1), "pkgs": int(m.group(2).replace(",", "")),
                            "route": m.group(3).replace(f"-{HUB}", "").replace(f"{HUB}-", ""), "eta": m.group(5)}
    soon = [t for t in seen.values() if datetime.strptime(t["eta"], "%Y-%m-%d %H:%M") > now - timedelta(hours=2)]
    return sorted(soon, key=lambda t: t["eta"])


def num(text):
    return int(text.replace(",", ""))


def parse_post(text):
    """The newest post's own numbers: {"waiting": {trunk, branch}, "before_cutoff": {trucks, pcs}, "by_dest": {...}}.
    "Waiting to bag" = packages here and not bagged yet (the report's count of what is left); "Due before cutoff
    today" = trucks still due before the inbound cutoff."""
    flat = re.sub(r"\s+", " ", text)
    out = {}
    head = None
    for head in re.finditer(r"Updated: (\d{4}-\d\d-\d\d \d\d:\d\d) Waiting to bag Linehaul ([\d,]+) Branch ([\d,]+) "
                            r"Due before cutoff today (\d+) ?trucks? ([\d,]+) ?pcs", flat):
        pass
    if head:
        out["posted"] = head.group(1)
        out["waiting"] = {"trunk": num(head.group(2)), "branch": num(head.group(3))}
        out["before_cutoff"] = {"trucks": int(head.group(4)), "pcs": num(head.group(5))}
    i = flat.rfind("⏳ Waiting to bag")
    j = flat.find("Due to arrive (", i)
    if i >= 0:
        out["by_dest"] = {m.group(1): num(m.group(2))
                          for m in re.finditer(r"\b([A-Z][A-Z0-9]*\d{0,2}(?:\.H)?) ?: ?([\d,]+)", flat[i:j if j > i else i + 1500])}
    return out


def read_due(cached, now):
    """True when the feed should be read again: the hourly report goes up at :16, so read it a minute or two after,
    not up to 40 minutes later (and at least every 45 min in case a post is late)."""
    if not cached or not cached.get("read"):
        return True
    read = datetime.fromisoformat(cached["read"])
    if "waiting" not in cached and now - read >= timedelta(minutes=10):
        return True  # the post's own numbers weren't read (older file, or the post's layout changed)
    due = now.replace(minute=17, second=0, microsecond=0)
    if now < due:
        due -= timedelta(hours=1)
    posted = cached.get("posted") and datetime.strptime(cached["posted"], "%Y-%m-%d %H:%M")
    if (not posted or posted < due - timedelta(minutes=5)) and now - read >= timedelta(minutes=5):
        return True  # a newer post should be up by now
    return now - read >= timedelta(minutes=45)


def read_feed():
    """The report feed's text, newest post last."""
    feed = json.loads(REPORT_FEED.read_text())
    if isinstance(feed, dict) and "posts" in feed:
        return "\n".join(p if isinstance(p, str) else p.get("text", "") for p in feed["posts"])
    return feed["text"] if isinstance(feed, dict) else str(feed)


def trucks(now):
    try:
        cached = json.loads(TRUCKS.read_text())
    except Exception:
        cached = None
    if not read_due(cached, now):
        return cached
    import fcntl
    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(TRUCKS.with_suffix(".lock"), "w")
    try:  # live_state.py and this script's own job can both come due in the same minute: one read at a time
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return cached or {"read": None, "posted": None, "list": []}
    try:
        text = read_feed()
        upd = re.findall(r"Updated: (\d{4}-\d\d-\d\d \d\d:\d\d)", text)
        cached = {"read": now.isoformat(timespec="minutes"), "posted": upd[-1] if upd else None, "list": parse_trucks(text, now),
                  **{k: v for k, v in parse_post(text).items() if k != "posted"}}
        tmp = TRUCKS.with_suffix(".tmp")
        tmp.write_text(json.dumps(cached, ensure_ascii=False))
        tmp.replace(TRUCKS)
    except Exception as e:  # feed unreadable: keep the last list, marked old (its own "posted" time says how old)
        if cached:
            cached["error"] = str(e)[:120]
        else:
            cached = {"read": None, "posted": None, "list": [], "error": str(e)[:120]}
    return cached


def build(now):
    out = {"updated": now.strftime("%Y-%m-%d %H:%M:%S")}
    for name, fn in (("speed", lambda: speed(now)), ("freight", lambda: freight(now)), ("trucks", lambda: trucks(now))):
        try:
            out[name] = fn()
        except Exception as e:
            out[name] = {"error": str(e)[:120]}
    t = out.get("trucks") or {}
    end = (now + timedelta(days=1 if now.hour >= 8 else 0)).replace(hour=6, minute=0).strftime("%Y-%m-%d %H:%M")
    t["coming"] = sum(x["pkgs"] for x in t.get("list", []) if x["eta"] >= (now - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M") and x["eta"] <= end)
    t["list"] = [x for x in t.get("list", []) if x["eta"] >= (now - timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M")][:6]
    return out


def k(n):
    return f"{n / 1000:.1f}k".replace(".0k", "k")


def say(now):
    """The answer to "how much is left / what's coming": left to sort split by tonight's (in before midnight) and
    later, not-unloaded trucks named, the hourly report's own waiting-to-bag count next to it, trucks still coming.
    Each line has its read time; a source that is too old prints UNKNOWN instead of a number. Saved files only."""
    import live_state as ls
    lines = []
    sp = {}
    try:
        c = json.loads(CARD.read_text())
        sp = c.get("speed") or {}
        card_age = ls.mins_ago(c.get("updated"), now)
        if sp.get("per_hour") is not None and card_age is not None and card_age <= 25:
            lines.append(f"speed: {k(sp['per_hour'])}/hr ({sp.get('hour')}, Vendor A {k(sp['vendor_a'])}, Vendor B {k(sp['vendor_b'])}, "
                         f"normal {k(sp['normal'])})  [read {c['updated'][11:16]}, {card_age} min ago]")
        else:
            sp = {}
            lines.append(f"speed: UNKNOWN (the Floor now card was last built {str(c.get('updated'))[5:16]})")
    except Exception:
        lines.append("speed: UNKNOWN (no Floor now card saved)")
    try:
        lines += ls.floor_lines(ls.sec_floor(now, None), sp, now)
    except Exception as e:
        lines.append(f"LEFT TO SORT: UNKNOWN (floor board file unreadable: {type(e).__name__})")
    try:
        t = json.loads(TRUCKS.read_text())
    except Exception:
        t = {"list": []}
    posted_age = ls.mins_ago(t.get("posted"), now)
    w = t.get("waiting") or {}
    if w and posted_age is not None and posted_age <= ls.BROADCAST_MAX_MIN:
        bc = t.get("before_cutoff") or {}
        lines.append(f"hourly report {t['posted'][11:16]} ({posted_age} min ago): waiting to bag {k(w['trunk'] + w['branch'])} = "
                     f"linehaul {k(w['trunk'])} + branch {k(w['branch'])}; trucks still due before its cutoff: {bc.get('trucks', '?')}")
    cut = (now - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M")
    end = (now + timedelta(days=1 if now.hour >= 8 else 0)).replace(hour=6, minute=0).strftime("%Y-%m-%d %H:%M")
    nxt = [x for x in t.get("list", []) if x["eta"] >= cut]
    before = [x for x in nxt if x["eta"] <= end]

    def one(x):
        h = datetime.strptime(x["eta"], "%Y-%m-%d %H:%M")
        return f"{x['route'].split('-')[0].replace('.H', '')} {k(x['pkgs'])} at {h.strftime('%-I:%M %p')}"
    stale = posted_age is None or posted_age > ls.BROADCAST_MAX_MIN
    if stale:
        lines.append(f"coming: UNKNOWN from the hourly report (newest post read is {str(t.get('posted'))[5:16]}); "
                     "use the 'trucks still coming (WMS)' line of live_state.py --brief")
    else:
        if before:
            lines.append("coming (ETAs as posted; a truck whose time just passed may already be here): " + ", ".join(one(x) for x in before) + f" = {k(sum(x['pkgs'] for x in before))} before 6 AM")
        after = [x for x in nxt if x["eta"] > end][:2]
        if after:
            lines.append("after 6 AM: " + ", ".join(one(x) for x in after))
        lines.append(f"[truck list posted {(t.get('posted') or '?')[11:16]}]")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--say", action="store_true")
    a = ap.parse_args()
    import hubconfig
    now = datetime.now(hubconfig.TIME_ZONE).replace(tzinfo=None)
    if a.say:
        print(say(now))
        return
    if not a.dry_run and 8 <= now.hour < 16:
        return
    card = build(now)
    DATA.mkdir(parents=True, exist_ok=True)
    CARD.write_text(json.dumps(card, ensure_ascii=False))
    if a.dry_run:
        print(json.dumps(card, ensure_ascii=False, indent=1))
        return
    import app_push
    app_push.data("floor", card)


if __name__ == "__main__":
    main()
