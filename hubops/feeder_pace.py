#!/usr/bin/env python3
"""Hourly feed-station check on the Sorter 1 cross-belt sorter, sent to the phone app.

The WMS feeder-station analysis gives, per station and hour, the boxes put on the belt (pickCnt) and the boxes
the machine couldn't recognize (refuseCnt). That is the workstation feed data used to judge labor productivity.
Station numbers are the floor's (older WMS data numbers seats 4 lower; see by_hour), and stations 1-5 come as
one group. A few minutes after each hour, this compares every station with its OWN normal (median of its busy
hours over the last 7 nights, so a quiet station at the end of a line isn't called slow) and sends one alert when:
- a station fed under 60% of what it should have fed at that hour's volume. Stations that only get the freight
  left over by the ones before them (typically floor 1-8 and 16-22: their counts rise and fall with the sorter's
  volume) are expected to drop when the sorter is quieter; stations that always have freight (typically floor
  9-15, 23-29: flat counts, so the person sets the pace) are expected to stay at their normal;
- a station's reject % is 4%+ and well above the sorter's average that hour. "every night" marks a station
  that is high on most nights too, which points at the station (scanner, belt), not the person.
Nothing is sent for an hour when the whole sorter ran under 80% of normal (break, start, end, no freight).

    python3 hubops/feeder_pace.py                     check the last full hour (what launchd runs at :02)
    python3 hubops/feeder_pace.py --dry-run           same, print instead of sending
    python3 hubops/feeder_pace.py --replay 2026-11-11 what each hour of that night would have sent
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median

import app_push
import chute_alerts as ca
import wms_client

FEED_LIST_API = "/api/sorter/feed"
ACTIVE_FROM, ACTIVE_UNTIL = 18, 7  # Hours whose results are checked (6 PM to 6 AM)
BASELINE_NIGHTS = 7
BUSY_HOUR = 15000  # A sorter hour counts toward "normal" only if at least this many boxes went on the belt
SORTER_NORMAL = 0.8  # The whole sorter must be at 80%+ of its normal hour before single stations are judged
SLOW = 0.6  # A station under 60% of its own normal is slow
MIN_NORMAL = 150  # Stations that normally feed fewer per hour than this are never called slow
REJECT_MIN = 4.0  # Reject % worth a mention ...
REJECT_TIMES = 1.6  # ... and at least this many times the sorter's average that hour
REJECT_MIN_FED = 200
EVERY_NIGHT = 0.6  # "every night" when a station was high on 60%+ of the baseline nights ...
EVERY_NIGHT_TIMES = 1.4  # ... high meaning 4%+ and 1.4x the sorter's average over the whole night


def operating_day(t):
    """WMS operating day (6 AM to 6 AM) that the hour starting at t belongs to."""
    return t.date() if t.hour >= 6 else t.date() - timedelta(days=1)


def fetch_day(token_box, day):
    body = {"centerId": wms_client.CENTER_ID, "startTime": f"{day} 06:00:01",
            "endTime": f"{day + timedelta(days=1)} 06:00:00", "timeZone": wms_client.TIME_ZONE}
    data = ca.wms(token_box, FEED_LIST_API, body)
    return data if isinstance(data, list) else []


def floor_station(seat):
    """The floor's station number for a WMS seatNo. Older WMS data numbers seats 4 lower than the floor (its
    seat 1 = floor 1-5 together, seat 26 = floor 30). Current data reports 1-30 (+35) by the floor's numbers,
    so the seat is the station. A seat number of 27+ in the same day is what tells the two apart; by_hour
    checks that."""
    return str(seat or "")


def station_order(s):
    return 1 if s == "1-5" else int(s) if str(s).isdigit() else 999


def by_hour(rows):
    """{"2026-11-12 01": {"": (fed, rejects), "1": (fed, rejects), ...}} from the WMS rows, by floor station.
    Old numbering (seats 1-26, 4 lower than the floor) is moved up 4, with seat 1 = floor 1-5."""
    seats = [int(r["seatNo"]) for r in rows if str(r.get("seatNo") or "").isdigit()]
    old = bool(seats) and max(seats) <= 26
    out = defaultdict(dict)
    for r in rows:
        if r.get("pickCnt") in (None, ""):
            continue
        seat = floor_station(r.get("seatNo"))
        if old and seat:
            seat = "1-5" if seat == "1" else str(int(seat) + 4)
        out[r["hourRange"]][seat] = (int(r["pickCnt"]), int(r.get("refuseCnt") or 0))
    return out


# ---------- the logic (no WMS or app calls here) ----------

def baseline(nights):
    """nights: list of by_hour() dicts. Normal fed per hour for the sorter ("") and each station, plus how
    often each station's reject % was high over a whole night."""
    hourly, pairs, rej_pcts = defaultdict(list), defaultdict(list), defaultdict(list)
    high_nights, seen_nights = defaultdict(int), defaultdict(int)
    for night in nights:
        totals = defaultdict(lambda: [0, 0])
        busy = {h: seats for h, seats in night.items() if seats.get("", (0, 0))[0] >= BUSY_HOUR}
        for seats in busy.values():
            for seat, (fed, rej) in seats.items():
                hourly[seat].append(fed)
                pairs[seat].append((seats[""][0], fed))
                if fed >= REJECT_MIN_FED:
                    rej_pcts[seat].append(100 * rej / fed)
                totals[seat][0] += fed
                totals[seat][1] += rej
        all_fed, all_rej = totals[""] if "" in totals else (0, 0)
        avg = 100 * all_rej / all_fed if all_fed else 0
        for seat, (fed, rej) in totals.items():
            if seat and fed >= 1000:
                seen_nights[seat] += 1
                pct = 100 * rej / fed
                if pct >= REJECT_MIN and pct >= EVERY_NIGHT_TIMES * avg:
                    high_nights[seat] += 1
    normal = {seat: median(v) for seat, v in hourly.items() if v}
    every_night = {s for s, n in high_nights.items() if seen_nights[s] >= 3 and n >= EVERY_NIGHT * seen_nights[s]}
    swing = {seat: swing_of(p) for seat, p in pairs.items() if seat}
    rej_normal = {seat: median(v) for seat, v in rej_pcts.items() if v}
    return {"normal": normal, "every_night": sorted(every_night, key=station_order), "swing": swing,
            "rej_normal": rej_normal}


