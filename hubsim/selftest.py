#!/usr/bin/env python3
"""Starts the WMS simulator in a thread on a free port and calls every endpoint once.

    python3 hubsim/selftest.py            exits 1 if any endpoint fails or answers empty where data is expected
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import model as m  # noqa: E402
import report  # noqa: E402
import server  # noqa: E402


def post(base, path, body, token="selftest"):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def size(data):
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in ("list", "records", "items"):
            if key in data:
                return len(data[key])
        return len(data)
    return 0


def main():
    t0 = time.time()
    httpd, sim = server.make_server(port=0, quiet=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    now = sim.now()
    print(f"simulator up in {time.time() - t0:.1f} s: night of {sim.current}, clock {sim.clock}, now {now:%Y-%m-%d %H:%M}")

    night = sim.night(sim.current)
    fmt = "%Y-%m-%d %H:%M:%S"
    day = m.work_day(now)
    window = {"centerId": 1, "centerIdEncryption": "HOU.H", "timeZone": "America/Chicago",
              "startTime": f"{day} 06:00:01", "endTime": f"{day + timedelta(days=1)} 06:00:00"}
    sorted_p = next(p for p in night.parcels if p.sorted_at and p.sorted_at <= now)
    bag = next(b for b in reversed(night.bags_by_created) if b["kind"] == "sorter" and b["created"] <= now and b["parcels"])
    truck = next(t for t in night.trucks if t["arrive"] <= now)
    gaylord = truck["gaylords"][0]
    ms_now = m.ms(now)

    checks = [  # (path, body, expect rows)
        ("/api/places", {}, True),
        ("/api/bags/search", {"packageNoList": [], "createBeginTime": (now - timedelta(hours=20)).strftime(fmt),
                              "createEndTime": now.strftime(fmt), "startCenterId": 1, "pageNum": 1, "pageSize": 1000}, True),
        ("/api/bags/search", {"packageNoList": [bag["id"], gaylord["id"]], "pageNum": 1, "pageSize": 50}, True),
        ("/api/bags/parcels", {"packageNo": bag["id"], "deptId": 1, "pageNum": 1, "pageSize": 1000}, True),
        ("/api/sorter/records", {"noType": 1, "sorterId": 1, "sortTimeStart": ms_now - 25 * 60000, "sortTimeEnd": ms_now,
                                 "sortStatus": 2, "pageNum": 1, "pageSize": 100}, True),
        ("/api/sorter/records", {"noType": 1, "sorterId": 1, "noList": [sorted_p.id], "sortTimeStart": ms_now - 86400000 * 2,
                                 "sortTimeEnd": ms_now, "pageNum": 1, "pageSize": 50}, True),
        ("/api/sorter/chute-log", {"opTimeStart": ms_now - 3 * 3600000, "opTimeEnd": ms_now, "pageNum": 1, "pageSize": 500}, True),
        ("/api/sorter/feed", {**window}, True),
        ("/api/sorter/plan", {}, True),
        ("/api/trucks/tasks", {"data": {"destTransitIdList": [1], "timeType": 1,
                                        "startLocalTime": (now - timedelta(days=1)).strftime(fmt),
                                        "endLocalTime": (now + timedelta(days=1)).strftime(fmt)}, "pageNum": 1, "pageSize": 200}, True),
        ("/api/trucks/tasks", {"data": {"originTransitIdList": [1], "timeType": 1,
                                        "startLocalTime": (now - timedelta(days=1)).strftime(fmt),
                                        "endLocalTime": (now + timedelta(days=1)).strftime(fmt)}, "pageNum": 1, "pageSize": 200}, True),
        ("/api/trucks/scans", {"data": {"destinationTransitId": "1", "originTransitId": str(m.PLACE_IDS[truck["origin"]]),
                                        "scanType": "1", "sealNo": "", "serialNumber": "", "taskNo": truck["task"],
                                        "startTime": f"{truck['arrive'].date() - timedelta(days=2)} 00:00:00",
                                        "endTime": f"{truck['arrive'].date() + timedelta(days=2)} 23:59:59"},
                               "pageNum": 1, "pageSize": 2000}, True),
        ("/api/scans/history", {"pageNum": 1, "pageSize": 500, "operateNoType": "10", "operateNos": [sorted_p.id],
                                "operateTimeStart": "2026-01-01 00:00:00", "operateTimeEnd": "2027-12-31 23:59:59"}, True),
        ("/api/scans/history", {"pageNum": 1, "pageSize": 500, "operateNoType": "20", "operateNos": [gaylord["id"]],
                                "operateTimeStart": "2026-01-01 00:00:00", "operateTimeEnd": "2027-12-31 23:59:59"}, True),
        ("/api/parcels/route", {"waybillNoList": [sorted_p.id], "pageNum": 1, "pageSize": 100}, True),
        ("/api/dashboard/trucks", {**window, "pageNum": 1, "pageSize": 500, "type": 7}, False),
        ("/api/dashboard/trucks", {**window, "pageNum": 1, "pageSize": 500, "type": 4}, False),
        ("/api/dashboard/sort-count", {**window}, True),
        ("/api/dashboard/arrivals", {**window}, True),
        ("/api/dashboard/arrival-stats", {**window}, True),
        ("/api/dashboard/arrival-hours", {**window, "type": 1}, False),
        ("/api/history/daily-totals", {"startDate": f"{day - timedelta(days=35)}", "endDate": f"{day}"}, True),
        ("/api/dispatch/departures", {}, True),
    ]
    failed = 0
    covered = set()
    for path, body, want_rows in checks:
        t = time.time()
        status, payload = post(base, path, body)
        n = size(payload.get("data"))
        ok = status == 200 and payload.get("code") == 0 and (n > 0 or not want_rows)
        failed += not ok
        covered.add(path)
        print(f"{'ok  ' if ok else 'FAIL'} {path:<30} {n:>6} rows  {1000 * (time.time() - t):6.0f} ms"
              + ("" if ok else f"  status {status} {str(payload)[:160]}"))
    status, _ = post(base, "/api/places", {}, token="expired")
    print(f"{'ok  ' if status == 401 else 'FAIL'} expired token -> HTTP {status}")
    failed += status != 401
    status, payload = post(base, "/api/sorter/records", {"sortTimeStart": ms_now - 60000, "sortTimeEnd": ms_now})
    print(f"{'ok  ' if payload.get('code') == -1 else 'FAIL'} no sorter id -> code {payload.get('code')} ({payload.get('msg')})")
    failed += payload.get("code") != -1
    text = report.post(sim, report.post_time(now))
    good = "Updated: " in text and "Waiting to bag" in text and "Due to arrive (" in text
    print(f"{'ok  ' if good else 'FAIL'} hourly report post ({len(text.splitlines())} lines)")
    failed += not good
    missing = set(server.ENDPOINTS) - covered
    if missing:
        print(f"FAIL not called: {', '.join(sorted(missing))}")
        failed += 1
    httpd.shutdown()
    print(f"{'all endpoints answered' if not failed else f'{failed} problem(s)'} ({time.time() - t0:.1f} s)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
