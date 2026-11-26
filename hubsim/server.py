#!/usr/bin/env python3
"""A fake warehouse management system (WMS) for the hub tools: synthetic data for tonight, stdlib only.

    python3 hubsim/server.py                      serve on 127.0.0.1:8765
    python3 hubsim/server.py --port 9000 --clock live

Every endpoint is a POST of a JSON body and answers {"code": 0, "msg": "ok", "data": ...}; see ENDPOINTS.md.
Any bearer token is accepted, except the literal token "expired", which gets a 401 (to try the login-expired path).

--clock decides where "now" falls in the synthetic night:
  live     the night runs on the real clock (at 10 AM the sorter is off and the night is over)
  evening  the whole night is shifted by whole hours so that "now" is about 10 PM, mid-shift
  auto     live between 5:30 PM and 4:30 AM, evening otherwise (default)

While it runs it also writes the hourly sort report feed to data/hourly-report.json (hubsim/report.py).
"""
import argparse
import bisect
import json
import sys
import threading
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model as m  # noqa: E402
import report  # noqa: E402

FULL_NIGHTS = 2  # tonight and the night before are built parcel by parcel; older nights only as hourly figures


def now_real():
    return datetime.now(m.TZ).replace(tzinfo=None)


def clean_anchor(hours):
    """Keeps the scripted "now" inside sorting hours and out of the breaks, so the jams have traffic."""
    if not 17.5 <= hours <= 28.5:
        return 22.5
    for s, e in m.BREAKS:
        if s - 0.45 <= hours <= e + 0.3:
            return e + 0.35
    return hours


class Sim:
    """The simulated WMS: picks the clock, builds the nights it needs and answers queries at `now()`."""

    def __init__(self, clock="auto", fixed_now=None):
        self.fixed_now = fixed_now
        real = self.now()
        live = clock == "live" or (clock == "auto" and (real.hour > 17 or (real.hour == 17 and real.minute >= 30)
                                                        or real.hour < 4 or (real.hour == 4 and real.minute < 30)))
        self.offset = timedelta() if live else timedelta(hours=real.hour - 22)
        nominal = real - self.offset
        self.current = m.work_day(nominal)
        hours = (nominal - datetime(self.current.year, self.current.month, self.current.day)).total_seconds() / 3600
        self.anchor = clean_anchor(hours)
        self.clock = "live" if live else "evening"
        self._nights, self._lock = {}, threading.Lock()
        days = self.full_days()
        self.night(days[0])  # tonight now; the nights before in the background (a request waits if it needs one)
        threading.Thread(target=lambda: [self.night(d) for d in days[1:]], daemon=True).start()

    def now(self):
        return self.fixed_now or now_real()

    def full_days(self):
        """Tonight (on the simulated clock, so it moves on at 6 AM while the server runs) and the nights before."""
        today = self.nominal_day(self.now())
        return [today - timedelta(days=k) for k in range(FULL_NIGHTS)]

    def night(self, day):
        with self._lock:
            if day not in self._nights:
                anchor = self.anchor if day == self.current else 22.5
                self._nights[day] = m.Night(day, self.offset, anchor)
            return self._nights[day]

    def nights(self):
        return [self.night(d) for d in self.full_days()]

    def nominal_day(self, real_time):
        return m.work_day(real_time - self.offset)

    # ----- lookups shared by several endpoints -----

    def parcel(self, pid):
        for n in self.nights():
            p = n.parcel_by_id.get(pid)
            if p:
                return n, p
        return None, None

    def bags_named(self, number):
        return [b for n in self.nights() for b in n.bag_by_id.get(number, [])]

    def gaylord(self, number):
        for n in self.nights():
            g = n.gaylord_by_id.get(number)
            if g:
                return g
        return None

    def inbound_trucks(self):
        return [t for n in self.nights() for t in n.trucks]

    def outbound_trucks(self):
        return [t for n in self.nights() for t in n.out_trucks]


# ---------- row builders (field names as the tools read them) ----------

def ms(t):
    return m.ms(t)


def seen(t, now):
    return t if t and t <= now else None


def bag_row(b, now):
    closed, out = seen(b["close"], now), seen(b["checkout"], now)
    kind = {"sorter": (2, "Sorter"), "manual": (1, "Manual"), "forecast": (3, "Customer Forecast")}[b["kind"]]
    n = m.bag_count(b, now)
    return {"packageNo": b["id"], "bagType": kind[0], "bagTypeString": kind[1],
            "packageTypeString": "Gaylord" if b["kind"] != "manual" else "Bag",
            "startCenterId": m.HUB_ID, "startCenterName": m.HUB, "destinCenterName": b["dest"], "destinAreaCode": b["route"],
            "waybillCount": n, "packageNum": n, "createTime": m.fmt(b["created"]), "optCreateTime": ms(b["created"]),
            "optFinishStatusTime": ms(closed), "optCheckOutTime": ms(out), "createByName": b["by"],
            "loadStatusString": "Loaded" if out else "Not loaded", "warehouseStatus": 2 if out else 1,
            "exchangeBureauCode": "", "chuteNo": b["chute"]}


