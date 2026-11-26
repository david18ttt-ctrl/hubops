#!/usr/bin/env python3
"""Night numbers for the closing report, plus the shared WMS helpers the other tools use (import as hn).

    python3 hub_numbers.py unsorted [--day 2026-11-04]   carry-over: late trucks' gaylords and packages
    python3 hub_numbers.py forecast [--day 2026-11-04]   tomorrow's package estimate
    python3 hub_numbers.py gaylords [--day 2026-11-04]   carry-over gaylord by gaylord: each late truck's bags, their
                                                         waybills, and which are still not sorted (+ a CSV)

--day is the working day (6 AM to 6 AM) being closed; it defaults to the current one. Read-only; posts nothing.

unsorted: every truck that ARRIVED on the night's date (12 AM to midnight) and still isn't sorted at closing counts
as carry-over; trucks arriving after midnight belong to the next night. The WMS "Expected to Sort" (what is really
still waiting among piece-scanned freight) is shown next to it, since the crew usually gets part of those trucks done.

forecast: average of the last 4 same-weekday totals, scaled by how the last 7 days compare with the 7 before, plus
whatever the WMS says is still waiting to be sorted beyond a normal night's leftover. The trucks the WMS already
expects tomorrow are printed as a floor and a check: evening trucks usually aren't scheduled yet at night, so that
number is always well under the total. Daily totals come from data/daily-totals.csv (date,total) when it exists,
otherwise from the WMS history call.
"""
import argparse
import csv
import json
import sys
from datetime import date, datetime, timedelta
from statistics import mean

import hubconfig
import wms_client

CENTER = {"centerId": wms_client.CENTER_ID, "centerIdEncryption": wms_client.CENTER_ID_ENCRYPTION,
          "timeZone": wms_client.TIME_ZONE}
TRUCKS_API = "/api/dashboard/trucks"  # the working day's inbound trucks, dashboard style
SORT_COUNT_API = "/api/dashboard/sort-count"  # sorted so far / expected to sort
ARRIVALS_API = "/api/dashboard/arrivals"  # trucks and packages expected for a day
ARRIVAL_STATS_API = "/api/dashboard/arrival-stats"  # packages on the road now
ARRIVAL_HOURS_API = "/api/dashboard/arrival-hours"  # expected arrivals hour by hour
DAILY_TOTALS_API = "/api/history/daily-totals"  # packages handled per past working day
SCAN_LIST_API = "/api/trucks/scans"  # Loading & Unloading Query
REGIONS_API = "/api/places"  # TMS origin/destination ids
SORT_RECORDS_API = "/api/sorter/records"
UNLOAD_CHECK_IN = "0"  # operationType of an unloading check-in at this hub
LOAD_CHECK_OUT = "1"  # operationType of the loading sign-out at the origin: what was put on the truck
SORTED_OK = 1  # sortStatus: sorted
TRUCK_LISTS = (4, 6, 7)  # on the way, arrived not unloaded, unloaded
LATE_FROM_HOUR = 0  # A night's trucks: in from 12 AM to midnight; still not sorted at closing = carry-over
NORMAL_LEFTOVER = 15000  # Rough packages still waiting at 6 AM on an ordinary night

TOKEN_FILE = hubconfig.DATA_DIR / ".wms-token"  # optional saved token, shared by every job
DAILY_TOTALS_FILE = hubconfig.DATA_DIR / "daily-totals.csv"


def window(day):
    start, end = f"{day} 06:00:01", f"{day + timedelta(days=1)} 06:00:00"
    return {**CENTER, "startTime": start, "endTime": end, "startTimeFj": start, "endTimeFj": end}


def browser_token():
    """A fresh token from the sign-in flow. The WMS here takes a bearer token from the environment
    (HUBOPS_WMS_TOKEN); with none set, the simulator's demo token."""
    return wms_client.default_token()


def wms_token(fresh=False):
    """The WMS token. The saved one (data/.wms-token) when the WMS still takes it: one small request, under a
    second. Only a dead or missing token asks for a new one. Every script should use this."""
    if not fresh and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        try:
            day = (datetime.now(hubconfig.TIME_ZONE) - timedelta(hours=6)).date()
            wms(token, SORT_COUNT_API, window(day))
            return token
        except wms_client.LoginExpired:
            pass  # fall through to a new token
        except Exception:  # the WMS or the network hiccuped: the token itself is probably fine, let the caller's call say so
            return token
    token = browser_token()
    try:
        hubconfig.data_path(TOKEN_FILE.name).write_text(token)
        TOKEN_FILE.chmod(0o600)
    except OSError:
        pass
    return token


