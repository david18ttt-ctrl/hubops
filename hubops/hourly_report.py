#!/usr/bin/env python3
"""Hourly sort report: WMS bag list -> per-destination table (CSV) -> picture -> team chat.

Every hour, a few minutes after the hour, the hub's bags made since 6 AM are read from the WMS and counted per
destination: packages and bags in each hour, the running total since 6 AM, each station's progress against its
capacity, and which stations are closed (past their cutoff, or at 100% of capacity). The table is saved to
data/hourly-report.csv, drawn as a picture (data/hourly-report.png) and posted to the team chat.

Nothing stale gets posted: a short read of the bag list, a bag list that stopped moving while the sorter should be
running, a saved table that doesn't add up to what the WMS returned, or a picture older than this run's read all
stop the post, and the failure goes to --alert-chat instead.

    python3 hourly_report.py --dry-run          read the WMS, save the table and picture; post nothing
    python3 hourly_report.py --image-only       no WMS: redraw the picture from the last saved table
    python3 hourly_report.py --chat <chat>      full run, posting to that chat
        --alert-chat <chat>   where failures go
        --skip-if-done        do nothing if this hour was already posted (and nothing 7-11 AM)

Counting rules:
- The report covers the operating day: 6 AM to 6 AM, named by the day it starts. It keeps last night's numbers
  until 11 AM (hubconfig.RESET_HOUR), since people still use them after 6.
- Packages are the bag's waybill count as the WMS shows it at read time. A bag's packages count in the hour the
  bag was made (the bag list has no per-package time). Bags the WMS lists twice during a read count once.
  Customers' pre-alert batches (bag type "Customer Forecast") are in the bag list but weren't bagged here, so they
  are left out.
- "This hour" is the hour that ends at the report's end time (the 22:00 report shows 21:00-22:00).
- Progress = packages since 6 AM / the station's capacity; the stations' total uses the sum of the capacities.
- A destination the report doesn't list still counts in the grand total, and an alert names it.

The picture is HTML drawn by headless Chromium (python playwright), else matplotlib, else only the HTML file is
written (and the post goes out as text). HUBOPS_REPORT_RENDERER=playwright|matplotlib|html forces one.
"""
import argparse
import csv
import html
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import chat_api
import hubconfig
import wms_client

BAGS_API = "/api/bags/search"
PAGE_SIZE = 5000  # one page holds the whole day, so new bags can't shift between pages
READ_TRIES = 3
FORECAST_BAG = 3  # bagType: a customer's pre-alert batch, not a bag made here

# The stations in the report's order: (name, cutoff, capacity in packages; None = no limit).
STATIONS = [
    ("CXO01", "3:00", 18000),
    ("SGR01", "3:00", 6000),
    ("AUS01", "20:30", 17000),
    ("ACT01", "20:30", 9000),
    ("VCT01", "20:30", 26000),
    ("BRO01", "21:30", 15000),
    ("HRL01", "22:00", 8000),
    ("SJT01", "23:30", 20000),
    ("GGG01", "0:00", 12000),
    ("TXK01", "0:00", None),
    ("SHV01", "1:00", 13000),
    ("AEX01", "2:00", 9000),
]
HUBS = ["PHX.H", "STL.H", "MSP.H", "SEA.H", "BOS.H", "PIT.H", "ABQ.H", "SAN.H"]
NO_DESTINATION = "(none)"

NO_POSTS_HOURS = range(7, 12)  # nothing changes between the 6 AM post and noon
STALE_AFTER = timedelta(minutes=45)  # no new bag for this long while the sorter runs = the bag list stopped
SORTING_FROM, SORTING_UNTIL = "17:00", "5:00"  # when the sorter always runs (operating-day clock)
MAX_AGE = timedelta(minutes=20)  # a read older than this at posting time is not posted

OUT_DIR = hubconfig.DATA_DIR
CSV_NAME = "hourly-report.csv"
META_NAME = "hourly-report-meta.json"
HTML_NAME = "hourly-report.html"
PNG_NAME = "hourly-report.png"
POSTED_NAME = "hourly-report-posted.txt"


class ReportProblem(Exception):
    pass


def out(name):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / name


