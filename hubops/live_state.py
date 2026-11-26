#!/usr/bin/env python3
"""Live state: the numbers the floor asks about most, pulled every 5 minutes so an answer can come from data that is
already collected instead of a lookup started after the question.

Each run saves data/live/state.json (one section per source, each with its own read time and error) and appends a
small line to data/live/history.jsonl (trends: speed, floor left, trucks). Local files only: no app/KV writes.
All WMS calls are summary lists (read-only), ~8 requests per run; the per-truck scan reads stay in unsorted_board.py.

Rule of this file: EVERY line of the brief carries the time its own data was read, taken from the data itself (the
board file's "made", the hourly report's "Updated" stamp), never from when this script happened to run. Data older
than its limit prints as UNKNOWN with no number, so an old count can never be quoted as current. watch() checks every
background job's output for freshness on each run, alerts the app once when one breaks, and the brief starts with
"!! BROKEN" lines so a dead source is visible before anything is quoted from it.

Sections:
  speed      this hour so far scaled to an hourly pace, Vendor A / Vendor B / rejects (WMS sorter feed, via floor_now.speed)
  sort       WMS realtime counts: already sorted today; "expected to sort" = only freight scanned in piece by piece
             (pickup, drop-off, rejects), NOT the linehaul gaylords on the floor
  floor      what is left per inbound truck (data/unsorted-latest.json, unsorted_board.py), with trucks nobody
             has unloaded; split by arrival before/after midnight (before midnight = tonight's; whatever of it is
             unsorted at closing is the night's carry-over)
  broadcast  the hourly sort report feed: "waiting to bag" and ETAs; the second, independent count that the floor
             number is checked against (floor_now.trucks reads the feed)
  inbound    linehaul trucks: on the way (pcs, gaylords, ETA), arrived not unloaded, unloaded - WMS
  outbound   every truck task leaving the hub yesterday→tomorrow: station, here since, planned/actual leave, status
  watch      which background jobs / sources are broken or stale right now

    python3 live_state.py           collect and save (the scheduled job, every 5 min)
    python3 live_state.py --brief   print the short brief from the saved state (0.1 s)
    python3 live_state.py --check   collect now, then print the brief and every watch problem (use after a fix)
"""
import argparse
import json
import subprocess
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "data"
OUT = DATA / "live"
STATE = OUT / "state.json"
HISTORY = OUT / "history.jsonl"
STALE_MIN = 15  # a WMS section older than this is flagged in the brief
BOARD_MAX_MIN = 30  # the floor board's own age limit: older = UNKNOWN, no number shown
BROADCAST_MAX_MIN = 100  # the hourly report (posted at :16): older than this it is not shown as current
ALERT_AGAIN_H = 6  # a problem that stays broken is alerted again this often
ALERT_HOURS = (14, 7)  # the phone only hears about a broken source in these hours (night shift); the brief always shows it
JOB_PREFIX = "com.hubops."  # launchd label prefix of the scheduled jobs

# Background jobs whose output the floor answers depend on: (name, what breaks without it, file under data/, max age in
# minutes, hours it runs (start, end) or None for always). Checked from the file's own age on every collect.
WATCH = (
    ("floor board", "floor / left-to-sort numbers", "unsorted-latest.json", 20, None),
    ("hourly report read", "waiting-to-bag cross-check and truck ETAs", "floor-trucks.json", 80, None),
    ("floor-now card", "the app's Floor now card", "floor-now.json", 25, (16, 8)),
    ("chute alerts", "stuck-chute / no-rule alerts", "chute-alerts-state.json", 12, (16, 8)),
    ("lost labels", "lost-label alerts", "lost-labels-state.json", 12, (16, 8)),
    ("loading board", "truck-at-dock pull alerts", "dock_state.json", 35, None),  # every 28 min by day, 10 at night
)


def now_local():
    import hubconfig
    return datetime.now(hubconfig.TIME_ZONE).replace(tzinfo=None)


def work_day(now):
    """The WMS working day starts 6 AM: before 6 AM it is still yesterday's."""
    return (now - timedelta(hours=6)).date()