def wms(token, path, body):
    return wms_client.call(path, body, token=token, allow_empty=True)["data"]


def trucks(token, day):
    """Every truck in the working day, one row each (the later list wins)."""
    found = {}
    for kind in TRUCK_LISTS:
        data = wms(token, TRUCKS_API, {**window(day), "pageNum": 1, "pageSize": 500, "type": kind}) or {}
        for truck in data.get("records") or []:
            found[truck.get("taskCode")] = truck
    return list(found.values())


def unsorted(token, day):
    late = late_trucks(token, day)
    print(f"Night {day:%m/%d}: trucks that arrived {day:%m/%d} 12 AM to midnight")
    for t in late:
        print(f"  arrived {t['actualArrivalTime'][11:16]}  {t.get('placeOfOrigin') or '?':<10} "
              f"{t.get('bagCount') or 0:>3} gaylords  {t.get('waybillCount') or 0:>6,} packages  ({t.get('taskCode')})")
    bags = sum(int(t.get("bagCount") or 0) for t in late)
    waybills = sum(int(t.get("waybillCount") or 0) for t in late)
    print(f"ARRIVED on those trucks: {bags} gaylords, {waybills:,} packages. This is everything that came in, sorted or not; "
          "it is NOT what is left.")
    try:  # what is really still waiting from them: the unsorted board, when its count is fresh
        board = json.loads((hubconfig.DATA_DIR / "unsorted-latest.json").read_text())
        made = datetime.strptime(str(board.get("made"))[:16], "%Y-%m-%d %H:%M")
        age = int((datetime.now(hubconfig.TIME_ZONE).replace(tzinfo=None) - made).total_seconds() // 60)
        if age <= 30:
            tasks = {t["taskCode"] for t in late}
            mine = [t for t in board["trucks"] if t["task"] in tasks]
            print(f"STILL NOT SORTED from them (carry-over if the night ended now; board read {made:%H:%M}, {age} min ago): "
                  f"{sum(t['open'] for t in mine)} gaylords, {sum(t['in_full'] for t in mine):,} packages"
                  + "".join(f"\n    {t['from']} in {t['arrived'][11:16]}: {t['in_full']:,} in {t['open']} gaylords"
                            + (f" ({t['state']})" if t.get("state") else "") for t in mine))
        else:
            print(f"STILL NOT SORTED: unknown here, the unsorted board is {age} min old. Run: python3 hubops/unsorted_board.py --dry-run")
    except Exception as e:
        print(f"STILL NOT SORTED: unknown here (board file unreadable: {type(e).__name__})")
    count = wms(token, SORT_COUNT_API, window(day))
    print(f"WMS 'Expected to Sort' right now: {int(count['allShouldSortCount']):,} "
          f"(this shift {int(count['tdShouldSortCount']):,}, older {int(count['hsShouldSortCount']):,}): only freight scanned in "
          "piece by piece (pickup, drop-off, rejects), not the gaylords on the floor.")


def late_trucks(token, day):
    """Trucks that arrived on `day` from 12 AM to midnight: the ones the carry-over rule counts (trucks in after
    midnight belong to the next day, so the 12-6 AM ones are this day's too). The WMS lists trucks by working day
    (6 AM to 6 AM), so the early ones come from the previous working day's list."""
    late_start = datetime(day.year, day.month, day.day, LATE_FROM_HOUR)
    midnight = datetime(day.year, day.month, day.day) + timedelta(days=1)
    seen = {t.get("taskCode"): t for d in (day - timedelta(days=1), day) for t in trucks(token, d)}
    late = [t for t in seen.values() if t.get("actualArrivalTime")
            and late_start <= datetime.strptime(t["actualArrivalTime"][:19], "%Y-%m-%d %H:%M:%S") < midnight]
    return sorted(late, key=lambda t: t["actualArrivalTime"])


def truck_waybills(token, truck, regions, day):
    """{bag number: [waybills]} on one truck into this hub, from Loading & Unloading Query: every gaylord scanned
    in here, plus the ones the origin signed out onto this truck that nobody has scanned in (still on the trailer,
    or unloaded with no scan). Without those a truck that was never unloaded would count as zero carry-over."""
    origin = regions.get(truck.get("placeOfOrigin"))
    if origin is None:
        return {}
    data = {"destinationTransitId": str(regions[hubconfig.HUB_CODE]), "originTransitId": str(origin), "scanType": "1",
            "sealNo": "", "serialNumber": "", "taskNo": truck["taskCode"],
            "startTime": f"{day - timedelta(days=1)} 00:00:00", "endTime": f"{day + timedelta(days=2)} 23:59:59"}
    bags, loaded, page, seen = {}, {}, 1, 0
    while True:
        res = wms(token, SCAN_LIST_API, {"data": data, "pageNum": page, "pageSize": 2000}) or {}
        rows = res.get("records") or []
        for r in rows:
            if not r.get("waybillNo"):
                continue
            kind = str(r.get("operationType"))
            if kind == UNLOAD_CHECK_IN:
                bags.setdefault(r.get("bagNo") or "(no bag)", set()).add(r["waybillNo"])
            elif kind == LOAD_CHECK_OUT:
                loaded.setdefault(r.get("bagNo") or "(no bag)", set()).add(r["waybillNo"])
        seen += len(rows)
        if not rows or seen >= int(res.get("total") or 0):
            break
        page += 1
    scanned = {w for ws in bags.values() for w in ws}
    for bag, ws in loaded.items():
        if bag not in bags and ws - scanned:
            bags[bag + " (not scanned in)"] = ws - scanned
    return {bag: sorted(w) for bag, w in bags.items()}


def sorted_waybills(token, waybills, since):
    """The waybills with a successful sort since `since` (machine and manual sorter; hand-bagging with PDAs
    isn't in Sorting Records). 100 numbers per request, 4 requests at a time. Sorting Records needs a sorter id;
    without one nothing is found."""
    from concurrent.futures import ThreadPoolExecutor
    start, end = int(since.timestamp() * 1000), int(datetime.now().timestamp() * 1000)

    def batch(numbers):
        found, body = set(), {"noType": 1, "sorterId": hubconfig.SORTER_ID, "noList": numbers, "sortTimeStart": start,
                              "sortTimeEnd": end, "sortStatus": SORTED_OK, "pageNum": 1, "pageSize": 100}
        while True:
            rows = (wms(token, SORT_RECORDS_API, body) or {}).get("list") or []
            found.update(r["orderCode"] for r in rows if r.get("orderCode"))
            if len(rows) < 100:
                return found
            body = {**body, "pageNum": body["pageNum"] + 1}

    with ThreadPoolExecutor(4) as pool:
        return set().union(*pool.map(batch, [waybills[i:i + 100] for i in range(0, len(waybills), 100)]), set())


def gaylords(token, day, csv_path=None):
    """Carry-over gaylord by gaylord: every bag on the late trucks, its waybills, and which aren't sorted yet."""
    regions = {r["regionName"]: r["id"] for r in wms(token, REGIONS_API, {}) or []}
    since = datetime(day.year, day.month, day.day, LATE_FROM_HOUR) - timedelta(hours=1)
    rows, left_bags, left_waybills, all_waybills = [], 0, 0, 0
    print(f"Night {day:%m/%d}: gaylords on trucks that arrived {day:%m/%d} 12 AM to midnight")
    for truck in late_trucks(token, day):
        bags = truck_waybills(token, truck, regions, day)
        waybills = sorted({w for ws in bags.values() for w in ws})
        done = sorted_waybills(token, waybills, since)
        open_bags = {b: [w for w in ws if w not in done] for b, ws in bags.items()}
        open_bags = {b: ws for b, ws in open_bags.items() if ws}
        print(f"\n{truck['actualArrivalTime'][11:16]} {truck.get('placeOfOrigin')} {truck['taskCode']}: "
              f"{len(bags)} gaylords, {len(waybills):,} waybills; sorted {len(done):,}, "
              f"not sorted {len(waybills) - len(done):,} in {len(open_bags)} gaylords")
        for bag_no, left in sorted(open_bags.items(), key=lambda x: -len(x[1])):
            print(f"  {bag_no}: {len(left):,} of {len(bags[bag_no]):,} not sorted")
        for bag_no, ws in bags.items():
            rows += [(truck["taskCode"], truck.get("placeOfOrigin"), truck["actualArrivalTime"][:16], bag_no, w,
                      "sorted" if w in done else "not sorted") for w in ws]
        left_bags += len(open_bags)
        left_waybills += len(waybills) - len(done)
        all_waybills += len(waybills)
    print(f"\nNot sorted yet: {left_waybills:,} of {all_waybills:,} waybills, in {left_bags} gaylords.")
    print("'Sorted' = a successful sort in WMS Sorting Records (the machine and the manual sorter). "
          "Packages bagged by hand with a PDA aren't in those records, so they'd show as not sorted.")
    if csv_path:
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Task code", "From", "Arrived", "Gaylord (bag)", "Waybill", "Status"])
            writer.writerows(rows)
        print(f"Every waybill: {csv_path}")


def daily_totals(token, day):
    """{date: packages handled that working day} for the last ~5 weeks: data/daily-totals.csv (date,total) when it
    exists, else the WMS history call."""
    totals = {}
    if DAILY_TOTALS_FILE.exists():
        with DAILY_TOTALS_FILE.open() as f:
            for row in csv.reader(f):
                try:
                    totals[date.fromisoformat(row[0].strip())] = int(float(row[1]))
                except (IndexError, ValueError):
                    continue  # header or a blank line
        return totals
    start = day - timedelta(days=35)
    for row in wms(token, DAILY_TOTALS_API, {"startDate": f"{start}", "endDate": f"{day}"}) or []:
        if row.get("total"):
            totals[date.fromisoformat(row["day"])] = int(row["total"])
    return totals


def forecast(token, day):
    target = day + timedelta(days=1)
    totals = daily_totals(token, day)
    same_day = [totals[d] for d in (target - timedelta(weeks=w) for w in range(1, 5)) if d in totals]
    last7 = [totals[day - timedelta(days=i)] for i in range(1, 8) if day - timedelta(days=i) in totals]
    prior7 = [totals[day - timedelta(days=i)] for i in range(8, 15) if day - timedelta(days=i) in totals]
    count = wms(token, SORT_COUNT_API, window(day))
    sorted_now, waiting = int(count["alreadySortedCount"]), int(count["allShouldSortCount"])
    tomorrow = wms(token, ARRIVALS_API, window(target)) or {}
    stats = wms(token, ARRIVAL_STATS_API, window(target)) or {}
    hours = wms(token, ARRIVAL_HOURS_API, {**window(target), "type": 1}) or []

    print(f"Estimating {target:%a %m/%d} (working day 6 AM {target:%m/%d} to 6 AM {target + timedelta(days=1):%m/%d})")
    print(f"Last {len(same_day)} {target:%A}s: {', '.join(f'{n:,}' for n in same_day)}")
    trend = mean(last7) / mean(prior7) if last7 and prior7 else 1.0
    print(f"Last 7 days vs the 7 before: {trend:.3f}")
    print(f"Tonight so far: {sorted_now:,} sorted, {waiting:,} still waiting to be sorted")
    print(f"WMS trucks already expected tomorrow: {tomorrow.get('vehicleEstimatedArrival') or 0} trucks, "
          f"{int(tomorrow.get('estimatedArrivalWaybillCount') or 0):,} packages "
          f"({int(stats.get('waybillInTransit') or 0):,} on the road now)")
    for h in hours:
        n = int(h.get("trunkWaybillNoCount") or 0) + int(h.get("branchWaybillNoCount") or 0)
        if n:
            print(f"    {h['hourRange'][11:]}  {h['vehicleCount']} truck(s)  {n:,}")
    if not same_day:
        print("No same-weekday history, can't estimate.")
        return
    base = mean(same_day) * trend
    extra = max(0, waiting - NORMAL_LEFTOVER)
    estimate = base + extra
    print(f"Estimate: {base:,.0f} (normal {target:%A}) + {extra:,} (extra leftover) = about {estimate:,.0f}; "
          f"round to {round(estimate, -4):,.0f}. Likely range {estimate * 0.95:,.0f} to {estimate * 1.05:,.0f}.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("what", choices=["unsorted", "forecast", "gaylords"])
    parser.add_argument("--csv", help="with gaylords: also write every waybill to this CSV file")
    parser.add_argument("--day", type=date.fromisoformat, help="working day being closed, like 2026-11-04")
    args = parser.parse_args()
    now = datetime.now(hubconfig.TIME_ZONE)
    day = args.day or (now.date() if now.hour >= 6 else now.date() - timedelta(days=1))
    token = wms_token()
    if args.what == "unsorted":
        unsorted(token, day)
    elif args.what == "gaylords":
        gaylords(token, day, args.csv)
    else:
        forecast(token, day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
