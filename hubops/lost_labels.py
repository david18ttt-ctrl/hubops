#!/usr/bin/env python3
"""Lost bag labels on Sorter 1, sent to the phone app within about 30 seconds.

When a chute's printer doesn't print, the WMS still says "success", so the worker presses again and a second
label comes out for a new bag. The packages are then split over two bag numbers in one gaylord, and the sooner
the floor hears, the easier it is to find that gaylord and fix it with the Scan tab.

The rule: two labels on the same chute, BOTH with packages, printed within 15 minutes of each other. A label
with 0 packages doesn't matter, so it's skipped (and doesn't break a pair: 272, then 0, then 3 is still a pair).
One of the two bags must also be under 20 packages (272 then 3): two full bags close together is a busy chute
changing gaylords, not a lost label. This applies to every chute.

How: every 30 seconds (launchd, 4 PM to 8 AM) one read of the last minutes of the WMS chute log (each
"label printed" row = a label printed: chute, bag number, time), then one lookup of just those bags' package
counts. Read-only in the WMS. Replaces the old near-empty-bag check in chute_alerts.py.

    python3 hubops/lost_labels.py              one check (what launchd runs)
    python3 hubops/lost_labels.py --dry-run    one check, print instead of sending
    python3 hubops/lost_labels.py --replay 2026-11-15   what a night (4 PM on) would have sent
"""
import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import hubconfig

HERE = Path(__file__).resolve().parent
STATE_FILE = hubconfig.DATA_DIR / "lost-labels-state.json"

CHUTE_LOG_API = "/api/sorter/chute-log"
BAGS_API = "/api/bags/search"  # Bag search
CENTER_ID = hubconfig.HUB_ID
PRINTED = 1  # opType: label printed
OK = 1  # opResult: success

PAIR = timedelta(minutes=15)  # Two labels with packages this close on one chute = one lost label
SMALL = 20  # A pair needs one of the two bags under this (see is_pair)
FRESH = timedelta(minutes=10)  # Only labels this recent are news (a restart mustn't send old ones)
COLD_START = timedelta(minutes=20)  # First read after a gap: enough to know each chute's last label
OVERLAP = timedelta(minutes=2)  # Chute-log rows can show up a little late
RETRY = timedelta(minutes=10)  # A printed bag the WMS can't find yet is tried again this long
BACKOFF = timedelta(minutes=5)  # After a failed WMS read (login expired), wait this long
ACTIVE_FROM, ACTIVE_UNTIL = 16, 8


def ms(t):
    return int(t.timestamp() * 1000)


def clock(ms_):
    return datetime.fromtimestamp(ms_ / 1000).strftime("%-I:%M %p")


def new_state():
    return {"read_until": None, "last": {}, "seen": {}, "pending": {}, "sent": {}, "wait_until": None}


# ---------- the logic (no WMS here, so tests and --replay use it as is) ----------

def prints_from(rows):
    """Labels that really printed, oldest first: [{"bag", "chute", "at"}]. Failed prints (no bag) are skipped."""
    out = []
    for r in rows:
        bag = r.get("bagLabelCode") or ""
        if r.get("opType") == PRINTED and r.get("opResult") == OK and bag:
            out.append({"bag": bag, "chute": str(r.get("chuteNo") or bag.rsplit("-", 1)[-1]).lstrip("0") or "0",
                        "at": r["opTime"]})
    return sorted(out, key=lambda p: p["at"])


def is_pair(a, b):
    """a, b: {"pcs", "where", "at"}, a printed before b on the same chute, both with packages."""
    if b["at"] - a["at"] > PAIR.total_seconds() * 1000:
        return False
    # Two FULL bags close together is just a busy chute changing gaylords (e.g. 405 then 490, 13 min apart), on
    # every chute. A lost label leaves one bag with a few packages (395 then 2).
    if min(a["pcs"], b["pcs"]) >= SMALL:
        return False
    return True


