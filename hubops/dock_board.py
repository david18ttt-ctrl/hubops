#!/usr/bin/env python3
"""Loading board: the dispatch truck page turned around for the sorting team. For every outbound truck
tonight, it shows whether the truck is at the dock, when it has to leave, how many boxes it holds, how many
are already out of the machine for it, and how many more to pull. It also sends a phone-app task when a
truck is at the dock and the boxes already out won't fill it.

    python3 hubops/dock_board.py            refresh: read the truck sheet and the WMS, send the page, update tasks
    python3 hubops/dock_board.py --dry-run  print the board, send nothing

Runs from launchd (every 10 minutes; every 30 minutes 5 AM–3 PM). Read-only everywhere.
- Trucks come from dispatch's departure log, a shared table in the team chat app (read through chat_api).
  "Here" means the driver arrival is filled in and there's no departure. "At dock" means a dock door and a TT
  task with no arrival logged: dispatch fills in arrival on only some rows, so it's probably there, and the
  board says so. The log often has no arrival or departure times, so those come from the WMS truck tasks
  (TASKS_API), matched by task number.
- Loaded: boxes (bags of 30+ packages) already checked out to that truck's own TT task in the WMS
  (truck scan details). Its room is what's left after them.
- Boxes out come from the WMS bag search: bags of 30+ packages closed for that destination in the last 24 h
  (SGR01: since 5 PM) and not checked out to any truck yet. Held routes are left out. They go to the trucks
  in loading order: at the dock first (by arrival), then dock assigned, then planned trucks by time. Each
  truck shows what's still out when it's its turn (first truck: all of it; the next: what's left after the
  first is full). Counting only boxes closed since that destination's last truck left would drop the boxes
  already on the other trucks at the dock when one truck leaves, so it isn't done that way.
- Room: a 53ft holds 27 boxes (AUS01 27, BRO01/HRL01 28, SGR01 29, cities 28); a 26ft holds 11 and a 16ft
  holds 4. These match web/src/truckspace.js and truckcity.js.
The page: <HUBOPS_BOARD_URL>/d/<view key> (web/src/unsorted.js).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import hubconfig

HERE = Path(__file__).resolve().parent
DATA = hubconfig.DATA_DIR
TZ = hubconfig.TIME_ZONE
STATE_FILE = DATA / "dock_state.json"
TOKEN_FILE = DATA / ".dock-chat-token"
PUSH_KEY_FILE = DATA / ".board-push-key"
WORKER = os.environ.get("HUBOPS_BOARD_URL", "http://127.0.0.1:8787").rstrip("/")
TABLE_APP = os.environ.get("HUBOPS_DISPATCH_TABLE_APP", "dispatch")  # dispatch's shared table app
DEPARTURES = os.environ.get("HUBOPS_DISPATCH_TABLE", "departures")  # its departure log
TASKS_API = "/api/trucks/tasks"  # every truck task in or out of the hub
BAGS_API = "/api/bags/search"
SCAN_API = "/api/trucks/scans"  # truck scan details (loading and unloading)
HUB_CODE = hubconfig.HUB_CODE
HUB_TRANSIT_ID = str(hubconfig.TMS_HUB_ID)  # the hub in the TMS region list (not the operations id)
CENTER_ID = hubconfig.HUB_ID
BOX_MIN = 30  # Packages before a bag counts as a box (smaller ones ride along)
LOOK_BACK_H = 30
SHOW_LEFT_H = 3
STALE_H = 12  # "Here" this long with no departure logged: it left and nobody wrote it down
# A destination's boxes start no earlier than this hour (SGR01's night is boxes made 5 PM–4 AM; trucklocal.js)
NIGHT_FROM = {"SGR01": 17}
DAY_EVERY_MIN = 28  # 5 AM–3 PM: every 30 minutes
NIGHT_EVERY_MIN = 9.5  # 3 PM–5 AM: every 10 minutes
# launchd wakes it every 30 s; most wakes only ask the board worker whether someone pulled the page down to
# refresh (one small GET, no sheet or WMS reads), and run the full refresh right away if they did.
FIELDS = ["Shift time", "Departure date", "Destination", "Truck type", "Task no.", "Driver", "Pickup time", "Dock",
          "Sent", "Cancelled", "Driver arrival", "Actual departure", "Leave by"]

SIGNED_OUT_MIN = 30  # Checked out at the hub and no scan since: the truck has gone (see read_trip)

FULL_53 = {"AUS01": 27, "BRO01": 28, "HRL01": 28, "SGR01": 29}
SMALL = {"26FT": 11, "16FT": 4}
CITY_GROUPS = {
    "PHX": ["PHX.H", "MKE.H"],
    "SEA": ["SEA.H", "PIT.H", "JAX.H", "PDX.H", "RNO.H", "COS.H", "GRR.H"],
    "MSP": ["MSP.H", "BUF.H", "RIC.H", "LIT.H"],
    "BOS": ["BOS.H", "ABQ.H", "BOI.H"],
    "STL": ["STL.H", "CMH.H", "ICT.H"],
    "SAN": ["SAN.H", "TUS.H"],
}
CITY_NAMES = {"PHX": "PHX", "SEA": "SEA / PIT", "MSP": "MSP", "BOS": "BOS / ABQ", "STL": "STL / CMH",
              "SAN": "SAN / TUS"}
HUB_GROUP = {h: g for g, hubs in CITY_GROUPS.items() for h in hubs}
STATION = re.compile(r"^[A-Z]{3}\d{2}$")
CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
RANK = {"here": 0, "dock": 1, "coming": 2}


# ---------- reading cells and keys ----------

def text(cell):
    if cell is None:
        return ""
    if isinstance(cell, list):
        return "".join(text(c.get("text", c.get("name", c.get("value")))) if isinstance(c, dict) else str(c) for c in cell)
    if isinstance(cell, dict):
        return text(cell.get("text", cell.get("name", cell.get("value"))))
    return str(cell)


def ms_of(cell):
    if isinstance(cell, (int, float)) and not isinstance(cell, bool):
        return int(cell)
    if isinstance(cell, dict) and isinstance(cell.get("value"), list) and cell["value"] and isinstance(cell["value"][0], (int, float)):
        return int(cell["value"][0])
    return None


def truck_key(dest):
    d = str(dest or "").strip().upper()
    if STATION.match(d):
        return d
    hubs = re.findall(r"[A-Z]+\.H", d)
    return HUB_GROUP.get(hubs[0]) if hubs else None


def bag_key(bag):
    d = str(bag.get("destinCenterName") or "").strip().upper()
    return d if STATION.match(d) else HUB_GROUP.get(d)


def name_of(key):
    return CITY_NAMES.get(key, key)


def room_of(key, kind):
    kind = kind or "53FT"
    if kind in SMALL:
        return SMALL[kind]
    return 28 if key in CITY_NAMES else FULL_53.get(key, 27)


def rows_from(items):
    out = []
    for it in items:
        f = it.get("fields", {})
        mt = text(f.get("Task no.")).strip()
        key = truck_key(text(f.get("Destination")))
        day = ms_of(f.get("Departure date") if f.get("Departure date") is not None else f.get("Shift time"))
        pickup = ms_of(f.get("Pickup time"))
        if not key or not day:
            continue
        task = re.match(r"^TT\d+", mt)
        out.append({
            "id": it.get("record_id"), "day": day, "key": key,
            "type": text(f.get("Truck type")).strip().upper() or "53FT",
            "cancelled": bool(re.search(r"cancel|true|yes", text(f.get("Cancelled")), re.I) or "cancel" in mt.lower()),
            "task": task.group(0) if task else None,
            "planned": mt if CLOCK.match(mt) else (datetime.fromtimestamp(pickup / 1000, TZ).strftime("%H:%M") if pickup else None),
            "sent": f.get("Sent") is True,
            "driver": text(f.get("Driver")).strip() or None,
            "arrived": ms_of(f.get("Driver arrival")),
            "left": ms_of(f.get("Actual departure")),
            "dock": text(f.get("Dock")).strip() or None,
            "leave_by": text(f.get("Leave by")).strip() or None,
        })
    return out


def on_night(clock_text, night_start):
    """'1:00' / '23:00' as a time on the night starting at night_start (6 AM, ms): before 6 AM is the next morning."""
    m = CLOCK.match(str(clock_text or "").strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return night_start + ((h + 24 if h < 6 else h) - 6) * 3600000 + mi * 60000


def state_of(r, now=None):
    if r["left"]:
        return "left"
    if r["arrived"]:
        if now and now - r["arrived"] > STALE_H * 3600000:
            return "left"
        return "here"
    if r["dock"] and r["task"]:
        return "dock"
    return "coming"


# ---------- the board ----------

def build(rows, bags, held, now, night_start, loaded=None):
    """now, night_start: ms. loaded: {TT task: boxes checked out to it}. Returns the board dict the page and
    the alerts use."""
    on_truck = loaded or {}
    h = 3600000
    day_start = night_start - 6 * h  # The departure date is midnight of the operating day
    trucks = []
    for r in rows:
        if r["cancelled"] or not (day_start - 24 * h <= r["day"] <= day_start):
            continue
        r = {**r, "state": state_of(r, now)}
        if r["state"] == "left" and (not r["left"] or now - r["left"] > SHOW_LEFT_H * h):
            continue
        # Yesterday's rows only while still open and started (an early-morning city truck, a late SGR01).
        if r["day"] != day_start and (r["state"] == "left" or not (r["arrived"] or r["dock"] or r["task"])):
            continue
        trucks.append(r)
    # A dock a truck is confirmed at can't also hold an older "dock assigned" truck: that one is stale.
    taken = {t["dock"] for t in trucks if t["state"] == "here" and t["dock"]}
    for t in trucks:
        if t["state"] == "dock" and t["dock"] in taken:
            t["state"] = "coming"
    last_left = {}
    for r in rows:
        if r["left"] and r["left"] <= now and now - r["left"] <= LOOK_BACK_H * h:
            last_left[r["key"]] = max(last_left.get(r["key"], 0), r["left"])
    keys = sorted({t["key"] for t in trucks})
    pools = {}
    for key in keys:
        since = now - 24 * h
        if key in NIGHT_FROM:
            floor = datetime.fromtimestamp(now / 1000, TZ).replace(hour=NIGHT_FROM[key], minute=0, second=0, microsecond=0)
            if floor.timestamp() * 1000 > now:
                floor -= timedelta(days=1)
            since = max(since, int(floor.timestamp() * 1000))
        out = loaded = filling = 0
        for b in bags:
            if bag_key(b) != key or str(b.get("exchangeBureauCode") or "") in held:
                continue
            if int(b.get("waybillCount") or 0) < BOX_MIN:
                continue
            closed = b.get("optFinishStatusTime")
            if not closed:
                if (b.get("optCreateTime") or 0) > since:
                    filling += 1
                continue
            if closed <= since or closed > now:
                continue
            if b.get("optCheckOutTime"):  # already on a truck (counted on that truck as Loaded)
                loaded += 1
                continue
            out += 1
        pools[key] = {"name": name_of(key), "since": since, "since_truck": key in last_left,
                      "out": out, "loaded": loaded, "filling": filling}

    def order(t):
        when = t["arrived"] or on_night(t["planned"], night_start) or float("inf")
        dock = int(t["dock"]) if (t["dock"] or "").isdigit() else 999
        return (RANK[t["state"]], when, dock)

    result = []
    for key in keys:
        left = pools[key]["out"]
        open_ = sorted((t for t in trucks if t["key"] == key and t["state"] != "left"), key=order)
        for t in open_:
            # "Already out" is what's still out when it's this truck's turn (55 out for SGR01 = the first truck
            # says 55, the second 55 - 29 = 26), so it can be bigger than the room.
            room = room_of(key, t["type"])
            on = min(room, on_truck.get(t["task"], 0)) if t["task"] else 0
            space = room - on
            out = left
            left = max(0, left - space)
            result.append({**t, "name": name_of(key), "room": room, "loaded": on, "out": out,
                           "pull": max(0, space - out), "extra": 0,
                           "leave_at": on_night(t["leave_by"], night_start), "planned_at": on_night(t["planned"], night_start)})
        if open_:
            result[-1]["extra"] = left
        for t in trucks:
            if t["key"] == key and t["state"] == "left":
                result.append({**t, "name": name_of(key), "room": room_of(key, t["type"]), "loaded": None, "out": None, "pull": None,
                               "extra": 0, "leave_at": on_night(t["leave_by"], night_start), "planned_at": None})
    return {"at": now, "trucks": result, "pools": pools}


def clock(ms):
    t = datetime.fromtimestamp(ms / 1000, TZ)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def clock_text(s):
    m = CLOCK.match(str(s or "").strip())
    if not m:
        return s
    h = int(m.group(1))
    return f"{h % 12 or 12}{'' if m.group(2) == '00' else ':' + m.group(2)} {'AM' if h < 12 else 'PM'}"


def leave_text(t):
    if t["leave_by"]:
        return f"Leave by {clock_text(t['leave_by'])}"
    if t["arrived"]:
        return f"Arrived {clock(t['arrived'])}"
    if t["planned"]:
        return f"Planned {clock_text(t['planned'])}"
    return "No time on the sheet"


def block(t):
    """One truck in the standard truck card format: leave by (or arrival), room, already out, pull."""
    head = " · ".join(x for x in (t["name"], f"Dock {t['dock']}" if t["dock"] else "No dock yet", t["driver"]) if x)
    lines = [head]
    if t["state"] == "here":
        lines.append(f"Here since {clock(t['arrived'])}")
    elif t["state"] == "dock":
        lines.append("At dock (arrival not logged)")
    lines += [leave_text(t), f"Room: {t['room']}"] + ([f"Loaded: {t['loaded']}"] if t.get("loaded") else []) + [
              f"Already out: {t['out']}",
              f"Pull: {t['pull']}" + (f" ({t['extra']} extra wait for the next truck)" if t["extra"] else "")]
    return "\n".join(lines)


def alerts(board, said):
    """A truck at the dock that still needs boxes: one alert per truck, updated in place (same key) when its
    numbers change, so the phone buzzes once when it shows up. Returns (new said, alerts)."""
    nxt, out = {}, []
    for t in board["trucks"]:
        if t["state"] not in ("here", "dock"):
            continue
        was = said.get(t["id"])
        if t["pull"] > 0 or was:
            nxt[t["id"]] = {"pull": t["pull"], "state": t["state"]}
            if not was or was.get("pull") != t["pull"] or was.get("state") != t["state"]:
                where = f"dock {t['dock']}" if t["dock"] else "the dock"
                title = f"{t['name']} truck at {where}: pull {t['pull']}" if t["pull"] > 0 else f"{t['name']} truck at {where}: full"
                out.append({"key": f"dock|{t['id']}", "title": title, "text": block(t)})
    return nxt, out


def dock_tasks(board):
    """Task list: one task per truck at the dock that still needs boxes. Gone once it's full or has left."""
    out = []
    for t in board["trucks"]:
        if t["state"] not in ("here", "dock") or t["pull"] <= 0:
            continue
        where = f"dock {t['dock']}" if t["dock"] else "its door (no dock number yet)"
        out.append({"tid": t["id"], "rank": 20, "ttl_min": 40,
                    "do": f"Pull {t['pull']} {t['name']} boxes to {where}",
                    "why": " · ".join(x for x in block(t).splitlines()[1:] if not x.startswith("Pull:"))})
    return out


