#!/usr/bin/env python3
"""Label station, the ops host's part: anyone with the scan link sends a package number (web/src/labelstation.js),
this finds the box (bag) it is in and hands back that bag's label, and the print agent by the label printer
(agent/label_agent.py) prints it.

Per request (read-only in the WMS, 2 reads, or 3 for a near-empty bag):
1. Parcel route: the bag the WMS has the package in now (each code from the photo is tried, the hub's PKG number first).
2. Bag search for that bag: route, chute, pieces, made by.
3. Only when the bag has under TINY_PCS pieces (an extra press of the chute's print button, see press_chain): that
   chute's bags of the last hours, so the label printed is the real box's, with the box's whole count.
A bag that is open AND still the newest on its chute (nobody pressed the button) gets no label: the phone says to
press the button once. Open in the WMS but a newer bag started on that chute = the button was pressed: it prints.

Runs all the time (launchd / systemd keep-alive), long-polling the app worker (one request per ~25 s while idle).
Without HUBOPS_APP_URL (demo mode) there is no worker to poll: --demo looks packages up and writes the print queue
the worker would hand the agent to data/labels/queue.json.

    python3 hubops/label_station.py              the loop
    python3 hubops/label_station.py --once       one long poll, then stop
    python3 hubops/label_station.py --lookup PKG…    just the lookup, printed here (sends nothing)
    python3 hubops/label_station.py --demo [PKG…]    lookups -> data/labels/queue.json (no codes: a sample of
                                                     tonight's boxes), for the print agent's offline dry run:
                                                     python3 agent/label_agent.py --queue data/labels/queue.json --dry-run
    python3 hubops/label_station.py --render BG…     draw a test label picture to data/labels/label-test.png

    HUBOPS_LABEL_PRINT_KEY   the print page's key (/p/<key>), used to draw label pictures in a headless browser
"""
import argparse
import base64
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import app_push
import chute_alerts
import hubconfig

LOG = hubconfig.DATA_DIR / "label-station.log"
QUEUE = hubconfig.DATA_DIR / "labels" / "queue.json"
DPI_SCALE = 203 / 96  # the label printer prints 203 dots an inch; the page lays the label out at 96 px an inch
ROUTE_API = chute_alerts.MASK_LIST_API  # parcel -> the bag it is in now
BAGS_API = chute_alerts.BAGS_API
HUB_ID = hubconfig.HUB_ID
SORTER_BAG = chute_alerts.SORTER_BAG
TINY_PCS = 20  # a bag under this many packages is "near-empty"
CHAIN = timedelta(minutes=15)  # near-empty bags closed this soon after a real box on the same chute belong to it
LONG_LOOK_BACK = timedelta(hours=14)  # a full box sits open 2-7 hours, sometimes more
FMT = "%Y-%m-%d %H:%M:%S"
_render = {}


def log(line):
    with hubconfig.data_path(LOG.name).open("a") as f:
        f.write(f"{datetime.now():%m-%d %H:%M:%S} {line}\n")


def local(ms):
    """WMS milliseconds -> hub-local datetime (naive, like every other time here)."""
    return datetime.fromtimestamp(ms / 1000, hubconfig.TIME_ZONE).replace(tzinfo=None)


def fmt_ms(ms):
    return local(ms).strftime(FMT) if ms else ""


# ---------- bags on a chute (no WMS calls here, so the tests use them as is) ----------

def chute_of(bag):
    return str(bag.get("packageNo") or "").rsplit("-", 1)[-1].lstrip("0")


def _made(b):
    return b.get("optCreateTime") or b.get("optFinishStatusTime") or 0


def newest_on_chute(bag, bags):
    """Whether `bag` is the last bag opened on its chute: its label is the chute's label for the box filling now."""
    chute = chute_of(bag)
    same = [b for b in list(bags) + [bag] if b.get("bagType") == SORTER_BAG and chute_of(b) == chute]
    return bool(same) and max(same, key=_made)["packageNo"] == bag["packageNo"]


