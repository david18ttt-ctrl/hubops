"""A deterministic synthetic parcel hub for the WMS simulator.

One `Night` is a whole working day (6 AM to 6 AM) of hub HOU.H, generated from a seed made of its date, so the
same day always looks the same:

- inbound linehaul trucks from the other hubs, each with gaylords (GL...) of parcels (PKG...). Some are unloaded,
  one is being unloaded, one sits in the yard never scanned in, one was unloaded without scans, some are still on
  the road and arrive later tonight;
- pickup freight brought in by the hub's own pickup trucks and scanned in piece by piece;
- Sorter 1 with 30 feed stations and 300 chutes: every station route (AUS01-004 ...) and every other hub has its
  chutes; NOREAD 001/109/214, reject 003, interception 146 and no-match 147 are the exception chutes;
- the sorter runs 4 PM to 5:15 AM with breaks, oldest truck first; parcels get read, rejected (no read, chute full,
  over the loop limit, no rule, not found) and re-fed; a few chutes jam for a while; one customer batch has a route
  the sort plan doesn't know;
- a bag (BG01...-<chute>) per chute at a time, closed by the label print when full; a couple of chutes print a
  second label minutes after the first (a lost label); one bag holds two boxes under one label (850+ pieces);
- outbound trucks to the stations and hubs at the dock, loading the closed bags, and leaving.

Times are local hub time (naive datetimes). The generator works on a "nominal" clock where hour 22.5 is 10:30 PM of
the working day; `offset` moves the whole night onto the real clock (the simulator can shift it so that "now" is
always mid-shift). Everything is precomputed once per night; what an endpoint shows depends only on `now`.
"""
import bisect
import heapq
import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Chicago")
HUB = "HOU.H"
HUB_ID = 1  # operations / dashboard id
TMS_HUB_ID = 1  # truck (TMS) id
SORTER_ID = 1
SORTER_NAME = "Sorter 1"

HUBS = ["PHX.H", "STL.H", "MSP.H", "SEA.H", "BOS.H", "PIT.H", "ABQ.H", "SAN.H"]
HUB_SHARE = {"PHX.H": 40, "STL.H": 16, "MSP.H": 14, "SEA.H": 12, "BOS.H": 9, "PIT.H": 8, "ABQ.H": 6, "SAN.H": 5}
TRANSIT_HOURS = {"PHX.H": 4, "STL.H": 3, "MSP.H": 7, "SEA.H": 9, "BOS.H": 5, "PIT.H": 6, "ABQ.H": 8, "SAN.H": 3}
STATIONS = ["AUS01", "ACT01", "VCT01", "BRO01", "HRL01", "SJT01", "GGG01", "TXK01", "SHV01", "AEX01", "CXO01", "SGR01"]
ROUTES_PER_STATION = [14, 9, 21, 12, 8, 16, 10, 6, 11, 7, 13, 5]
STATION_SHARE = [10, 6, 16, 9, 5, 12, 7, 4, 8, 5, 10, 3]
PLACE_IDS = {HUB: TMS_HUB_ID, **{h: 11 + i for i, h in enumerate(HUBS)}, **{s: 101 + i for i, s in enumerate(STATIONS)}}

CHUTES = 300
SEATS = 30
NOREAD_CHUTES = ("001", "109", "214")
REJECT_CHUTE = "003"
INTERCEPT_CHUTE = "146"
NO_MATCH_CHUTE = "147"
EXCEPTION_CHUTES = {*NOREAD_CHUTES, REJECT_CHUTE, INTERCEPT_CHUTE, NO_MATCH_CHUTE}
HUB_CHUTES = {h: (f"{49 + i:03d}", f"{188 + i:03d}") for i, h in enumerate(HUBS)}  # side 2 and its mirror on side 1

REASONS = {
    "CHUTE_FULL": "Chute full", "OVER_LOOP_LIMIT": "Over loop limit", "NOT_DROPPED": "Not dropped",
    "CHUTE_NO_BAG": "No bag on chute", "NO_MATCHED_RULE": "No matched rule", "NOREAD": "No read",
    "WAYBILL_NOT_FOUND": "Waybill not found", "INTERCEPTED": "Intercepted",
}

# operateType codes in the scan history
ORDER, PICKUP_ARRIVAL, CHECK_IN, UNLOAD, BAGGING, REBAG, CHECKOUT = 100, 612, 202, 211, 217, 219, 200
LOADING, DEPARTED, ARRIVED = 410, 411, 412
SCAN_NAMES = {ORDER: "Order placed", PICKUP_ARRIVAL: "Pickup Arrival", CHECK_IN: "Transit Center Check-in",
              UNLOAD: "Unload the goods", BAGGING: "Bagging", REBAG: "Bag Consolidation", CHECKOUT: "Transit Center Checkout",
              LOADING: "Loading", DEPARTED: "Vehicle Departure", ARRIVED: "Vehicle Arrival"}