def gaylord_row(g, now):
    """An inbound gaylord as the bag list shows it: made at its origin hub, not here."""
    t = g["truck"]
    return {"packageNo": g["id"], "bagType": 2, "bagTypeString": "Sorter", "packageTypeString": "Gaylord",
            "startCenterId": m.PLACE_IDS[t["origin"]], "startCenterName": t["origin"], "destinCenterName": m.HUB,
            "destinAreaCode": m.HUB, "waybillCount": len(g["parcels"]), "packageNum": len(g["parcels"]),
            "createTime": m.fmt(g["loaded"] - timedelta(hours=2)), "optCreateTime": ms(g["loaded"] - timedelta(hours=2)),
            "optFinishStatusTime": ms(g["loaded"] - timedelta(minutes=20)), "optCheckOutTime": ms(g["loaded"]),
            "createByName": "Sorter 1", "loadStatusString": "Loaded", "warehouseStatus": 2, "exchangeBureauCode": ""}


def record_row(rec):
    t, pid, n, status, reason, matched, drop, seat = rec
    route = m.CHUTE_ROUTE.get(matched, "") if matched else ""
    return {"id": f"SR{pid[3:]}{n}", "orderCode": pid, "barcodes": [pid], "sorterId": m.SORTER_ID,
            "sorterName": m.SORTER_NAME, "platId": seat, "sortTime": ms(t), "deviceSortTime": m.fmt(t),
            "sortStatus": status, "sortStatusDesc": "Sorted" if status == 1 else "Failed",
            "matchedChutes": [matched] if matched else [], "dropChuteNo": drop, "chuteRemark": route,
            "refluxReason": reason, "refluxReasonName": m.REASONS.get(reason, "")}


def inbound_task_row(t, now):
    dep, arr = seen(t["depart"], now), seen(t["arrive"], now)
    scanned = [g for g in t["gaylords"] if g["scan"] and g["scan"] <= now]
    done = seen(t["unload_done"], now)
    status = ("Unloaded" if done else "Unloading" if scanned else "Arrived") if arr else "In transit" if dep else "Planned"
    return {"taskNo": t["task"], "taskCode": t["task"], "placeOfOrigin": t["origin"], "destination": m.HUB,
            "originTransitId": m.PLACE_IDS[t["origin"]], "destTransitId": m.TMS_HUB_ID,
            "plannedDepartureTime": m.fmt(t["planned_depart"]), "plannedArrivalTime": m.fmt(t["planned_arrive"]),
            "timeOfArrivalOrigin": m.fmt(seen(t["depart"] - timedelta(hours=1), now)),
            "actualDepartureTime": m.fmt(dep), "actualArrivalTime": m.fmt(arr),
            "estimationArrivalTime": m.fmt(t["eta"]) if dep else None,
            "loadBagCount": len(t["gaylords"]) if dep else 0,
            "loadWaybillCount": sum(len(g["parcels"]) for g in t["gaylords"]) if dep else 0,
            "unLoadBagCount": len(scanned), "unLoadWaybillCount": sum(len(g["parcels"]) for g in scanned),
            "modelName": "53FT", "vehicleStatusStr": status, "licensePlateNo": t["plate"],
            "transportTypeStr": "Linehaul", "estimationLoadingRate": None}


def outbound_visible(t, now):
    """Dispatch makes an outbound task minutes before the truck docks."""
    return t["task"] and t["dock_at"] - timedelta(minutes=15) <= now


def outbound_task_row(t, now):
    at_dock, left, arr = seen(t["dock_at"], now), seen(t["left"], now), seen(t["arrive"], now)
    loaded = [b for b in t["bags"] if b["checkout"] <= now]
    unloaded = loaded if seen(t["unloaded"], now) else []
    room = 11 if t["type"] == "26FT" else 28
    boxes = sum(1 for b in loaded if len(b["parcels"]) >= 30)
    status = "Arrived" if arr else "Departed" if left else "Loading" if at_dock else "Planned"
    return {"taskNo": t["task"], "taskCode": t["task"], "placeOfOrigin": m.HUB, "destination": t["dest"],
            "originTransitId": m.TMS_HUB_ID, "destTransitId": m.PLACE_IDS[t["dest"]],
            "plannedDepartureTime": m.fmt(t["plan"]), "plannedArrivalTime": m.fmt(t["plan"] + (t["arrive"] - t["left"])),
            "timeOfArrivalOrigin": m.fmt(at_dock), "actualDepartureTime": m.fmt(left), "actualArrivalTime": m.fmt(arr),
            "estimationArrivalTime": m.fmt(t["arrive"]) if left else None,
            "loadBagCount": len(loaded), "loadWaybillCount": sum(len(b["parcels"]) for b in loaded),
            "unLoadBagCount": len(unloaded), "unLoadWaybillCount": sum(len(b["parcels"]) for b in unloaded),
            "modelName": t["type"], "vehicleStatusStr": status, "licensePlateNo": t["plate"],
            "transportTypeStr": "Linehaul" if t["dest"].endswith(".H") else "Station delivery",
            "estimationLoadingRate": min(100, round(100 * boxes / room))}