def press_chain(bag, bags):
    """The bags that are clearly ONE box with `bag`: a real box (TINY_PCS+ packages) and the near-empty bags closed
    right after it on the same chute, each within CHAIN of the real box closing, with no other bag in between
    (e.g. 400, then 1, 10 and 3: the box's label printed, then more presses of the button). Anything else stands
    alone: two real boxes are never one, small bags with no real box just before them are not joined, and a small
    bag more than CHAIN after the box is its own. Returns the box first; [bag] when it stands alone."""
    if not bag.get("optFinishStatusTime"):
        return [bag]
    chute, limit = chute_of(bag), CHAIN.total_seconds() * 1000
    same = sorted({b["packageNo"]: b for b in bags if b.get("bagType") == SORTER_BAG and b.get("optFinishStatusTime")
                   and chute_of(b) == chute}.values(), key=lambda b: b["optFinishStatusTime"])
    i = next((k for k, b in enumerate(same) if b["packageNo"] == bag["packageNo"]), None)
    if i is None:
        return [bag]
    small = lambda b: (b.get("waybillCount") or 0) < TINY_PCS  # noqa: E731
    j = i
    while j >= 0 and small(same[j]):  # back to the real box this bag came after
        j -= 1
    if j < 0:
        return [bag]
    box = same[j]
    chain = [box]
    for b in same[j + 1:]:
        if not small(b) or b["optFinishStatusTime"] - box["optFinishStatusTime"] > limit:
            break
        chain.append(b)
    return chain if any(b["packageNo"] == bag["packageNo"] for b in chain) else [bag]


def item(keep, chain, **extra):
    """The label's facts for the box `keep` (+ the near-empty bags that belong to it)."""
    route = keep.get("exchangeBureauCode") or keep.get("destinAreaCode") or keep.get("destinCenterName") or ""
    return {"bag": keep["packageNo"], "route": route, "dest": keep.get("destinCenterName") or "",
            "chute": chute_of(keep), "pcs": sum(b.get("waybillCount") or 0 for b in chain),
            "date": keep.get("createTime") or "", "origin": keep.get("startCenterName") or hubconfig.HUB_CODE,
            "operator": keep.get("createByName") or "",
            "merge": [b["packageNo"] for b in chain if b["packageNo"] != keep["packageNo"]], **extra}


# ---------- the WMS ----------

def read_bags(box, now, back):
    """The hub's bags opened in the last `back` before `now` (hub-local datetimes)."""
    body = {"packageNoList": [], "createBeginTime": (now - back).strftime(FMT), "createEndTime": now.strftime(FMT),
            "startCenterId": HUB_ID, "pageSize": 1000}
    bags = []
    for page in range(1, 10):
        got = chute_alerts.wms(box, BAGS_API, {**body, "pageNum": page}).get("list") or []
        bags += got
        if len(got) < 1000:
            break
    return bags


def pressed_since(bag, box, now):
    """Whether a newer bag was started on this bag's chute, i.e. the button WAS pressed even though the WMS never
    marked this bag finished (a big box left unfinished + a new bag seconds later is common). Only the chute's
    newest bag is really still filling."""
    if bag.get("bagType") != SORTER_BAG or not bag.get("optCreateTime"):
        return False
    back = min(now - local(bag["optCreateTime"]) + timedelta(minutes=5), LONG_LOOK_BACK)
    return not newest_on_chute(bag, read_bags(box, now, back))


