#!/usr/bin/env python3
"""Live alerts from the Sorter 1 cross-belt sorter, sent to the phone app.

Every 2 minutes (launchd) it reads the sorter's failed sorts from the WMS sorting records and alerts when:
- a chute is stuck: one chute keeps rejecting boxes (chute full, over-loop, not dropped, no bag, or matched
  but dropped in the exception chute) for 5+ minutes with 15+ rejects. It alerts again every 5 minutes (10, 15, 20, ...)
  while the chute is still stuck.
- boxes have no chute: 60+ NO_MATCHED_RULE rejects in each of the last two 10-minute blocks. The alert names
  the stations those boxes are for, e.g. a whole hub missing from the sort plan.
- (lost bag labels: lost_labels.py, its own job every 30 seconds; it replaced the old near-empty-bag check.)
- a bag of 850+ packages not yet on a truck: almost always two boxes sharing one label (checked every 10 min).
It also turns each Send from the phone app's Scan tab into its Labels screen (floor_scan.py, when installed).

It never looks at break times: when the machine stops, nothing is rejected and nothing is sent.
Read-only in the WMS (except the floor-scan merges, floor_scan.py). Alerts go to the phone app only.

    python3 hubops/chute_alerts.py                    one check (what launchd runs)
    python3 hubops/chute_alerts.py --dry-run          one check, print the alert instead of sending it
    python3 hubops/chute_alerts.py --replay 2026-11-11  replay that night from the WMS and print what it would have sent
"""
import argparse
import json
import os
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import app_push
import hub_numbers as hn
import hubconfig
import wms_client

HERE = Path(__file__).resolve().parent
DATA = hubconfig.DATA_DIR
STATE_FILE = DATA / "chute-alerts-state.json"
WMS_TOKEN_FILE = DATA / ".wms-token"
ALERT_LOG = DATA / "chute-alerts.log"  # Every alert sent, one line each

SORTER_ID = hubconfig.SORTER_ID  # Sorter 1 in the WMS sorting records
MASK_LIST_API = "/api/parcels/route"
BAGS_API = "/api/bags/search"  # Bag search
HUB_ID = hubconfig.HUB_ID
FAILED = 2

ACTIVE_FROM, ACTIVE_UNTIL = 16, 8  # Only check 4 PM to 8 AM (the night shift)
GAP = timedelta(minutes=5)  # A chute with a reject this recent is rejecting right now
# A chute can be full on and off for hours, half its boxes dropping, with quiet stretches of 2-13 minutes. A
# 5-minute rule would call it fixed in a quiet stretch and then report a "new" problem minutes later. So rejects
# within 15 minutes are the same problem, and it's called fixed only after 15 quiet minutes, with a message
# saying so (silence alone isn't "fixed").
SAME_PROBLEM = timedelta(minutes=15)
STUCK_EVERY = 5  # Minutes: first alert at 5 min stuck, then every 5 more (10, 15, ...) until it clears
STUCK_MIN_REJECTS = 15
STUCK_TASK_MIN = 10  # A stuck chute goes on the task list (and buzzes) only after 10 min; the Chutes tab shows it sooner
NO_RULE_BLOCK = timedelta(minutes=10)
NO_RULE_MIN = 60  # Rejects per 10-minute block, two blocks in a row. Normal nights: 5-28
NO_RULE_REPEAT = timedelta(minutes=60)  # Same problem: remind once an hour, not every check
OVERLAP = timedelta(minutes=5)  # Each read starts this far before the last one ended; ids drop repeats
KEEP = timedelta(minutes=40)
# Exception chutes: NOREAD 001/109/214, interception 146, no match 147, and the reject chutes 1/3.
EXCEPTION_CHUTES = {"1", "3", "001", "109", "214", "146", "147"}
CHUTE_REASONS = {"CHUTE_FULL", "LOOP_EXCEPTION", "NOT_DROPPED", "OVER_LOOP_LIMIT", "CHUTE_NO_BAG", ""}
NO_RULE = "NO_MATCHED_RULE"

SORTER_BAG = 2  # bagType: made at a sorter chute (the bag number ends in -<chute>)


def parse(text):
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")