def dashboard_truck_row(t, now):
    arr, done = seen(t["arrive"], now), seen(t["unload_done"], now)
    return {"taskCode": t["task"], "placeOfOrigin": t["origin"], "originTransitId": m.PLACE_IDS[t["origin"]],
            "destinationTransitId": m.TMS_HUB_ID, "actualArrivalTime": m.fmt(arr), "estimationArrivalTime": m.fmt(t["eta"]),
            "unloadCompleteTime": m.fmt(done), "bagCount": len(t["gaylords"]),
            "waybillCount": sum(len(g["parcels"]) for g in t["gaylords"]), "transportType": "Linehaul",
            "deadline": f"{m.work_day(t['arrive'])} 22:00:00", "vehicleNo": t["plate"]}


def scan_event(no, kind, t, dept, who="", bag="", checkout="", task=""):
    return {"operateNo": no, "operateType": kind, "operateTypeDesc": m.SCAN_NAMES[kind], "operateTime": m.fmt(t),
            "operateTimestamp": ms(t), "operatorDeptName": dept, "operatorName": who, "bagNo": bag or None,
            "checkoutNo": checkout or None, "taskNo": task or None}


def bag_out_events(no, b, now, as_bag):
    """Sign-out, loading, departure and arrival of a bag made here (for the bag or for a parcel in it)."""
    out = []
    t = b.get("truck")
    if not b["checkout"] or b["checkout"] > now or not t:
        return out
    co = f"CO{b['id'][4:16]}"
    out.append(scan_event(no, m.CHECKOUT, b["checkout"], m.HUB, "Loader 2", b["id"], co, t["task"]))
    out.append(scan_event(no, m.LOADING, b["checkout"] + timedelta(minutes=1), m.HUB, "Loader 2", b["id"], "", t["task"]))
    if t["left"] <= now:
        out.append(scan_event(no, m.DEPARTED, t["left"], m.HUB, "Dispatch", b["id"] if as_bag else "", "", t["task"]))
    if t["arrive"] <= now:
        out.append(scan_event(no, m.ARRIVED, t["arrive"], t["dest"], "", "", "", t["task"]))
    if t["unloaded"] <= now:
        out.append(scan_event(no, m.CHECK_IN, t["unloaded"], t["dest"], "Station crew", b["id"]))
    return out


def parcel_events(p, now):
    out = []
    if p.gaylord:
        g, t = p.gaylord, p.truck
        out.append(scan_event(p.id, m.ORDER, g["loaded"] - timedelta(hours=30), p.origin))
        out.append(scan_event(p.id, m.CHECKOUT, g["loaded"], p.origin, "Operator 4", g["id"], f"CO{g['id'][2:14]}", t["task"]))
        out.append(scan_event(p.id, m.LOADING, g["loaded"] + timedelta(minutes=5), p.origin, "Operator 4", g["id"], "", t["task"]))
        out.append(scan_event(p.id, m.DEPARTED, t["depart"], p.origin, "", "", "", t["task"]))
        out.append(scan_event(p.id, m.ARRIVED, t["arrive"], m.HUB, "", "", "", t["task"]))
        if g["scan"]:
            out.append(scan_event(p.id, m.UNLOAD, g["scan"], m.HUB, g["unloader"], g["id"], "", t["task"]))
    else:
        b = p.batch
        out.append(scan_event(p.id, m.ORDER, b["arrive"] - timedelta(hours=20), ""))
        out.append(scan_event(p.id, m.PICKUP_ARRIVAL, p.arrived, m.HUB, f"Operator {1 + int(b['dock'])}", b["id"]))
    if p.bag and p.sorted_at:
        out.append(scan_event(p.id, m.BAGGING, p.sorted_at, m.HUB, p.bag["by"], p.bag["id"]))
        out += bag_out_events(p.id, p.bag, now, False)
    return [e for e in out if e["operateTimestamp"] <= ms(now)]