def lookup(codes, now=None, box=None):
    """(label dict, None) or (None, why not)."""
    now = now or hubconfig.now_local()
    box = box or [chute_alerts.wms_token()]
    res = chute_alerts.wms(box, ROUTE_API, {"waybillNoList": codes, "pageNum": 1, "pageSize": 20})
    rows = {r.get("waybillNo"): r for r in res.get("list") or []}
    found = next(((c, rows[c]) for c in codes if c in rows), None)
    if not found and rows:
        found = next(iter(rows.items()))
    if not found:
        return None, f"The WMS doesn't know package {codes[0]}"
    code, row = found
    bag_no = row.get("packageNo")
    if not bag_no:
        return None, f"Package {code} isn't in a bag in the WMS yet (not sorted?)"
    got = chute_alerts.wms(box, BAGS_API, {"packageNoList": [bag_no], "startCenterId": HUB_ID,
                                           "pageNum": 1, "pageSize": 10}).get("list") or []
    bag = next((b for b in got if b.get("packageNo") == bag_no and b.get("startCenterId") == HUB_ID), None)
    if not bag:
        return None, f"Package {code} is in {bag_no}, which isn't a {hubconfig.HUB_CODE} bag (not sorted here yet?)"
    if bag.get("optCheckOutTime"):
        return None, f"Bag {bag_no} already left on a truck ({fmt_ms(bag['optCheckOutTime'])})"
    keep, chain, note = bag, [bag], ""
    if not bag.get("optFinishStatusTime") and not pressed_since(bag, box, now):
        # Nobody pressed the chute button. A copy of the open bag's label would leave the chute filling the same bag
        # into the next box (2 boxes, 1 label). Pressing once closes it and prints the real label; if that label
        # doesn't come out, the bag is closed and scanning again prints it here.
        return None, (f"La caja sigue abierta (rampa {chute_of(bag)} · {bag.get('waybillCount') or 0} pcs). "
                      "Si no has presionado el botón, presiónalo UNA vez. Si ya lo presionaste, espera 5 minutos y vuelve a escanear. · "
                      "Box still open: if you didn't press the button, press it ONCE. If you already did, wait 5 minutes and scan again.")
    if (bag.get("waybillCount") or 0) < TINY_PCS and bag.get("bagType") == SORTER_BAG and bag.get("optFinishStatusTime"):
        closed = local(bag["optFinishStatusTime"])
        recent = read_bags(box, closed + timedelta(minutes=1), LONG_LOOK_BACK)
        chain = press_chain(bag, recent + [bag])
        keep = max(chain, key=lambda x: x.get("waybillCount") or 0)
        if keep is not bag:
            note = f"Package was in near-empty bag {bag_no}; label is for the real box"
    label = item(keep, chain, pkg=code)
    label["date"] = fmt_ms(keep.get("optCreateTime")) or label["date"]
    label["note"] = note
    return {k: label[k] for k in ("bag", "route", "dest", "chute", "pcs", "date", "origin", "operator", "merge", "pkg", "note")}, None


# ---------- the label's picture ----------

def render_png(label):
    """The label as a 4x6 picture (812x1218), drawn by the print page itself in a headless browser, so the print
    agent only prints pictures and every label looks the same. None if drawing fails (or no worker / print key):
    the agent then prints the facts with the printer's own fonts."""
    key = os.environ.get("HUBOPS_LABEL_PRINT_KEY")
    if app_push.demo() or not key:
        return None
    try:
        if "page" not in _render:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(viewport={"width": 480, "height": 640}, device_scale_factor=DPI_SCALE).new_page()
            page.goto(f"{app_push.APP}/p/{key}/?render=1", wait_until="load", timeout=30000)
            page.wait_for_function("window.renderLabel && window.ZXingWASM", timeout=30000)
            _render.update(pw=pw, browser=browser, page=page)
        page = _render["page"]
        page.evaluate("x => renderLabel(x)", label)
        png = page.locator("#sheet .label").screenshot(type="png", timeout=15000)
        return "data:image/png;base64," + base64.b64encode(png).decode()
    except Exception:  # noqa: BLE001 - no picture is fine, the agent prints the facts
        traceback.print_exc()
        for k in ("browser", "pw"):
            try:
                _render[k].close() if k == "browser" else _render[k].stop()
            except Exception:  # noqa: BLE001
                pass
        _render.clear()
        return None


# ---------- the worker ----------

def call(path, body=None, timeout=40):
    req = urllib.request.Request(app_push.APP + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": app_push.USER_AGENT,
                                          "X-Push-Key": app_push.push_key()})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read().decode() or "null")


def answer(job):
    """(label or None, why not) for one request, never raising: a WMS problem is said on the phone."""
    try:
        return lookup(job["codes"])
    except Exception as e:  # noqa: BLE001 - WMS down / login expired: say so on the phone instead of hanging
        traceback.print_exc()
        return None, f"WMS lookup failed ({type(e).__name__}). Try again in a few minutes."