# ---------- reading ----------

def chat_token():
    """The chat app's token, kept 90 minutes so each run costs one table call, not two."""
    import chat_api
    if TOKEN_FILE.exists():
        saved = json.loads(TOKEN_FILE.read_text())
        if saved.get("until", 0) > time.time():
            return saved["token"]
    token = chat_api.get_token()
    DATA.mkdir(exist_ok=True)
    TOKEN_FILE.write_text(json.dumps({"token": token, "until": time.time() + 90 * 60}))
    TOKEN_FILE.chmod(0o600)
    return token


def read_rows():
    import chat_api
    body = {"field_names": FIELDS, "sort": [{"field_name": "Shift time", "desc": True}], "automatic_fields": False}
    path = f"/bitable/v1/apps/{TABLE_APP}/tables/{DEPARTURES}/records/search?page_size=500"
    try:
        res = chat_api.call("POST", path, chat_token(), body)
    except chat_api.ChatError:
        TOKEN_FILE.unlink(missing_ok=True)
        res = chat_api.call("POST", path, chat_token(), body)
    return rows_from(res["data"].get("items") or [])


def read_bags(now):
    import chute_alerts
    box = [chute_alerts.wms_token()]
    fmt = "%Y-%m-%d %H:%M:%S"
    start = (now - timedelta(hours=LOOK_BACK_H)).strftime(fmt)
    bags = []
    for page in range(1, 30):
        d = chute_alerts.wms(box, BAGS_API, {"packageNoList": [], "createBeginTime": start, "createEndTime": now.strftime(fmt),
                                             "startCenterId": CENTER_ID, "pageNum": page, "pageSize": 1000})
        got = d.get("list") or []
        bags += got
        if not got or len(bags) >= int(d.get("total") or 0):
            break
    return bags


