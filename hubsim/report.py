"""The hourly sort report feed, as the hub's reporting job posts it at :16 every hour.

The simulator writes it to data/hourly-report.json ({"text": ...}, the last two posts, newest last; the path can be
changed with HUBOPS_REPORT_FEED) so floor_now.py and live_state.py have their second, independent count.
"""
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import model as m

FEED = Path(os.environ.get("HUBOPS_REPORT_FEED") or Path(__file__).resolve().parent.parent / "data" / "hourly-report.json")
POST_MINUTE = 16


def post_time(now):
    """The newest post's time: :16 of this hour, or of the last one before :16."""
    t = now.replace(minute=POST_MINUTE, second=0, microsecond=0)
    return t if t <= now else t - timedelta(hours=1)


def k(n):
    return f"{n:,}"


def post(sim, at):
    """One post's text, with the numbers as they were at `at`."""
    day = m.work_day(at)
    start = datetime(day.year, day.month, day.day, 6)
    cutoff = datetime(day.year, day.month, day.day, 22)
    trunk, branch, by_trunk, by_branch = 0, 0, {}, {}
    for n in sim.nights():
        for p in n.parcels:
            here = p.arrived if p.kind == "pickup" else (p.truck["arrive"] if p.truck else None)
            if not here or here > at or (p.sorted_at and p.sorted_at <= at):
                continue
            if p.kind == "pickup":
                branch += 1
                by_branch[p.dest] = by_branch.get(p.dest, 0) + 1
            else:
                trunk += 1
                by_trunk[p.dest] = by_trunk.get(p.dest, 0) + 1
    coming = sorted((t for t in sim.inbound_trucks() if t["depart"] <= at < t["arrive"]), key=lambda t: t["eta"])
    due = [t for t in coming if t["eta"] < cutoff]
    pcs = lambda t: sum(len(g["parcels"]) for g in t["gaylords"])  # noqa: E731
    lines = [f"{m.HUB} sort report",
             f"{start:%m-%d %H:%M}~{start + timedelta(days=1):%m-%d %H:%M} CT  Updated: {at:%Y-%m-%d %H:%M}",
             "Waiting to bag", "Linehaul", f"{k(trunk)}  Branch", k(branch),
             "Due before cutoff today", f"{len(due)} trucks  {k(sum(pcs(t) for t in due))} pcs",
             "Bagged this hour", "⏳ Waiting to bag", "🚛 Linehaul:"]
    for dest, n in sorted(by_trunk.items(), key=lambda x: -x[1])[:8]:
        lines += [dest, ":", k(n)]
    lines.append("🚚 Branch:")
    for dest, n in sorted(by_branch.items(), key=lambda x: -x[1])[:6]:
        lines += [dest, ":", k(n)]
    lines.append(f"🚛 Due to arrive ({len(coming)} trucks)")
    for t in coming:
        lines.append(f"{t['task']}  {k(pcs(t))}  {t['origin']}-{m.HUB}({t['depart']:%H:%M})  ETA: {m.fmt(t['eta'])} (CT)")
    return "\n".join(lines)


def write_feed(sim, now=None, path=FEED):
    """Writes the last two posts (newest last). Returns the newest post's time."""
    at = post_time(now or sim.now())
    text = post(sim, at - timedelta(hours=1)) + "\n" + post(sim, at)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"text": text}, ensure_ascii=False))
    tmp.replace(path)
    return at


def keep_writing(sim, path=FEED):
    """A daemon thread that writes the feed now and again after every :16."""
    def loop():
        last = None
        stop = threading.Event()
        while not stop.wait(0 if last is None else 30):
            at = post_time(sim.now())
            if at != last:
                last = write_feed(sim, path=path)

    threading.Thread(target=loop, daemon=True, name="hourly-report").start()