def handle(job):
    label, why = answer(job)
    if label:
        img = render_png(label)
        if img:
            label["img"] = img
        call(f"/label-jobs/{job['id']}", {"status": "done", "label": label})
        log(f"#{job['id']} {job['codes'][0]} -> {label['bag']} {label['route']} {label['pcs']} pcs {label['note']}")
    else:
        call(f"/label-jobs/{job['id']}", {"status": "error", "msg": why})
        log(f"#{job['id']} {job['codes'][0]} -> no label: {why}")


def once(wait=25):
    for job in call(f"/label-jobs?wait={wait}", timeout=wait + 20) or []:
        handle(job)


def sample_codes(now=None, box=None):
    """One package from each kind of box the scan link meets: a near-empty bag pressed right after a real box, a
    closed box, and a box still filling on its chute."""
    now = now or hubconfig.now_local()
    box = box or [chute_alerts.wms_token()]
    bags = [b for b in read_bags(box, now, LONG_LOOK_BACK) if b.get("bagType") == SORTER_BAG and not b.get("optCheckOutTime")]
    tiny = [b for b in bags if b.get("optFinishStatusTime") and (b.get("waybillCount") or 0) < TINY_PCS and len(press_chain(b, bags)) > 1]
    closed = [b for b in bags if b.get("optFinishStatusTime") and (b.get("waybillCount") or 0) >= TINY_PCS]
    filling = [b for b in bags if not b.get("optFinishStatusTime") and newest_on_chute(b, bags) and b.get("waybillCount")]
    codes = []
    for group in (tiny, closed, filling):
        for b in group[:1]:
            got = chute_alerts.wms(box, "/api/bags/parcels", {"packageNo": b["packageNo"], "deptId": HUB_ID, "pageNum": 1, "pageSize": 1})
            codes += [r["waybillNo"] for r in (got.get("list") or [])[:1]]
    return codes


def demo_queue(codes):
    """Demo mode: each code looked up and queued the way the worker's /next hands labels to the print agent."""
    jobs = []
    for i, code in enumerate(codes, 1):
        label, why = answer({"codes": [code]})
        if label:
            jobs.append({"id": i, "label": label})
            print(f"#{i} {code} -> {label['bag']} · {label['route']} · chute {label['chute']} · {label['pcs']} pcs"
                  + (f" · {label['note']}" if label["note"] else ""))
        else:
            print(f"#{i} {code} -> no label: {why}")
    path = hubconfig.data_path("labels", QUEUE.name)
    path.write_text(json.dumps(jobs, ensure_ascii=False, indent=1))
    print(f"{len(jobs)} label(s) queued in {path.relative_to(hubconfig.REPO)}")
    return jobs


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--once", action="store_true")
    p.add_argument("--lookup", nargs="+")
    p.add_argument("--demo", nargs="*", metavar="CODE")
    p.add_argument("--render")
    a = p.parse_args()
    if a.render:
        img = render_png({"bag": a.render, "route": "VCT01-004", "chute": "087", "origin": hubconfig.HUB_CODE,
                          "date": "2026-11-19 02:30:00"})
        if not img:
            print("no picture: needs HUBOPS_APP_URL, HUBOPS_LABEL_PRINT_KEY and playwright", file=sys.stderr)
            return 2
        path = hubconfig.data_path("labels", "label-test.png")
        path.write_bytes(base64.b64decode(img.split(",", 1)[1]))
        print(f"wrote {path}")
        return 0
    if a.lookup:
        print(json.dumps(lookup([c.upper() for c in a.lookup]), ensure_ascii=False, indent=1))
        return 0
    if a.demo is not None:
        demo_queue([c.upper() for c in a.demo] or sample_codes())
        return 0
    if app_push.demo():
        print("HUBOPS_APP_URL is not set: nothing to poll. Use --lookup or --demo.", file=sys.stderr)
        return 2
    if a.once:
        once()
        return 0
    while True:
        try:
            once()
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"{datetime.now():%H:%M:%S} app unreachable: {e}", file=sys.stderr)
            time.sleep(15)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            time.sleep(15)


if __name__ == "__main__":
    sys.exit(main())