def tonight_cutoff(now):
    """Midnight that ends the working day's arrivals: a truck in before it is tonight's freight (unsorted at closing =
    the night's carry-over); a truck in after it belongs to the next day."""
    d = work_day(now) + timedelta(days=1)
    return datetime(d.year, d.month, d.day)


def tonight_start(now):
    """Midnight that STARTS tonight's arrivals (the one before the working day's date). A truck in before it belonged to
    an earlier night: whatever of it is still on the floor is that night's carry-over. Without this, after 6 AM every
    leftover from last night would be shown as the new day's freight."""
    d = work_day(now)
    return datetime(d.year, d.month, d.day)


def split_by_night(trucks, now, key="arrived"):
    """(carried over from earlier nights, tonight's, after tonight's midnight) by arrival time."""
    start, cut = (f"{x:%Y-%m-%d %H:%M}" for x in (tonight_start(now), tonight_cutoff(now)))
    return ([t for t in trucks if t[key][:16] < start], [t for t in trucks if start <= t[key][:16] < cut],
            [t for t in trucks if t[key][:16] >= cut])


def sec_speed(now, tok):
    import floor_now
    return floor_now.speed(now)


def sec_sort(now, tok):
    import hub_numbers as hn
    c = hn.wms(tok, hn.SORT_COUNT_API, hn.window(work_day(now))) or {}
    n = lambda key: int(c.get(key) or 0)  # noqa: E731
    return {"sorted_today": n("alreadySortedCount"), "sorted_bags_today": n("alreadySortedBagCount"),
            "expected_to_sort": n("allShouldSortCount"), "expected_this_shift": n("tdShouldSortCount"),
            "expected_older": n("hsShouldSortCount"),
            # where the piece-scanned waiting freight came from (WMS "Cargo Source")
            "wait_pickup": n("allShouldPickCollectCount"), "wait_transfer": n("allShouldPickTransferCount"),
            "wait_dropoff": n("allShouldPickCusPickupCount"), "wait_customs": n("allShouldPickClearanceCount"),
            "bags_not_signed_out": n("allPendingSignoutBagCount"), "pcs_not_signed_out": n("allPendingSignoutCount")}


def sec_floor(now, tok):
    b = json.loads((DATA / "unsorted-latest.json").read_text())
    trucks = [{"from": (t["from"] or "?").replace(".H", ""), "arrived": t["arrived"][:16], "left": t["left"], "total": t["total"],
               "in_full": t["in_full"], "full_gaylords": t["open"], "gaylords": t["gaylords"], "task": t.get("task"),
               "state": t.get("state", ""), "sorted": t.get("sorted", t["total"] - t["left"]), "noscan_full": t.get("noscan_full", 0),
               "noscan_in_full": t.get("noscan_in_full", 0)}
              for t in b.get("trucks", [])]
    return {"read": b.get("made"), "started": (b.get("started") or "")[:16], "took_s": b.get("took_s"), "trucks": trucks,
            "strays": sum(a["left"] for a in b.get("almost", [])), "stray_trucks": len(b.get("almost", [])),
            "in_full": sum(t["in_full"] for t in trucks), "full_gaylords": sum(t["full_gaylords"] for t in trucks)}


def sec_broadcast(now, tok):
    """The newest hourly sort report, read from the feed when a new one is due (floor_now.trucks decides)."""
    import floor_now
    t = floor_now.trucks(now)
    return {"posted": t.get("posted"), "read": t.get("read"), "waiting": t.get("waiting"), "by_dest": t.get("by_dest"),
            "before_cutoff": t.get("before_cutoff"), "error": t.get("error"), "trucks": len(t.get("list") or [])}