# ---------- the operating day ----------

def operating_day(now):
    return hubconfig.operating_day(now)


def day_start(day):
    return datetime(day.year, day.month, day.day, hubconfig.WORK_DAY_START_HOUR)


def operating_minutes(hour, minute):
    return hubconfig.operating_minutes(hour, minute)


def clock_minutes(text):
    hour, minute = (int(part) for part in str(text).split(":")[:2])
    return operating_minutes(hour, minute)


def end_hour(now):
    """The report's end time as an hour: between 6 and 11 AM last night is over, so it stays at 6."""
    return 6 if 6 <= now.hour < hubconfig.RESET_HOUR else now.hour


def hour_label(now):
    return f"{end_hour(now)}:00"


def this_hour_slot(now):
    """The hour slot (0 = 6-7 AM ... 23 = 5-6 AM) that ends at the report's end time."""
    return (end_hour(now) - hubconfig.WORK_DAY_START_HOUR - 1) % 24


def slot_name(slot):
    return f"{(hubconfig.WORK_DAY_START_HOUR + slot) % 24:02d}:00"


def row_is_closed(now, cutoff, progress):
    """Past its cutoff time, or at 100% of capacity even before the cutoff."""
    if isinstance(progress, (int, float)) and progress >= 1:
        return True
    try:
        cut = clock_minutes(cutoff)
    except ValueError:
        return False
    if 6 <= now.hour < hubconfig.RESET_HOUR:  # the morning after: every cutoff of the night has passed
        return True
    return operating_minutes(now.hour, now.minute) >= cut


def sorter_should_run(now):
    at = operating_minutes(now.hour, now.minute)
    return clock_minutes(SORTING_FROM) <= at < clock_minutes(SORTING_UNTIL)


# ---------- the WMS ----------

def read_bags(day, now, token=None):
    """One read of the day's bags made here, newest first, each bag once."""
    start = day_start(day)
    end = min(start + timedelta(days=1), now)
    rows, seen, page_num, total = [], set(), 1, 0
    while True:
        body = {"packageNoList": [], "createBeginTime": f"{start:%Y-%m-%d %H:%M:%S}",
                "createEndTime": f"{end:%Y-%m-%d %H:%M:%S}", "startCenterId": hubconfig.HUB_ID,
                "pageNum": page_num, "pageSize": PAGE_SIZE}
        data = wms_client.call_data(BAGS_API, body, token=token) or {}
        records = data.get("list") or []
        for record in records:
            if record.get("packageNo") not in seen:
                seen.add(record.get("packageNo"))
                rows.append(record)
        total = int(data.get("total") or 0)
        if len(rows) >= total or not records or page_num * PAGE_SIZE >= total:
            break
        page_num += 1
    return {"rows": rows, "total": total}


def fetch_bags(day, now, token=None):
    """The day's bags. A short read is tried again; still short, the report would be wrong, so it stops."""
    bags = {"rows": [], "total": 0}
    for _ in range(READ_TRIES):
        bags = read_bags(day, now, token)
        if len(bags["rows"]) >= bags["total"]:
            return bags
    raise ReportProblem(f"expected {bags['total']} bags from the WMS, got {len(bags['rows'])}")


def parse_time(text):
    return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")


# ---------- the table ----------