def gaylord_events(g, now):
    t = g["truck"]
    out = [scan_event(g["id"], m.CHECKOUT, g["loaded"], t["origin"], "Operator 4", g["id"], f"CO{g['id'][2:14]}", t["task"]),
           scan_event(g["id"], m.LOADING, g["loaded"] + timedelta(minutes=5), t["origin"], "Operator 4", g["id"], "", t["task"]),
           scan_event(g["id"], m.DEPARTED, t["depart"], t["origin"], "", g["id"], "", t["task"]),
           scan_event(g["id"], m.ARRIVED, t["arrive"], m.HUB, "", g["id"], "", t["task"])]
    if g["scan"]:
        out.append(scan_event(g["id"], m.UNLOAD, g["scan"], m.HUB, g["unloader"], g["id"], "", t["task"]))
    return [e for e in out if e["operateTimestamp"] <= ms(now)]


def inbound_scan_rows(t, now):
    rows = []
    for g in t["gaylords"]:
        loader = f"Operator {3 + len(g['id']) % 5}"
        for p in g["parcels"]:
            if g["loaded"] <= now:
                rows.append((g["loaded"], {"taskNo": t["task"], "waybillNo": p.id, "bagNo": g["id"], "operationType": "1",
                                           "operationTypeName": "Loading check-out", "operatorDept": t["origin"],
                                           "targetCenterName": p.dest, "createByName": loader, "createTime": m.fmt_us(g["loaded"])}))
            if g["scan"] and g["scan"] <= now:
                rows.append((g["scan"], {"taskNo": t["task"], "waybillNo": p.id, "bagNo": g["id"], "operationType": "0",
                                         "operationTypeName": "Unloading check-in", "operatorDept": m.HUB,
                                         "targetCenterName": p.dest, "createByName": g["unloader"], "createTime": m.fmt_us(g["scan"])}))
    return rows


def outbound_scan_rows(t, now):
    rows = []
    for b in t["bags"]:
        for p in b["parcels"]:
            if b["checkout"] <= now:
                rows.append((b["checkout"], {"taskNo": t["task"], "waybillNo": p.id, "bagNo": b["id"], "operationType": "1",
                                             "operationTypeName": "Loading check-out", "operatorDept": m.HUB,
                                             "targetCenterName": t["dest"], "createByName": "Loader 2",
                                             "createTime": m.fmt_us(b["checkout"])}))
            if t["unloaded"] <= now:
                rows.append((t["unloaded"], {"taskNo": t["task"], "waybillNo": p.id, "bagNo": b["id"], "operationType": "0",
                                             "operationTypeName": "Unloading check-in", "operatorDept": t["dest"],
                                             "targetCenterName": t["dest"], "createByName": "Station crew",
                                             "createTime": m.fmt_us(t["unloaded"])}))
    return rows


# ---------- request helpers ----------

class Refused(Exception):
    """The WMS refuses the request itself: answered as code -1 with the reason."""


def page(rows, body, default=20):
    n = max(1, int(body.get("pageNum") or 1))
    size = max(1, int(body.get("pageSize") or default))
    return rows[(n - 1) * size:n * size], len(rows), n, size


def paged(rows, body, key="list", default=20):
    part, total, n, size = page(rows, body, default)
    return {key: part, "total": total, "pageNum": n, "pageSize": size}