def sec_inbound(now, tok):
    import hub_numbers as hn
    day = work_day(now)
    out = {"on_way": [], "not_unloaded": [], "unloaded": []}
    seen = {}
    for kind, name in ((4, "on_way"), (6, "not_unloaded"), (7, "unloaded")):
        rows = (hn.wms(tok, hn.TRUCKS_API, {**hn.window(day), "pageNum": 1, "pageSize": 500, "type": kind}) or {}).get("records") or []
        for t in rows:
            arrived = t.get("actualArrivalTime") or ""
            if name != "on_way" and arrived < (now - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M"):
                continue  # old stuck tasks with a few strays
            row = {"from": (t.get("placeOfOrigin") or "?").replace(".H", ""), "task": t.get("taskCode"),
                   "pcs": int(t.get("waybillCount") or 0), "gaylords": int(t.get("bagCount") or 0),
                   "arrived": arrived[5:16] or None, "eta": (t.get("estimationArrivalTime") or "")[5:16] or None,
                   "unloaded": (t.get("unloadCompleteTime") or "")[5:16] or None}
            # WMS keeps a truck in "on the way" for a while after it arrives, and the arrived row starts with 0
            # packages: one row per task, the arrived one wins and keeps the bigger count (otherwise a truck that
            # is already at the door can still show as "on the way").
            old = seen.get(row["task"])
            if old:
                row["pcs"], row["gaylords"] = max(row["pcs"], old[1]["pcs"]), max(row["gaylords"], old[1]["gaylords"])
                out[old[0]].remove(old[1])
            seen[row["task"]] = (name, row)
            out[name].append(row)
    try:  # ETAs from the hourly report; WMS has none until the truck is close
        rp = json.loads((DATA / "floor-trucks.json").read_text())
        etas = {x["task"]: x["eta"][5:] for x in rp.get("list", [])}
        for t in out["on_way"]:
            t["eta"] = t["eta"] or etas.get(t["task"])
        out["report_post"] = rp.get("posted")
    except Exception:
        pass
    out["on_way"].sort(key=lambda t: t["eta"] or "99")
    return out


def sec_outbound(now, tok):
    import dock_board
    tasks = dock_board.read_tasks(now)
    rows = []
    since = (now - timedelta(hours=18)).strftime("%Y-%m-%d %H:%M")
    for r in tasks.values():
        left = r.get("actualDepartureTime") or ""
        plan = r.get("plannedDepartureTime") or ""
        if (left and left < since) or (not left and plan and plan < (now - timedelta(hours=30)).strftime("%Y-%m-%d %H:%M")):
            continue
        rows.append({"to": r.get("destination"), "task": r.get("taskNo"), "status": r.get("vehicleStatusStr"),
                     "here": (r.get("timeOfArrivalOrigin") or "")[5:16] or None, "plan_leave": plan[5:16] or None,
                     "left": left[5:16] or None, "trailer": r.get("licensePlateNo"), "full_pct": r.get("estimationLoadingRate"),
                     "kind": r.get("transportTypeStr")})
    rows.sort(key=lambda x: x["left"] or x["plan_leave"] or "")
    return {"trucks": rows}


SECTIONS = (("speed", sec_speed), ("sort", sec_sort), ("floor", sec_floor), ("broadcast", sec_broadcast), ("inbound", sec_inbound),
            ("outbound", sec_outbound))
NEEDS_WMS = ("speed", "sort", "inbound", "outbound")


def collect():
    now = now_local()
    try:
        old = json.loads(STATE.read_text())
    except Exception:
        old = {}
    tok = None
    try:
        import chute_alerts as ca
        tok = [ca.wms_token()]
    except Exception as e:
        tok_err = f"WMS login: {e}"[:200]
    state = {"made": now.strftime("%Y-%m-%d %H:%M:%S")}
    for name, fn in SECTIONS:
        t0 = time.time()
        try:
            if tok is None and name in NEEDS_WMS:
                raise RuntimeError(tok_err)
            data = fn(now, tok[0] if name in ("sort", "inbound") and tok else tok)
            state[name] = {"at": now.strftime("%Y-%m-%d %H:%M"), "secs": round(time.time() - t0, 1), "data": data}
        except Exception as e:
            prev = old.get(name) or {}
            state[name] = {**prev, "error": f"{type(e).__name__}: {e}"[:300], "failed_at": now.strftime("%Y-%m-%d %H:%M"),
                           "fails": int(prev.get("fails", 0)) + 1}
            traceback.print_exc()
        else:
            state[name]["fails"] = 0
    state["watch"] = watch(state, old.get("watch") or {}, now_local())
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False))
    tmp.replace(STATE)
    sp = (state.get("speed") or {}).get("data") or {}
    fl = (state.get("floor") or {}).get("data") or {}
    so = (state.get("sort") or {}).get("data") or {}
    bc = ((state.get("broadcast") or {}).get("data") or {}).get("waiting") or {}
    with HISTORY.open("a") as f:
        f.write(json.dumps({"t": state["made"][:16], "per_hour": sp.get("per_hour"), "vendor_a": sp.get("vendor_a"),
                            "vendor_b": sp.get("vendor_b"), "floor": fl.get("in_full"),
                            "left": sum(t["left"] for t in fl.get("trucks", [])) or None,
                            "board_read": fl.get("read"), "broadcast_waiting": (bc.get("trunk", 0) + bc.get("branch", 0)) or None,
                            "sorted_today": so.get("sorted_today")}) + "\n")