def clock(t):
    return t.strftime("%I:%M %p").lstrip("0")


def reason(row):
    return row.get("refluxReason") or row.get("refluxReasonName") or ""


def new_state():
    return {"seen": {}, "stuck": {}, "no_rule": [], "no_rule_sent": None, "read_until": None}


# ---------- the logic (no WMS or app calls here, so tests and --replay use it as is) ----------

def update(state, rows):
    """Adds new failed sorts to the state: stuck spells per chute, and the no-rule rejects."""
    for row in sorted(rows, key=lambda r: r.get("deviceSortTime") or ""):
        rid, when = row.get("id"), row.get("deviceSortTime")
        if not when or (rid and rid in state["seen"]):
            continue
        if rid:
            state["seen"][rid] = when
        t, why = parse(when), reason(row)
        if why == NO_RULE:
            state["no_rule"].append([when, row.get("orderCode")])
            continue
        chutes = row.get("matchedChutes") or []
        if why not in CHUTE_REASONS or not chutes or chutes[0] in EXCEPTION_CHUTES:
            continue
        chute = chutes[0]
        if when <= state.get("fixed_at", {}).get(chute, ""):
            continue  # from before someone pressed "Fixed" (the WMS can post rejects a few minutes late)
        spell = state["stuck"].get(chute)
        if spell and t - parse(spell["last"]) <= SAME_PROBLEM and t >= parse(spell["start"]) - GAP:
            pause = t - parse(spell["last"])
            if pause > GAP:  # it went quiet, then started again: the next alert says so
                spell["paused"] = int(pause.total_seconds() // 60)
            spell["last"] = max(spell["last"], when)
            spell["start"] = min(spell["start"], when)
            spell["rejects"] += 1
            if row.get("orderCode") and row["orderCode"] not in spell["boxes"]:
                spell["boxes"].append(row["orderCode"])
        else:
            state["stuck"][chute] = {"start": when, "last": when, "rejects": 1, "sent": [],
                                     "dest": row.get("chuteRemark") or "",
                                     "boxes": [row["orderCode"]] if row.get("orderCode") else []}
        if row.get("chuteRemark"):
            state["stuck"][chute]["dest"] = row["chuteRemark"]


# Sorter 1 printed chute sheet, used only when the WMS gives no route for the chute.
CHUTE_MAP = {**{str(c): "SGR01 fine sort" for c in range(2, 44)}, **{str(c): "city linehaul" for c in range(49, 70)},
             **{str(c): "CXO01 fine sort" for c in range(322, 366)},
             **{str(c): "CXO01 fine sort" for c in range(256, 280)},
             **{str(c): f"TXK01-{c - 135:03d}" for c in range(136, 146)},
             **{str(c): f"AEX01-{c - 298:03d}" for c in range(299, 304)},
             **{str(c): f"SJT01-{c - 307:03d}" for c in range(308, 317)},
             **{str(c): f"GGG01-{c - 424:03d}" for c in range(425, 429)},
             "49": "LIT.H", "188": "LIT.H", "240": "BRO01", "367": "BRO01", "241": "AUS01", "370": "AUS01",
             "242": "HRL01", "371": "HRL01", "368": "VCT01", "369": "ACT01", "106": "SHV01-270", "107": "SHV01-271",
             "212": "SHV01-271", "217": "Customer A", "216": "Customer B"}


def chute_route(chute):
    """The route on a chute: the WMS's active sort plan as saved in data/chute_map.json, then the printed sheet
    (CHUTE_MAP) for chutes the plan doesn't use."""
    key = str(chute).lstrip("0") or "0"
    return saved_chutes().get(key) or CHUTE_MAP.get(key, "route unknown")


_SAVED_CHUTES = None


def saved_chutes():
    global _SAVED_CHUTES
    if _SAVED_CHUTES is None:
        try:
            _SAVED_CHUTES = json.loads((DATA / "chute_map.json").read_text())["chutes"]
        except (OSError, ValueError, KeyError):
            _SAVED_CHUTES = {}
    return _SAVED_CHUTES


def stuck_alerts(state, now):
    """Lines for chutes that just reached 5, 10, 15, ... minutes stuck."""
    lines = []
    for chute, spell in sorted(state["stuck"].items(), key=lambda kv: kv[1]["start"]):
        start, last = parse(spell["start"]), parse(spell["last"])
        if now - last > GAP or spell["rejects"] < STUCK_MIN_REJECTS:
            continue
        minutes = int((last - start).total_seconds() // 60)
        level = minutes // STUCK_EVERY * STUCK_EVERY
        if level < STUCK_EVERY or level in spell["sent"]:
            continue
        spell["sent"].append(level)
        still = "STILL stuck" if level > STUCK_EVERY else "stuck"
        where = spell["dest"] or chute_route(chute)
        back = f" · BACK after {spell.pop('paused')} quiet min, not fixed" if spell.get("paused") and spell["sent"][:-1] else ""
        spell.pop("paused", None)
        lines.append(f"{where} (chute {chute}) · {still} {minutes} min{back} · {len(spell['boxes'])} boxes "
                     f"going around · since {clock(start)}")
    return lines


def fixed_alerts(state, now):
    """'✓ fixed' once for each alerted chute that has had no reject for SAME_PROBLEM."""
    lines = []
    for chute, spell in sorted(state["stuck"].items(), key=lambda kv: kv[1]["start"]):
        last = parse(spell["last"])
        if not spell["sent"] or spell.get("fixed") or now - last < SAME_PROBLEM:
            continue
        spell["fixed"] = True
        minutes = int((last - parse(spell["start"])).total_seconds() // 60)
        lines.append(f"✓ {spell['dest'] or chute_route(chute)} (chute {chute}) · fixed: no rejects since {clock(last)} · "
                     f"was stuck {minutes} min ({clock(parse(spell['start']))}–{clock(last)})")
    return lines


def no_rule_spike(state, now):
    """The no-rule boxes of the last 20 minutes if both 10-minute blocks hit the bar, else None."""
    times = [(parse(t), box) for t, box in state["no_rule"]]
    recent = [box for t, box in times if now - NO_RULE_BLOCK < t <= now]
    before = [box for t, box in times if now - 2 * NO_RULE_BLOCK < t <= now - NO_RULE_BLOCK]
    if len(recent) < NO_RULE_MIN or len(before) < NO_RULE_MIN:
        return None
    sent = state.get("no_rule_sent")
    if sent and now - parse(sent) < NO_RULE_REPEAT:
        return None
    state["no_rule_sent"] = now.strftime("%Y-%m-%d %H:%M:%S")
    return recent + before


def no_rule_line(boxes, stations):
    """stations: {box: station}. 'No chute (NO_MATCHED_RULE) · 150 rejects / 120 boxes in 20 min · VCT01 40 …'"""
    unique = list(dict.fromkeys(b for b in boxes if b))
    found = [s for s in (stations.get(b) for b in unique) if s]
    top = Counter(found).most_common(5)
    where = (" · ".join(f"{s} {n}" for s, n in top) + f" (of {len(found)} looked up)") if found else "stations not found"
    return (f"No chute (NO_MATCHED_RULE) · {len(boxes)} rejects / {len(unique)} boxes in 20 min\n"
            f"For: {where}")


def mark_fixed(state, presses):
    """"Fixed" presses on the app's Chutes tab ([{chute, start, at}], at in UTC ISO): the spell shown there ends
    at the press. No more "STILL stuck" or "fixed" message for it; a reject after the press starts a new spell (a
    new jam, alerted again at 5 minutes). Each press is used once."""
    done = state.setdefault("fixed_done", [])
    for p in presses or []:
        chute, key = str(p.get("chute") or ""), f"{p.get('chute')}|{p.get('start')}|{p.get('at')}"
        if not chute or key in done:
            continue
        done.append(key)
        try:
            at = datetime.fromisoformat(str(p["at"]).replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        state.setdefault("fixed_at", {})[chute] = at.strftime("%Y-%m-%d %H:%M:%S")
        spell = state["stuck"].get(chute)
        if spell and spell["start"] <= at.strftime("%Y-%m-%d %H:%M:%S"):
            h = state.setdefault("history", {}).get(f"{chute}|{spell['start']}")
            if h:
                h["cleared"] = True
            del state["stuck"][chute]
    del done[:-100]


def prune(state, now):
    cut = (now - KEEP).strftime("%Y-%m-%d %H:%M:%S")
    if "fixed_at" in state:
        state["fixed_at"] = {c: t for c, t in state["fixed_at"].items() if t >= cut}
    state["seen"] = {k: v for k, v in state["seen"].items() if v >= cut}
    state["no_rule"] = [x for x in state["no_rule"] if x[0] >= cut]
    state["stuck"] = {c: s for c, s in state["stuck"].items() if s["last"] >= cut}


def check(state, rows, now, lookup):
    """One run: add rows, return the alert text (or None). lookup(boxes) -> {box: station}."""
    update(state, rows)
    lines = stuck_alerts(state, now) + fixed_alerts(state, now)
    boxes = no_rule_spike(state, now)
    if boxes:
        lines.append(no_rule_line(boxes, lookup(list(dict.fromkeys(boxes))[:100])))
    prune(state, now)
    return "\n".join(lines) if lines else None


# ---------- WMS and the phone app ----------

def wms_token(fresh=False):
    """The saved WMS token; a new one (hub_numbers.browser_token) only when it has expired."""
    if not fresh and WMS_TOKEN_FILE.exists():
        return WMS_TOKEN_FILE.read_text().strip()
    token = hn.browser_token()
    DATA.mkdir(exist_ok=True)
    WMS_TOKEN_FILE.write_text(token)
    os.chmod(WMS_TOKEN_FILE, 0o600)
    return token


def wms(token_box, path, body):
    """One WMS read. token_box is a one-item list so an expired token is swapped for the caller."""
    try:
        return wms_client.call(path, body, token=token_box[0], allow_empty=True)["data"] or {}
    except wms_client.LoginExpired:
        token_box[0] = wms_token(fresh=True)
        return wms_client.call(path, body, token=token_box[0], allow_empty=True)["data"] or {}


def ms(t):
    return int(t.timestamp() * 1000)


def fetch_fails(token_box, start, end):
    rows, page = [], 1
    while True:
        body = {"noType": 1, "sorterId": SORTER_ID, "sortTimeStart": ms(start), "sortTimeEnd": ms(end),
                "sortStatus": FAILED, "pageNum": page, "pageSize": 100}
        got = wms(token_box, hn.SORT_RECORDS_API, body).get("list") or []
        rows += got
        if len(got) < 100 or page >= 80:
            return rows
        page += 1


def stations(token_box, boxes):
    out = {}
    for i in range(0, len(boxes), 100):
        res = wms(token_box, MASK_LIST_API, {"waybillNoList": boxes[i:i + 100], "pageNum": 1, "pageSize": 100})
        for row in res.get("list") or []:
            out[row.get("waybillNo")] = row.get("destinationCenter")
    return out


def send(text, title=None, buzz=True):
    """One line in ALERT_LOG, plus (buzz) a notification in the phone app. Stuck chutes, 850+ bags and lost
    labels go to the task list (do_now.py) instead, which buzzes once per new task: those use buzz=False."""
    line = f"{datetime.now():%m/%d %H:%M} " + " | ".join(t.strip() for t in text.splitlines() if t.strip())
    DATA.mkdir(exist_ok=True)
    with ALERT_LOG.open("a") as f:
        f.write(line + "\n")
    if buzz:
        lines = text.splitlines()
        app_push.notify(title or ("Stuck chute" if len(lines) == 1 else f"{len(lines)} alerts"), text)


def chute_tasks(state, now):
    """Task-list entries: every alerted chute that is rejecting right now. A chute quiet for GAP drops off
    (someone swapped it); if it starts again within SAME_PROBLEM it's the same spell and comes back."""
    out = []
    for chute, spell in state["stuck"].items():
        start, last = parse(spell["start"]), parse(spell["last"])
        if not spell["sent"] or spell.get("fixed") or now - last > GAP:
            continue
        minutes = int((last - start).total_seconds() // 60)
        if minutes < STUCK_TASK_MIN:
            continue
        where = spell["dest"] or chute_route(chute)
        out.append({"tid": f"{chute}|{spell['start']}", "rank": 10,
                    "do": f"Chute {chute} {where}: swap in an empty gaylord / clear the jam",
                    "why": f"Stuck {minutes} min · {len(spell['boxes'])} boxes going around · since {clock(start)}",
                    "extra": {"fixed": {"chute": str(chute), "start": spell["start"]}}})
    return out


def big_task(line, bag):
    return {"tid": bag, "rank": 30, "ttl_min": 120, "do": "Check this bag: 2 boxes on 1 label?", "why": line}


def to_do_now(source, tasks=None, put=None):
    """Hands tasks to do_now.py; a failure there must never stop the chute alerts."""
    try:
        import do_now
        if put:
            do_now.put(source, **put)
        else:
            do_now.replace(source, tasks)
    except Exception as e:  # noqa: BLE001
        print(f"{datetime.now():%m/%d %H:%M} task list not updated: {type(e).__name__}: {e}", file=sys.stderr)


APP_EVERY = timedelta(minutes=10)  # Resend unchanged app data this often, so the page can tell the job is alive
LIVE_MIN_REJECTS = 5  # A chute shows on the app's live list from this many rejects in one spell


def app_view(state, now):
    """What the app's Chutes tab shows: chutes rejecting right now, and tonight's alerted spells."""
    live = []
    for chute, spell in sorted(state["stuck"].items(), key=lambda kv: kv[1]["start"]):
        if now - parse(spell["last"]) > SAME_PROBLEM or spell["rejects"] < LIVE_MIN_REJECTS:
            continue
        live.append({"chute": chute, "where": spell["dest"] or chute_route(chute), "start": spell["start"],
                     "last": spell["last"], "rejects": spell["rejects"], "boxes": len(spell["boxes"]),
                     "alerted": bool(spell["sent"])})
    hist = state.setdefault("history", {})
    for chute, spell in state["stuck"].items():
        if spell["sent"]:
            start, last = parse(spell["start"]), parse(spell["last"])
            hist[f"{chute}|{spell['start']}"] = {
                "chute": chute, "where": spell["dest"] or chute_route(chute), "start": spell["start"],
                "last": spell["last"], "minutes": int((last - start).total_seconds() // 60),
                "boxes": len(spell["boxes"]), "cleared": now - last > SAME_PROBLEM}
    cut = (now - timedelta(hours=16)).strftime("%Y-%m-%d %H:%M:%S")  # Tonight only
    for k in [k for k, v in hist.items() if v["start"] < cut]:
        del hist[k]
    for v in hist.values():
        v["cleared"] = v["cleared"] or now - parse(v["last"]) > SAME_PROBLEM
    return {"live": live, "history": sorted(hist.values(), key=lambda v: v["start"]), "labels": label_errors_view()}


def label_errors_view():
    """label_errors.py's list for the app's Labels tab, sent along with the chutes (no extra storage writes).
    Its own "updated" time is left out so an unchanged list doesn't count as a change."""
    try:
        view = json.loads((DATA / "label-errors.json").read_text())
    except (OSError, ValueError):
        return None
    view.pop("updated", None)
    return view


def fixed_presses():
    if not app_push.APP:  # demo mode: no app to ask
        return []
    req = urllib.request.Request(f"{app_push.APP}/app-fixed", headers={"X-Push-Key": app_push.push_key(),
                                                                       "User-Agent": "hubops-app"})
    with urllib.request.urlopen(req, timeout=20) as res:
        return json.loads(res.read().decode())


def push_app(state, now):
    view = app_view(state, now)
    sig = json.dumps(view, sort_keys=True)
    last = parse(state["app_pushed"]) if state.get("app_pushed") else None
    if sig == state.get("app_sig") and last and now - last < APP_EVERY:
        return
    try:
        app_push.data("chutes", {"updated": now.strftime("%Y-%m-%d %H:%M:%S"), **view})
        state["app_sig"], state["app_pushed"] = sig, now.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:  # noqa: BLE001 - the app missing one update must not stop the alerts
        print(f"{now:%m/%d %H:%M} app update failed: {type(e).__name__}: {e}", file=sys.stderr)


def load_state():
    try:
        return {**new_state(), **json.loads(STATE_FILE.read_text())}
    except (OSError, ValueError):
        return new_state()


def save_state(state):
    DATA.mkdir(exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(STATE_FILE)


def active(now):
    return now.hour >= ACTIVE_FROM or now.hour < ACTIVE_UNTIL


def run_once(dry_run=False, any_hour=False):
    now = datetime.now()
    if not any_hour and not active(now):
        return
    state = load_state()
    read_until = parse(state["read_until"]) if state.get("read_until") else None
    start = (read_until - OVERLAP) if read_until and now - read_until < KEEP else now - timedelta(minutes=25)
    token_box = [wms_token()]
    rows = fetch_fails(token_box, start, now)
    state["read_until"] = now.strftime("%Y-%m-%d %H:%M:%S")
    if not dry_run:
        try:  # "Fixed" presses from the phone (one small read); a hiccup just waits for the next check
            mark_fixed(state, fixed_presses())
        except Exception as e:  # noqa: BLE001
            print(f"{now:%m/%d %H:%M} fixed presses not read: {type(e).__name__}: {e}", file=sys.stderr)
    text = check(state, rows, now, lambda boxes: stations(token_box, boxes))
    if text:
        if dry_run:
            print(text)
        else:
            send(text, buzz=False)
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("No chute (NO_MATCHED_RULE)"):
                    to_do_now("norule", put={"tid": state.get("no_rule_sent") or now.strftime("%H%M"), "rank": 40,
                                             "ttl_min": 60, "do": "Tell the shift lead: boxes with no chute rule are going around",
                                             "why": " · ".join(lines[i:i + 2])})
        print(f"{now:%m/%d %H:%M} sent: {text!r}")
    if not dry_run:
        to_do_now("chute", chute_tasks(state, now))
        try:  # one customer's batch going round as "waybill not found" → a quest (bad_batch.py, optional plug-in)
            import bad_batch
        except ImportError:
            bad_batch = None
        if bad_batch:
            try:
                bad_batch.run(state, rows, now, lambda path, body: wms(token_box, path, body),
                              put=lambda tasks: to_do_now("quest", tasks))
            except Exception as e:  # noqa: BLE001 - the chute alerts come first
                print(f"{now:%m/%d %H:%M} bad batch check failed: {type(e).__name__}: {e}", file=sys.stderr)
    # Near-empty bags are handled by lost_labels.py (its own job, every 30 seconds)
    big = big_text(token_box, state, now)
    if big:
        if dry_run:
            print(big)
        else:
            send(big, title="2 boxes on 1 label?", buzz=False)
            for line in big.splitlines():
                to_do_now("big", put=big_task(line, line.rsplit(" · ", 1)[-1]))
        print(f"{now:%m/%d %H:%M} sent: {big!r}")
    if not dry_run:
        push_app(state, now)
        floor_scans(state)
    save_state(state)


def floor_scans(state):
    """A new Send from the phone's Scan tab: merge the split boxes into the labels on the floor, add labels for
    boxes with none to the label PDF, and update the phone's Labels list (floor_scan.act). One read of the scan
    list each check; the WMS and the app's storage only when there's a new Send. Skipped when floor_scan.py
    isn't installed."""
    try:
        import floor_scan
    except ImportError:
        return
    try:
        got = floor_scan.batches()
        sig = "|".join(f"{b.get('id')}:{b.get('sent')}" for b in got[-5:])
        if not got or state.get("scans_seen") == sig:
            return
        labels, _, n = floor_scan.build()
        if labels:
            # Merges are kept in floor-merges.json and labels in tonight's list, so a run that fails part-way is
            # simply tried again at the next check: nothing is merged or added twice.
            lines = floor_scan.act(labels)
            floor_scan.publish(labels)
            # Every Send gets an answer, even "whole, nothing to merge", so the scanner knows it was checked.
            send("\n".join([floor_scan.summary(labels), *lines]), title="Scan checked")
            print(f"{datetime.now():%m/%d %H:%M} labels: {n} scans -> {len(labels['items'])} boxes, {len(lines)} actions")
        state["scans_seen"] = sig
    except Exception as e:  # noqa: BLE001 - the chute alerts come first
        print(f"{datetime.now():%m/%d %H:%M} floor scans failed: {type(e).__name__}: {e}", file=sys.stderr)


BIG_PCS = 850  # A box this big is almost always two boxes sharing one label
BIG_EVERY = timedelta(minutes=10)


def read_bags(token_box, now, back):
    """Bags made at the hub opened in the last `back` (bag search)."""
    fmt = "%Y-%m-%d %H:%M:%S"
    body = {"packageNoList": [], "createBeginTime": (now - back).strftime(fmt), "createEndTime": now.strftime(fmt),
            "startCenterId": HUB_ID, "pageSize": 1000}
    bags = []
    for page in range(1, 10):
        got = wms(token_box, BAGS_API, {**body, "pageNum": page}).get("list") or []
        bags += got
        if len(got) < 1000:
            return bags
    return bags


def chute_of(bag):
    return str(bag.get("packageNo") or "").rsplit("-", 1)[-1].lstrip("0")


def big_text(token_box, state, now):
    """Bags of BIG_PCS+ not yet on a truck, each sent once (open ones too: that's when it can still be fixed).
    One long read every BIG_EVERY. Its own try: a failure here must not stop the chute alerts."""
    try:
        last = parse(state["big_read"]) if state.get("big_read") else None
        if last and now - last < BIG_EVERY:
            return None
        state["big_read"] = now.strftime("%Y-%m-%d %H:%M:%S")
        done = state.setdefault("big_done", {})
        cut = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        for k in [k for k, v in done.items() if v < cut]:
            del done[k]
        lines = []
        for b in read_bags(token_box, now, timedelta(hours=20)):  # a box can sit open all night
            pcs = b.get("waybillCount") or 0
            if b.get("bagType") != SORTER_BAG or pcs < BIG_PCS or b.get("optCheckOutTime") or b["packageNo"] in done:
                continue
            done[b["packageNo"]] = state["big_read"]
            chute = chute_of(b)
            where = b.get("destinAreaCode") or b.get("destinCenterName") or chute_route(chute)
            opened = datetime.fromtimestamp(b["optCreateTime"] / 1000).strftime("%-I:%M %p") if b.get("optCreateTime") else "?"
            lines.append(f"{where} (chute {chute}) · {pcs} pcs · {'closed' if b.get('optFinishStatusTime') else 'still filling'}"
                         f" · opened {opened} · {b['packageNo']}")
        return "\n".join(lines) or None
    except Exception as e:  # noqa: BLE001
        print(f"{now:%m/%d %H:%M} big box check failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


def replay(day):
    """What the alerts would have sent on the night starting 5 PM on `day`, checking every 2 minutes."""
    token_box = [wms_token()]
    st = datetime.strptime(day, "%Y-%m-%d").replace(hour=17)
    hours = [(st + timedelta(hours=h), st + timedelta(hours=h + 1)) for h in range(14)]
    with ThreadPoolExecutor(4) as pool:
        rows = [r for part in pool.map(lambda w: fetch_fails(token_box, *w), hours) for r in part]
    rows.sort(key=lambda r: r.get("deviceSortTime") or "")
    state, i, now, sent = new_state(), 0, st, 0
    while now <= st + timedelta(hours=14):
        batch = []
        while i < len(rows) and parse(rows[i]["deviceSortTime"]) <= now:
            batch.append(rows[i])
            i += 1
        text = check(state, batch, now, lambda boxes: stations(token_box, boxes))
        if text:
            sent += 1
            print(f"--- {clock(now)}\n{text}")
        now += timedelta(minutes=2)
    print(f"\n{len(rows):,} failed sorts read, {sent} alerts would have been sent.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--any-hour", action="store_true", help="check even outside the night-shift hours")
    parser.add_argument("--replay", metavar="YYYY-MM-DD")
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
        return
    try:
        run_once(args.dry_run, args.any_hour)
    except Exception as e:  # noqa: BLE001 - one bad check must not stop the next one
        print(f"{datetime.now():%m/%d %H:%M} check failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