def when(value):
    """A time from a body: milliseconds or a date-time string."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return m.from_ms(value)
    return m.parse(value)


def window_of(body, now):
    start = when(body.get("startTime")) or datetime.combine(m.work_day(now), datetime.min.time()) + timedelta(hours=6)
    end = when(body.get("endTime")) or start + timedelta(days=1)
    return start, end


# ---------- endpoints ----------

def ep_places(sim, body, now):
    kinds = {m.HUB: "hub"} | {h: "hub" for h in m.HUBS} | {s: "station" for s in m.STATIONS}
    return [{"id": i, "regionName": name, "regionCode": name, "type": kinds[name]} for name, i in m.PLACE_IDS.items()]


def ep_bags_search(sim, body, now):
    numbers = [str(x).strip().upper() for x in body.get("packageNoList") or [] if x]
    if numbers:  # a number search ignores startCenterId and the dates; every bag that ever had the number
        rows = []
        for no in numbers:
            rows += [bag_row(b, now) for b in sim.bags_named(no) if b["created"] <= now]
            g = sim.gaylord(no)
            if g and g["loaded"] <= now:
                rows.append(gaylord_row(g, now))
        return paged(rows, body, default=50)
    start = when(body.get("createBeginTime")) or now - timedelta(days=1)
    end = min(when(body.get("createEndTime")) or now, now)
    center = body.get("startCenterId")
    if center not in (None, "", m.HUB_ID, str(m.HUB_ID)):
        return paged([], body)
    rows = []
    for n in sim.nights():
        lo = bisect.bisect_left(n.bag_created, start)
        hi = bisect.bisect_right(n.bag_created, end)
        rows += n.bags_by_created[lo:hi]
    rows.sort(key=lambda b: b["created"], reverse=True)
    part, total, num, size = page(rows, body, default=50)
    return {"list": [bag_row(b, now) for b in part], "total": total, "pageNum": num, "pageSize": size}


def ep_bag_parcels(sim, body, now):
    no = str(body.get("packageNo") or "").strip().upper()
    bags = [b for b in sim.bags_named(no) if b["created"] <= now]
    if not bags:
        return paged([], body, default=1000)
    b = max(bags, key=lambda x: x["created"])
    out = seen(b["checkout"], now)
    rows = [{"waybillNo": p.id, "packageNo": b["id"], "optLoadTime": ms(p.sorted_at), "weight": p.weight,
             "statusString": "Signed out" if out else "Bagged"}
            for p in sorted(b["parcels"], key=lambda p: p.sorted_at) if p.sorted_at <= now]
    return paged(rows, body, default=1000)


def ep_sort_records(sim, body, now):
    if not body.get("sorterId"):
        raise Refused("sorterId is required: select a sorter")
    start = when(body.get("sortTimeStart")) or now - timedelta(hours=1)
    end = min(when(body.get("sortTimeEnd")) or now, now)
    if end - start > timedelta(days=31):
        raise Refused("time range over 31 days")
    status = body.get("sortStatus")
    numbers = [str(x).strip().upper() for x in body.get("noList") or [] if x]
    rows = []
    if numbers:
        for no in numbers:
            n, p = sim.parcel(no)
            if p:
                rows += [(t, p.id, i, st, r, mc, d, seat) for i, (t, st, r, mc, d, seat) in enumerate(p.attempts)]
        rows = [r for r in rows if start <= r[0] <= end]
    else:
        for n in sim.nights():
            lo = bisect.bisect_left(n.record_times, start)
            hi = bisect.bisect_right(n.record_times, end)
            rows += n.records[lo:hi]
    if status not in (None, ""):
        rows = [r for r in rows if r[3] == int(status)]
    rows.sort(key=lambda r: r[0], reverse=True)
    part, total, num, size = page(rows, body, default=100)
    return {"list": [record_row(r) for r in part], "total": total, "pageNum": num, "pageSize": size}


def ep_chute_log(sim, body, now):
    start = when(body.get("opTimeStart")) or now - timedelta(hours=1)
    end = min(when(body.get("opTimeEnd")) or now, now)
    chute = str(body.get("chuteNo") or "").lstrip("0")
    rows = []
    for n in sim.nights():
        lo = bisect.bisect_left(n.log_times, start)
        hi = bisect.bisect_right(n.log_times, end)
        rows += n.chute_log[lo:hi]
    if chute:
        rows = [r for r in rows if r[1].lstrip("0") == chute]
    rows.sort(key=lambda r: r[0], reverse=True)
    names = {1: "Print bag label", 2: "Chute full signal"}
    out = [{"id": f"CL{ms(t)}{c}{k}", "sorterId": m.SORTER_ID, "chuteNo": c, "bagLabelCode": bag, "opType": k,
            "opTypeName": names[k], "opResult": ok, "opResultName": "Success" if ok else "Failed", "opTime": ms(t),
            "opTimeStr": m.fmt(t)} for t, c, bag, k, ok in rows]
    return paged(out, body, default=500)


def ep_feed(sim, body, now):
    start, end = window_of(body, now)
    hours = {}
    built = {n.day: n for n in sim.nights()}
    day = sim.nominal_day(start) - timedelta(days=1)
    while day <= sim.nominal_day(end):
        night = built.get(day)
        part = night.feed_rows(start, end, now) if night else m.light_feed(day, sim.offset, start, end, now)
        for h, seats in part.items():
            for seat, (fed, ref) in seats.items():
                v = hours.setdefault(h, {}).setdefault(seat, [0, 0])
                v[0] += fed
                v[1] += ref
        day += timedelta(days=1)
    rows = []
    for h in sorted(hours):
        label = h.strftime("%Y-%m-%d %H")
        seats = hours[h]
        fed, ref = sum(v[0] for v in seats.values()), sum(v[1] for v in seats.values())
        rows.append({"hourRange": label, "seatNo": None, "seatName": "All", "pickCnt": fed, "refuseCnt": ref})
        rows += [{"hourRange": label, "seatNo": str(s), "seatName": f"Feed {s}", "pickCnt": v[0], "refuseCnt": v[1]}
                 for s, v in sorted(seats.items())]
    return rows


def in_range(t, start, end):
    return t is not None and (start is None or t >= start) and (end is None or t <= end)


def ep_truck_tasks(sim, body, now):
    data = body.get("data") or {}
    dest_ids = [int(x) for x in data.get("destTransitIdList") or []]
    origin_ids = [int(x) for x in data.get("originTransitIdList") or []]
    start, end = when(data.get("startLocalTime")), when(data.get("endLocalTime"))
    names = {i: name for name, i in m.PLACE_IDS.items()}
    rows = []
    inbound = m.TMS_HUB_ID in dest_ids or (not dest_ids and not origin_ids)
    outbound = m.TMS_HUB_ID in origin_ids or (not dest_ids and not origin_ids)
    if inbound:  # into this hub
        want = {names.get(i) for i in origin_ids if i != m.TMS_HUB_ID}
        for t in sim.inbound_trucks():
            if (want and t["origin"] not in want) or t["planned_depart"] - timedelta(hours=12) > now:
                continue
            if in_range(t["planned_depart"], start, end) or in_range(t["planned_arrive"], start, end):
                rows.append(inbound_task_row(t, now))
    if outbound:  # out of this hub
        want = {names.get(i) for i in dest_ids if i != m.TMS_HUB_ID}
        for t in sim.outbound_trucks():
            if not outbound_visible(t, now) or (want and t["dest"] not in want):
                continue
            if in_range(t["plan"], start, end) or in_range(t["dock_at"], start, end):
                rows.append(outbound_task_row(t, now))
    rows.sort(key=lambda r: r["plannedDepartureTime"] or "", reverse=True)
    return paged(rows, body, key="records")


_SCAN_CACHE = {}


def ep_truck_scans(sim, body, now):
    data = body.get("data") or {}
    task = str(data.get("taskNo") or "").strip().upper()
    if not task:
        raise Refused("taskNo is required")
    start, end = when(data.get("startTime")), when(data.get("endTime"))
    if start and end and end - start > timedelta(days=5):
        raise Refused("time range over 5 days")
    key = (task, now.strftime("%Y-%m-%d %H:%M"))
    rows = _SCAN_CACHE.get(key)
    if rows is None:
        t = next((t for t in sim.inbound_trucks() if t["task"] == task), None)
        o = next((t for t in sim.outbound_trucks() if t["task"] == task), None)
        rows = inbound_scan_rows(t, now) if t else outbound_scan_rows(o, now) if o and outbound_visible(o, now) else []
        rows.sort(key=lambda r: r[0], reverse=True)  # newest first
        if len(_SCAN_CACHE) > 200:
            _SCAN_CACHE.clear()
        _SCAN_CACHE[key] = rows
    picked = [r for tm, r in rows if in_range(tm, start, end)]
    return paged(picked, body, key="records")


def ep_scan_history(sim, body, now):
    kind = str(body.get("operateNoType") or "10")
    rows = []
    for no in [str(x).strip().upper() for x in body.get("operateNos") or [] if x]:
        if kind == "10":
            _, p = sim.parcel(no)
            if p:
                rows += parcel_events(p, now)
        else:
            g = sim.gaylord(no)
            if g:
                rows += gaylord_events(g, now)
            for b in sim.bags_named(no):
                if b["created"] <= now:
                    rows += bag_out_events(no, b, now, True)
    rows.sort(key=lambda r: r["operateTimestamp"], reverse=True)
    return paged(rows, body, default=500)


def ep_parcel_route(sim, body, now):
    rows = []
    for no in [str(x).strip().upper() for x in body.get("waybillNoList") or [] if x]:
        _, p = sim.parcel(no)
        if not p:
            continue
        if p.bag and p.sorted_at and p.sorted_at <= now:
            current = p.bag["id"]
        else:
            current = (p.gaylord or {}).get("id") or (p.batch or {}).get("id")
        rows.append({"waybillNo": p.id, "destinationCenter": p.dest, "courierWorkArea": p.route, "packageNo": current,
                     "weight": p.weight, "orderStatusName": "Cancelled" if any(a[2] == "WAYBILL_NOT_FOUND" for a in p.attempts) else "In transit"})
    return paged(rows, body, default=100)


def ep_dashboard_trucks(sim, body, now):
    start, end = window_of(body, now)
    kind = int(body.get("type") or 7)
    rows = []
    for t in sim.inbound_trucks():
        if not in_range(t["arrive"] if t["arrive"] <= now else t["eta"], start, end):
            continue
        departed, arrived = t["depart"] <= now, t["arrive"] <= now
        done = t["unload_done"] is not None and t["unload_done"] <= now
        state = 7 if done else 6 if arrived else 4 if departed else None
        if state == kind:
            rows.append(dashboard_truck_row(t, now))
    rows.sort(key=lambda r: r["actualArrivalTime"] or r["estimationArrivalTime"])
    return paged(rows, body, key="records", default=500)


def ep_sort_count(sim, body, now):
    start, end = window_of(body, now)
    stop = min(end, now)
    sorted_n = bags_n = 0
    waiting = {"pickup": 0, "dropoff": 0, "transfer": 0}
    td = hs = 0
    pending_bags = pending_pcs = 0
    for n in sim.nights():
        sorted_n += bisect.bisect_right(n.sorted_times, stop) - bisect.bisect_left(n.sorted_times, start)
        for b in n.bags:
            if b["kind"] == "forecast":
                continue
            if b["close"] and start <= b["close"] <= stop:
                bags_n += 1
            if b["close"] and b["close"] <= now and not (b["checkout"] and b["checkout"] <= now):
                pending_bags += 1
                pending_pcs += len(b["parcels"])
        for p in n.piece_scanned:
            came = p.arrived if p.kind == "pickup" else next((a[0] for a in p.attempts if a[1] == 2), None)
            if not came or came > now or (p.sorted_at and p.sorted_at <= now):
                continue
            if came >= start:
                td += 1
            else:
                hs += 1
            if p.kind == "pickup":  # about one pickup parcel in ten was dropped off at the counter instead
                waiting["dropoff" if p.id[-1] == "7" else "pickup"] += 1
            else:
                waiting["transfer"] += 1
    return {"alreadySortedCount": sorted_n, "alreadySortedBagCount": bags_n, "allShouldSortCount": td + hs,
            "tdShouldSortCount": td, "hsShouldSortCount": hs, "allShouldPickCollectCount": waiting["pickup"],
            "allShouldPickTransferCount": waiting["transfer"], "allShouldPickCusPickupCount": waiting["dropoff"],
            "allShouldPickClearanceCount": 0, "allPendingSignoutBagCount": pending_bags,
            "allPendingSignoutCount": pending_pcs}


def expected(sim, body, now):
    """Trucks and packages the WMS expects in a window: departed trucks heading here, plus a light estimate of
    the trucks already scheduled for nights not built yet."""
    start, end = window_of(body, now)
    trucks = [t for t in sim.inbound_trucks() if in_range(t["eta"], start, end) and t["arrive"] > now]
    pcs = sum(len(g["parcels"]) for t in trucks for g in t["gaylords"])
    day = sim.nominal_day(start + timedelta(hours=1))
    if day not in {n.day for n in sim.nights()}:
        extra = int(m.volume(day) * 0.85 * 0.22)
        return trucks, len(trucks) + 2, pcs + extra, day
    return trucks, len(trucks), pcs, day


def ep_arrivals(sim, body, now):
    _, n, pcs, _ = expected(sim, body, now)
    return {"vehicleEstimatedArrival": n, "estimatedArrivalWaybillCount": pcs}


def ep_arrival_stats(sim, body, now):
    trucks, _, pcs, _ = expected(sim, body, now)
    on_road = sum(len(g["parcels"]) for t in trucks if t["depart"] <= now for g in t["gaylords"])
    return {"waybillInTransit": on_road or pcs // 3, "waybillArrived": 0}


def ep_arrival_hours(sim, body, now):
    trucks, n, pcs, day = expected(sim, body, now)
    hours = {}
    for t in trucks:
        h = t["eta"].replace(minute=0, second=0, microsecond=0)
        v = hours.setdefault(h, [0, 0])
        v[0] += 1
        v[1] += sum(len(g["parcels"]) for g in t["gaylords"])
    if n > len(trucks):  # the light estimate: the two early trucks of a night not built yet
        extra = pcs - sum(v[1] for v in hours.values())
        base = datetime(day.year, day.month, day.day) + sim.offset
        for h in (13, 15):
            v = hours.setdefault(base + timedelta(hours=h), [0, 0])
            v[0] += 1
            v[1] += extra // 2
    return [{"hourRange": f"{h:%Y-%m-%d %H:00}-{h + timedelta(hours=1):%H:00}", "vehicleCount": v[0],
             "trunkWaybillNoCount": v[1], "branchWaybillNoCount": 0} for h, v in sorted(hours.items())]


def ep_daily_totals(sim, body, now):
    start = when(body.get("startDate")) or now - timedelta(days=35)
    end = when(body.get("endDate")) or now
    out, d = [], start.date()
    while d < min(end.date() + timedelta(days=1), m.work_day(now)):
        out.append({"day": d.isoformat(), "total": m.volume(d)})
        d += timedelta(days=1)
    return out


def ep_departures(sim, body, now):
    """Dispatch's departure log, one row per outbound truck, as a shared table: {"items": [{record_id, fields}]}."""
    items = []
    for i, t in enumerate(sim.outbound_trucks()):
        plan = t["plan"]
        if plan - timedelta(hours=20) > now:
            continue
        day = m.work_day(plan)
        midnight = ms(datetime(day.year, day.month, day.day))
        docked = t["dock_at"] is not None and t["dock_at"] <= now
        fields = {"Departure date": midnight, "Shift time": midnight, "Destination": t["dest"],
                  "Truck type": t["type"], "Pickup time": ms(plan),
                  "Task no.": t["task"] if t["task"] and outbound_visible(t, now) else plan.strftime("%H:%M"),
                  "Driver": t["driver"] if docked else None,
                  "Dock": t["dock"] if t["dock_at"] is not None and t["dock_at"] - timedelta(minutes=20) <= now else None,
                  "Sent": bool(t["left"] and t["left"] + timedelta(minutes=30) <= now),
                  "Cancelled": "Yes" if t["cancelled"] else ""}
        items.append({"record_id": f"rec{(t['task'] or f'X{day:%y%m%d}{i:04d}')[2:]}",
                      "fields": {k: v for k, v in fields.items() if v not in (None, "")}})
    items.sort(key=lambda it: it["fields"].get("Pickup time", 0), reverse=True)
    return {"items": items, "total": len(items), "has_more": False}


