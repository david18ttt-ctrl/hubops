#!/usr/bin/env python3
"""One lookup per THING on the floor, live from the WMS. Give it whatever number or name is in the question; it works
out what it is and prints everything the WMS knows about it, answer first. Any way the question is asked ("which
gaylord is this box in", "did it get sorted", "what truck did it come on") is answered from the same full record.

    python3 look.py PKG010852010082           a box: the truck and gaylord it came in, sorted or not, its bag now, left?
    python3 look.py GL2611140219              a gaylord or a bag made here: what it is, where it has been, how much is in it
    python3 look.py VCT01 | st03-004 | north   a destination: every box for it still here (full numbers), its gaylords on
                                              the floor, its trucks (at the dock / next / left), trucks in from it
    python3 look.py TT2611210034 | stl.h    a truck, or every truck in from one place this working day

    python3 look.py box|bag|dest|truck <thing>   force the kind (e.g. `look.py truck stl` = trucks IN from STL.H)
    --list   with a bag/gaylord: every waybill in it (with sorted / not sorted for a gaylord)
    --days N with a destination: how far back to look for boxes not signed out (default 3)

Several things at once: python3 look.py PKG… PKG… GL…   Read-only: WMS reads only, nothing written.
The WMS calls behind each part are listed in hubsim/ENDPOINTS.md.
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import hubconfig
import chute_alerts as ca

HERE = Path(__file__).resolve().parent
TZ = hubconfig.TIME_ZONE
BOARD = HERE.parent / "data" / "unsorted-latest.json"
BOARD_MAX_MIN = 30  # the floor board older than this isn't used for "still on the floor"

SCANS_API = "/api/scans/history"  # Operation Data: every scan of a waybill or a bag
ROUTE_API = ca.MASK_LIST_API  # Delivery waybill management: route, destination station, bag it is in now
SORT_API = "/api/sorter/records"  # Sorting Records (machine)
BAGS_API = "/api/bags/search"  # Box No. Query: bags made at a center
BAG_WAYBILLS_API = "/api/bags/parcels"
TASKS_API = "/api/trucks/tasks"  # truck tasks in and out
TRUCK_SCANS_API = "/api/trucks/scans"  # Loading&Unloading Query
REGIONS_API = "/api/places"  # TMS place ids (the hub = 1)
CENTER_ID, HUB_TRANSIT = hubconfig.HUB_ID, hubconfig.TMS_HUB_ID
HUB = hubconfig.HUB_CODE

# operateType codes in Operation Data
ORDER, PICKUP_ARRIVAL, CHECK_IN, UNLOAD, BAGGING, REBAG, CHECKOUT = 100, 612, 202, 211, 217, 219, 200
LOADING, DEPARTED, ARRIVED = 410, 411, 412
SMALL_BOX = 30  # packages: under this a bag is a near-empty / leftover bag, not a box (same as the dock board)
STALE_OPEN_H = 24  # a bag 'open' this long: the WMS never got the close (button not pressed, or it left)
LIKELY_LEFT_H = 48  # a bag not signed out for this long has usually left without a sign-out scan (missed sign-out)

BOX_RE = re.compile(r"^(PKG\d|1Z|9\d{9,})", re.I)
BAG_RE = re.compile(r"^(GL\d|SB\d|BG\d|[A-Z]{2}\d{10,}-\d+$)", re.I)
TRUCK_RE = re.compile(r"^TT\d{8,}$", re.I)


# ---------- small helpers ----------

def now_local():
    return datetime.now(TZ).replace(tzinfo=None)


def from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).replace(tzinfo=None) if ms else None


def parse_time(text):
    """The WMS writes times three ways: '2026-11-21 04:25:40', '11/21/2026 04:25:40' (month first) and
    '04:25:40 21/11/2026' (time first = DAY/month/year)."""
    if not text:
        return None
    text = str(text)[:19]
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%H:%M:%S %d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def hm(t, now=None):
    """'4:25 AM', with the date when it isn't today."""
    if not t:
        return "?"
    now = now or now_local()
    clock = t.strftime("%-I:%M %p")
    return clock if t.date() == now.date() else f"{t:%-m/%-d} {clock}"