def tally(rows, day):
    """{destination: {"bags": [24 slots], "pcs": [24 slots]}} from the bag list."""
    start = day_start(day)
    out_ = {}
    for row in rows:
        if row.get("bagType") == FORECAST_BAG:
            continue
        made = parse_time(row["createTime"])
        slot = int((made - start).total_seconds() // 3600)
        if not 0 <= slot < 24:
            continue
        dest = str(row.get("destinCenterName") or "").strip() or NO_DESTINATION
        counts = out_.setdefault(dest, {"bags": [0] * 24, "pcs": [0] * 24})
        counts["bags"][slot] += 1
        counts["pcs"][slot] += int(row.get("waybillCount") or 0)
    return out_


def progress_text(progress):
    return "N/A" if progress is None else f"{progress * 100:.2f}%"


def build_table(counts, now, read_at, bag_count):
    """The report as rows plus totals; every number comes from `counts`."""
    slot = this_hour_slot(now)
    # Hours in the CSV: 6 AM through the hour now running, so each row's hours add up to its total.
    elapsed = int((now - day_start(operating_day(now))).total_seconds() // 3600) + 1
    hours = max(slot + 1, min(24, elapsed))

    def line(section, name, cutoff, capacity):
        c = counts.get(name) or {"bags": [0] * 24, "pcs": [0] * 24}
        done = sum(c["pcs"])
        progress = done / capacity if capacity else None
        closed = section == "Station" and row_is_closed(now, cutoff, progress)
        return {"section": section, "destination": name, "cutoff": cutoff or "N/A",
                "capacity": capacity, "hour_bags": c["bags"][slot], "hour_pcs": c["pcs"][slot],
                "bags": sum(c["bags"]), "pcs": done, "progress": progress, "closed": closed,
                "by_hour_pcs": c["pcs"][:hours], "by_hour_bags": c["bags"][:hours]}

    stations = [line("Station", name, cutoff, capacity) for name, cutoff, capacity in STATIONS]
    hubs = [line("Linehaul", name, None, None) for name in HUBS]
    listed = {name for name, _, _ in STATIONS} | set(HUBS)
    unlisted = {name: sum(c["pcs"]) for name, c in counts.items() if name not in listed}
    others = [line("Unlisted", name, None, None) for name in sorted(unlisted)]

    def total(rows_, capacity=None):
        out_ = {k: sum(r[k] for r in rows_) for k in ("hour_bags", "hour_pcs", "bags", "pcs")}
        out_["capacity"] = capacity
        out_["progress"] = out_["pcs"] / capacity if capacity else None
        return out_

    capacity = sum(c for _, _, c in STATIONS if c)
    table = {
        "day": operating_day(now).isoformat(), "now": now.strftime("%Y-%m-%d %H:%M:%S"),
        "read_at": read_at.strftime("%Y-%m-%d %H:%M:%S"), "window": f"6:00 - {hour_label(now)}",
        "this_hour": f"{slot_name(slot)}-{slot_name(slot + 1)}", "slots": [slot_name(s) for s in range(hours)],
        "wms_bags": bag_count, "stations": stations, "hubs": hubs, "unlisted": others,
        "station_total": total(stations, capacity), "hub_total": total(hubs),
    }
    table["grand_total"] = total(stations + hubs + others)
    return table, unlisted


# ---------- the CSV ----------

CSV_FIXED = ["section", "destination", "cutoff", "capacity", "this_hour_bags", "this_hour_pcs", "bags_since_6",
             "pcs_since_6", "progress", "closed"]


def csv_rows(table):
    slots = table["slots"]
    header = CSV_FIXED + [f"pcs {s}" for s in slots] + [f"bags {s}" for s in slots]
    rows = [header]
    for r in table["stations"] + table["hubs"] + table["unlisted"]:
        rows.append([r["section"], r["destination"], r["cutoff"], "" if r["capacity"] is None else r["capacity"],
                     r["hour_bags"], r["hour_pcs"], r["bags"], r["pcs"],
                     "" if r["progress"] is None else f"{r['progress']:.4f}", "yes" if r["closed"] else "",
                     *r["by_hour_pcs"], *r["by_hour_bags"]])
    for label, t in (("Stations total", table["station_total"]), ("Linehaul total", table["hub_total"]),
                     ("Grand total", table["grand_total"])):
        rows.append(["Total", label, "", "" if t["capacity"] is None else t["capacity"], t["hour_bags"],
                     t["hour_pcs"], t["bags"], t["pcs"], "" if t["progress"] is None else f"{t['progress']:.4f}", ""])
    return rows


def save_table(table):
    """Writes the CSV and its read time (data/hourly-report-meta.json). Returns the CSV path."""
    path = out(CSV_NAME)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="") as f:
        csv.writer(f).writerows(csv_rows(table))
    tmp.replace(path)
    out(META_NAME).write_text(json.dumps(table, indent=1))
    return path


def check_saved(path, want_pcs, want_bags):
    """The saved CSV's grand total must be what the WMS returned, or the report isn't posted."""
    with Path(path).open(newline="") as f:
        rows = list(csv.DictReader(f))
    grand = next((r for r in rows if r["section"] == "Total" and r["destination"] == "Grand total"), None)
    detail_pcs = sum(int(r["pcs_since_6"]) for r in rows if r["section"] != "Total")
    detail_bags = sum(int(r["bags_since_6"]) for r in rows if r["section"] != "Total")
    got = [int(grand["pcs_since_6"]) if grand else None, detail_pcs, int(grand["bags_since_6"]) if grand else None, detail_bags]
    if got != [want_pcs, want_pcs, want_bags, want_bags]:
        raise ReportProblem(f"saved table never matched the WMS: table {got}, WMS {[want_pcs, want_bags]}")
    for r in rows:
        hourly = [int(v) for c, v in r.items() if c.startswith("pcs ") and v not in ("", None)]
        if r["section"] != "Total" and sum(hourly) != int(r["pcs_since_6"]):
            raise ReportProblem(f"{r['destination']}: its hours add up to {sum(hourly)}, its total is {r['pcs_since_6']}")


def load_table():
    """The last saved table (for --image-only)."""
    try:
        return json.loads(out(META_NAME).read_text())
    except (OSError, ValueError) as e:
        raise ReportProblem(f"no saved table to draw ({e}); run without --image-only first") from None


def stale_reason(rows, now):
    """Why the bag list looks stale, or None. Only while the sorter always runs: then a bag is made every few
    minutes, so a long gap means the WMS stopped answering with new data."""
    if not sorter_should_run(now):
        return None
    made = [parse_time(r["createTime"]) for r in rows if r.get("bagType") != FORECAST_BAG and r.get("createTime")]
    newest = max(made) if made else None
    if newest is None or now - newest > STALE_AFTER:
        when = f"{newest:%m/%d %H:%M}" if newest else "never"
        return f"the WMS bag list looks stale: newest bag made {when}, the sorter should be running at {now:%H:%M}"
    return None


# ---------- the picture ----------

def k(n):
    return f"{n:,}"


def cell(value, rowspan=1, colspan=1, cls=""):
    text = "" if value is None else html.escape(str(value)).replace("\n", "<br>")
    return f'<td rowspan="{rowspan}" colspan="{colspan}" class="{cls}">{text}</td>'


def progress_cell(progress, rowspan=1, cls=""):
    text = progress_text(progress)
    bar = ""
    if progress is not None:
        width = max(min(progress * 100, 100), 0.5)
        bar = f'<div class="bar" style="width:calc((100% - 4px) * {width / 100})"></div>'
    return f'<td rowspan="{rowspan}" class="progress {cls}">{bar}<span>{html.escape(text)}</span></td>'


COLUMN_WIDTHS = [76, 84, 66, 76, 72, 74, 80, 86, 92, 92]
HEADERS = ["Group", "Destination", "Cutoff", "Capacity", "Bags this hour", "Pcs this hour", "Bags since 6 AM",
           "Pcs since 6 AM", "Progress", "Total"]


def report_html(t):
    """The report as one HTML table, laid out like the team's sheet: stations, linehaul, grand total."""
    day = datetime.strptime(t["day"], "%Y-%m-%d")
    read_at = parse_time(t["read_at"])
    cols = "".join(f'<col style="width:{w}px">' for w in COLUMN_WIDTHS)
    rows = ['<tr class="h">' + cell(f"{day:%Y/%m/%d}", colspan=2, cls="plain") + cell(f"{day:%A}", colspan=2) +
            cell("Report window", colspan=2) + cell(t["window"], colspan=2, cls="plain") +
            cell("Sort progress", colspan=2) + "</tr>",
            '<tr class="h">' + "".join(cell(x) for x in HEADERS) + "</tr>"]
    stations = t["stations"]
    for i, r in enumerate(stations):
        green = " green" if r["closed"] else ""
        first = cell("Stations", rowspan=len(stations), cls="orange b") if i == 0 else ""
        total = cell(k(t["station_total"]["pcs"]), rowspan=len(stations) + 1, cls="orange big") if i == 0 else ""
        rows.append("<tr>" + first + cell(r["destination"], cls="b" + green) + cell(r["cutoff"], cls=green) +
                    cell("N/A" if r["capacity"] is None else k(r["capacity"]), cls=green) +
                    "".join(cell(k(r[x]), cls=green) for x in ("hour_bags", "hour_pcs", "bags", "pcs")) +
                    progress_cell(r["progress"]) + total + "</tr>")
    s = t["station_total"]
    rows.append('<tr class="orange">' + cell("Stations total", colspan=3, cls="b") + cell(k(s["capacity"] or 0)) +
                "".join(cell(k(s[x])) for x in ("hour_bags", "hour_pcs", "bags", "pcs")) +
                progress_cell(s["progress"], cls="white") + "</tr>")
    hubs = t["hubs"] + t["unlisted"]
    hub_pcs = t["hub_total"]["pcs"] + sum(u["pcs"] for u in t["unlisted"])
    for i, r in enumerate(hubs):
        numbers = "".join(cell(k(r[x])) for x in ("hour_bags", "hour_pcs", "bags", "pcs"))
        if i == 0:  # the merged cells: group, cutoff + capacity (none for linehaul), progress, total
            rows.append("<tr>" + cell("Linehaul", rowspan=len(hubs), cls="orange b") + cell(r["destination"], cls="b") +
                        cell("N/A", rowspan=len(hubs), colspan=2) + numbers + cell("", rowspan=len(hubs)) +
                        cell(k(hub_pcs), rowspan=len(hubs), cls="orange big") + "</tr>")
        else:
            rows.append("<tr>" + cell(r["destination"], cls="b") + numbers + "</tr>")
    g = t["grand_total"]
    rows.append('<tr class="orange grand">' + cell("Linehaul + stations total", colspan=6, cls="b") +
                cell(k(g["pcs"]), colspan=4) + "</tr>")
    rows.append('<tr class="foot">' + cell(f"{hubconfig.HUB_NAME} · this hour = {t['this_hour']} · WMS bag list read "
                                           f"{read_at:%m/%d %H:%M} · {k(t['wms_bags'])} bags", colspan=10) + "</tr>")
    return f"""<!doctype html><meta charset="utf-8"><title>Hourly sort report</title><style>
    body {{ margin: 0; background: #fff; font-family: Arial, "Liberation Sans", Helvetica, sans-serif; }}
    #report {{ display: inline-block; padding: 10px; background: #fff; }}
    table {{ border-collapse: collapse; table-layout: fixed; }}
    tr {{ height: 26px; }}
    td {{ border: 1px solid #000; text-align: center; padding: 0 3px; font-size: 14px; line-height: 18px;
          color: #000; font-weight: normal; }}
    .h td, .b {{ font-weight: bold; }}
    .h td.plain {{ font-weight: normal; }}
    .h td, .orange, tr.orange td {{ background: #ffd3a1; }}
    .big {{ font-size: 20px; }}
    tr.grand {{ height: 45px; }}
    tr.grand td {{ font-size: 26px; line-height: 32px; }}
    tr.foot td {{ border: 0; text-align: left; font-size: 12px; color: #444; padding-top: 6px; }}
    .progress {{ position: relative; }}
    tr.orange td.white {{ background: #fff; }}
    .progress span {{ position: relative; }}
    .bar {{ position: absolute; left: 2px; top: 2px; bottom: 2px; background: #4e83ec; }}
    td.green {{ background: #33cc2a; }}
    </style><div id="report"><table><colgroup>{cols}</colgroup>{"".join(rows)}</table></div>"""


def text_report(t):
    """The report as plain text, for a post when no picture could be drawn."""
    lines = [f"Sort progress {t['day']} {t['window']} (WMS read {t['read_at'][11:16]})"]
    for r in t["stations"]:
        lines.append(f"{r['destination']} {k(r['pcs'])} (+{k(r['hour_pcs'])}) {progress_text(r['progress'])}"
                     + (" closed" if r["closed"] else ""))
    lines.append(f"Stations {k(t['station_total']['pcs'])} · linehaul {k(t['hub_total']['pcs'])} · "
                 f"total {k(t['grand_total']['pcs'])}")
    return "\n".join(lines)


def renderer_order():
    forced = (os.environ.get("HUBOPS_REPORT_RENDERER") or "").strip().lower()
    return [forced] if forced else ["playwright", "matplotlib", "html"]


def render_playwright(page_html, png):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception:  # noqa: BLE001 - no bundled Chromium: try the installed Chrome
            browser = p.chromium.launch(channel="chrome", headless=True)
        try:
            page = browser.new_page(device_scale_factor=2)
            page.set_content(page_html)
            page.locator("#report").screenshot(path=str(png))
        finally:
            browser.close()


def render_matplotlib(t, png):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    body, colors = [], []
    for r in t["stations"]:
        body.append(["Stations", r["destination"], r["cutoff"], "N/A" if r["capacity"] is None else k(r["capacity"]),
                     k(r["hour_bags"]), k(r["hour_pcs"]), k(r["bags"]), k(r["pcs"]), progress_text(r["progress"]), ""])
        colors.append(["#ffd3a1"] + ["#33cc2a" if r["closed"] else "#ffffff"] * 8 + ["#ffd3a1"])
    s = t["station_total"]
    body.append(["Stations total", "", "", k(s["capacity"] or 0), k(s["hour_bags"]), k(s["hour_pcs"]), k(s["bags"]),
                 k(s["pcs"]), progress_text(s["progress"]), k(s["pcs"])])
    colors.append(["#ffd3a1"] * 10)
    for r in t["hubs"] + t["unlisted"]:
        body.append(["Linehaul", r["destination"], "N/A", "N/A", k(r["hour_bags"]), k(r["hour_pcs"]), k(r["bags"]),
                     k(r["pcs"]), "", ""])
        colors.append(["#ffd3a1"] + ["#ffffff"] * 8 + ["#ffd3a1"])
    body.append(["Total", "", "", "", "", "", "", "", "", k(t["grand_total"]["pcs"])])
    colors.append(["#ffd3a1"] * 10)
    fig, ax = plt.subplots(figsize=(11, 0.32 * (len(body) + 3)))
    ax.axis("off")
    table = ax.table(cellText=body, colLabels=HEADERS, cellColours=colors, colColours=["#ffd3a1"] * 10,
                     cellLoc="center", loc="upper center")
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    ax.set_title(f"{t['day']}  ·  sort progress {t['window']}  ·  WMS read {t['read_at'][5:16]}", fontsize=11)
    fig.savefig(png, dpi=160, bbox_inches="tight")
    plt.close(fig)


def render_image(t):
    """Draws the report. Returns (path, how): the PNG and the renderer used, or the HTML file and "html" when no
    renderer worked. The HTML is always written next to it."""
    page_html = report_html(t)
    html_path = out(HTML_NAME)
    html_path.write_text(page_html)
    png = out(PNG_NAME)
    errors = []
    for how in renderer_order():
        if how == "html":
            return html_path, "html"
        try:
            tmp = png.with_name(png.stem + ".tmp.png")
            if how == "playwright":
                render_playwright(page_html, tmp)
            elif how == "matplotlib":
                render_matplotlib(t, tmp)
            else:
                raise ReportProblem(f"unknown renderer {how}")
            tmp.replace(png)
            return png, how
        except ReportProblem:
            raise
        except Exception as e:  # noqa: BLE001 - missing library or browser: try the next way
            errors.append(f"{how}: {str(e).splitlines()[0] if str(e) else type(e).__name__}")
    raise ReportProblem("couldn't draw the report: " + "; ".join(errors))


# ---------- posting ----------

def check_fresh(table, picture, how, now):
    """Refuses to post numbers older than MAX_AGE, or a picture that wasn't drawn from this run's read."""
    read_at = parse_time(table["read_at"])
    if now - read_at > MAX_AGE:
        raise ReportProblem(f"the WMS read is from {read_at:%H:%M}, older than {int(MAX_AGE.total_seconds() // 60)} min")
    if how != "html" and datetime.fromtimestamp(Path(picture).stat().st_mtime) < read_at - timedelta(seconds=1):
        raise ReportProblem("the picture is older than this run's WMS read")


def post_report(chat, picture, how, table, now, note=None):
    # A post a few minutes after the hour is that hour's report, so it shows 17:00, not 17:02.
    shown = now.replace(minute=0) if now.minute < 10 else now
    caption = shown.strftime("%m/%d   %H:%M") + (f"\n{note}" if note else "")
    if how == "html":
        chat_api.send_text(f"{caption}\n{text_report(table)}", to=chat)
    else:
        chat_api.send_image(picture, caption, to=chat)


def posted_key(now):
    return now.strftime("%Y-%m-%d %H")


def already_posted(now):
    try:
        return out(POSTED_NAME).read_text().strip() == posted_key(now)
    except OSError:
        return False


def alert(chat, text):
    if chat:
        chat_api.send_text(text, to=chat)


# ---------- one run ----------

def build(now, token=None):
    """Reads the WMS and saves the table. Returns (table, unlisted, csv path). The read time is `now`."""
    day = operating_day(now)
    rows = fetch_bags(day, now, token)["rows"]
    reason = stale_reason(rows, now)
    if reason:
        raise ReportProblem(reason)
    counts = tally(rows, day)
    pcs = sum(sum(c["pcs"]) for c in counts.values())
    bags = sum(sum(c["bags"]) for c in counts.values())
    table, unlisted = build_table(counts, now, now, bags)
    path = save_table(table)
    check_saved(path, pcs, bags)
    return table, unlisted, path


def shown_path(path):
    path = Path(path)
    return path.relative_to(hubconfig.REPO) if path.is_relative_to(hubconfig.REPO) else path


def main(argv=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="read and draw, no posting")
    parser.add_argument("--image-only", action="store_true", help="no WMS, no posting: redraw the last saved table")
    parser.add_argument("--chat", help="chat to post the report to")
    parser.add_argument("--alert-chat", help="chat for failure alerts")
    parser.add_argument("--skip-if-done", action="store_true", help="do nothing if this hour was already posted")
    args = parser.parse_args(argv)

    fixed_clock = now is not None  # tests and replays give the time; a real run reads the clock again to post
    now = now or hubconfig.now_local()
    try:
        if args.image_only:
            picture, how = render_image(load_table())
            print(f"Picture: {shown_path(picture)} ({how})")
            return 0
        if args.skip_if_done and now.hour in NO_POSTS_HOURS:
            print("No posts between the 6 AM post and noon.")
            return 0
        if args.skip_if_done and already_posted(now):
            print("This hour was already reported.")
            return 0
        if not args.dry_run and not args.chat:
            raise ReportProblem("no --chat given, not posting")
        table, unlisted, path = build(now, wms_client.default_token())
        g = table["grand_total"]
        print(f"WMS: {k(table['wms_bags'])} bags, {k(g['pcs'])} pcs since 6 AM "
              f"(this hour {table['this_hour']}: {k(g['hour_pcs'])} pcs) · read {table['read_at'][11:16]}")
        print(f"Table: {shown_path(path)}")
        picture, how = render_image(table)
        print(f"Picture: {shown_path(picture)} ({how})")
        if args.dry_run:
            if unlisted:
                print("Not listed in the report: " + ", ".join(f"{n} ({k(c)} pcs)" for n, c in unlisted.items()))
            return 0
        check_fresh(table, picture, how, now if fixed_clock else hubconfig.now_local())
        post_report(args.chat, picture, how, table, now)
        out(POSTED_NAME).write_text(posted_key(now))
        if unlisted:
            listing = ", ".join(f"{name} ({count:,} waybills)" for name, count in unlisted.items())
            alert(args.alert_chat, f"The report doesn't list these destinations: {listing}. "
                                   "Add them to STATIONS or HUBS in hourly_report.py.")
        print("Posted.")
        return 0
    except (wms_client.LoginExpired, wms_client.WmsError, chat_api.ChatError, ReportProblem, RuntimeError, OSError,
            ValueError, KeyError, TypeError, IndexError) as e:
        print(f"FAILED: {e}", file=sys.stderr)
        try:
            alert(args.alert_chat, f"Hourly sort report {now:%H:%M} failed, nothing posted: {e}")
        except (chat_api.ChatError, OSError) as alert_error:
            print(f"Alert failed too: {alert_error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