def read_loaded(tasks):
    """{TT task: boxes (bags of 30+ packages) checked out to it} from the truck scan details. The rows are one
    per package, so a bag's size is its row count. One request per truck at the dock."""
    import chute_alerts
    box = [chute_alerts.wms_token()]
    out = {}
    for task in tasks:
        data = {"originTransitId": HUB_TRANSIT_ID, "scanType": "", "sealNo": "", "serialNumber": "", "taskNo": task,
                "startTime": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d 00:00:00"),
                "endTime": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")}
        sizes = {}
        for page in range(1, 6):
            d = chute_alerts.wms(box, SCAN_API, {"data": data, "pageNum": page, "pageSize": 10000})
            got = d.get("records") or d.get("list") or []
            for r in got:
                if str(r.get("operationType")) == "1" and r.get("bagNo"):
                    sizes[r["bagNo"]] = sizes.get(r["bagNo"], 0) + 1
            if len(got) < 10000:
                break
        out[task] = sum(1 for n in sizes.values() if n >= BOX_MIN)
    return out


def read_trip(tasks):
    """{TT task: (what, ms)} from each task's newest scan in the truck scan details (one 1-row request per
    task): ("unloaded", ms) once the destination scanned it in, ("loaded", ms) for the last check-out at the hub.
    The truck sheet often never gets an arrival or departure time (a truck can show "coming, pull 20" until
    morning while the WMS has it signed out and unloaded at the station), so the WMS settles it."""
    import chute_alerts
    box = [chute_alerts.wms_token()]
    out = {}
    for task in tasks:
        data = {"originTransitId": HUB_TRANSIT_ID, "scanType": "", "sealNo": "", "serialNumber": "", "taskNo": task,
                "startTime": (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d 00:00:00"),
                "endTime": (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")}
        try:
            got = chute_alerts.wms(box, SCAN_API, {"data": data, "pageNum": 1, "pageSize": 1})
        except Exception as e:  # noqa: BLE001 - the sheet alone is still a board
            print(f"trip check failed for {task}: {e}")
            continue
        rows = got.get("records") or got.get("list") or []
        if not rows:
            continue
        r = rows[0]
        try:
            ms = int(datetime.strptime(r["createTime"], "%m/%d/%Y %H:%M:%S").replace(tzinfo=TZ).timestamp() * 1000)
        except (KeyError, ValueError):
            continue
        unloaded = str(r.get("operationType")) == "0" and str(r.get("operatorDept") or "") not in ("", HUB_CODE)
        out[task] = ("unloaded" if unloaded else "loaded", ms)
    return out


def read_tasks(now):
    """{TT task: WMS truck task} for trucks leaving the hub, planned from yesterday to tomorrow (one request).
    Dispatch makes each task minutes before the truck docks, so it has the real dock and departure times."""
    import chute_alerts
    box = [chute_alerts.wms_token()]
    data = {"originTransitIdList": [int(HUB_TRANSIT_ID)], "timeType": 1,
            "startLocalTime": (now - timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"),
            "endLocalTime": (now + timedelta(days=1)).strftime("%Y-%m-%d 23:59:59")}
    out = {}
    for page in range(1, 4):
        d = chute_alerts.wms(box, TASKS_API, {"data": data, "pageNum": page, "pageSize": 200})
        got = d.get("records") or []
        out.update({r["taskNo"]: r for r in got if r.get("taskNo")})
        if len(got) < 200:
            break
    return out


def local_ms(text_):
    try:
        return int(datetime.strptime(str(text_), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp() * 1000)
    except ValueError:
        return None


def with_tasks(rows, tasks):
    """Rows with arrival, departure and leave-by from the truck's WMS task. A row ticked Sent with no departure
    anywhere has left: at its arrival, else its planned pickup."""
    # A WMS task no row names (dispatch hasn't typed the task number yet) goes to the first row of that
    # destination with no task, so a truck at the dock shows as here even while its row is still blank.
    named = {r["task"] for r in rows if r["task"]}
    spare = sorted((t for n, t in tasks.items() if n not in named and t.get("placeOfOrigin") == HUB_CODE),
                   key=lambda t: str(t.get("timeOfArrivalOrigin") or t.get("plannedDepartureTime") or ""))
    rows = list(rows)
    for t in spare:
        key = truck_key(t.get("destination"))
        for i, r in enumerate(rows):
            if r["key"] == key and not r["task"] and not r["cancelled"] and not r.get("sent") and not r["left"]:
                rows[i] = {**r, "task": t["taskNo"]}
                break
    out = []
    for r in rows:
        t = tasks.get(r["task"]) if r["task"] else None
        if t:
            planned_out = local_ms(t.get("plannedDepartureTime")) if t.get("plannedDepartureTime") else None
            r = {**r, "arrived": r["arrived"] or (local_ms(t["timeOfArrivalOrigin"]) if t.get("timeOfArrivalOrigin") else None),
                 "left": r["left"] or (local_ms(t["actualDepartureTime"]) if t.get("actualDepartureTime") else None),
                 "leave_by": r["leave_by"] or (datetime.fromtimestamp(planned_out / 1000, TZ).strftime("%H:%M") if planned_out else None)}
        if r.get("sent") and not r["left"]:
            r = {**r, "left": r["arrived"] or on_night(r["planned"], r["day"] + 6 * 3600000) or r["day"]}
        out.append(r)
    return out


def with_trip(rows, trips, now):
    """Rows with the sheet's missing times filled from the WMS: a truck unloaded at its destination, or checked
    out at the hub with no new scan for SIGNED_OUT_MIN (and no arrival in the sheet), has left; one checked out
    more recently is here, loading."""
    out = []
    for r in rows:
        trip = trips.get(r["task"]) if r["task"] and not r["left"] else None
        if trip:
            what, ms = trip
            # A truck the sheet has at the dock may just be paused mid-load; only its unload elsewhere ends it.
            if what == "unloaded" or (not r["arrived"] and now - ms > SIGNED_OUT_MIN * 60000):
                r = {**r, "left": ms, "left_by": "wms"}
            elif not r["arrived"]:
                r = {**r, "arrived": ms, "arrived_by": "wms"}
        out.append(r)
    return out


def push_key():
    """The board worker's push key: HUBOPS_BOARD_KEY, else data/.board-push-key, else "" (demo)."""
    if os.environ.get("HUBOPS_BOARD_KEY"):
        return os.environ["HUBOPS_BOARD_KEY"]
    try:
        return PUSH_KEY_FILE.read_text().strip()
    except OSError:
        return ""


def held_routes(key):
    req = urllib.request.Request(f"{WORKER}/dock-held", headers={"X-Push-Key": key, "User-Agent": "hubops-dock"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return set(json.loads(res.read() or b"{}").keys())
    except (OSError, ValueError) as e:
        print(f"held routes not read ({e}); none left out this time")
        return set()


def refresh_asked(key):
    """When the page last asked for a refresh (ms), or 0."""
    req = urllib.request.Request(f"{WORKER}/dock-refresh", headers={"X-Push-Key": key, "User-Agent": "hubops-dock"})
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return int(json.loads(res.read() or b"{}").get("asked") or 0)
    except (OSError, ValueError):
        return 0


def push(board, key):
    body = json.dumps(board, ensure_ascii=False).encode()
    for wait in (5, 20, None):
        try:
            req = urllib.request.Request(f"{WORKER}/dock-data", data=body, method="POST", headers={
                "Content-Type": "application/json", "User-Agent": "hubops-dock", "X-Push-Key": key})
            with urllib.request.urlopen(req, timeout=60) as res:
                if res.status == 200:
                    return
                raise OSError(f"HTTP {res.status}")
        except OSError as e:
            if wait is None:
                raise SystemExit(f"page update failed: {e}")
            time.sleep(wait)


def chute_ranges(nums):
    """[298, 299, 300, 302] -> "298–300, 302"."""
    out, nums = [], sorted(set(nums))
    for n in nums:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return ", ".join(f"{a:03d}" if a == b else f"{a:03d}–{b:03d}" for a, b in out)


def chutes_by_key(path=None):
    """{"AEX01": "298–302", "PHX": "051–052, 190–191", ...} from the saved WMS chute map (data/chute_map.json),
    so each dock card shows where that truck's boxes come from. {} when there's no saved map."""
    try:
        dests = json.loads((path or DATA / "chute_map.json").read_text())["dests"]
    except (OSError, ValueError, KeyError):
        return {}
    keys = {}
    for dest, nums in dests.items():
        d = str(dest).upper()
        key = d.split("-")[0] if STATION.match(d.split("-")[0]) else HUB_GROUP.get(d)
        if key:
            keys.setdefault(key, []).extend(nums)
    return {k: chute_ranges(v) for k, v in keys.items()}


def night_start_of(now):
    six = now.replace(hour=6, minute=0, second=0, microsecond=0)
    if now.hour < 6:
        six -= timedelta(days=1)
    return int(six.timestamp() * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--now", action="store_true", help="run even in the daytime half-hour gap")
    args = parser.parse_args()
    now = datetime.now(TZ)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    key = push_key()
    if not args.dry_run and not args.now:
        every = DAY_EVERY_MIN if 5 <= now.hour < 15 else NIGHT_EVERY_MIN
        ran = state.get("ran", 0)
        if time.time() - ran < every * 60 and refresh_asked(key) <= ran * 1000:
            return 0
    rows = read_rows()
    try:
        rows = with_tasks(rows, read_tasks(now.replace(tzinfo=None)))
    except Exception as e:  # noqa: BLE001 - the sheet and the scan check still make a board
        print(f"WMS truck tasks not read: {e}")
    bags = read_bags(now.replace(tzinfo=None))
    held = held_routes(key) if not args.dry_run else set()
    now_ms = int(now.timestamp() * 1000)
    open_tasks = sorted({r["task"] for r in rows if r["task"] and not r["cancelled"] and not r["left"]
                         and now_ms - r["day"] <= 48 * 3600000})
    rows = with_trip(rows, read_trip(open_tasks), now_ms)
    tasks_here = sorted({r["task"] for r in rows if r["task"] and not r["cancelled"] and now_ms - r["day"] <= 48 * 3600000
                         and state_of(r, now_ms) in ("here", "dock")})
    board = build(rows, bags, held, now_ms, night_start_of(now), read_loaded(tasks_here))
    board["chutes"] = chutes_by_key()
    if args.dry_run:
        for t in board["trucks"]:
            if t["state"] == "left":
                print(f"[left {clock(t['left'])}] {t['name']} {t['driver'] or ''}")
            else:
                print(f"[{t['state']}]\n{block(t)}\n")
        for k, p in board["pools"].items():
            print(f"{p['name']}: out {p['out']} (already checked out {p['loaded']}), filling {p['filling']}, since {clock(p['since'])}")
        return 0
    said, new = alerts(board, state.get("said", {}))
    # These go to the task list (do_now.py, one buzz per new truck), not the Alerts tab.
    try:
        import do_now
        do_now.replace("dock", dock_tasks(board))
    except Exception as e:  # noqa: BLE001 - the board still goes out
        print(f"task list not updated: {type(e).__name__}: {e}")
    push(board, key)
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps({"ran": time.time(), "said": said}))
    print(f"{now:%H:%M} sent: {sum(t['state'] != 'left' for t in board['trucks'])} trucks, {len(new)} alerts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
