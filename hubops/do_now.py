"""Do now: the one list of things someone on the floor has to physically do right now.

Every job that finds a physical task puts it here instead of buzzing the phone itself:
  chute_alerts.py  stuck chutes (gone by itself once the chute is quiet), 850+ bags, no-rule spikes
  lost_labels.py   a bag that probably lost its label
  dock_board.py    a truck at the dock that still needs boxes pulled (gone once it's full or left)
  floor_coach.py   the hour's floor moves (replaced each hour)
A task = one action (who / where / how many) + the one number behind it. Information that needs nobody to act
stays in the Alerts tab without a buzz (scripts/webpush/flush.js).

`python3 do_now.py sync` (every minute) sends the list to the app when it changed (one storage write per change)
and buzzes the phone once for tasks it hasn't seen yet. Done presses in the app come back through GET /app-done
and hide that task until its job drops it.
`python3 do_now.py show` prints the list.
"""
import fcntl
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

import hubconfig

FILE = hubconfig.DATA_DIR / "do-now.json"
LOCK = hubconfig.DATA_DIR / "do-now.lock"
LOG = hubconfig.DATA_DIR / "do-now.log"
DONE_KEEP = timedelta(hours=14)  # a Done press hides that task for the rest of the shift
CLEARED_KEEP = 40  # the last tasks that went away, shown greyed under the list
FMT = "%Y-%m-%d %H:%M:%S"


def _empty():
    return {"tasks": {}, "done": {}, "cleared": [], "synced_sig": None, "done_seen": []}


@contextmanager
def _store():
    """The list on disk, locked: several jobs write to it at once."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.touch(exist_ok=True)
    with LOCK.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = {**_empty(), **json.loads(FILE.read_text())}
        except (OSError, ValueError):
            data = _empty()
        yield data
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        tmp.replace(FILE)


def _now(now=None):
    return (now or datetime.now()).strftime(FMT)


def _task(source, tid, do, why, rank, ttl_min, push, extra, now):
    t = {"id": f"{source}|{tid}", "source": source, "do": str(do).strip()[:300], "why": str(why or "").strip()[:400],
         "rank": rank, "since": _now(now), "updated": _now(now), "push": bool(push), "pushed": False}
    if ttl_min:
        t["until"] = (now + timedelta(minutes=ttl_min)).strftime(FMT)
    if extra:
        t["extra"] = extra
    return t


def _merge(data, new, now):
    """Adds or updates one task. An update keeps its first time and whether it was pushed: no second buzz."""
    old = data["tasks"].get(new["id"])
    if old:
        new["since"], new["pushed"] = old["since"], old["pushed"]
        if old["do"] == new["do"] and old["why"] == new["why"]:
            new["updated"] = old["updated"]
    data["tasks"][new["id"]] = new


def _drop(data, tid, why, now):
    t = data["tasks"].pop(tid, None)
    if t:
        data["cleared"] = (data["cleared"] + [{"id": tid, "do": t["do"], "since": t["since"], "gone": _now(now), "how": why}])[-CLEARED_KEEP:]


def put(source, tid, do, why="", rank=50, ttl_min=None, push=True, extra=None, now=None):
    """One task. Same source + tid again = the same task, updated quietly."""
    now = now or datetime.now()
    with _store() as data:
        _merge(data, _task(source, tid, do, why, rank, ttl_min, push, extra, now), now)


def replace(source, tasks, now=None):
    """Everything `source` has open right now: tasks = [dict(tid=, do=, why=, rank=, ttl_min=, push=, extra=)].
    Its tasks not in the list are gone (fixed / done / no longer true)."""
    now = now or datetime.now()
    with _store() as data:
        keep = set()
        for t in tasks:
            new = _task(source, t["tid"], t["do"], t.get("why"), t.get("rank", 50), t.get("ttl_min"), t.get("push", True),
                        t.get("extra"), now)
            keep.add(new["id"])
            _merge(data, new, now)
        for tid in [k for k, t in data["tasks"].items() if t["source"] == source and k not in keep]:
            _drop(data, tid, "cleared", now)


def clear(source, tid, now=None):
    with _store() as data:
        _drop(data, f"{source}|{tid}", "cleared", now or datetime.now())


def tidy(data, presses, now):
    """Expired tasks go; Done presses ([{id, at}] from the app) hide their task for the shift."""
    stamp = _now(now)
    for tid in [k for k, t in data["tasks"].items() if t.get("until") and t["until"] < stamp]:
        _drop(data, tid, "expired", now)
    for p in presses or []:
        tid = str(p.get("id") or "")
        if tid and tid not in data["done"]:
            data["done"][tid] = stamp
            _drop(data, tid, "done", now)
    cut = (now - DONE_KEEP).strftime(FMT)
    data["done"] = {k: v for k, v in data["done"].items() if v >= cut}
    for tid in [k for k in data["tasks"] if k in data["done"]]:  # a job put it back after the press
        del data["tasks"][tid]


def ranked(data):
    return sorted(data["tasks"].values(), key=lambda t: (t["rank"], t["since"]))


def view(data, now):
    """What the app's Do now tab gets."""
    return {"updated": _now(now), "tasks": [{k: t.get(k) for k in ("id", "source", "do", "why", "since", "updated", "extra")}
                                             for t in ranked(data)],
            "cleared": list(reversed(data["cleared"][-12:]))}