def in_hours(hours, now):
    if not hours:
        return True
    start, end = hours
    return start <= now.hour or now.hour < end if start > end else start <= now.hour < end


def file_age_min(path, now):
    """Minutes since the data in a watched file was made: its own "made" stamp when it has one, else the file time."""
    p = DATA / path
    if not p.exists():
        return None
    if path.endswith("unsorted-latest.json"):
        try:
            made = datetime.strptime(str(json.loads(p.read_text()).get("made"))[:16], "%Y-%m-%d %H:%M")
            return int((now - made).total_seconds() // 60)
        except Exception:
            return None
    return int((time.time() - p.stat().st_mtime) // 60)


def watch(state, before, now):
    """Finds what is broken right now and tells the app once per problem (again every 6 h while it stays broken).
    A job that fails quietly is the worst case: its last good number keeps looking current, so every source is
    checked here on each run."""
    problems = {}
    for name, feeds, path, max_min, hours in WATCH:
        if not in_hours(hours, now):
            continue
        age = file_age_min(path, now)
        if age is None:
            problems[name] = f"{name}: no output file (data/{path}); {feeds} unavailable"
        elif age > max_min:
            problems[name] = f"{name}: nothing new for {age // 60} h {age % 60} min (limit {max_min} min); {feeds} are stale"
    for sec, _ in SECTIONS:
        s = state.get(sec) or {}
        if s.get("fails", 0) >= 2:
            problems[f"live:{sec}"] = f"live {sec}: failing since {s.get('failed_at')} ({(s.get('error') or '?')[:90]}); last good {s.get('at') or 'never'}"
    bc = (state.get("broadcast") or {}).get("data") or {}
    if bc.get("error"):
        problems["broadcast"] = f"hourly report: can't read the feed ({bc['error'][:80]})"
    try:  # a job whose last run crashed (launchd's last exit status)
        out = subprocess.run(["/bin/launchctl", "list"], capture_output=True, text=True, timeout=10).stdout
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[2].startswith(JOB_PREFIX) and parts[1] not in ("0", "-") and parts[0] == "-":
                job = parts[2].replace(JOB_PREFIX, "")
                problems[f"job:{job}"] = f"job {job}: last run crashed (exit {parts[1]}); see data/{job}.log"
    except Exception:
        pass
    since = dict(before.get("since") or {})
    alerted = dict(before.get("alerted") or {})
    stamp = now.strftime("%Y-%m-%d %H:%M")
    for key in list(since):
        if key not in problems:
            since.pop(key), alerted.pop(key, None)
    for key, text in problems.items():
        since.setdefault(key, stamp)
        first = datetime.strptime(since[key], "%Y-%m-%d %H:%M")
        last = alerted.get(key) and datetime.strptime(alerted[key], "%Y-%m-%d %H:%M")
        # two runs in a row before the phone hears about it, so one slow run isn't an alert
        if in_hours(ALERT_HOURS, now) and now - first >= timedelta(minutes=4) and (not last or now - last >= timedelta(hours=ALERT_AGAIN_H)):
            try:
                import app_push
                app_push.alert("Hub data broken", f"{text}\nSince {since[key][5:]}. Numbers from it are off until fixed.", key=f"watch-{key}")
                alerted[key] = stamp
            except Exception:
                traceback.print_exc()
    return {"at": stamp, "problems": problems, "since": since, "alerted": alerted}


def k(n):
    """12,340 → 12.3k; under 1,000 the number itself (148, not 0.1k)."""
    n = n or 0
    return str(n) if abs(n) < 1000 else f"{n / 1000:.1f}k".replace(".0k", "k")


def clock(mmdd_hhmm):
    """'11-20 02:58' → '2:58 AM'."""
    try:
        return datetime.strptime(mmdd_hhmm[-5:], "%H:%M").strftime("%-I:%M %p")
    except Exception:
        return mmdd_hhmm or "?"


def mins_ago(stamp, now):
    """Whole minutes since a 'YYYY-MM-DD HH:MM' (or ISO) stamp; None when it can't be read."""
    try:
        return int((now - datetime.strptime(str(stamp).replace("T", " ")[:16], "%Y-%m-%d %H:%M")).total_seconds() // 60)
    except Exception:
        return None


def ago(m):
    return "?" if m is None else (f"{m} min ago" if m < 90 else f"{m // 60} h {m % 60} min ago")


def floor_lines(fl, sp, now):
    """What is left to sort, from the board, split at midnight (tonight's vs next day's). No number at all when the
    board is old."""
    age = mins_ago(fl.get("read"), now)
    if age is None or age > BOARD_MAX_MIN:
        return [f"LEFT TO SORT: UNKNOWN. The floor board's last good run was {str(fl.get('read'))[5:16]} ({ago(age)}), so it is broken. "
                "Do not give any floor / left-to-sort number from it. Get a live count: python3 unsorted_board.py --dry-run"]
    took = f", run took {fl['took_s']} s" if fl.get("took_s") else ""
    lines = [f"LEFT TO SORT (floor board read {str(fl['read'])[11:16]}, {ago(age)}{took}):"]

    def one(t):
        where = ""
        if t["state"] == "not unloaded":
            where = f" NOT UNLOADED ({t['noscan_full']} gaylords, none scanned in, none of it sorted)"
        elif t["state"] == "no scans":
            where = (f" UNLOADED WITHOUT SCANS ({t['noscan_full']} full gaylords never scanned in, but {t.get('sorted', 0):,} of its packages "
                     "are already sorted, so it is being run)")
        elif t["noscan_full"]:
            where = f" ({t['full_gaylords'] - t['noscan_full']} full gaylords on the floor + {t['noscan_full']} never scanned in)"
        else:
            where = f" ({t['full_gaylords']} full gaylords)"
        return f"{t['from']} in {clock(t['arrived'])} {k(t['left'])}{where}"

    pace = sp.get("per_hour") if (sp.get("per_hour") or 0) > 1000 else None
    how = "at this pace"
    if not pace and sp.get("normal"):  # belt stopped or barely moving: time at the normal pace, said as such
        pace, how = sp["normal"], f"at the normal {k(sp['normal'])}/hr (the belt is not running at that now)"
    carried, tonight, later = split_by_night(fl["trucks"], now)
    start = tonight_start(now)
    parts = [(f"carried over = trucks in before 12 AM {start:%-m/%-d} (an earlier night's carry-over, still here)", carried)] if carried else []
    parts += [(f"tonight's = trucks in {start:%-m/%-d} 12 AM to midnight (unsorted at closing = carry-over)", tonight),
              ("trucks in after midnight (next day's, not carry-over)", later)]
    for label, part in parts:
        total = sum(t["left"] for t in part)
        belt = f", {total / pace:.1f} h of belt {how}" if pace and total else ""
        big = [t for t in part if t["left"] >= 300 or t["state"] in ("not unloaded", "no scans")]
        small = [t for t in part if t not in big]
        tail = f"; + {len(small)} truck(s) with under 300 each ({k(sum(t['left'] for t in small))})" if small else ""
        lines.append(f"  {label}: {k(total)}{belt}" + (" — " + "; ".join(one(t) for t in big) + tail if part else ""))
    if fl.get("strays"):
        lines.append(f"  strays on {fl['stray_trucks']} older trucks: {k(fl['strays'])} (leftovers of gaylords already run: rejects, no-reads; not belt freight)")
    return lines


def brief():
    try:
        s = json.loads(STATE.read_text())
    except Exception:
        print("LIVE STATE missing (live_state.py has not run). Nothing below is known.")
        return
    now = now_local()
    made_age = mins_ago(s["made"], now)
    lines = [f"LIVE STATE, collected {s['made'][11:16]} ({ago(made_age)}). Each line names when ITS data was read. "
             "A line that says UNKNOWN or STALE has no current number: get it live. Never fill a gap with an estimate."]
    if made_age is None or made_age > 12:
        lines.append(f"!! BROKEN: live_state.py itself has not run for {ago(made_age)[:-4]}: every WMS line below is that old. Run: python3 live_state.py --check")
    for text in ((s.get("watch") or {}).get("problems") or {}).values():
        lines.append(f"!! BROKEN: {text}")

    def age(sec):
        sec = s.get(sec) or {}
        if not sec.get("at"):
            return "  [NO DATA: " + (sec.get("error") or "?")[:80] + "]"
        m = mins_ago(sec["at"], now)
        if m is None or m > STALE_MIN or sec.get("error"):
            return f"  [STALE: read {ago(m)}" + (f", error: {sec['error'][:80]}" if sec.get("error") else "") + "]"
        return ""

    sp = (s.get("speed") or {}).get("data") or {}
    fl = (s.get("floor") or {}).get("data") or {}
    if fl:
        lines += floor_lines(fl, sp, now)
    else:
        lines.append("LEFT TO SORT: UNKNOWN (no floor board data)." + age("floor"))
    bc = (s.get("broadcast") or {}).get("data") or {}
    w = bc.get("waiting") or {}
    b_age = mins_ago(bc.get("posted"), now)
    if w and b_age is not None and b_age <= BROADCAST_MAX_MIN:
        total = w.get("trunk", 0) + w.get("branch", 0)
        cutoff = bc.get("before_cutoff") or {}
        board_total = sum(t["left"] for t in fl.get("trucks", [])) + (fl.get("strays") or 0) if fl else None
        check = ""
        board_age = mins_ago(fl.get("read"), now) if fl else None
        if board_total is not None and board_age is not None and board_age <= BOARD_MAX_MIN:
            unscanned = sum(t.get("noscan_in_full", 0) for t in fl.get("trucks", []))
            check = f" | board total at {str(fl['read'])[11:16]}: {k(board_total)}" + (f", {k(unscanned)} of it in gaylords never scanned in (the report may not count those)" if unscanned else "")
            if total and not 0.5 <= board_total / total <= 2:
                check += "  !! these two disagree by more than 2x: check before quoting either"
        lines.append(f"hourly report {bc['posted'][11:16]} ({ago(b_age)}): waiting to be bagged {k(total)} = linehaul {k(w.get('trunk'))} + branch {k(w.get('branch'))}"
                     f"; trucks still due before its cutoff: {cutoff.get('trucks', '?')}" + (f" ({k(cutoff.get('pcs'))})" if cutoff.get("trucks") else "") + check)
    elif bc:
        lines.append(f"hourly report: no current post (newest read is from {str(bc.get('posted'))[5:16]}, {ago(b_age)})"
                     + (f" [{bc['error'][:80]}]" if bc.get("error") else ""))
    if sp.get("stopped"):
        lh = sp.get("last_hour") or {}
        lines.append(f"speed: 0. Nothing fed in {sp.get('hour')} so far ({sp.get('minutes')} min): the sorter is stopped"
                     + (f"; last full hour {lh['hour']}: {lh['fed']:,} fed" if lh else "") + f" (normal {k(sp.get('normal'))}/hr){age('speed')}")
    elif sp.get("per_hour") is not None:
        part = f"{sp.get('hour')}, first {sp.get('minutes')} min scaled to an hour" if (sp.get("minutes") or 60) < 60 else f"{sp.get('hour')}, full hour"
        lines.append(f"speed: {k(sp['per_hour'])}/hr ({part}; Vendor A {k(sp.get('vendor_a'))}, Vendor B {k(sp.get('vendor_b'))}, "
                     f"rej {sp.get('rej')}%, normal {k(sp.get('normal'))}){age('speed')}")
    so = (s.get("sort") or {}).get("data") or {}
    if so:
        lines.append(f"sorted today (since 6 AM): {so['sorted_today']:,} pcs, {so['sorted_bags_today']:,} bags{age('sort')}")
        if "wait_pickup" in so:
            lines.append(f"WMS 'expected to sort' {k(so['expected_to_sort'])} = ONLY freight scanned in piece by piece and not sorted yet: "
                         f"pickup {k(so['wait_pickup'])}, transfer {k(so['wait_transfer'])}, customer drop-off {k(so['wait_dropoff'])}. "
                         "Linehaul gaylords are not in it, so it is NOT the floor count.")
    ib = (s.get("inbound") or {}).get("data") or {}
    if ib:
        past = (now - timedelta(minutes=30)).strftime("%m-%d %H:%M")
        cut = tonight_cutoff(now).strftime("%m-%d %H:%M")
        way = [f"{t['from']} {k(t['pcs'])} at {clock(t['eta'])}" + (" (ETA PASSED: may already be here)" if t["eta"] < past else "")
               if t.get("eta") else f"{t['from']} {k(t['pcs'])} (no ETA)"
               for t in ib.get("on_way", [])]
        before = [t for t in ib.get("on_way", []) if t.get("eta") and t["eta"] < cut]
        note = f"; due before midnight: {len(before)} ({k(sum(t['pcs'] for t in before))})" if now.strftime("%m-%d %H:%M") < cut else "; all of these arrive after midnight = next day's freight"
        lines.append("trucks still coming (WMS): " + (", ".join(way) or "none") + f" = {k(sum(t['pcs'] for t in ib.get('on_way', [])))}{note}{age('inbound')}")
        nu = [f"{t['from']} {k(t['pcs']) if t['pcs'] > 300 else '(count not in WMS yet)'} in {clock(t['arrived'])}" for t in ib.get("not_unloaded", [])
              if t["pcs"] > 300 or (t.get("arrived") or "") >= (now - timedelta(hours=3)).strftime("%m-%d %H:%M")]
        if nu:
            lines.append("WMS arrived, unload not finished: " + ", ".join(nu))
        recent = sorted(ib.get("unloaded", []), key=lambda t: t.get("arrived") or "")[-6:]
        un = [f"{t['from']} {k(t['pcs'])} in {clock(t['arrived'])} unloaded {clock(t['unloaded'])}" for t in recent]
        if un:
            lines.append("unloaded recently: " + ", ".join(un))
    ob = (s.get("outbound") or {}).get("data") or {}
    if ob:
        cut = (now - timedelta(hours=10)).strftime("%m-%d %H:%M")
        late = (now - timedelta(hours=2)).strftime("%m-%d %H:%M")
        here = [t for t in ob["trucks"] if t.get("here") and not t.get("left") and t["here"] >= cut]
        gone = [t for t in ob["trucks"] if t.get("left")][-8:]
        lines.append("outbound at dock now (WMS: arrived, no departure scan): "
                     + (", ".join(f"{t['to']} (here {clock(t['here'])}, plan {clock(t['plan_leave'])}"
                                  + (", 2+ h past plan: may have left unscanned" if (t.get("plan_leave") or "99") < late else "") + ")" for t in here) or "none")
                     + age("outbound"))
        lines.append("outbound left recently: " + (", ".join(f"{t['to']} {clock(t['left'])}" for t in gone) or "none"))
    print("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brief", action="store_true")
    ap.add_argument("--check", action="store_true", help="collect now, then print the brief (use after fixing something)")
    a = ap.parse_args()
    if a.brief:
        brief()
    else:
        collect()
        if a.check:
            brief()


if __name__ == "__main__":
    main()