SORTER_OPEN, SORTER_CLOSE = 16.0, 29.25  # 4 PM to 5:15 AM (hours from the nominal day's midnight)
BREAKS = ((19.5, 19.84), (23.75, 24.5), (27.0, 27.25))
LOOP_S = 90  # seconds for a rejected parcel to come round to the chutes again
LOOPS = 3  # attempts before the sorter gives up and drops it in the reject chute
SECOND = timedelta(seconds=1)


def ms(t):
    return int(t.replace(tzinfo=TZ).timestamp() * 1000) if t else None


def fmt(t):
    return t.strftime("%Y-%m-%d %H:%M:%S") if t else None


def fmt_us(t):
    """Month-first, as the truck scan list writes it."""
    return t.strftime("%m/%d/%Y %H:%M:%S") if t else None


def from_ms(value):
    return datetime.fromtimestamp(value / 1000, TZ).replace(tzinfo=None)


def parse(text):
    text = str(text or "")[:19]
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(text, f)
        except ValueError:
            pass
    return None


def work_day(t):
    return (t - timedelta(hours=6)).date()


# ---------- the sort plan (fixed, not per day) ----------

def build_plan():
    """{route: [chutes]} and {chute: route} for the station routes and the hubs."""
    rng = random.Random("HOU.H-plan-v1")
    reserved = {int(c) for c in EXCEPTION_CHUTES} | {int(c) for pair in HUB_CHUTES.values() for c in pair}
    free = (c for c in range(4, CHUTES + 1) if c not in reserved)
    routes = {}
    for station, n in zip(STATIONS, ROUTES_PER_STATION):
        for r in range(1, n + 1):
            routes[f"{station}-{r:03d}"] = [f"{next(free):03d}"]
    for route in rng.sample(sorted(routes), 20):  # the busiest routes get a second chute
        routes[route].append(f"{next(free):03d}")
    for hub, pair in HUB_CHUTES.items():
        routes[hub] = list(pair)
    chutes = {c: r for r, cs in routes.items() for c in cs}
    return routes, chutes


ROUTES, CHUTE_ROUTE = build_plan()
STATION_ROUTES = {s: [r for r in ROUTES if r.startswith(s + "-")] for s in STATIONS}
UNPLANNED_ROUTE = f"GGG01-{ROUTES_PER_STATION[6] + 1:03d}"  # a new route the sort plan hasn't got yet


def plan_view():
    """The active sort plan, in the shape a saved chute map uses: {"chutes": {chute: route}, "dests": {...}}."""
    return {"name": "Plan 1", "sorter": SORTER_NAME,
            "chutes": {c.lstrip("0"): r for c, r in sorted(CHUTE_ROUTE.items())},
            "dests": {r: [int(c) for c in cs] for r, cs in ROUTES.items()},
            "exceptions": {"noread": [int(c) for c in NOREAD_CHUTES], "reject": [int(REJECT_CHUTE)],
                           "intercept": [int(INTERCEPT_CHUTE)], "no_match": [int(NO_MATCH_CHUTE)]}}


# ---------- light day facts (no parcels): volumes, seats ----------

WEEKDAY = (1.15, 1.05, 1.0, 0.95, 0.9, 0.8, 0.7)


def volume(day):
    """Packages that come in on a working day."""
    rng = random.Random(f"HOU.H-volume:{day}")
    growth = 1 + 0.004 * (day - date(2026, 11, 1)).days
    return int(215000 * WEEKDAY[day.weekday()] * growth * rng.uniform(0.96, 1.04))


def seat_profile(day):
    """(speed weight, induction reject rate) per feed station. Two slow seats and one seat with a lot of rejects."""
    rng = random.Random(f"HOU.H-seats:{day}")
    speed = {s: max(0.3, rng.gauss(1.0, 0.13)) for s in range(1, SEATS + 1)}
    for slow, factor in zip(rng.sample([s for s in range(1, SEATS + 1) if s != 16], 2), (0.45, 0.55)):
        speed[slow] *= factor  # two seats clearly slow tonight (short-staffed or a new feeder)
    reject = {s: max(0.004, rng.gauss(0.015, 0.004)) for s in range(1, SEATS + 1)}
    reject[16] = 0.07  # this station rejects far more than the others most nights
    return speed, reject


def hour_rates(day):
    """Sorter speed (parcels/hour) for each nominal hour of the night."""
    rng = random.Random(f"HOU.H-rates:{day}")
    return {h: rng.uniform(17000, 23000) for h in range(int(SORTER_OPEN), int(SORTER_CLOSE) + 1)}