def swing_of(pairs):
    """How much a station's count moves with the sorter's total: 0 = flat (the person's pace sets it, freight is
    always there), 1 = in step, 1.5+ = it only gets what's left over (starved, e.g. the end of a line).
    Least-squares slope scaled to the averages, kept between 0 and 2."""
    if len(pairs) < 10:
        return 0.0
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    vx = sum((x - mx) ** 2 for x, _ in pairs)
    if not vx or not my:
        return 0.0
    slope = sum((x - mx) * (y - my) for x, y in pairs) / vx
    return max(0.0, min(2.0, slope * mx / my))


def expected(seat, fed_all, base):
    """What a station should feed in an hour where the sorter fed fed_all: its normal, moved up or down as much
    as that station usually moves with the sorter's volume."""
    normal = base["normal"]
    ratio = fed_all / normal[""] - 1
    return max(0.3 * normal[seat], normal[seat] * (1 + base.get("swing", {}).get(seat, 0.0) * ratio))


def label(hour):
    """'2026-11-12 01' -> '1–2 AM'"""
    t = datetime.strptime(hour, "%Y-%m-%d %H")
    a, b = t.strftime("%I %p").lstrip("0"), (t + timedelta(hours=1)).strftime("%I %p").lstrip("0")
    return f"{a.split()[0]}–{b}" if a.split()[1] == b.split()[1] else f"{a}–{b}"


def n(x):
    return f"{int(round(x)):,}"


def report(hour, seats, base):
    """The alert text for one hour, or None when there is nothing to act on."""
    normal = base["normal"]
    fed_all, rej_all = seats.get("", (0, 0))
    if not normal.get("") or fed_all < SORTER_NORMAL * normal[""]:
        return None
    avg = 100 * rej_all / fed_all if fed_all else 0
    want = {s: expected(s, fed_all, base) for s in seats if s and normal.get(s, 0) >= MIN_NORMAL}
    slow = sorted(((fed / want[s], s, fed) for s, (fed, _) in seats.items()
                   if s in want and fed < SLOW * want[s]))[:4]
    rejects = sorted(((100 * rej / fed, s) for s, (fed, rej) in seats.items()
                      if s and fed >= REJECT_MIN_FED and 100 * rej / fed >= REJECT_MIN
                      and 100 * rej / fed >= REJECT_TIMES * avg), reverse=True)[:3]
    missing = sorted((s for s in normal if s and normal[s] >= MIN_NORMAL and s not in seats), key=station_order)
    if not slow and not rejects and not missing:
        return None
    lines = [f"Feed stations {label(hour)} · {n(fed_all)} fed (normal {n(normal[''])})"]
    if slow:
        lines.append("Slow: " + " · ".join(f"{s}: {n(fed)} (expected {n(want[s])})" for _, s, fed in slow))
    if missing:
        lines.append("Fed nothing: " + ", ".join(missing))
    if rejects:
        lines.append("Rejects: " + " · ".join(
            f"{s}: {pct:.1f}%" + (" (every night)" if s in base["every_night"] else "") for pct, s in rejects)
            + f" · avg {avg:.1f}%")
    return "\n".join(lines)


