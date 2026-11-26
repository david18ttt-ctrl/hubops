#!/usr/bin/env python3
"""Unsorted truckloads board: every truck at the hub that still has packages not sorted, oldest arrival first, with
each gaylord's bag number and short code and every waybill still waiting. The floor uses it to pick which gaylords
the pallet workers pull first (oldest truck first: the 12/24-hour clocks start when the truck arrives).

    python3 unsorted_board.py            refresh: read the WMS, send the board to the page (every 10 minutes)
    python3 unsorted_board.py --dry-run  print the board, send nothing

Read-only in the WMS: the truck list (central dashboard), each truck's unloading scans (Loading&Unloading
Query: bag -> waybills), the bags made at the hub (Box No. Query, Manual and Sorter bags, with their
waybills) and Sorting Records. "Sorted" means the waybill is already in a bag made here (by hand with a
PDA or by the machine) or had a successful sort on the machine or the manual sorter.

Truck bag lists and sorted waybills are kept in data/unsorted.sqlite, so each refresh only asks the WMS
about waybills not sorted yet. The page is web/src/unsorted.js at HUBOPS_APP_URL (or HUBOPS_UNSORTED_URL);
the push key comes from HUBOPS_UNSORTED_PUSH_KEY or data/.unsorted-push-key.
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import hubconfig
import hub_numbers as hn

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
DB = DATA / "unsorted.sqlite"
PUSH_KEY_FILE = DATA / ".unsorted-push-key"
PUSH_URL = (os.environ.get("HUBOPS_UNSORTED_URL") or os.environ.get("HUBOPS_APP_URL") or "http://127.0.0.1:8787").rstrip("/") + "/unsorted-data"
SCHEDULE_FILE = DATA / "shift-schedule.json"  # who works in which building each day (see schedule())
LOOK_BACK_DAYS = 2  # Working days of trucks to look at (today and yesterday)
FULL_SHARE = 0.5  # A gaylord with at least this share of its packages not sorted is still a load to pull;
                  # less is stray packages (problem parcels, mis-sorts), listed apart
MIN_LOAD = 100  # A truck needs this many packages in full gaylords to count as a load (station returns
                # of a few dozen packages go with the strays)
REFETCH_HOURS = 4  # A truck's bag list is read again until it's unloaded and this old
SAMPLE = 20  # Waybills asked about per untouched gaylord (see refresh_sorted)
STRAY_EVERY_MIN = 30  # Leftovers of gaylords run long ago are asked about this often
SETTLED_HOURS = 6  # A truck this old with a gaylord under half left: those are leftovers, not freight being run
LOAD_VERSION = 2  # Saved truck loads older than this layout are read again (2 = bags never scanned in are listed too)
BAGS_API = "/api/bags/search"  # Box No. Query
BAG_WAYBILLS_API = "/api/bags/parcels"
CENTER_ID = 1  # the hub in Box No. Query
HUB_TRANSIT_ID = 1  # the hub in the TMS place list
OPEN_BAG_HOURS = 3  # Bags this new are read again each time: packages are still going in
SCHEDULE_MINUTES = 60  # The shift schedule is read again this often
NIGHT_UNTIL = 8  # Before 8 AM a scan belongs to the night shift that started the day before
GUESS_DAYS = 3  # Off that day (blank)? The nearest day within this many with a building is a guess


def open_db():
    DB.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=120)  # Waits out the 10-minute job if both run at once
    db.executescript("""
        CREATE TABLE IF NOT EXISTS trucks (task TEXT PRIMARY KEY, fetched TEXT, final INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS sorted (waybill TEXT PRIMARY KEY, seen TEXT);
        CREATE TABLE IF NOT EXISTS bags_read (bag TEXT, created INTEGER, read TEXT, PRIMARY KEY (bag, created));
        CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, fetched TEXT, data TEXT);
    """)
    return db


def short_code(bag):
    """What to look for on the bag tag: the last 4 digits, plus the suffix if there is one
    (BG012611250028-25 -> 0028-25, GL2611090008265 -> 8265)."""
    main, _, suffix = bag.partition("-")
    digits = "".join(c for c in main if c.isdigit())
    code = (digits or main)[-4:]
    return f"{code}-{suffix}" if suffix else code


def scan_query(truck):
    arrived = datetime.strptime(truck["actualArrivalTime"][:10], "%Y-%m-%d").date()
    return {"destinationTransitId": str(truck.get("destinationTransitId") or HUB_TRANSIT_ID),
            "originTransitId": str(truck["originTransitId"]), "scanType": "1", "sealNo": "", "serialNumber": "",
            "taskNo": truck["taskCode"], "startTime": f"{arrived - timedelta(days=2)} 00:00:00",
            "endTime": f"{arrived + timedelta(days=2)} 23:59:59"}


def scan_count(token, truck):
    """How many scan records the WMS has for the task right now: one tiny request. Unchanged since the last full read
    = nothing new was scanned, so the saved load is still right (a full read is up to 14 pages of 2,000 rows)."""
    res = hn.wms(token, hn.SCAN_LIST_API, {"data": scan_query(truck), "pageNum": 1, "pageSize": 1}) or {}
    return int(res.get("total") or 0)


def task_scans(token, truck):
    """Every scan of one truck task into the hub: ([(kind, bag, waybill, station, who, time)], the WMS's record count).
    kind "in" = unloading check-in here; "out" = loading sign-out at the origin: what was put on the truck, so the
    load is known before anyone scans it in (a truck that sits at the door unscanned still shows on the board)."""
    data = scan_query(truck)
    rows, page, seen, total = [], 1, 0, 0
    while True:
        res = hn.wms(token, hn.SCAN_LIST_API, {"data": data, "pageNum": page, "pageSize": 2000}) or {}
        records = res.get("records") or []
        total = int(res.get("total") or 0)
        for r in records:
            kind = {hn.UNLOAD_CHECK_IN: "in", hn.LOAD_CHECK_OUT: "out"}.get(str(r.get("operationType")))
            if kind and r.get("waybillNo"):
                rows.append((kind, r.get("bagNo") or "(no bag)", r["waybillNo"], r.get("targetCenterName") or "",
                             r.get("createByName") or "", r.get("createTime") or ""))
        seen += len(records)
        if not records or seen >= total:
            return rows, total
        page += 1


def wms_time(text):
    """'11/10/2026 23:07:54' -> '2026-11-10 23:07'."""
    try:
        return datetime.strptime(text, "%m/%d/%Y %H:%M:%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16]


def truck_load(db, token, truck, now):
    """The truck's bags and waybills, from the cache once it's unloaded and a few hours old.
    A bag with "noscan" was signed out at the origin onto this truck and never scanned in here: it is still on the
    trailer, or it was unloaded without a scan."""
    arrived = datetime.strptime(truck["actualArrivalTime"][:19], "%Y-%m-%d %H:%M:%S")
    row = db.execute("SELECT data, final, fetched FROM trucks WHERE task = ?", (truck["taskCode"],)).fetchone()
    if row and json.loads(row[0]).get("v") == LOAD_VERSION:
        # Unloaded and a few hours old: never read again. Otherwise new scans can still come in, but the older the
        # truck the rarer: an old task the WMS never closed would otherwise be re-read in full every run.
        age, since = now - arrived, now - datetime.fromisoformat(row[2])
        every = timedelta() if age < timedelta(hours=6) else timedelta(minutes=30) if age < timedelta(hours=24) else timedelta(hours=6)
        if row[1] or since < every:
            return json.loads(row[0])
        saved = json.loads(row[0])
        if saved.get("n") is not None and scan_count(token, truck) == saved["n"]:  # nothing scanned since the last read
            final = bool(truck.get("unloadCompleteTime")) and saved.get("who") and age > timedelta(hours=REFETCH_HOURS)
            db.execute("UPDATE trucks SET fetched = ?, final = ? WHERE task = ?", (now.isoformat(), int(bool(final)), truck["taskCode"]))
            return saved
    scans, records = task_scans(token, truck)
    bags = {}
    for kind, bag, waybill, station, who, when in scans:
        if kind != "in":
            continue
        b = bags.setdefault(bag, {"waybills": {}, "first": None, "last": None})
        b["waybills"][waybill] = station
        t = wms_time(when)
        b["first"] = min(filter(None, [b["first"], t]))
        b["last"] = max(filter(None, [b["last"], t]))
    for kind, bag, waybill, station, who, when in scans:
        if kind == "out" and (bag not in bags or bags[bag].get("noscan")):
            bags.setdefault(bag, {"waybills": {}, "first": None, "last": None, "noscan": True})["waybills"][waybill] = station
    who_scanned = {}
    for kind, _, _, _, who, when in scans:
        if kind == "in":
            count, first = who_scanned.get(who, (0, None))
            who_scanned[who] = (count + 1, min(filter(None, [first, wms_time(when)])))
    load = {"v": LOAD_VERSION, "n": records, "bags": bags, "unloaders": sorted(who_scanned, key=lambda w: -who_scanned[w][0])[:2],
            "who": who_scanned}
    final = bool(truck.get("unloadCompleteTime")) and who_scanned and now - arrived > timedelta(hours=REFETCH_HOURS)
    db.execute("INSERT OR REPLACE INTO trucks VALUES (?, ?, ?, ?)",
               (truck["taskCode"], now.isoformat(), int(bool(final)), json.dumps(load)))
    return load


def read_all(token, path, body):
    rows, page = [], 1
    while True:
        data = hn.wms(token, path, {**body, "pageNum": page, "pageSize": 1000}) or {}
        batch = data.get("list") or []
        rows += batch
        if not batch or len(rows) >= int(data.get("total") or 0):
            return rows
        page += 1


def refresh_bagged(db, token, since, now):
    """Saves as sorted every waybill in a bag made at the hub (by hand or by the machine) since `since`.
    Bags already read and older than a few hours aren't read again."""
    from concurrent.futures import ThreadPoolExecutor
    # The WMS refuses a create-time range much over a day (time.range.exceed), so ask in 20 h pieces
    bags, start, end = [], since, now + timedelta(hours=1)
    while start < end:
        stop = min(start + timedelta(hours=20), end)
        bags += read_all(token, BAGS_API, {"packageNoList": [], "startCenterId": CENTER_ID,
                                           "createBeginTime": f"{start:%Y-%m-%d %H:%M:%S}",
                                           "createEndTime": f"{stop:%Y-%m-%d %H:%M:%S}"})
        start = stop
    done = {(b, c) for b, c in db.execute("SELECT bag, created FROM bags_read")}
    fresh = int((now - timedelta(hours=OPEN_BAG_HOURS)).timestamp() * 1000)
    # Only hand-made (PDA) bags: machine-sorted waybills already show up in Sorting Records
    # (refresh_sorted), and reading every sorter bag's waybills each run would be a needless bulk load on the WMS.
    todo = [b for b in bags if b.get("bagTypeString") == "Manual" and b.get("waybillCount")
            and ((b["packageNo"], b.get("optCreateTime") or 0) not in done or (b.get("optCreateTime") or 0) > fresh)]

    def waybills(bag):
        rows = read_all(token, BAG_WAYBILLS_API, {"packageNo": bag["packageNo"], "deptId": CENTER_ID})
        return bag, [r["waybillNo"] for r in rows if r.get("waybillNo")]

    with ThreadPoolExecutor(4) as pool:
        for bag, numbers in pool.map(waybills, todo):
            db.executemany("INSERT OR IGNORE INTO sorted VALUES (?, ?)", [(w, now.isoformat()) for w in numbers])
            db.execute("INSERT OR REPLACE INTO bags_read VALUES (?, ?, ?)",
                       (bag["packageNo"], bag.get("optCreateTime") or 0, now.isoformat()))
    db.commit()
    return len(todo)


def refresh_sorted(db, token, trucks, loads, since, now):
    """Asks Sorting Records which waybills have been sorted and returns every waybill known sorted.

    Asking about every waybill still waiting, every run, would be 800 requests and an 8-minute run for 80,000
    waiting, slowest exactly when the floor is fullest. Instead:
      untouched gaylord (nothing sorted yet): only SAMPLE of its waybills are asked. A gaylord is dumped onto the
        belt whole, so if none of them is sorted it hasn't been run; the moment one is, the whole gaylord is asked.
      gaylord being run (some sorted): every waybill still waiting is asked, every run.
      leftovers of a gaylord run long ago (rejects, no-reads; the truck is SETTLED_HOURS old): asked every
        STRAY_EVERY_MIN minutes instead of every run.
    Same answers, a fraction of the requests."""
    every = list({w for load in loads.values() for b in load["bags"].values() for w in b["waybills"]})
    known = set()
    for i in range(0, len(every), 900):
        chunk = every[i:i + 900]
        known |= {w for (w,) in db.execute(f"SELECT waybill FROM sorted WHERE waybill IN ({','.join('?' * len(chunk))})", chunk)}
    row = db.execute("SELECT fetched FROM cache WHERE key = 'strays_asked'").fetchone()
    strays_due = not row or datetime.fromisoformat(row[0]) < now - timedelta(minutes=STRAY_EVERY_MIN)
    ask, sampled = set(), []
    for task, load in loads.items():
        arrived = datetime.strptime(trucks[task]["actualArrivalTime"][:19], "%Y-%m-%d %H:%M:%S")
        settled = now - arrived > timedelta(hours=SETTLED_HOURS)
        for b in load["bags"].values():
            left = [w for w in b["waybills"] if w not in known]
            size = len(b["waybills"])
            if not left:
                continue
            if len(left) == size and size >= 2 * SAMPLE:
                ask.update(sorted(left)[::size // SAMPLE][:SAMPLE])
                sampled.append(left)
            elif settled and len(left) < FULL_SHARE * size:
                if strays_due:
                    ask.update(left)
            else:
                ask.update(left)
    found = hn.sorted_waybills(token, sorted(ask), since) if ask else set()
    more = {w for left in sampled if any(w in found for w in left) for w in left if w not in ask}
    if more:  # gaylords that just started going onto the belt
        found |= hn.sorted_waybills(token, sorted(more), since)
    db.executemany("INSERT OR IGNORE INTO sorted VALUES (?, ?)", [(w, now.isoformat()) for w in found])
    if strays_due:
        db.execute("INSERT OR REPLACE INTO cache VALUES ('strays_asked', ?, '')", (now.isoformat(),))
    print(f"{datetime.now():%H:%M:%S} sorted check: asked {len(ask) + len(more):,} of {len(every) - len(known):,} waiting "
          f"({len(sampled)} untouched gaylords sampled, {len(more):,} more for gaylords just started), {len(found):,} newly sorted")
    return known | found


def name_tokens(name):
    """Name words used to match WMS operator names to schedule names (case and order don't matter)."""
    return set(re.findall(r"[a-z]+", str(name or "").lower()))


def building_of(cell):
    """The building a schedule cell names: "Building 1", "Building 2" or None (off, blank, a bare shift letter)."""
    cell = str(cell or "").lower()
    if re.search(r"building ?2|\bb2\b", cell):
        return "Building 2"
    if re.search(r"building ?1|\bb1\b|hub ?1|\bh1\b", cell):
        return "Building 1"
    return None


def people(values):
    """[(name tokens, {date: cell})] from the schedule file: {"people": [{"name": ..., "days": {"YYYY-MM-DD": cell}}]}."""
    out = []
    for person in (values or {}).get("people", []):
        words = name_tokens(person.get("name"))
        days = {}
        for d, cell in (person.get("days") or {}).items():
            try:
                days[datetime.strptime(d, "%Y-%m-%d").date()] = str(cell).strip()
            except ValueError:
                continue
        if words:
            out.append((words, days))
    return out


def where(staff, name, when):
    """(building | None, sure) for a person at a time. sure is False when their cell that day names no building and
    the building comes from the nearest day that does."""
    want = name_tokens(name)
    if not want:
        return None, False
    rows = [cells for words, cells in staff if want <= words or (len(words) >= 2 and words <= want)]
    if not rows:
        return None, False
    day = when.date()
    order = [day - timedelta(days=1), day] if when.hour < NIGHT_UNTIL else [day]
    for d in order:
        for cells in rows:
            b = building_of(cells.get(d, ""))
            if b:
                return b, True
    for gap in range(1, GUESS_DAYS + 1):
        for d in (day - timedelta(days=gap), day + timedelta(days=gap)):
            for cells in rows:
                b = building_of(cells.get(d, ""))
                if b:
                    return b, False
    return None, False


def truck_building(staff, unloaders):
    """The building ("Building 1", "Building 2", with "?" when only guessed) for a truck, from {name: (scans, first scan
    time)}, or "". The building with the most scans by people the schedule puts there that day wins."""
    sure, guess = {}, {}
    for name, (count, first) in unloaders.items():
        try:
            when = datetime.strptime(first[:16], "%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            continue
        b, certain = where(staff, name, when)
        if b:
            pot = sure if certain else guess
            pot[b] = pot.get(b, 0) + count
    if sure:
        return max(sure, key=sure.get)
    if guess:
        return max(guess, key=guess.get) + "?"
    return ""


def schedule(db, now):
    """The shift schedule's people, read from data/shift-schedule.json at most once an hour; the last copy if the
    file can't be read."""
    row = db.execute("SELECT fetched, data FROM cache WHERE key = 'schedule'").fetchone()
    if not row or datetime.fromisoformat(row[0]) < now - timedelta(minutes=SCHEDULE_MINUTES):
        try:
            values = json.loads(SCHEDULE_FILE.read_text())
            db.execute("INSERT OR REPLACE INTO cache VALUES ('schedule', ?, ?)",
                       (now.isoformat(), json.dumps(values, ensure_ascii=False)))
            db.commit()
            return people(values)
        except Exception as e:  # The board still works without the building tags
            print(f"shift schedule not read: {e}", file=sys.stderr)
    return people(json.loads(row[1])) if row else []


def building(staff, load):
    """The building a truck was unloaded in, from who unloaded it and the shift schedule."""
    who = load.get("who")
    if who is None:  # Saved before the board knew scan times: the top unloaders at the first unload time
        first = min((b["first"] for b in load["bags"].values() if b["first"]), default="")
        who = {name: (1, first) for name in load.get("unloaders", [])}
    return truck_building(staff, who) if staff else ""


def build(db, token, now):
    day = now.date() if now.hour >= 6 else now.date() - timedelta(days=1)
    staff = schedule(db, now)
    trucks = {}
    for back in range(LOOK_BACK_DAYS):
        for t in hn.trucks(token, day - timedelta(days=back)):
            if t.get("actualArrivalTime") and t.get("originTransitId"):
                trucks[t["taskCode"]] = t
    t0 = datetime.now()
    loads = {task: truck_load(db, token, t, now) for task, t in trucks.items()}
    print(f"{datetime.now():%H:%M:%S} truck scans read: {len(trucks)} trucks in {(datetime.now() - t0).total_seconds():.0f} s")
    oldest = min((datetime.strptime(t["actualArrivalTime"][:19], "%Y-%m-%d %H:%M:%S") for t in trucks.values()), default=now)
    refresh_bagged(db, token, oldest - timedelta(hours=1), now)
    done = refresh_sorted(db, token, trucks, loads, oldest - timedelta(hours=1), now)
    db.commit()
    # A waybill scanned in under one task isn't counted again on the truck it was signed out on (bags get scanned
    # to the wrong truck); a bag never scanned in anywhere counts on the truck it left the origin on.
    scanned_in = {w for load in loads.values() for b in load["bags"].values() if not b.get("noscan") for w in b["waybills"]}
    board, almost, waybill_lists = [], [], {}
    for task, t in trucks.items():
        load = loads[task]
        bags, lines = [], []
        total = left = 0
        for bag, b in load["bags"].items():
            noscan = bool(b.get("noscan"))
            mine = {w: s for w, s in b["waybills"].items() if not (noscan and w in scanned_in)}
            waiting = {w: s for w, s in mine.items() if w not in done}
            total += len(mine)
            left += len(waiting)
            if waiting:
                stations = {}
                for s in waiting.values():
                    stations[s] = stations.get(s, 0) + 1
                entry_bag = {"bag": bag, "code": short_code(bag), "left": len(waiting), "of": len(mine),
                             "full": len(waiting) >= FULL_SHARE * len(mine),
                             "to": sorted(stations, key=lambda s: -stations[s])[:3]}
                if noscan:
                    entry_bag["noscan"] = True
                bags.append(entry_bag)
                # One row per gaylord, no waybill numbers: those stay on this machine.
                lines.append(f"{bag},{short_code(bag)},{len(waiting)},{len(mine)},"
                             + " ".join(sorted(stations, key=lambda s: -stations[s])) + (",not scanned in" if noscan else ","))
        if not left:
            continue
        bags.sort(key=lambda b: (not b["full"], -b["left"]))
        firsts = [b["first"] for b in load["bags"].values() if b["first"]]
        lasts = [b["last"] for b in load["bags"].values() if b["last"]]
        full = [b for b in bags if b["full"]]
        scanned_bags = sum(1 for b in load["bags"].values() if not b.get("noscan"))
        # "not unloaded": no gaylord scanned in and none of its packages sorted. "no scans": none scanned in, but its
        # packages are being sorted, so it was unloaded without scanning. "unloading": some scanned in, unload not closed.
        if not scanned_bags:
            state = "no scans" if left < total else "not unloaded"
        else:
            state = "unloading" if not t.get("unloadCompleteTime") and any(b.get("noscan") for b in bags) else ""
        entry = {"task": task, "from": t.get("placeOfOrigin"), "arrived": t["actualArrivalTime"][:19],
                 "kind": t.get("transportType") or "", "total": total, "left": left,
                 "in_full": sum(b["left"] for b in full), "gaylords": len(load["bags"]), "open": len(full),
                 "state": state, "sorted": total - left, "noscan_full": sum(1 for b in full if b.get("noscan")),
                 "noscan_in_full": sum(b["left"] for b in full if b.get("noscan")),
                 "bags": bags, "unloaded": [min(firsts) if firsts else "", max(lasts) if lasts else ""],
                 "by": load["unloaders"], "where": building(staff, load)}
        waybill_lists[task] = "bag,code,not_sorted,packages,stations,scan\n" + "\n".join(lines)
        if entry["in_full"] >= MIN_LOAD:
            board.append(entry)
        else:
            almost.append({k: entry[k] for k in ("task", "from", "arrived", "left", "total", "gaylords", "where")})
    board.sort(key=lambda e: e["arrived"])
    almost.sort(key=lambda e: e["arrived"])
    count = hn.wms(token, hn.SORT_COUNT_API, hn.window(day)) or {}
    return {"updated": now.strftime("%Y-%m-%d %H:%M:%S"), "day": day.isoformat(), "trucks": board, "almost": almost,
            "wms": {"waiting": int(count.get("allShouldSortCount") or 0), "older": int(count.get("hsShouldSortCount") or 0)}}, waybill_lists


def push(board, waybill_lists):
    """Board (small JSON) and every truck's waybills (one gzipped blob), each tried 3 times."""
    import gzip
    import time
    key = os.environ.get("HUBOPS_UNSORTED_PUSH_KEY") or (PUSH_KEY_FILE.read_text().strip() if PUSH_KEY_FILE.exists() else "")
    if not key:
        raise SystemExit(f"no HUBOPS_UNSORTED_PUSH_KEY or {PUSH_KEY_FILE.name}, so the page wasn't updated")
    blob = "\n".join(f"#{task}\n{csv}" for task, csv in waybill_lists.items())
    for path, body, kind in ((PUSH_URL, json.dumps(board, ensure_ascii=False).encode(), "application/json"),
                             (PUSH_URL.replace("/unsorted-data", "/unsorted-lists"), gzip.compress(blob.encode()),
                              "application/octet-stream")):
        for wait in (5, 20, None):
            try:
                req = urllib.request.Request(path, data=body, method="POST", headers={
                    "Content-Type": kind, "User-Agent": "hubops-unsorted", "X-Push-Key": key})
                with urllib.request.urlopen(req, timeout=120) as res:
                    if res.status == 200:
                        break
                    raise OSError(f"HTTP {res.status}")
            except OSError as e:
                if wait is None:
                    raise SystemExit(f"page update failed: {e}")
                time.sleep(wait)


def only_one(wait_minutes=20):
    """Holds data/unsorted.lock so one copy runs at a time: the scheduled job and a by-hand run would otherwise
    collide ("database is locked") and double the WMS reads. Waits for the other copy; None if it never finishes."""
    import fcntl
    import time
    DATA.mkdir(parents=True, exist_ok=True)
    lock = open(DATA / "unsorted.lock", "w")
    for _ in range(wait_minutes * 30):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lock
        except OSError:
            time.sleep(2)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # the log shows each step as it happens, not all at the end
    lock = only_one()
    if lock is None:
        print("another copy has been running for 20 minutes; not starting a second one", file=sys.stderr)
        return 1
    now = datetime.now(hubconfig.TIME_ZONE).replace(tzinfo=None)
    token = hn.wms_token()  # the shared login: one token for every WMS job
    print(f"{now:%H:%M:%S} started")
    db = open_db()
    board, waybill_lists = build(db, token, now)
    # The sorted check is the last WMS read, so the counts are as of the END of the run (a run takes minutes):
    # "updated"/"made" say that time, "started" when the run began.
    done_at = datetime.now(hubconfig.TIME_ZONE).replace(tzinfo=None)
    board.update(started=board["updated"], updated=done_at.strftime("%Y-%m-%d %H:%M:%S"), took_s=int((done_at - now).total_seconds()))
    try:  # floor_now.py and live_state.py read this (belt freight left)
        tmp = DATA / "unsorted-latest.tmp"
        tmp.write_text(json.dumps(dict(board, made=done_at.strftime("%Y-%m-%d %H:%M")), ensure_ascii=False))
        tmp.replace(DATA / "unsorted-latest.json")
    except OSError:
        pass
    db.execute("DELETE FROM sorted WHERE seen < ?", ((now - timedelta(days=5)).isoformat(),))
    db.execute("DELETE FROM trucks WHERE fetched < ?", ((now - timedelta(days=5)).isoformat(),))
    db.execute("DELETE FROM bags_read WHERE read < ?", ((now - timedelta(days=5)).isoformat(),))
    db.commit()
    if args.dry_run:
        for e in board["trucks"]:
            print(f"{e['arrived'][5:16]} {e['from']:<7} {e['where'] or '?':<5} {e['task']}: {e['left']:,} of {e['total']:,} not sorted; "
                  f"{e['open']} of {e['gaylords']} gaylords still full ({e['in_full']:,})"
                  + (f"  ** {(e['state'] or 'missed scan').upper()}: {e['noscan_full']} gaylords / {e['noscan_in_full']:,} never scanned in **" if e["noscan_full"] else "") + "  e.g. "
                  f"{', '.join(b['code'] for b in e['bags'][:6] if b['full'])}")
        for a in board["almost"]:
            print(f"   stray only: {a['arrived'][5:16]} {a['from']} {a['left']} packages")
        print(f"WMS waiting {board['wms']['waiting']:,} (older {board['wms']['older']:,})")
        return 0
    push(board, waybill_lists)
    print(f"{done_at:%H:%M} sent: {len(board['trucks'])} trucks, {sum(e['left'] for e in board['trucks']):,} packages not sorted"
          f" ({board['took_s']} s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