def open_minutes(h):
    """Minutes the sorter runs in nominal hour h (breaks and opening hours taken out)."""
    a, b = max(h, SORTER_OPEN), min(h + 1, SORTER_CLOSE)
    if b <= a:
        return 0.0
    run = b - a
    for s, e in BREAKS:
        run -= max(0.0, min(b, e) - max(a, s))
    return max(0.0, run) * 60


class Parcel:
    __slots__ = ("id", "dest", "route", "chute", "kind", "origin", "truck", "gaylord", "batch", "weight", "manual",
                 "no_rule", "feeds", "attempts", "sorted_at", "bag", "arrived")

    def __init__(self, pid, dest, route, kind):
        self.id, self.dest, self.route, self.kind = pid, dest, route, kind
        self.chute = None
        self.origin = self.truck = self.gaylord = self.batch = None
        self.weight = 0.0
        self.manual = self.no_rule = False
        self.feeds = []  # [(time, seat)] each pass through a feed station
        self.attempts = []  # sort records: (time, status, reason, matched chute, drop chute, seat)
        self.sorted_at = None
        self.bag = None
        self.arrived = None  # piece scan at this hub (pickup) or the gaylord's unload scan


class Night:
    """One working day of the hub. `day` is the nominal date, `offset` moves it onto the real clock and
    `anchor` (nominal hours) is "now" for the scripted events: jams, the lost labels, the late trucks."""

    def __init__(self, day, offset=timedelta(), anchor=22.5):
        self.day, self.offset, self.anchor = day, offset, anchor
        self.base = datetime(day.year, day.month, day.day) + offset
        self.rng = random.Random(f"HOU.H:{day}")
        self.tag = day.strftime("%y%m%d")
        self._seq = 0
        self.speed, self.seat_reject = seat_profile(day)
        self.rates = hour_rates(day)
        self.parcels, self.trucks, self.gaylords, self.batches = [], [], [], []
        self.bags, self.out_trucks, self.chute_log = [], [], []
        self.spells = {}  # chute -> [(start, end)]
        self.build()

    # ----- helpers -----

    def T(self, hours):
        return self.base + timedelta(hours=hours)

    def hours(self, t):
        return (t - self.base).total_seconds() / 3600

    def seq(self):
        self._seq += 1
        return self._seq

    def parcel_id(self, n):
        # 15 digits: day tag, then a scrambled sequence so neighbours don't look consecutive
        return f"PKG{self.tag}{(n * 7919 + self.day.toordinal() * 31) % 10 ** 9:09d}"

    def pick_seat(self):
        seats = list(self.speed)
        return self.rng.choices(seats, weights=[self.speed[s] for s in seats])[0]

    # ----- the build -----

    def build(self):
        self.make_inbound()
        self.make_pickup()
        self.unload()
        self.run_sorter()
        self.script_problems()
        self.sort_parcels()
        self.make_bags()
        self.bag_problems()
        self.number_bags()
        self.load_outbound()
        self.index()

    def dest_mix(self, origin):
        """A destination for a parcel coming from `origin`: mostly stations, some other hubs."""
        rng = self.rng
        if origin != "PICKUP" and rng.random() < 0.3:
            hubs = [h for h in HUBS if h != origin]
            hub = rng.choices(hubs, weights=[HUB_SHARE[h] for h in hubs])[0]
            return hub, hub
        station = rng.choices(STATIONS, weights=STATION_SHARE)[0]
        routes = STATION_ROUTES[station]
        route = rng.choices(routes, weights=[1 + (i % 4 == 0) * 1.5 for i in range(len(routes))])[0]
        return station, route

    def new_parcel(self, origin, kind):
        dest, route = self.dest_mix(origin)
        p = Parcel(self.parcel_id(len(self.parcels) + 1), dest, route, kind)
        p.origin = origin
        p.weight = round(self.rng.lognormvariate(0.2, 0.7), 2)
        p.manual = self.rng.random() < 0.012  # oversized: hand-sorted, never on the belt
        self.parcels.append(p)
        return p

    def make_inbound(self):
        """~12 linehaul trucks; arrival hours relative to the anchor give each its role tonight."""
        rng, a = self.rng, self.anchor
        total = int(volume(self.day) * 0.85)
        plan = [  # (arrival hour, role)
            (13.4, ""), (15.0, ""), (16.1, ""), (17.3, "missed_scan"), (a - 4.2, ""), (a - 3.0, "no_scans"),
            (a - 2.1, ""), (a - 0.85, "not_unloaded"), (a - 0.6, "unloading"), (a + 1.1, ""), (a + 2.6, ""),
            (a + 4.4, ""),
        ]
        plan.sort()
        others = HUBS[2:]
        rng.shuffle(others)
        extra = rng.choices(HUBS, weights=[HUB_SHARE[h] for h in HUBS], k=len(plan))
        origins = ["PHX.H", "STL.H"] + others + extra  # every hub sends at least one truck; the two biggest come early
        origins = origins[:2] + rng.sample(origins[2:len(plan)], len(plan) - 2)
        shares = [rng.uniform(0.7, 1.3) for _ in plan]
        for i, ((hour, role), origin) in enumerate(zip(plan, origins)):
            arrive = self.T(hour + rng.uniform(-0.1, 0.1))
            transit = TRANSIT_HOURS[origin] + rng.uniform(-0.3, 0.5)
            depart = arrive - timedelta(hours=transit)
            truck = {"task": f"TT{self.tag}{2000 + i:04d}", "origin": origin, "role": role, "depart": depart,
                     "arrive": arrive, "eta": arrive + timedelta(minutes=rng.randint(-25, 25)),
                     "planned_depart": depart - timedelta(minutes=rng.randint(0, 40)),
                     "planned_arrive": arrive + timedelta(minutes=rng.randint(-30, 30)),
                     "plate": f"TR{rng.randint(10000, 99999)}", "gaylords": [], "unload_done": None}
            pcs = int(total * shares[i] / sum(shares))
            n = 0
            while n < pcs:
                size = max(120, min(650, int(rng.gauss(420, 90))))
                g = {"id": f"GL{11 + HUBS.index(origin)}{self.tag}{self.seq():04d}", "truck": truck, "parcels": [],
                     "loaded": depart - timedelta(minutes=rng.randint(30, 90)), "scan": None, "avail": None,
                     "unloader": f"Unloader {rng.randint(1, 9)}"}
                for _ in range(min(size, pcs - n)):
                    p = self.new_parcel(origin, "linehaul")
                    p.truck, p.gaylord = truck, g
                    g["parcels"].append(p)
                n += len(g["parcels"])
                truck["gaylords"].append(g)
                self.gaylords.append(g)
            self.trucks.append(truck)

    def make_pickup(self):
        """The hub's own pickup trucks: customer freight scanned in piece by piece at docks 1-6."""
        rng = self.rng
        total = volume(self.day) - sum(len(g["parcels"]) for g in self.gaylords)
        hours = sorted(rng.uniform(15.0, 21.5) for _ in range(6))
        for i, hour in enumerate(hours):
            arrive = self.T(hour)
            b = {"id": f"PU{self.tag}{i + 1:03d}", "arrive": arrive, "dock": str(1 + i % 6), "parcels": [],
                 "avail": arrive + timedelta(minutes=rng.randint(35, 60)), "truck": None}
            for _ in range(total // len(hours)):
                p = self.new_parcel("PICKUP", "pickup")
                p.batch = b
                p.arrived = arrive + timedelta(seconds=rng.randint(60, 40 * 60))
                b["parcels"].append(p)
            self.batches.append(b)

    def unload(self):
        """Unload scans: each gaylord scanned in (one scan covers all its parcels), then staged for the belt."""
        rng = self.rng
        for t in self.trucks:
            role = t["role"]
            if role == "not_unloaded":
                start = self.T(self.anchor + 0.85)  # the crew gets to it later tonight
            elif role == "unloading":
                start = self.T(self.anchor - 0.42)
            else:
                start = t["arrive"] + timedelta(minutes=rng.randint(15, 35))
            cur = start
            missed = rng.randrange(len(t["gaylords"])) if role == "missed_scan" else None
            for i, g in enumerate(t["gaylords"]):
                # the crew on tonight's slow truck is short-handed: a gaylord every 4-6 minutes
                cur += timedelta(seconds=rng.randint(70, 150) if role != "unloading" else rng.randint(240, 360))
                if role == "no_scans" or i == missed:  # unloaded and run without a scan
                    g["avail"] = cur + timedelta(minutes=rng.randint(10, 30))
                else:
                    g["scan"] = cur
                    g["avail"] = cur + timedelta(minutes=rng.randint(5, 25))
                for p in g["parcels"]:
                    p.arrived = g["scan"]
            t["unload_done"] = cur + timedelta(minutes=2) if role not in ("no_scans",) else None

    def skip_breaks(self, t):
        for s, e in BREAKS:
            if self.T(s) <= t < self.T(e):
                return self.T(e)
        return t

    def run_sorter(self):
        """Feeds every belt parcel, one gaylord / pickup batch at a time, oldest truck first."""
        rng = self.rng
        items = [(g["avail"], g["truck"]["arrive"], i, g) for i, g in enumerate(self.gaylords)]
        items += [(b["avail"], b["arrive"], 10 ** 6 + i, b) for i, b in enumerate(self.batches)]
        items.sort(key=lambda x: x[0])
        close = self.T(SORTER_CLOSE)
        cur, heap, i = self.T(SORTER_OPEN), [], 0
        while i < len(items) or heap:
            while i < len(items) and items[i][0] <= cur:
                heapq.heappush(heap, (items[i][1], items[i][2], items[i][3]))
                i += 1
            if not heap:
                cur = max(cur, items[i][0])
                continue
            _, _, item = heapq.heappop(heap)
            item["run"] = [None, None]
            for p in item["parcels"]:
                if p.manual:
                    continue
                cur = self.skip_breaks(cur)
                if cur >= close:
                    return
                p.feeds.append((cur, self.pick_seat()))
                item["run"][0] = item["run"][0] or cur
                item["run"][1] = cur
                rate = self.rates.get(int(self.hours(cur)), 20000)
                cur += timedelta(seconds=3600 / rate * rng.uniform(0.6, 1.4))

    def chute_traffic(self, start, end):
        """Chutes by how many parcels are fed for them in [start, end), busiest first."""
        count = {}
        for p in self.parcels:
            if p.feeds and start <= p.feeds[0][0] < end and p.route in ROUTES:
                c = ROUTES[p.route][0]
                count[c] = count.get(c, 0) + 1
        return sorted(count, key=lambda c: -count[c])

    def script_problems(self):
        """Tonight's scripted trouble, placed around the anchor: three jammed chutes (one already cleared) and a
        customer batch for a route the sort plan doesn't have. Each jam goes on the busiest chute of its window,
        so it rejects enough to be noticed."""
        a = self.anchor
        self.stuck_chutes = []
        for s, e in ((a - 16 / 60, a + 24 / 60), (a - 7 / 60, a + 14 / 60), (a - 100 / 60, a - 70 / 60)):
            chute = next(c for c in self.chute_traffic(self.T(s), self.T(e))
                         if c not in EXCEPTION_CHUTES and c not in self.stuck_chutes)
            self.stuck_chutes.append(chute)
            self.spells.setdefault(chute, []).append((self.T(s), self.T(e)))
        start, end = self.T(a - 24 / 60), self.T(a + 18 / 60)
        for p in self.parcels:
            if p.feeds and start <= p.feeds[0][0] < end and p.dest in STATIONS and self.rng.random() < 0.012:
                p.dest, p.route, p.no_rule = "GGG01", UNPLANNED_ROUTE, True
        busy = self.chute_traffic(self.T(SORTER_OPEN), self.T(SORTER_CLOSE))
        self.busy_chutes = [c for c in busy if c not in EXCEPTION_CHUTES and c not in self.stuck_chutes]

    def stuck(self, chute, t):
        return any(s <= t < e for s, e in self.spells.get(chute, ()))

    def spell_end(self, chute, t):
        return next((e for s, e in self.spells.get(chute, ()) if s <= t < e), t)

    def sort_parcels(self):
        """Sort records for every fed parcel: read and dropped, or rejected, looped and re-fed."""
        rng, close, loop = self.rng, self.T(SORTER_CLOSE), timedelta(seconds=LOOP_S)
        for p in self.parcels:
            if not p.feeds:
                continue
            if p.no_rule:
                t, seat = p.feeds[0]
                for k in range(LOOPS):
                    p.attempts.append((t + k * loop, 2, "NO_MATCHED_RULE", None, NO_MATCH_CHUTE if k == LOOPS - 1 else "", seat))
                continue
            chutes = ROUTES[p.route]
            if p.dest.endswith(".H"):  # hub freight favours the side-2 chute
                p.chute = chutes[0] if rng.random() < 0.8 else chutes[1]
            else:
                p.chute = rng.choice(chutes)
            while p.feeds:
                t, seat = p.feeds[-1]
                roll = rng.random()
                if roll < 0.018 and len(p.feeds) == 1:  # label not read
                    drop = NOREAD_CHUTES[0] if seat <= 15 else rng.choice(NOREAD_CHUTES[1:])
                    p.attempts.append((t, 2, "NOREAD", None, drop, seat))
                    again = t + timedelta(minutes=rng.randint(30, 120))
                    if rng.random() < 0.5 and self.skip_breaks(again) < close:
                        p.feeds.append((self.skip_breaks(again), self.pick_seat()))
                        continue
                    break
                if roll < 0.0215 and len(p.feeds) == 1:
                    p.attempts.append((t, 2, "WAYBILL_NOT_FOUND", None, NO_MATCH_CHUTE, seat))
                    break
                if roll < 0.023 and len(p.feeds) == 1:
                    p.attempts.append((t, 2, "INTERCEPTED", None, INTERCEPT_CHUTE, seat))
                    break
                done = False
                for k in range(LOOPS):
                    tk = t + k * loop
                    if self.stuck(p.chute, tk):
                        last = k == LOOPS - 1
                        p.attempts.append((tk, 2, "OVER_LOOP_LIMIT" if last else "CHUTE_FULL", p.chute,
                                           REJECT_CHUTE if last else "", seat))
                        continue
                    if k == 0 and rng.random() < 0.005:
                        p.attempts.append((tk, 2, rng.choice(["CHUTE_FULL", "NOT_DROPPED", "CHUTE_NO_BAG"]), p.chute, "", seat))
                        continue
                    p.attempts.append((tk, 1, "", p.chute, p.chute, seat))
                    p.sorted_at = tk
                    done = True
                    break
                if done:
                    break
                again = self.skip_breaks(self.spell_end(p.chute, t) + timedelta(minutes=rng.randint(10, 40)))
                if again >= close:
                    break
                p.feeds.append((again, self.pick_seat()))

    def make_bags(self):
        """One bag per chute at a time; the label print closes it when it is full. End of night: every bag with
        30+ pieces is closed, smaller ones stay open on the chute."""
        rng = self.rng
        by_chute = {}
        for p in self.parcels:
            if p.sorted_at:
                by_chute.setdefault(p.chute, []).append(p)
        end = self.T(SORTER_CLOSE) + timedelta(minutes=10)
        for chute, ps in by_chute.items():
            ps.sort(key=lambda p: p.sorted_at)
            hub = CHUTE_ROUTE[chute].endswith(".H")
            target = lambda: rng.randint(280, 460) if hub else rng.randint(40, 150)  # noqa: E731
            bag, size, last_close = None, 0, None
            for p in ps:
                if bag is None:  # the label print that closed the last bag starts the next one
                    bag = self.new_bag(chute, last_close or p.sorted_at - timedelta(minutes=rng.randint(1, 8)))
                    size = target()
                bag["parcels"].append(p)
                p.bag = bag
                if len(bag["parcels"]) >= size:
                    last_close = p.sorted_at + timedelta(seconds=rng.randint(40, 240))
                    self.close_bag(bag, last_close)
                    bag = None
            if bag is not None and len(bag["parcels"]) >= 30:
                self.close_bag(bag, end + timedelta(seconds=rng.randint(0, 900)))
        # hand-sorted parcels: PDA bags per station, made through the night
        manual = {}
        for p in self.parcels:
            if p.manual and p.dest in STATIONS:
                src = p.gaylord or p.batch
                ready = (src.get("avail") or src.get("arrive")) if src else None
                if ready and ready < end:
                    manual.setdefault(p.dest, []).append((ready + timedelta(minutes=rng.randint(10, 60)), p))
        for station, items in manual.items():
            items.sort(key=lambda x: x[0])
            for i in range(0, len(items), 25):
                chunk = items[i:i + 25]
                bag = self.new_bag("", chunk[0][0] - timedelta(minutes=2), kind="manual", dest=station)
                for t, p in chunk:
                    p.sorted_at, p.bag = t, bag
                    bag["parcels"].append(p)
                self.close_bag(bag, chunk[-1][0] + timedelta(minutes=3))
        # customers' pre-alert batches show up in the bag list too; they aren't boxes
        for i in range(3):
            bag = self.new_bag("", self.T(rng.uniform(9, 15)), kind="forecast", dest=rng.choice(STATIONS))
            bag["count"] = rng.randint(200, 900)
            self.close_bag(bag, bag["created"] + timedelta(minutes=1))

    def new_bag(self, chute, created, kind="sorter", dest=None):
        route = CHUTE_ROUTE.get(chute) if chute else None
        dest = dest or (route if route and route.endswith(".H") else route.split("-")[0] if route else "")
        bag = {"id": None, "chute": chute, "route": route or dest, "dest": dest, "kind": kind, "created": created,
               "close": None, "checkout": None, "truck": None, "parcels": [], "hold": False, "count": None,
               "by": SORTER_NAME if kind == "sorter" else f"Operator {self.rng.randint(1, 30)}"}
        self.bags.append(bag)
        return bag

    def close_bag(self, bag, t):
        bag["close"] = t

    def split_bag(self, bag, at, tiny):
        """A lost label: the bag is closed at `at`, its next `tiny` parcels go in a bag closed right after (the
        second print), and the rest start a third bag."""
        before = [p for p in bag["parcels"] if p.sorted_at < at]
        after = [p for p in bag["parcels"] if p.sorted_at >= at]
        if len(before) < 40 or len(after) < tiny + 1:
            return False
        bag["parcels"], old_close = before, bag["close"]
        self.close_bag(bag, at)
        small = self.new_bag(bag["chute"], at)
        small["parcels"] = after[:tiny]
        self.close_bag(small, small["parcels"][-1].sorted_at + timedelta(seconds=self.rng.randint(20, 90)))
        rest = after[tiny:]
        if rest:
            third = self.new_bag(bag["chute"], small["close"])
            third["parcels"] = rest
            third["close"] = old_close
            for p in rest:
                p.bag = third
        for p in small["parcels"]:
            p.bag = small
        return True

    def bag_problems(self):
        """Bag trouble: one bag holding two boxes under one label (850+ pieces), and lost labels on a few busy
        chutes around the anchor."""
        a = self.anchor
        self.big_bag = None
        hub_chutes = [c for pair in HUB_CHUTES.values() for c in pair]
        by_traffic = self.chute_traffic(self.T(SORTER_OPEN), self.T(a))
        for chute in [c for c in by_traffic if c in hub_chutes]:
            # consecutive closed bags merged into one: the second box was scanned under the first box's label
            mine = sorted((b for b in self.bags if b["chute"] == chute and b["close"] and b["close"] < self.T(a - 0.25)),
                          key=lambda b: b["created"])
            for i in range(len(mine)):
                group, n = [], 0
                for b in mine[i:i + 4]:
                    group.append(b)
                    n += len(b["parcels"])
                    if n >= 860:
                        break
                if n >= 860:
                    first = group[0]
                    for b in group[1:]:
                        first["parcels"] += b["parcels"]
                        self.bags.remove(b)
                    first["close"], first["hold"] = group[-1]["close"], True
                    for p in first["parcels"]:
                        p.bag = first
                    self.big_bag = first
                    break
            if self.big_bag:
                break
        chutes = iter(c for c in self.busy_chutes if not self.big_bag or c != self.big_bag["chute"])
        for when in (a - 2.5, a - 4 / 60, a + 7 / 60, a + 22 / 60):
            at = self.T(when) - timedelta(minutes=3)
            for chute in chutes:
                bag = next((b for b in self.bags if b["chute"] == chute and b["created"] < at
                            and (b["close"] is None or b["close"] > at + timedelta(minutes=5))), None)
                if bag and self.split_bag(bag, at, self.rng.randint(2, 6)):
                    break

    def number_bags(self):
        self.bags.sort(key=lambda b: b["created"])
        for n, b in enumerate(self.bags, 1):
            if b["kind"] == "forecast":
                b["id"] = f"CF{self.tag}{n:04d}"
            elif b["kind"] == "manual":
                b["id"] = f"BG01{self.tag}{n:04d}"
            else:
                b["id"] = f"BG01{self.tag}{n:04d}-{b['chute']}"
            b["times"] = sorted(p.sorted_at for p in b["parcels"])
            if b["close"] and b["chute"]:  # the label print at the chute (hand-made bags are printed on a PDA)
                self.chute_log.append((b["close"], b["chute"], b["id"], 1, 1))
        rng = self.rng
        for b in rng.sample(self.bags, min(25, len(self.bags))):  # print presses that didn't print
            if b["close"] and b["chute"]:
                self.chute_log.append((b["close"] - timedelta(seconds=rng.randint(5, 30)), b["chute"], "", 1, 0))
        for c, spans in self.spells.items():  # the chute-full signal while a chute is jammed
            for s, e in spans:
                self.chute_log.append((s + timedelta(seconds=30), c, "", 2, 1))

    def load_outbound(self):
        """Outbound trucks: two night waves to every station, one to every hub (two to the biggest), and a morning
        truck to each. Each truck loads the closed bags for its destination while it is at the dock."""
        rng, a = self.rng, self.anchor
        plans = []
        for s in STATIONS:
            plans.append((s, rng.uniform(21.2, a + 0.3)))
            plans.append((s, rng.uniform(26.5, 28.5)))
        for h in HUBS:
            plans.append((h, rng.uniform(27.0, 29.0)))
        plans.append(("PHX.H", a + 0.4))
        for d in STATIONS + HUBS:  # the morning shift's trucks take what the night closed last
            plans.append((d, rng.uniform(30.5, 33.0)))
        plans.sort(key=lambda x: x[1])
        for i, (dest, hour) in enumerate(plans):
            plan = self.T(hour)
            dock_at = plan - timedelta(minutes=rng.randint(60, 120))
            left = plan + timedelta(minutes=rng.randint(-10, 35))
            transit = timedelta(hours=TRANSIT_HOURS[dest] if dest in TRANSIT_HOURS else rng.uniform(1.0, 3.5))
            self.out_trucks.append({
                "task": f"TT{self.tag}{5000 + i:04d}", "dest": dest, "plan": plan, "dock_at": dock_at, "left": left,
                "arrive": left + transit, "unloaded": left + transit + timedelta(minutes=rng.randint(15, 40)),
                "dock": str(13 + i % 10), "plate": f"TR{rng.randint(10000, 99999)}",
                "type": "26FT" if dest in ("TXK01", "SGR01") else "53FT", "driver": f"Driver {rng.randint(1, 40)}",
                "bags": [], "cancelled": False})
        for t in self.out_trucks:
            ready = sorted((b for b in self.bags if b["kind"] in ("sorter", "manual") and b["dest"] == t["dest"]
                            and b["close"] and not b["checkout"] and not b["hold"] and b["close"] < t["left"] - timedelta(minutes=10)),
                           key=lambda b: b["close"])
            cur = t["dock_at"] + timedelta(minutes=5)
            for b in ready:
                cur = max(cur, b["close"] + timedelta(minutes=rng.randint(5, 20))) + timedelta(seconds=rng.randint(30, 120))
                if cur >= t["left"]:
                    break
                b["checkout"], b["truck"] = cur, t
                t["bags"].append(b)
        cancelled = {"task": None, "dest": "HRL01", "plan": self.T(a + 2), "dock_at": None, "left": None, "arrive": None,
                     "unloaded": None, "dock": None, "plate": None, "type": "26FT", "driver": None, "bags": [], "cancelled": True}
        self.out_trucks.append(cancelled)

    def index(self):
        self.parcel_by_id = {p.id: p for p in self.parcels}
        self.bag_by_id = {}
        for b in self.bags:
            self.bag_by_id.setdefault(b["id"], []).append(b)
        self.gaylord_by_id = {g["id"]: g for g in self.gaylords}
        self.truck_by_task = {t["task"]: t for t in self.trucks}
        self.out_by_task = {t["task"]: t for t in self.out_trucks if t["task"]}
        records = []
        for p in self.parcels:
            for n, (t, status, reason, matched, drop, seat) in enumerate(p.attempts):
                records.append((t, p.id, n, status, reason, matched, drop, seat))
        records.sort(key=lambda r: r[0])
        self.records = records
        self.record_times = [r[0] for r in records]
        self.chute_log.sort(key=lambda r: r[0])
        self.log_times = [r[0] for r in self.chute_log]
        self.sorted_times = sorted(p.sorted_at for p in self.parcels if p.sorted_at)
        self.bags_by_created = sorted(self.bags, key=lambda b: b["created"])
        self.bag_created = [b["created"] for b in self.bags_by_created]
        # piece-scanned freight that may be waiting: pickup, and belt rejects dropped in an exception chute
        self.piece_scanned = [p for p in self.parcels if p.kind == "pickup" or (p.attempts and p.attempts[-1][1] == 2)]

    # ---------- views at `now` ----------

    def bag_count(self, b, now):
        return bag_count(b, now)

    def feed_rows(self, start, end, now):
        """{hour: {seat: [fed, refused]}} for feed passes in [start, min(end, now)]."""
        out = {}
        stop = min(end, now)
        for p in self.parcels:
            for t, seat in p.feeds:
                if start <= t <= stop:
                    h = t.replace(minute=0, second=0, microsecond=0)
                    out.setdefault(h, {}).setdefault(seat, [0, 0])[0] += 1
        rng = random.Random(f"HOU.H-refuse:{self.day}")
        for h in sorted(out):
            for seat, v in out[h].items():
                v[1] = int(v[0] * self.seat_reject[seat] * rng.uniform(0.7, 1.3))
        return out


def bag_count(b, now):
    """Pieces in a bag at `now`."""
    if b["count"] is not None:
        return b["count"] if b["created"] <= now else 0
    return bisect.bisect_right(b["times"], now)


def light_feed(day, offset, start, end, now):
    """Feed counts for an older night without building it: the sorter's hourly profile split over the seats."""
    speed, reject = seat_profile(day)
    rates = hour_rates(day)
    rng = random.Random(f"HOU.H-light:{day}")
    base = datetime(day.year, day.month, day.day) + offset
    share = volume(day) / sum(rates[h] * open_minutes(h) / 60 for h in rates)
    out = {}
    total_speed = sum(speed.values())
    for h, rate in rates.items():
        t = base + timedelta(hours=h)
        if not (start <= t <= min(end, now)):
            continue
        fed = rate * open_minutes(h) / 60 * min(1.0, share) * rng.uniform(0.93, 1.07)
        if fed <= 0:
            continue
        out[t] = {s: [int(fed * speed[s] / total_speed), 0] for s in speed}
        for s, v in out[t].items():
            v[1] = int(v[0] * reject[s] * rng.uniform(0.7, 1.3))
    return out