def stations(seats, base, judge=True):
    """Every feed station for the app's bar charts: each line's speed next to that station's normal, and its
    reject % next to that station's normal reject %. seats: {station: (fed, rejects)} for one hour, or
    per-hour averages for the night. judge=False: no slow/high flags (a whole night isn't one hour's check)."""
    normal, rn = base["normal"], base.get("rej_normal", {})
    fed_all, rej_all = seats.get("", (0, 0))
    avg = 100 * rej_all / fed_all if fed_all else 0
    judged = judge and normal.get("") and fed_all >= SORTER_NORMAL * normal[""]
    out = []
    for st in sorted({x for x in list(seats) + list(normal) if x}, key=station_order):
        fed, rej = seats.get(st, (0, 0))
        want = expected(st, fed_all, base) if normal.get(st) and normal.get("") and fed_all else normal.get(st)
        pct = 100 * rej / fed if fed >= REJECT_MIN_FED else None
        out.append({
            "s": st, "fed": round(fed), "normal": round(want) if want else None,
            "rej": None if pct is None else round(pct, 1),
            "rej_normal": round(rn[st], 1) if st in rn else None,
            "slow": bool(judged and want and normal.get(st, 0) >= MIN_NORMAL and fed < SLOW * want),
            "high": bool(judged and pct is not None and pct >= REJECT_MIN and pct >= REJECT_TIMES * avg),
            "every_night": st in base["every_night"]})
    return out


def night_so_far(tonight, base):
    """Per-station average per busy hour over tonight so far (sorter fed BUSY_HOUR+), for the "Tonight" view."""
    busy = [h for h in tonight.values() if h.get("", (0, 0))[0] >= BUSY_HOUR]
    if not busy:
        return None
    tot = defaultdict(lambda: [0, 0])
    for h in busy:
        for st, (fed, rej) in h.items():
            tot[st][0] += fed
            tot[st][1] += rej
    per_hour = {st: (fed / len(busy), rej / len(busy)) for st, (fed, rej) in tot.items()}
    return {"hours": len(busy), "fed": round(tot[""][0]), "stations": stations(per_hour, base, judge=False),
            "rej": round(100 * tot[""][1] / tot[""][0], 1) if tot[""][0] else None,
            "per_hour": round(per_hour[""][0]), "normal": round(base["normal"].get("", 0))}


# ---------- running it ----------

def check_hour(token_box, hour_start):
    """(alert text or None, details for the app)."""
    day = operating_day(hour_start)
    nights = [by_hour(fetch_day(token_box, day - timedelta(days=i))) for i in range(1, BASELINE_NIGHTS + 1)]
    base = baseline(nights)
    key = hour_start.strftime("%Y-%m-%d %H")
    tonight = by_hour(fetch_day(token_box, day))
    seats = tonight.get(key, {})
    fed_all, rej_all = seats.get("", (0, 0))
    detail = {"fed": fed_all, "normal": round(base["normal"].get("", 0)),
              "rej": round(100 * rej_all / fed_all, 1) if fed_all else None,
              "stations": stations(seats, base), "night": night_so_far(tonight, base)}
    return report(key, seats, base), detail


def run_once(dry_run=False, any_hour=False):
    now = datetime.now()
    hour_start = (now - timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    if not any_hour and not (hour_start.hour >= ACTIVE_FROM or hour_start.hour < ACTIVE_UNTIL):
        return
    text, detail = check_hour([ca.wms_token()], hour_start)
    if text:
        if dry_run:
            print(text)
        else:
            app_push.alert("Feed stations", text)
        print(f"{now:%m/%d %H:%M} sent: {text!r}")
    if not dry_run:
        push_app(now, label(hour_start.strftime("%Y-%m-%d %H")), text, detail)


APP_FILE = ca.DATA / "feeder-app.json"  # Tonight's hours, for the phone app's Feeders tab


def push_app(now, hour, text, detail=None):
    """The app's Feeders tab: this hour on top, tonight's earlier hours below (nothing is flagged = text None)."""
    try:
        saved = json.loads(APP_FILE.read_text())
    except (OSError, ValueError):
        saved = {"hours": []}
    cut = (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M:%S")
    hours = [h for h in saved["hours"] if h["updated"] >= cut and h["hour"] != hour]
    night = (detail or {}).pop("night", None)
    hours.append({"hour": hour, "updated": now.strftime("%Y-%m-%d %H:%M:%S"), "text": text, **(detail or {})})
    APP_FILE.parent.mkdir(exist_ok=True)
    APP_FILE.write_text(json.dumps({"hours": hours}, ensure_ascii=False))
    latest = hours[-1]
    try:
        app_push.data("feeder", {**latest, "earlier": hours[-2::-1], "night": night})
    except Exception as e:  # noqa: BLE001 - the app missing an hour must not stop the alert
        print(f"{now:%m/%d %H:%M} app update failed: {type(e).__name__}: {e}", file=sys.stderr)


def replay(day_text):
    token_box = [ca.wms_token()]
    day = date.fromisoformat(day_text)
    nights = [by_hour(fetch_day(token_box, day - timedelta(days=i))) for i in range(1, BASELINE_NIGHTS + 1)]
    base = baseline(nights)
    print(f"Normal sorter hour {n(base['normal'].get('', 0))}; high rejects most nights: {base['every_night']}")
    tonight, sent = by_hour(fetch_day(token_box, day)), 0
    for key in sorted(tonight):
        text = report(key, tonight[key], base)
        if text:
            sent += 1
            print(f"--- {text}")
    print(f"\n{sent} alerts would have been sent.")


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