def to_push(data):
    """Tasks not buzzed yet. Marks them buzzed. Returns (title, body) or None."""
    new = [t for t in ranked(data) if t["push"] and not t["pushed"]]
    for t in data["tasks"].values():
        if t["push"]:
            t["pushed"] = True
    if not new:
        return None
    if len(new) == 1:
        return new[0]["do"][:120], new[0]["why"][:280]
    return f"{len(new)} things to do", "\n".join("• " + t["do"][:110] for t in new[:5])


# ---------- the app ----------

def _app(path, body=None):
    """GET (body None) or POST to the phone app; demo mode keeps it under data/app/ (app_push.request)."""
    import app_push
    return app_push.request(path, body)


def log(line):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now():%m/%d %H:%M:%S} {line}\n")


def sync(now=None):
    now = now or datetime.now()
    try:
        presses = _app("/app-done") or []
    except Exception as e:  # noqa: BLE001 - the list still goes out; presses are read next minute
        presses = []
        log(f"done presses not read: {type(e).__name__}: {e}")
    with _store() as data:
        seen = set(data["done_seen"])
        fresh = [p for p in presses if f"{p.get('id')}|{p.get('at')}" not in seen]
        tidy(data, fresh, now)
        data["done_seen"] = [f"{p.get('id')}|{p.get('at')}" for p in presses][-100:]
        out = view(data, now)
        sig = json.dumps([out["tasks"], out["cleared"]], sort_keys=True, ensure_ascii=False)
        changed = sig != data["synced_sig"]
        if changed:
            try:
                _app("/app-data/donow", out)
                data["synced_sig"] = sig
            except Exception as e:  # noqa: BLE001
                log(f"app not updated: {type(e).__name__}: {e}")
                return
        buzz = to_push(data)
    if buzz:
        import app_push
        try:
            app_push.notify(buzz[0], buzz[1], tag="donow")
            log(f"buzz: {buzz[0]} | {buzz[1][:150]!r}")
        except Exception as e:  # noqa: BLE001
            log(f"buzz failed: {type(e).__name__}: {e}")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "show"
    if cmd == "sync":
        sync()
    elif cmd == "show":
        with _store() as data:
            for t in ranked(data):
                print(f"[{t['rank']}] {t['do']}\n      {t['why']}  ({t['id']}, since {t['since'][11:16]})")
            if not data["tasks"]:
                print("(nothing to do)")
    else:
        sys.exit("usage: do_now.py sync|show")


if __name__ == "__main__":
    main()