def step(state, prints, bags, now_ms):
    """Feeds new printed labels through each chute's last label. prints: from prints_from; bags: {bag number:
    {"pcs", "where"}} for those it could look up (missing ones wait in state["pending"] up to RETRY).
    Returns alerts: [{"chute", "where", "pairs": [(a, b)]}], one per chute. Updates state in place."""
    todo = {p["bag"]: p for p in state["pending"].values()}
    todo.update({p["bag"]: p for p in prints if p["bag"] not in state["seen"]})
    fresh_from = now_ms - FRESH.total_seconds() * 1000
    alerts = {}
    for p in sorted(todo.values(), key=lambda p: p["at"]):
        info = bags.get(p["bag"])
        if info is None:
            if now_ms - p["at"] < RETRY.total_seconds() * 1000:
                state["pending"][p["bag"]] = p
            else:
                state["pending"].pop(p["bag"], None)
                state["seen"][p["bag"]] = p["at"]
            continue
        state["pending"].pop(p["bag"], None)
        state["seen"][p["bag"]] = p["at"]
        if not info["pcs"]:
            continue  # A 0-package label doesn't matter
        cur = {"bag": p["bag"], "pcs": info["pcs"], "where": info["where"], "at": p["at"]}
        prev = state["last"].get(p["chute"])
        # A label already sent as the second of a pair doesn't start another: 272, 3, then 162 is one lost label
        # (the 162 is just the next box), not two alerts.
        if prev and prev["at"] < cur["at"] and is_pair(prev, cur) and cur["at"] >= fresh_from \
                and cur["bag"] not in state["sent"] and prev["bag"] not in state["sent"]:
            state["sent"][cur["bag"]] = cur["at"]
            a = alerts.setdefault(p["chute"], {"chute": p["chute"], "where": cur["where"], "pairs": []})
            a["pairs"].append((prev, cur))
        if not prev or prev["at"] <= cur["at"]:
            state["last"][p["chute"]] = cur
    return sorted(alerts.values(), key=lambda a: a["pairs"][-1][1]["at"])


def text(alert, route=lambda chute: ""):
    """'Chute 51 · PHX.H · 2 labels 2 min apart · 8:57 PM
      BG0126111600193-51 272 pcs 8:55 PM → BG0126111600266-51 3 pcs 8:57 PM
      scan the gaylord at chute 51'"""
    pairs = alert["pairs"]
    chain = [pairs[0][0]] + [b for _, b in pairs]
    gap = max(1, round((chain[-1]["at"] - chain[0]["at"]) / 60000))
    where = alert["where"] or route(alert["chute"])
    head = (f"Chute {alert['chute']} · {where} · {len(chain)} labels {gap} min apart · {clock(chain[-1]['at'])}")
    bags = " → ".join(f"{b['bag']} {b['pcs']} pcs {clock(b['at'])}" for b in chain)
    return "\n".join([head, "  " + bags, f"  scan the gaylord at chute {alert['chute']}"])


def prune(state, now_ms):
    cut = now_ms - 6 * 3600 * 1000
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cut}
    state["sent"] = {k: v for k, v in state["sent"].items() if v >= cut}
    state["last"] = {k: v for k, v in state["last"].items() if v["at"] >= cut}


# ---------- WMS ----------

def read_log(wms, token_box, start, end):
    rows, page = [], 1
    while True:
        got = wms(token_box, CHUTE_LOG_API, {"opTimeStart": ms(start), "opTimeEnd": ms(end),
                                             "pageNum": page, "pageSize": 500}).get("list") or []
        rows += got
        if len(got) < 500 or page >= 40:
            return rows
        page += 1