def ago(t, now=None):
    if not t:
        return ""
    mins = int(((now or now_local()) - t).total_seconds() // 60)
    if mins < 0:
        return f"in {-mins} min" if mins > -90 else f"in {-mins // 60} h"
    return f"{mins} min ago" if mins < 90 else f"{mins // 60} h {mins % 60:02d} min ago" if mins < 48 * 60 else f"{mins // 1440} days ago"


def chute_of(bag_no):
    tail = str(bag_no or "").rsplit("-", 1)
    return tail[1].lstrip("0") if len(tail) == 2 and tail[1].isdigit() else ""


class Wms:
    """The shared login; counts calls so every answer can say how many it took."""

    def __init__(self):
        self.box = [ca.wms_token()]
        self.calls = 0
        self._regions = None

    def __call__(self, path, body):
        self.calls += 1
        return ca.wms(self.box, path, body)

    def regions(self):
        if self._regions is None:
            got = self(REGIONS_API, {})
            rows = got if isinstance(got, list) else []
            self._regions = {r["regionName"].upper(): r["id"] for r in rows if r.get("regionName")}
        return self._regions


def scans(wms, number, kind="10"):
    """Every scan of a waybill (kind 10) or a bag (kind 20), oldest first. A number search returns the whole history."""
    rows, page = [], 1
    while True:
        got = wms(SCANS_API, {"pageNum": page, "pageSize": 500, "operateNoType": kind, "operateNos": [number],
                              "operateTimeStart": "2026-01-01 00:00:00", "operateTimeEnd": "2027-12-31 23:59:59"}) or {}
        rows += got.get("list") or []
        if len(rows) >= int(got.get("total") or 0) or not got.get("list"):
            break
        page += 1
    for r in rows:
        r["t"] = from_ms(r.get("operateTimestamp")) or parse_time(r.get("operateTime"))
    return sorted(rows, key=lambda r: r["t"] or datetime.min)


def last(rows, *types, at=None, after=None):
    for r in reversed(rows):
        if r.get("operateType") in types and (at is None or r.get("operatorDeptName") == at) \
                and (after is None or (r["t"] and r["t"] >= after)):
            return r
    return None


def bag_rows(wms, numbers, here_only=True):
    body = {"packageNoList": list(numbers), "pageNum": 1, "pageSize": 50}
    if here_only:
        body["startCenterId"] = CENTER_ID
    return (wms(BAGS_API, body) or {}).get("list") or []


def board():
    """The floor board (unsorted_board.py, every 10 min): {bag number: (truck, bag)} and its read time, or ({}, None, why)."""
    try:
        b = json.loads(BOARD.read_text())
        made = datetime.strptime(str(b["made"])[:16], "%Y-%m-%d %H:%M")
    except Exception as e:
        return {}, None, f"floor board unreadable ({type(e).__name__})", []
    age = (now_local() - made).total_seconds() / 60
    if age > BOARD_MAX_MIN:
        return {}, made, f"floor board is {int(age)} min old (last read {hm(made)}): not used", []
    index = {bag["bag"]: (t, bag) for t in b.get("trucks", []) for bag in t.get("bags", [])}
    return index, made, "", b.get("trucks", [])


def tasks(wms, inbound=True, place_id=None, start=None, end=None):
    """Truck tasks into (inbound) or out of the hub, planned/arrived between start and end."""
    now = now_local()
    start, end = start or now - timedelta(days=1), end or now + timedelta(days=1)
    data = {("destTransitIdList" if inbound else "originTransitIdList"): [HUB_TRANSIT], "timeType": 1,
            "startLocalTime": start.strftime("%Y-%m-%d %H:%M:%S"), "endLocalTime": end.strftime("%Y-%m-%d %H:%M:%S")}
    if place_id is not None:
        data["originTransitIdList" if inbound else "destTransitIdList"] = [place_id]
    out = {}
    for page in range(1, 6):
        got = (wms(TASKS_API, {"data": data, "pageNum": page, "pageSize": 200}) or {}).get("records") or []
        out.update({r["taskNo"]: r for r in got if r.get("taskNo")})
        if len(got) < 200:
            break
    rows = list(out.values())
    key = "placeOfOrigin" if inbound else "destination"
    if place_id is not None:  # the list filter isn't trusted: check the place on each row too
        names = {n for n, i in wms.regions().items() if i == place_id}
        rows = [r for r in rows if str(r.get(key) or "").upper() in names]
    return rows


def truck_line(t, now=None):
    """One truck task in a line: from → to, where it is now, counts. Inbound = coming to the hub; outbound = leaving it."""
    now = now or now_local()
    dep, arr = parse_time(t.get("actualDepartureTime")), parse_time(t.get("actualArrivalTime"))
    eta = parse_time(t.get("estimationArrivalTime")) or parse_time(t.get("plannedArrivalTime"))
    loaded, unloaded = int(t.get("loadWaybillCount") or 0), int(t.get("unLoadWaybillCount") or 0)
    counts = f"{t.get('loadBagCount') or 0} bags / {loaded:,} pcs loaded"
    if t.get("destination") == HUB:
        if arr:
            where = f"arrived {hm(arr, now)}"
            counts += f", {t.get('unLoadBagCount') or 0} bags / {unloaded:,} pcs scanned off here"
        elif dep:
            where = f"on the road (left {hm(dep, now)}, due {hm(eta, now)})"
        else:
            where = f"not left yet (plan: leave {hm(parse_time(t.get('plannedDepartureTime')), now)})"
    else:
        at_dock = parse_time(t.get("timeOfArrivalOrigin"))
        plan = parse_time(t.get("plannedDepartureTime"))
        if dep:
            where = f"left here {hm(dep, now)}" + (f", arrived {t.get('destination')} {hm(arr, now)}" if arr else f", due there {hm(eta, now)}")
        elif at_dock:
            where = f"AT THE DOCK since {hm(at_dock, now)} (leave by {hm(plan, now)})"
        else:
            where = f"not here yet (plan: leave {hm(plan, now)})"
    return (f"{t.get('placeOfOrigin')} → {t.get('destination')} {t.get('taskNo')} ({t.get('modelName') or '?'}, "
            f"{t.get('vehicleStatusStr') or '?'}): {where}; {counts}")


# ---------- a box ----------

def look_box(wms, number):
    number = number.strip().upper()
    now = now_local()
    with ThreadPoolExecutor(3) as pool:
        f_scans = pool.submit(scans, wms, number, "10")
        f_route = pool.submit(lambda: ((wms(ROUTE_API, {"waybillNoList": [number], "pageNum": 1, "pageSize": 10}) or {})
                                       .get("list") or [{}])[0])
        ms = int(time.time() * 1000)
        f_sort = pool.submit(lambda: (wms(SORT_API, {"noType": 1, "sorterId": ca.SORTER_ID, "noList": [number],
                                                     "sortTimeStart": ms - 30 * 86400000, "sortTimeEnd": ms,
                                                     "pageNum": 1, "pageSize": 50}) or {}).get("list") or [])
        history, route, sorts = f_scans.result(), f_route.result(), f_sort.result()
    if not history and not route.get("waybillNo"):
        print(f"BOX {number}: the WMS has no record of it (no scans, not in the waybill list). Check the number.")
        return
    arrived = last(history, ARRIVED, CHECK_IN, UNLOAD, PICKUP_ARRIVAL, at=HUB)
    first_here = next((r for r in history if r.get("operatorDeptName") == HUB), None)
    unload = last(history, UNLOAD, at=HUB)
    check_in = last(history, CHECK_IN, at=HUB)
    here_since = first_here["t"] if first_here else None
    bagged = last(history, BAGGING, REBAG, at=HUB, after=here_since) if here_since else None
    out_checkout = last(history, CHECKOUT, at=HUB, after=here_since) if here_since else None
    out_departed = last(history, DEPARTED, at=HUB, after=here_since) if here_since else None
    left_origin = last(history, CHECKOUT, after=None)
    origin_checkout = next((r for r in reversed(history) if r.get("operateType") == CHECKOUT
                            and r.get("operatorDeptName") != HUB and (not here_since or (r["t"] and r["t"] <= here_since))), None)
    origin = origin_checkout.get("operatorDeptName") if origin_checkout else None
    gaylord = (unload or {}).get("bagNo") or (origin_checkout or {}).get("bagNo")
    if gaylord and here_since is None and origin_checkout:
        gaylord = origin_checkout.get("bagNo")

    # the truck it came on: the floor board knows its gaylords; otherwise the inbound task from that origin
    index, board_made, board_note, _ = board()
    truck, board_bag = index.get(gaylord, (None, None)) if gaylord else (None, None)
    task = None
    if not truck and origin and origin_checkout:
        pid = wms.regions().get(origin.upper())
        if pid is not None:
            left_t = (last(history, DEPARTED) or origin_checkout)["t"]
            cands = [t for t in tasks(wms, True, pid, left_t - timedelta(days=1), left_t + timedelta(days=2))
                     if parse_time(t.get("actualDepartureTime"))]
            if cands:
                task = min(cands, key=lambda t: abs((parse_time(t["actualDepartureTime"]) - left_t).total_seconds()))
                if abs((parse_time(task["actualDepartureTime"]) - left_t).total_seconds()) > 4 * 3600:
                    task = None

    # the bag it is in now (made here)
    # (the waybill list's packageNo is whatever bag it is in, the incoming gaylord too, so only a bag MADE here counts)
    out_bag_no, out_bag = (bagged or {}).get("bagNo"), None
    candidates = [n for n in (out_bag_no, route.get("packageNo")) if n and n != gaylord]
    if candidates and here_since:
        made_here = [b for b in bag_rows(wms, candidates) if b.get("startCenterName") == HUB]
        out_bag = next((b for n in candidates for b in made_here if b.get("packageNo") == n), None)
        out_bag_no = (out_bag or {}).get("packageNo") or out_bag_no
    good_sorts = [s for s in sorts if s.get("matchedChutes")]
    failed = [s for s in sorts if not s.get("matchedChutes") or s.get("refluxReason")]

    # ---- the answer line ----
    dest = route.get("destinationCenter") or (out_bag or {}).get("destinCenterName") or "?"
    rte = route.get("courierWorkArea") or ""
    head = f"BOX {number} → {dest}{' route ' + rte if rte and rte != dest else ''}"
    truck_txt = (f"{truck['from']} truck {truck['task']} (in {hm(parse_time(truck['arrived']), now)})" if truck else
                 f"{task.get('placeOfOrigin')} truck {task.get('taskNo')} (in {hm(parse_time(task.get('actualArrivalTime')), now)})"
                 if task and task.get("actualArrivalTime") else
                 f"{task.get('placeOfOrigin')} truck {task.get('taskNo')}" if task else (f"a truck from {origin}" if origin else None))
    if not truck_txt and first_here:  # no hub sent it: pickup / drop-off freight, arrived in a batch
        truck_txt = (f"pickup / drop-off ({first_here.get('operateTypeDesc')} {hm(first_here['t'], now)}"
                     + (f", batch {first_here['bagNo']}" if first_here.get("bagNo") else "") + ")")
        gaylord = None
    truck_txt = truck_txt or "?"
    if out_checkout or (out_departed and out_bag_no):
        when = (out_checkout or out_departed)["t"]
        state = (f"LEFT {HUB}: bag {out_bag_no or (out_checkout or {}).get('bagNo')} signed out {hm(when, now)} "
                 f"({(out_checkout or {}).get('checkoutNo') or 'no sign-out no.'})")
    elif out_bag_no:
        b = out_bag or {}
        closed = from_ms(b.get("optFinishStatusTime"))
        state = (f"SORTED, in bag {out_bag_no} (chute {chute_of(out_bag_no) or '?'}, {b.get('destinCenterName') or dest}, "
                 f"{b.get('waybillCount') or '?'} pcs), " + (f"box closed {hm(closed, now)}" if closed else "the WMS shows the box still open (never closed)")
                 + ", not signed out yet")
    elif here_since:
        how = (f"in gaylord {gaylord} (code {gaylord[-4:]})" if gaylord else "loose (no gaylord)")
        state = f"NOT SORTED: {how}, came on {truck_txt}"
        if unload:
            state += f", scanned off the truck {hm(unload['t'], now)}"
        elif check_in:
            state += f", checked in {hm(check_in['t'], now)}"
        else:
            state += ", truck arrived but this gaylord was never scanned in (on the trailer, or unloaded without a scan)"
        if failed and not good_sorts:
            state += f"; sorter rejected it {hm(from_ms(failed[0].get('sortTime')), now)} ({failed[0].get('refluxReasonName') or failed[0].get('refluxReason') or failed[0].get('sortStatusDesc')})"
        elif good_sorts:
            g = good_sorts[0]
            state += (f"; the sorter DID read it {hm(from_ms(g.get('sortTime')), now)} → chute {'/'.join(g['matchedChutes'])} "
                      f"but no bag scan here (check that chute)")
    elif origin_checkout:
        state = f"NOT HERE YET: signed out of {origin} {hm(origin_checkout['t'], now)} in gaylord {gaylord}, on {truck_txt}"
        if task:
            state += f"; {truck_line(task, now)}"
    else:
        lastscan = history[-1] if history else {}
        state = f"NOT AT {HUB}: last scan {lastscan.get('operateTypeDesc')} at {lastscan.get('operatorDeptName')} {hm(lastscan.get('t'), now)}"
    print(f"{head}\n  {state}")

    # ---- the facts ----
    if here_since:
        print(f"  Came in: {truck_txt}" + (f", gaylord {gaylord}" if gaylord else "") +
              (f" — gaylord now {board_bag['left']} of {board_bag['of']} left to sort (board {hm(board_made, now)})" if board_bag else ""))
    if good_sorts:
        s = good_sorts[0]
        print(f"  Sorter: read {hm(from_ms(s.get('sortTime')), now)}, chutes {'/'.join(s.get('matchedChutes') or [])} "
              f"({s.get('chuteRemark') or ''}, {s.get('sortStatusDesc') or ''})")
    for s in failed[:3]:
        print(f"  Sorter reject: {hm(from_ms(s.get('sortTime')), now)} {s.get('refluxReasonName') or s.get('refluxReason') or s.get('sortStatusDesc')}"
              f" → dropped in chute {s.get('dropChuteNo') or '?'}")
    if out_bag:
        b = out_bag
        print(f"  Bag {b['packageNo']}: {b.get('bagTypeString')}, made {hm(from_ms(b.get('optCreateTime')), now)} by {b.get('createByName')}, "
              f"{b.get('waybillCount')} pcs, {b.get('loadStatusString') or ''}"
              + (f", signed out {hm(from_ms(b.get('optCheckOutTime')), now)}" if b.get("optCheckOutTime") else ""))
    print("  Scans: " + " · ".join(f"{hm(r['t'], now)} {r.get('operateTypeDesc')} @{r.get('operatorDeptName')}"
                                   + (f" [{r['bagNo']}]" if r.get("bagNo") else "") for r in history[-9:]))
    if board_note:
        print(f"  ({board_note})")


# ---------- a gaylord or a bag ----------

def look_bag(wms, number, list_all=False):
    number = number.strip().upper()
    now = now_local()
    with ThreadPoolExecutor(2) as pool:
        f_hist = pool.submit(scans, wms, number, "20")
        f_here = pool.submit(bag_rows, wms, [number])
        history = f_hist.result()
        # the bag list ignores startCenterId when a number is given: only bags MADE here count as ours
        made_here = [b for b in f_here.result() if b.get("packageNo") == number and b.get("startCenterName") == HUB]
    index, board_made, board_note, _ = board()
    truck, board_bag = index.get(number, (None, None))
    if made_here:
        b = max(made_here, key=lambda x: x.get("optCreateTime") or 0)  # bag numbers get reused: the newest
        made, closed, out = (from_ms(b.get(k)) for k in ("optCreateTime", "optFinishStatusTime", "optCheckOutTime"))
        pcs = int(b.get("waybillCount") or 0)
        if out:
            state = f"LEFT: signed out {hm(out, now)}"
        elif closed:
            state = f"CLOSED {hm(closed, now)}, NOT signed out" + (" — this old it probably left without a sign-out (missed sign-out)"
                                                                     if now - closed > timedelta(hours=LIKELY_LEFT_H) else "")
        else:
            state = f"OPEN in the WMS (never closed; made {ago(made, now)})"
        print(f"BAG {number} → {b.get('destinCenterName')}: {pcs} pcs, chute {chute_of(number) or '?'}, {state}")
        print(f"  Made {hm(made, now)} by {b.get('createByName')} ({b.get('bagTypeString')}, {b.get('packageTypeString')}), "
              f"{b.get('loadStatusString') or ''}{', exchange code ' + b['exchangeBureauCode'] if b.get('exchangeBureauCode') else ''}")
        if len(made_here) > 1:
            print(f"  (the WMS reused this bag number {len(made_here)} times; this is the newest)")
        dep = last(history, DEPARTED, LOADING, at=HUB, after=made)
        if dep:
            print(f"  Truck: {dep.get('operateTypeDesc')} {hm(dep['t'], now)} by {dep.get('operatorName')}")
        if list_all:
            rows, page = [], 1
            while True:
                got = wms(BAG_WAYBILLS_API, {"packageNo": number, "deptId": CENTER_ID, "pageNum": page, "pageSize": 1000}) or {}
                lst = got.get("list") if isinstance(got, dict) else got
                rows += lst or []
                if not lst or len(lst) < 1000:
                    break
                page += 1
            mine = [r for r in rows if not made or not r.get("optLoadTime") or from_ms(r["optLoadTime"]) >= made - timedelta(minutes=10)]
            print(f"  {len(mine)} waybills:")
            for r in mine:
                print(f"    {r.get('waybillNo')}  in {hm(from_ms(r.get('optLoadTime')), now)}  {r.get('statusString') or ''}")
        return
    if not history and not board_bag:
        print(f"BAG {number}: the WMS has no record of it. Check the number.")
        return
    # an inbound gaylord (made at another hub)
    unload = last(history, UNLOAD, CHECK_IN, at=HUB)
    origin_out = next((r for r in reversed(history) if r.get("operateType") == CHECKOUT and r.get("operatorDeptName") != HUB), None)
    arrived = last(history, ARRIVED, at=HUB)
    origin = (origin_out or {}).get("operatorDeptName") or (truck or {}).get("from") or "?"
    if board_bag:
        left, of = board_bag["left"], board_bag["of"]
        state = (f"{left:,} of {of:,} pcs NOT sorted yet" if left else f"all {of:,} pcs sorted") + f" (floor board {hm(board_made, now)})"
        if board_bag.get("noscan"):
            state += ", never scanned in here"
    elif unload:
        state = "not on the floor board: fully sorted, or its truck is more than 2 days old"
    elif arrived:
        state = "truck is here but this gaylord was never scanned off it (on the trailer, or unloaded without a scan)"
    else:
        state = f"not at {HUB} yet"
    print(f"GAYLORD {number} (code {number[-4:]}) from {origin}: {state}")
    if truck:
        print(f"  Truck: {truck['from']} {truck['task']}, in {hm(parse_time(truck['arrived']), now)}; that truck has "
              f"{truck['left']:,} of {truck['total']:,} pcs left, {truck['open']} gaylords open{', ' + truck['state'] if truck.get('state') else ''}")
    if board_bag and board_bag.get("to"):
        print(f"  Freight inside for: {', '.join(board_bag['to'])}")
    if unload:
        print(f"  Scanned in here {hm(unload['t'], now)} by {unload.get('operatorName')}")
    print("  Scans: " + " · ".join(f"{hm(r['t'], now)} {r.get('operateTypeDesc')} @{r.get('operatorDeptName')}" for r in history[-8:]))
    if board_note:
        print(f"  ({board_note})")
    if list_all:
        list_gaylord(wms, number, truck, origin_out, now)


def list_gaylord(wms, number, truck, origin_out, now):
    """Every waybill in an inbound gaylord, sorted or not (Loading&Unloading Query for its truck + Sorting Records)."""
    import hub_numbers as hn
    task = (truck or {}).get("task")
    origin = (truck or {}).get("from") or (origin_out or {}).get("operatorDeptName")
    pid = wms.regions().get(str(origin or "").upper())
    if not task or pid is None:
        print("  (can't list it: its truck isn't known)")
        return
    when = parse_time(truck["arrived"]) if truck else now
    data = {"destinationTransitId": str(HUB_TRANSIT), "originTransitId": str(pid), "scanType": "", "sealNo": "",
            "serialNumber": "", "taskNo": task, "startTime": f"{when - timedelta(days=1):%Y-%m-%d} 00:00:00",
            "endTime": f"{when + timedelta(days=1):%Y-%m-%d} 23:59:59"}
    waybills, page, seen = set(), 1, 0
    while True:
        res = wms(TRUCK_SCANS_API, {"data": data, "pageNum": page, "pageSize": 2000}) or {}
        rows = res.get("records") or []
        waybills |= {r["waybillNo"] for r in rows if r.get("bagNo") == number and r.get("waybillNo")}
        seen += len(rows)
        if not rows or seen >= int(res.get("total") or 0):
            break
        page += 1
    done = hn.sorted_waybills(wms.box[0], sorted(waybills), when - timedelta(hours=2)) if waybills else set()
    print(f"  {len(waybills)} waybills, {len(waybills) - len(done)} not sorted:")
    for w in sorted(waybills, key=lambda w: w in done):
        print(f"    {w}  {'sorted' if w in done else 'NOT sorted'}")


# ---------- a destination ----------

def match_dest(name, query):
    name, query = str(name or "").upper(), query.upper().replace(" ", "")
    return name == query or name.startswith(query) or name.replace(".H", "") == query.replace(".H", "")


def best_place(names, query):
    """The TMS place a short name means: exact, then the hub (PHX → PHX.H), then station 01 (AUS → AUS01), then any start."""
    q = query.upper().replace(" ", "")
    for want in (q, q + ".H", q + "01", q.replace(".H", "")):
        if want in names:
            return want
    return next((n for n in sorted(names) if n.startswith(q)), None)


def look_dest(wms, query, days=3):
    q = query.strip().upper().replace(" ", "")
    route = None
    m = re.match(r"^([A-Z]{2,5}\d{0,2})-(\d{1,3})$", q)  # a route: VCT01-004 / VCT01-4 (ACT01 alone is a station)
    if m:
        q, route = m.group(1), m.group(2)
    now = now_local()
    fmt = "%Y-%m-%d %H:%M:%S"
    body = {"packageNoList": [], "createBeginTime": (now - timedelta(days=days)).strftime(fmt), "createEndTime": now.strftime(fmt),
            "startCenterId": CENTER_ID, "pageSize": 1000}

    def all_bags():  # first page says how many; the rest are read 4 at a time (3 days = ~5 pages, 3 s each)
        first = wms(BAGS_API, {**body, "pageNum": 1}) or {}
        pages = -(-int(first.get("total") or 0) // 1000)
        with ThreadPoolExecutor(4) as pool:
            rest = list(pool.map(lambda n: (wms(BAGS_API, {**body, "pageNum": n}) or {}).get("list") or [], range(2, pages + 1)))
        seen, bags = set(), []
        for b in (first.get("list") or []) + [b for page in rest for b in page]:
            key = (b.get("packageNo"), b.get("optCreateTime"))
            if key not in seen:  # a bag made while reading can shift onto the next page
                seen.add(key)
                bags.append(b)
        return bags

    regions = wms.regions()
    place = best_place(regions, q)
    pid = regions.get(place) if place else None
    with ThreadPoolExecutor(3) as pool:
        f_bags = pool.submit(all_bags)
        f_out = pool.submit(tasks, wms, False, pid, now - timedelta(hours=18), now + timedelta(hours=30)) if pid is not None else None
        f_in = pool.submit(tasks, wms, True, pid, now - timedelta(hours=30), now + timedelta(hours=30)) if pid is not None else None
        bags = f_bags.result()
        out_trucks = f_out.result() if f_out else []
        in_trucks = f_in.result() if f_in else []
    forecasts = [b for b in bags if b.get("bagTypeString") == "Customer Forecast"]  # customers' pre-alert batches, not boxes
    bags = [b for b in bags if b.get("bagTypeString") != "Customer Forecast"]
    mine = [b for b in bags if (str(b.get("destinCenterName") or "").upper() == place) if place] or \
        [b for b in bags if match_dest(b.get("destinCenterName"), q)]
    names = sorted({b.get("destinCenterName") for b in mine})
    if route:
        mine = [b for b in mine if str(b.get("destinAreaCode") or "").rsplit("-", 1)[-1].lstrip("0") == route.lstrip("0")]  # VCT01-001 must not match VCT01-010
    if not mine and not out_trucks and not in_trucks:
        known = sorted({str(b.get("destinCenterName")) for b in bags})
        print(f"DESTINATION {query}: no bags made here for it in the last {days} days and no trucks. "
              f"Destinations with bags: {', '.join(known)}")
        return
    here = [b for b in mine if not b.get("optCheckOutTime")]
    left = [b for b in mine if b.get("optCheckOutTime")]
    boxes = [b for b in here if int(b.get("waybillCount") or 0) >= 1]
    empty = len(here) - len(boxes)
    open_all = [b for b in boxes if not b.get("optFinishStatusTime")]
    stale_open = [b for b in open_all if now - from_ms(b.get("optCreateTime")) > timedelta(hours=STALE_OPEN_H)]
    open_ = [b for b in open_all if b not in stale_open]
    closed = [b for b in boxes if b.get("optFinishStatusTime")]
    old = [b for b in closed if now - from_ms(b["optFinishStatusTime"]) > timedelta(hours=LIKELY_LEFT_H)]
    ready = [b for b in closed if b not in old]
    pcs = lambda bs: sum(int(b.get("waybillCount") or 0) for b in bs)
    big = lambda bs: sum(1 for b in bs if int(b.get("waybillCount") or 0) >= SMALL_BOX)
    label = "/".join(names) or q
    print(f"DESTINATION {label}{' route ' + route if route else ''} (bags made here in the last {days} days, read {hm(now, now)}): "
          f"{len(ready)} closed boxes waiting to load ({pcs(ready):,} pcs, {big(ready)} of them {SMALL_BOX}+ pcs), "
          f"{len(open_)} still open on chutes ({pcs(open_):,} pcs)"
          + (f", {len(old)} closed 2+ days ago never signed out (probably left without a sign-out)" if old else "")
          + (f", {len(stale_open)} 'open' for over {STALE_OPEN_H} h (the WMS never closed them: check the chute)" if stale_open else ""))
    for title, group in (("Closed, waiting to load", ready), ("Open on the chute", open_),
                         (f"Open in the WMS for over {STALE_OPEN_H} h (never closed; on the floor or gone?)", stale_open),
                         (f"Closed {LIKELY_LEFT_H}+ h ago, no sign-out", old)):
        if not group:
            continue
        print(f"  {title}:")
        for b in sorted(group, key=lambda b: b.get("optCreateTime") or 0):
            done = from_ms(b.get("optFinishStatusTime"))
            print(f"    {b['packageNo']}  chute {chute_of(b['packageNo']) or '?':>3}  {int(b.get('waybillCount') or 0):>4} pcs  "
                  f"made {hm(from_ms(b.get('optCreateTime')), now)}" + (f", closed {hm(done, now)} ({ago(done, now)})" if done else "")
                  + f"  {b.get('bagTypeString') or ''}{' ' + b['destinCenterName'] if len(names) > 1 else ''}")
    if empty:
        print(f"  ({empty} empty bags (0 pcs) not counted)")
    if left:
        newest = max(left, key=lambda b: b["optCheckOutTime"])
        recent = [b for b in left if now - from_ms(b["optCheckOutTime"]) < timedelta(hours=12)]
        print(f"  Signed out last 12 h: {len(recent)} bags, {pcs(recent):,} pcs; last one {hm(from_ms(newest['optCheckOutTime']), now)}")
    # gaylords on the floor with freight for it
    _, board_made, board_note, trucks_on_board = board()
    with_it = [(t, g) for t in trucks_on_board for g in t.get("bags", [])
               if g.get("left") and any(match_dest(d, q) for d in g.get("to") or [])]
    on_floor = [(t, g) for t, g in with_it if g.get("full")]
    strays = len(with_it) - len(on_floor)
    if on_floor or strays:
        print(f"  Gaylords not run yet that hold some {label} freight (floor board {hm(board_made, now)}): {len(on_floor)}"
              + (f" (+ {strays} already-run gaylords with a few leftovers)" if strays else ""))
        for t, g in sorted(on_floor, key=lambda x: x[0]["arrived"]):
            print(f"    {g['bag']} (code {g['code']}) {g['left']}/{g['of']} left, {t['from']} truck in {hm(parse_time(t['arrived']), now)}"
                  f"{' NOT SCANNED IN' if g.get('noscan') else ''}; also for {', '.join(x for x in g['to'] if not match_dest(x, q)) or '-'}")
    elif board_note:
        print(f"  (gaylords on the floor: {board_note})")
    if out_trucks:
        print(f"  Trucks to {place}:")
        for t in sorted(out_trucks, key=lambda t: t.get("plannedDepartureTime") or ""):
            arrived = parse_time(t.get("actualArrivalTime"))
            dep = parse_time(t.get("actualDepartureTime"))
            if dep and now - dep > timedelta(hours=12):
                continue
            print(f"    {truck_line(t, now)}")
    if in_trucks:
        print(f"  Trucks in from {place}:")
        for t in sorted(in_trucks, key=lambda t: t.get("actualArrivalTime") or t.get("plannedArrivalTime") or ""):
            print(f"    {truck_line(t, now)}")


# ---------- a truck ----------

def look_truck(wms, query):
    q = query.strip().upper()
    now = now_local()
    _, board_made, board_note, on_board = board()
    by_task = {t["task"]: t for t in on_board}
    if TRUCK_RE.match(q):
        found = None
        for inbound in (True, False):
            rows = tasks(wms, inbound, None, now - timedelta(days=3), now + timedelta(days=2))
            found = next((t for t in rows if t.get("taskNo") == q), None)
            if found:
                break
        if not found:
            print(f"TRUCK {q}: not found in WMS truck tasks from 3 days back to 2 days ahead.")
            return
        show = [found]
    else:
        regions = wms.regions()
        place = best_place(regions, q)
        if not place:
            print(f"TRUCK {q}: no place called that in the WMS.")
            return
        show = tasks(wms, True, regions[place], now - timedelta(hours=30), now + timedelta(hours=30))
        print(f"TRUCKS IN FROM {place}, last 30 h and next 30 h: {len(show)}")
    for t in sorted(show, key=lambda t: t.get("actualArrivalTime") or t.get("plannedArrivalTime") or ""):
        print(f"  {truck_line(t, now)}")
        b = by_task.get(t.get("taskNo"))
        if b:
            print(f"    Floor board {hm(board_made, now)}: {b['left']:,} of {b['total']:,} pcs left to sort, {b['open']} of "
                  f"{b['gaylords']} gaylords open{', ' + b['state'] if b.get('state') else ''}"
                  + (f", {b['noscan_full']} gaylords never scanned in" if b.get("noscan_full") else "")
                  + (f"; unloaded by {', '.join(b['by'])}" if b.get("by") else ""))
            opened = [g for g in b.get("bags", []) if g.get("left")]
            if opened:
                print("    Gaylords not done: " + ", ".join(f"{g['code']} {g['left']}/{g['of']}" + ("*" if g.get("noscan") else "")
                                                         for g in sorted(opened, key=lambda g: -g["left"])))
        elif t.get("actualArrivalTime") and t.get("destination") == HUB:
            print(f"    Not on the floor board ({board_note or 'board ' + hm(board_made, now)}): sorted, or more than 2 days old")


# ---------- what is it ----------

def kind_of(thing):
    t = thing.strip().upper()
    if TRUCK_RE.match(t):
        return "truck"
    if BAG_RE.match(t):
        return "bag"
    if BOX_RE.match(t) or (len(t) >= 12 and t.isalnum() and any(c.isdigit() for c in t)):
        return "box"
    return "dest"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("things", nargs="+")
    p.add_argument("--list", action="store_true", help="with a bag or gaylord: every waybill in it")
    p.add_argument("--days", type=int, default=3, help="with a destination: days of bags to look at (default 3)")
    args = p.parse_args(argv)
    things, forced = args.things, None
    if things[0].lower() in ("box", "bag", "gaylord", "dest", "destination", "truck") and len(things) > 1:
        forced = {"gaylord": "bag", "destination": "dest"}.get(things[0].lower(), things[0].lower())
        things = things[1:]
    wms = Wms()
    start = time.time()
    for i, thing in enumerate(things):
        if i:
            print()
        kind = forced or kind_of(thing)
        try:
            if kind == "box":
                look_box(wms, thing)
            elif kind == "bag":
                look_bag(wms, thing, args.list)
            elif kind == "truck":
                look_truck(wms, thing)
            else:
                look_dest(wms, thing, args.days)
        except Exception as e:  # one bad lookup shouldn't hide the others
            print(f"{thing}: lookup failed ({type(e).__name__}: {e})")
    print(f"Source: WMS live, read {now_local():%-m/%-d %-I:%M %p}, {wms.calls} calls, {time.time() - start:.1f} s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