def ep_plan(sim, body, now):
    return m.plan_view()


ENDPOINTS = {
    "/api/places": ep_places,
    "/api/bags/search": ep_bags_search,
    "/api/bags/parcels": ep_bag_parcels,
    "/api/sorter/records": ep_sort_records,
    "/api/sorter/chute-log": ep_chute_log,
    "/api/sorter/feed": ep_feed,
    "/api/sorter/plan": ep_plan,
    "/api/trucks/tasks": ep_truck_tasks,
    "/api/trucks/scans": ep_truck_scans,
    "/api/scans/history": ep_scan_history,
    "/api/parcels/route": ep_parcel_route,
    "/api/dashboard/trucks": ep_dashboard_trucks,
    "/api/dashboard/sort-count": ep_sort_count,
    "/api/dashboard/arrivals": ep_arrivals,
    "/api/dashboard/arrival-stats": ep_arrival_stats,
    "/api/dashboard/arrival-hours": ep_arrival_hours,
    "/api/history/daily-totals": ep_daily_totals,
    "/api/dispatch/departures": ep_departures,
}
OPEN_PATHS = {"/api/dispatch/departures"}  # the shared table is read with the chat app's token, not the WMS one


class Handler(BaseHTTPRequestHandler):
    sim = None
    quiet = False

    def send_json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/") in ("", "/api/health"):
            sim = self.sim
            self.send_json(200, {"code": 0, "msg": "ok", "data": {
                "hub": m.HUB, "now": m.fmt(sim.now()), "clock": sim.clock, "offset_hours": sim.offset.total_seconds() / 3600,
                "night": sim.current.isoformat(), "endpoints": sorted(ENDPOINTS)}})
            return
        self.send_json(404, {"code": 404, "msg": "use POST with a JSON body"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        fn = ENDPOINTS.get(path)
        if fn is None:
            self.send_json(404, {"code": 404, "msg": f"unknown endpoint {path}"})
            return
        auth = self.headers.get("Authorization") or ""
        if path not in OPEN_PATHS and (not auth.startswith("Bearer ") or auth[7:].strip() in ("", "expired")):
            self.send_json(401, {"code": 401, "msg": "login expired"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
            data = fn(self.sim, body if isinstance(body, dict) else {}, self.sim.now())
        except Refused as e:
            self.send_json(200, {"code": -1, "msg": str(e), "data": None})
            return
        except (ValueError, TypeError) as e:
            self.send_json(200, {"code": -1, "msg": f"bad request: {e}", "data": None})
            return
        self.send_json(200, {"code": 0, "msg": "ok", "data": data})

    def log_message(self, fmt_, *args):
        if not self.quiet:
            sys.stderr.write(f"{self.log_date_time_string()} {fmt_ % args}\n")


def make_server(host="127.0.0.1", port=8765, clock="auto", quiet=False, fixed_now=None):
    sim = Sim(clock, fixed_now)
    handler = type("SimHandler", (Handler,), {"sim": sim, "quiet": quiet})
    return ThreadingHTTPServer((host, port), handler), sim


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--clock", choices=["auto", "live", "evening"], default="auto")
    parser.add_argument("--quiet", action="store_true", help="don't log each request")
    parser.add_argument("--no-report", action="store_true", help="don't write the hourly report feed")
    args = parser.parse_args()
    server, sim = make_server(args.host, args.port, args.clock, args.quiet)
    if not args.no_report:
        report.keep_writing(sim)  # data/hourly-report.json, refreshed after every :16
    print(f"WMS simulator on http://{args.host}:{args.port}  hub {m.HUB}, night of {sim.current:%a %m/%d}, "
          f"clock {sim.clock} (offset {sim.offset.total_seconds() / 3600:+.0f} h), now {sim.now():%H:%M}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