def look_up(wms, token_box, numbers, now):
    """{bag number: {"pcs", "where"}} from the bag search, for just these bags."""
    out, fmt = {}, "%Y-%m-%d %H:%M:%S"
    numbers = list(dict.fromkeys(numbers))
    for i in range(0, len(numbers), 200):
        body = {"packageNoList": numbers[i:i + 200], "createBeginTime": (now - timedelta(hours=36)).strftime(fmt),
                "createEndTime": now.strftime(fmt), "startCenterId": CENTER_ID, "pageSize": 1000, "pageNum": 1}
        for b in wms(token_box, BAGS_API, body).get("list") or []:
            out[b["packageNo"]] = {"pcs": b.get("waybillCount") or 0,
                                   "where": b.get("destinCenterName") or b.get("destinAreaCode") or ""}
    return out


def load_state():
    try:
        return {**new_state(), **json.loads(STATE_FILE.read_text())}
    except (OSError, ValueError):
        return new_state()


def save_state(state):
    STATE_FILE.parent.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def run_once(dry_run=False, any_hour=False):
    import chute_alerts as ca
    now = datetime.now()
    if not any_hour and not (now.hour >= ACTIVE_FROM or now.hour < ACTIVE_UNTIL):
        return
    state = load_state()
    if state.get("wait_until") and now < datetime.fromisoformat(state["wait_until"]):
        return
    last = datetime.fromisoformat(state["read_until"]) if state.get("read_until") else None
    start = last - OVERLAP if last and now - last < COLD_START else now - COLD_START
    try:
        token_box = [ca.wms_token()]
        prints = prints_from(read_log(ca.wms, token_box, start, now))
        need = [p["bag"] for p in prints if p["bag"] not in state["seen"]] + list(state["pending"])
        bags = look_up(ca.wms, token_box, need, now) if need else {}
    except Exception as e:  # noqa: BLE001 - one bad read must not stop the next one; don't hammer a dead login
        state["wait_until"] = (now + BACKOFF).isoformat(timespec="seconds")
        save_state(state)
        print(f"{now:%m/%d %H:%M:%S} read failed, waiting {BACKOFF}: {type(e).__name__}: {e}", file=sys.stderr)
        return
    state["wait_until"] = None
    state["read_until"] = now.isoformat(timespec="seconds")
    found = step(state, prints, bags, ms(now))
    prune(state, ms(now))
    save_state(state)
    for a in found:
        msg = text(a, ca.chute_route)
        if dry_run:
            print(msg)
        else:
            ca.send(msg, title="Lost label?", buzz=False)
            ca.to_do_now("label", put={"tid": f"{a['chute']}|{a['pairs'][-1][1]['bag']}", "rank": 20, "ttl_min": 90,
                                       "do": f"Chute {a['chute']}: scan the gaylord, a bag probably lost its label",
                                       "why": " · ".join(x.strip() for x in msg.splitlines()[:2])})
        print(f"{now:%m/%d %H:%M:%S} sent: {msg!r}")


def replay(day):
    """What a night would have sent, checking every 30 seconds from 4 PM to 8 AM."""
    import chute_alerts as ca
    token_box = [ca.wms_token()]
    st = datetime.strptime(day, "%Y-%m-%d").replace(hour=16)
    end = min(st + timedelta(hours=16), datetime.now())
    rows = []
    t = st
    while t < end:
        rows += read_log(ca.wms, token_box, t, min(t + timedelta(hours=2), end))
        t += timedelta(hours=2)
    prints = prints_from(rows)
    bags = look_up(ca.wms, token_box, [p["bag"] for p in prints], end)
    state, i, now, sent = new_state(), 0, st, 0
    while now <= end:
        batch = []
        while i < len(prints) and prints[i]["at"] <= ms(now):
            batch.append(prints[i])
            i += 1
        for a in step(state, batch, bags, ms(now)):
            sent += 1
            print(text(a, ca.chute_route))
        now += timedelta(seconds=30)
    print(f"\n{len(prints)} labels printed, {sent} alerts would have been sent.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--any-hour", action="store_true", help="check even outside the night-shift hours")
    parser.add_argument("--replay", metavar="YYYY-MM-DD")
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
    else:
        run_once(args.dry_run, args.any_hour)


if __name__ == "__main__":
    main()
