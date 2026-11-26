#!/usr/bin/env python3
"""Floor coach: once an hour during the night shift, look at everything the tools can see and say what to DO.

The floor's data shows where the night is losing time, but nobody has a minute to read it all. A typical case:
the hub chutes (049-056) stuck for up to 3 hours with ~6,000 boxes circling while the station chutes had no stuck
chute at all, and the hub chutes planned with 6 pallet workers against 35 on the other chutes: moving pallet
workers over would have helped. This job turns the hour's numbers into that kind of move.

Each run writes a snapshot of the last hour (stuck chutes by area, feed stations against their own normal, sorter
speed, what is left to sort, inbound trucks still coming, dock pulls, label errors, what was said in the sorting
team's chat) to data/coach/, then one LLM call (prompts/coach.md as the system prompt) turns it into at most 4
moves. The moves replace the coach's tasks on the phone app's task list (do_now.py) and go out as one alert; an
hour with nothing worth doing sends nothing. Read-only everywhere.

    python3 hubops/floor_coach.py              the last hour (scheduled at :08 past each hour, 5 PM-6 AM)
    python3 hubops/floor_coach.py --dry-run    print the snapshot and the moves, send nothing
    python3 hubops/floor_coach.py --demo       no API call: print the exact request it would send and a canned
                                               example answer (also what happens with no ANTHROPIC_API_KEY)
    python3 hubops/floor_coach.py --snapshot-only
    python3 hubops/floor_coach.py --at "2026-11-25 22:08"   as if it were then (the log and chat windows move; the
                                                            feeder hour is whatever is saved, so best for tonight)

The LLM: the Messages API over HTTPS (urllib, no SDK). ANTHROPIC_API_KEY is the key; HUBOPS_COACH_MODEL picks the
model (default claude-opus-5-5); HUBOPS_COACH_API_URL overrides the endpoint (tests, a proxy).
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import hubconfig

HERE = Path(__file__).resolve().parent
DATA = hubconfig.DATA_DIR
OUT_DIR = DATA / "coach"  # snapshot.md (what the model reads) + history.json (moves already given)
PROMPT = hubconfig.REPO / "prompts" / "coach.md"
WINDOW = timedelta(minutes=65)
ACTIVE_FROM, ACTIVE_UNTIL = 17, 7  # runs at 5 PM ... 6 AM
SORT_CHAT = os.environ.get("HUBOPS_SORT_CHAT", "sorting team")  # the sorting team's chat in the chat feed
CHAT_FEED = "chat-inbox.jsonl"  # team chat messages, one JSON line each (whatever relays the chat writes it)
LIVE_MAX_MIN = 12  # live_state.py runs every 5 min; older than this, its watchdog list is not current

API_URL = os.environ.get("HUBOPS_COACH_API_URL") or "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 16000
API_TIMEOUT = 300
MAX_MOVES = 4
# Models that take server-side refusal fallbacks: a safety decline is re-run on a fallback model inside the call.
FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5", "claude-opus-5", "claude-fable-5-2", "claude-fable-5-1")
FALLBACK_BETA = "server-side-fallback-2026-07-01"


# ---------- where a chute is on the floor ----------

def area_of(chute):
    """The headcount sheet's areas: Zone 2 001-048, Zone 2 049-069 (the hub chutes PHX.H 049 ... SAN.H 056 and
    the station chutes next to them), Zone 1 188-215 (the hub chutes' mirror side) and Zone 1's other chutes."""
    try:
        c = int(chute)
    except (TypeError, ValueError):
        return "?"
    if 1 <= c <= 48:
        return "Zone 2 001-048"
    if 49 <= c <= 69:
        return "Zone 2 049-069"
    if 188 <= c <= 215:
        return "Zone 1 188-215"
    return "Zone 1 other chutes"


def group_of(where):
    """'VCT01-004' -> 'VCT01', 'PHX.H' -> 'PHX.H (hub)'."""
    where = where or ""
    if where.endswith(".H"):
        return f"{where} (hub)"
    return where.split("-")[0] or "?"


# ---------- the last hour, from the files the other jobs keep ----------

STUCK_RE = r"([A-Z0-9.\-]+) \(chute (\d+)\) · (STILL )?stuck (\d+) min[^|]*?· (\d+) boxes going around"
FIXED_RE = r"✓ ([A-Z0-9.\-]+) \(chute (\d+)\) · fixed"


def _log_lines(now, lo):
    """chute_alerts.py's log (data/chute-alerts.log, whole nights): (time, line) from lo to now."""
    try:
        lines = (DATA / "chute-alerts.log").read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        m = re.match(r"(\d\d)/(\d\d) (\d\d):(\d\d) ", line)
        if not m or " sent:" in line:
            continue
        t = datetime(now.year, int(m[1]), int(m[2]), int(m[3]), int(m[4]))
        if t > now + timedelta(days=1):  # a December line read in January
            t = t.replace(year=now.year - 1)
        if lo <= t <= now:
            yield t, line


def stuck_from_log(now):
    """Stuck chutes in the last WINDOW. {(where, chute): [max minutes, max boxes circling, "fixed" or False]}"""
    out = {}
    for _, line in _log_lines(now, now - WINDOW):
        for where, ch, _still, mins, boxes in re.findall(STUCK_RE, line):
            v = out.setdefault((where, ch), [0, 0, False])
            v[0], v[1] = max(v[0], int(mins)), max(v[1], int(boxes))
        for where, ch in re.findall(FIXED_RE, line):
            if (where, ch) in out:
                out[(where, ch)][2] = "fixed"
    return out


def stuck_text(now):
    rows = stuck_from_log(now)
    if not rows:
        return "No chute was stuck in the last hour."
    by_area = defaultdict(list)
    for (where, ch), (mins, boxes, fixed) in rows.items():
        by_area[area_of(ch)].append((boxes, mins, where, ch, fixed))
    lines = []
    for area, v in sorted(by_area.items(), key=lambda kv: -sum(x[0] for x in kv[1])):
        lines.append(f"- {area}: {len(v)} chutes stuck, {sum(x[0] for x in v):,} boxes circling at worst")
        for boxes, mins, where, ch, fixed in sorted(v, reverse=True)[:6]:
            lines.append(f"  - chute {ch} {group_of(where)}: stuck up to {mins} min, {boxes:,} boxes circling"
                         + (" (fixed since)" if fixed == "fixed" else ""))
    return "\n".join(lines)


def tonight_stuck(now):
    """Stuck minutes per route group since 4 PM: where trouble keeps coming back."""
    start = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.hour < 16:
        start -= timedelta(days=1)
    rows = {}
    for _, line in _log_lines(now, start):
        for where, ch, _, mins, _boxes in re.findall(STUCK_RE, line):
            rows[(where, ch)] = max(rows.get((where, ch), 0), int(mins))
    grp = defaultdict(int)
    for (where, ch), mins in rows.items():
        grp[f"{group_of(where)} [{area_of(ch)}]"] += mins
    top = sorted(grp.items(), key=lambda kv: -kv[1])[:8]
    return "\n".join(f"- {g}: {m} stuck-minutes" for g, m in top) or "none"


def feeders(now):
    """The newest hour of feed-station numbers (feeder_pace.py, :02 past each hour)."""
    try:
        hours = json.loads((DATA / "feeder-app.json").read_text())["hours"]
    except (OSError, ValueError, KeyError):
        return "No feed-station numbers."
    if not hours:
        return "No feed-station numbers."
    h = hours[-1]
    if h.get("updated", "") < (now - timedelta(minutes=80)).strftime("%Y-%m-%d %H:%M:%S"):
        return f"No feed-station numbers for the last hour (newest is {h.get('hour')})."
    lines = [f"Hour {h['hour']} (read {str(h.get('updated'))[11:16]}): sorter fed {h.get('fed', 0):,} "
             f"(normal {h.get('normal', 0):,}), rejects {h.get('rej')}%"]
    st = h.get("stations") or []
    slow = [s for s in st if s.get("normal") and s["normal"] >= 150 and s["fed"] < 0.7 * s["normal"]]
    fast = [s for s in st if s.get("normal") and s["normal"] >= 150 and s["fed"] > 1.3 * s["normal"]]
    high = [s for s in st if s.get("rej") is not None and s["rej"] >= 4 and s["rej"] >= 1.6 * (s.get("rej_normal") or 2.7)]

    def one(s):
        rej = "?" if s.get("rej") is None else s["rej"]
        return f"{s['s']} ({s['fed']}/hr vs normal {s['normal']}, rejects {rej}% vs normal {s.get('rej_normal')}%)"
    lines.append("Slow (under 70% of that station's normal): " + (", ".join(one(s) for s in slow) or "none"))
    lines.append("Fast (over 130%): " + (", ".join(one(s) for s in fast) or "none"))
    every = [s["s"] for s in st if s.get("every_night")]
    lines.append("High rejects vs that station's normal: " + (", ".join(one(s) for s in high) or "none")
                 + (" — 'every night' stations: " + ", ".join(every) if every else ""))
    if h.get("text"):
        lines.append("The hourly check said: " + h["text"].replace("\n", " | "))
    return "\n".join(lines)


def last_lines(name, pattern, n=3):
    """The last n lines of a job's log under data/ that match pattern."""
    try:
        lines = (DATA / name).read_text(errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if re.search(pattern, line)][-n:]


def unsorted_text():
    """What is left to sort, from the board when its count is fresh; a dead board says so instead of old counts
    (a coach fed a morning's count all night, as if it were current, gives moves for freight that isn't there)."""
    import live_state as ls
    now = ls.now_local()
    try:
        lines = ls.floor_lines(ls.sec_floor(now, None), {}, now)
    except Exception as e:  # noqa: BLE001
        return f"no data (floor board unreadable: {type(e).__name__})"
    runs = last_lines("unsorted.log", r"sent: \d+ trucks", 4) if "UNKNOWN" not in lines[0] else []
    return "\n".join(lines + (["Last board runs (time, trucks, packages not sorted):"] + [f"- {x}" for x in runs] if runs else []))


def speed_text(now):
    """This hour's sorter pace, read now from the WMS sorter feed (floor_now.speed, the same numbers as the brief)."""
    try:
        import floor_now
        sp = floor_now.speed(now)
    except Exception as e:  # noqa: BLE001 - the WMS down must not stop the coach
        return f"unknown (sorter feed not readable: {type(e).__name__})"
    read = f"read {now:%-I:%M %p}"
    if sp.get("stopped"):
        lh = sp.get("last_hour") or {}
        return (f"{read}: nothing fed in {sp.get('hour')} so far ({sp.get('minutes')} min): the sorter is stopped"
                + (f"; last full hour {lh['hour']}: {lh['fed']:,} fed" if lh else "") + f" (normal {sp.get('normal', 0):,}/hr)")
    part = (f"{sp.get('hour')}, first {sp.get('minutes')} min scaled to an hour" if (sp.get("minutes") or 60) < 60
            else f"{sp.get('hour')}, full hour")
    return (f"{read}: {sp.get('per_hour', 0):,}/hr ({part}) · Vendor A seats {sp.get('vendor_a', 0):,} · "
            f"Vendor B seats {sp.get('vendor_b', 0):,} · rejects {sp.get('rej')}% · normal {sp.get('normal', 0):,}/hr"
            + (f" · fed since 4 PM {sp['night']:,}" if sp.get("night") else ""))


def inbound_text(now):
    """Linehaul trucks on the way and arrived-not-unloaded, read now from the WMS truck dashboard."""
    try:
        import hub_numbers as hn
        import live_state as ls
        ib = ls.sec_inbound(now, hn.wms_token())
    except Exception as e:  # noqa: BLE001
        return f"unknown (truck dashboard not readable: {type(e).__name__})"
    way = [f"{t['from']} {t['pcs']:,} pcs / {t['gaylords']} gaylords, ETA {ls.clock(t['eta']) if t.get('eta') else 'not known'}"
           for t in ib.get("on_way", [])]
    here = [f"{t['from']} {t['pcs']:,} pcs, in {ls.clock(t['arrived'] or '')}" for t in ib.get("not_unloaded", [])]
    return (f"Read {now:%-I:%M %p}.\n- still coming: " + ("; ".join(way) or "none")
            + "\n- arrived, unload not finished: " + ("; ".join(here) or "none"))


def broken_text(now):
    """Dead data sources, from live_state.py's watchdog: the coach must not build a move on a number from one.
    When live_state itself hasn't run lately, its file-age checks are made here (read-only, no alerts)."""
    try:
        import live_state as ls
    except Exception:  # noqa: BLE001
        return "unknown (live state unreadable)"
    try:
        state = json.loads(ls.STATE.read_text())
        age = ls.mins_ago(state.get("made"), now)
    except Exception:  # noqa: BLE001
        state, age = None, None
    if state is not None and age is not None and age <= LIVE_MAX_MIN:
        problems = (state.get("watch") or {}).get("problems") or {}
        return "\n".join(f"- {p}" for p in problems.values()) or "none"
    lines = [f"- live state not collected for {ls.ago(age)}" if state else "- live state never collected"]
    for name, feeds, path, max_min, hours in ls.WATCH:
        if not ls.in_hours(hours, now):
            continue
        a = ls.file_age_min(path, now)
        if a is None:
            lines.append(f"- {name}: no output file (data/{path}); {feeds} unavailable")
        elif a > max_min:
            lines.append(f"- {name}: nothing new for {a // 60} h {a % 60} min (limit {max_min} min); {feeds} are stale")
    return "\n".join(lines)


def dock_text(now):
    got = last_lines("dock.log", r"sent: \d+ trucks", 3)
    path = DATA / "dock_state.json"
    try:
        said = json.loads(path.read_text()).get("said", {})
        written = datetime.fromtimestamp(path.stat().st_mtime)
        where = {"dock": "at a dock door", "here": "arrived, not docked yet"}
        pulls = [f"truck {i} ({where.get(v.get('state'), v.get('state'))}): {v.get('pull')} boxes still to pull out to it"
                 for i, v in enumerate(said.values(), 1)]
    except (OSError, ValueError):
        pulls, written = [], None
    out = "\n".join(f"- {x}" for x in got)
    if pulls:
        out += (f"\n- trucks short of boxes (the loading board's alerts, written {written:%-I:%M %p}; no door numbers here): "
                + "; ".join(pulls))
    return out.strip() or "no data (the loading board has not saved a run)"


def labels_text():
    try:
        d = json.loads((DATA / "label-errors.json").read_text())
    except (OSError, ValueError):
        return "no data"
    worst = sorted(d.get("chutes", []), key=lambda c: -len(c["bags"]))[:5]
    sh = d.get("shift") or ["?", "?"]
    return (f"Company count this shift: {sh[0]} flagged of {sh[1]} bags · {d.get('open')} still open · "
            f"newest card {d.get('last_card')}\nChutes with the most open flagged bags: "
            + ", ".join(f"{c['chute']} {c.get('where') or ''} ({len(c['bags'])})" for c in worst))


def headcount_text():
    """Tonight's planned people per area (data/headcount-plan.json: [{area, plan, company}])."""
    try:
        plan = json.loads((DATA / "headcount-plan.json").read_text())
    except (OSError, ValueError):
        return "no plan saved (the usual plan is in the rules)"
    return "\n".join(f"- {p['area']}: {p['plan']} planned" + (f" ({p['company']})" if p.get("company") else "") for p in plan)


def chat_text(now):
    """The sorting team's chat and messages that mention the reader, last WINDOW. Text only; cards and pictures
    noted. Read from data/chat-inbox.jsonl: {"at": "YYYY-mm-dd HH:MM:SS", "chat", "from" (a role), "text",
    "type": "text" | "card" | "picture", "mentions_me": bool}, one message per line."""
    try:
        lines = (DATA / CHAT_FEED).read_text(errors="replace").splitlines()
    except OSError:
        return f"- (team chat not readable: no data/{CHAT_FEED})"
    lo, hi = (now - WINDOW).strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")
    out = []
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        at = str(m.get("at") or "")
        if not lo <= at <= hi or not (m.get("chat") == SORT_CHAT or m.get("mentions_me")):
            continue
        where = m.get("chat") if m.get("chat") == SORT_CHAT else f"{m.get('chat')}, mentions me"
        who = f"{m['from']}: " if m.get("from") else ""
        kind = m.get("type") or "text"
        if kind == "card":
            body = f"[bot card] {str(m.get('text') or '')[:80]}"
        elif kind == "picture":
            body = "[picture]"
        else:
            body = str(m.get("text") or "")[:400]
        out.append(f"- {at[11:16]} {where}: {who}{body}")
    return "\n".join(out) or "nothing"


def earlier_moves(now):
    try:
        hist = json.loads((OUT_DIR / "history.json").read_text())
    except (OSError, ValueError):
        return [], "none"
    cut = (now - timedelta(hours=14)).strftime("%Y-%m-%d %H:%M")
    hist = [h for h in hist if h["at"] >= cut]
    return hist, "\n".join(f"- {h['at'][11:]}: " + " / ".join(m["do"] for m in h["moves"]) for h in hist[-4:]) or "none"


def snapshot(now):
    _, earlier = earlier_moves(now)
    return f"""# Floor snapshot {now:%a %m/%d %I:%M %p} (last hour unless said)

## Broken data sources right now (never base a move on a number from one of these)
{broken_text(now)}

## Stuck chutes, last hour, by area (full gaylord not swapped / chute blocked: boxes go around the belt)
{stuck_text(now)}

## Stuck-minutes tonight since 4 PM by route group (where it keeps coming back)
{tonight_stuck(now)}

## Feed stations, newest hour, each against its OWN normal
{feeders(now)}

## Sorter speed this hour (WMS sorter feed)
{speed_text(now)}

## Planned headcount per area tonight
{headcount_text()}

## Unsorted freight (floor board, newest last)
{unsorted_text()}

## Inbound linehaul trucks (WMS)
{inbound_text(now)}

## Dock (outbound trucks)
{dock_text(now)}

## Label errors (company one-bag-one-label count)
{labels_text()}

## What people said, last hour (sorting team chat + messages that mention me)
{chat_text(now)}

## Moves already suggested tonight (don't repeat unless it still isn't fixed; then say so)
{earlier}
"""


# ---------- the model turns it into moves ----------

# The answer's shape. The API's structured outputs don't take array-length limits, so "at most 4" is in the
# prompt and enforced by parse_answer.
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["headline", "moves"],
          "properties": {"headline": {"type": "string"},
                         "moves": {"type": "array", "items": {
                             "type": "object", "additionalProperties": False, "required": ["do", "why"],
                             "properties": {"do": {"type": "string"}, "why": {"type": "string"}}}}}}

# --demo prints this, labelled as an example: the answer's format, not advice for tonight.
EXAMPLE_ANSWER = {
    "headline": "Hub chutes are the bottleneck: 2 stuck, 360 boxes circling.",
    "moves": [
        {"do": "Move 2 Vendor C pallet workers from Zone 1's station chutes to chutes 053 and 054 to swap full gaylords.",
         "why": "BOS.H 053 stuck 25 min with 240 boxes circling and PIT.H 054 stuck 12 min with 120; Zone 1 had no stuck chute."},
        {"do": "Bring 10 empty gaylords to chutes 049-069 now.",
         "why": "Both stuck chutes are hub chutes and they stuck again within the hour."},
        {"do": "Tell the maintenance lead to check the scanner at feed station 16.",
         "why": "Station 16 rejects 7% against 2% average and is high every night: the station, not the person."},
    ]}


def task_text(snap):
    return "Give tonight's floor moves for this hour as the rules say. The snapshot is below.\n\n" + snap


def model_name():
    return os.environ.get("HUBOPS_COACH_MODEL") or DEFAULT_MODEL


def build_request(snap, key=None):
    """(url, headers, body) for one Messages API call: the rules as the system prompt, the snapshot as the user
    message, the answer constrained to SCHEMA."""
    model = model_name()
    body = {"model": model, "max_tokens": MAX_TOKENS, "system": PROMPT.read_text(),
            "messages": [{"role": "user", "content": task_text(snap)}],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    headers = {"content-type": "application/json", "anthropic-version": API_VERSION, "x-api-key": key or ""}
    if model in FALLBACK_MODELS:
        body["fallbacks"] = "default"
        headers["anthropic-beta"] = FALLBACK_BETA
    return API_URL, headers, body


def parse_answer(text):
    """The model's text -> {"headline", "moves": [{"do", "why"}] (at most MAX_MOVES)}. Takes plain JSON or JSON in
    a code fence; moves without a "do" are dropped. Raises ValueError when there is no JSON object."""
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    elif not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < start:
            raise ValueError("no JSON object in the answer")
        text = text[start:end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("the answer is not a JSON object")
    moves = []
    for m in data.get("moves") or []:
        if isinstance(m, dict) and str(m.get("do") or "").strip():
            moves.append({"do": str(m["do"]).strip(), "why": str(m.get("why") or "").strip()})
    return {"headline": str(data.get("headline") or "").strip(), "moves": moves[:MAX_MOVES]}


def call_api(url, headers, body, retries=1):
    """POSTs the request; returns the answer's text. Retries once on 429 / 5xx / a dropped connection."""
    data = json.dumps(body, ensure_ascii=False).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as res:
                msg = json.loads(res.read().decode())
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if attempt < retries and (e.code == 429 or e.code >= 500):
                time.sleep(float(e.headers.get("retry-after") or 10) if e.headers else 10)
                continue
            raise RuntimeError(f"API HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < retries:
                time.sleep(10)
                continue
            raise RuntimeError(f"API not reachable: {e}") from None
    stop = msg.get("stop_reason")
    if stop == "refusal":
        raise RuntimeError(f"the model declined: {(msg.get('stop_details') or {}).get('category')}")
    if stop == "max_tokens":
        raise RuntimeError("the answer was cut off at max_tokens")
    return "".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text")


def ask(snap, key):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "snapshot.md").write_text(snap)
    return parse_answer(call_api(*build_request(snap, key)))


def text_of(res):
    return "\n".join(f"{i}. {m['do']}\n   Why: {m['why']}" for i, m in enumerate(res["moves"], 1))


def coach_tasks(res, now):
    return [{"tid": f"{now:%m%d%H}|{i}", "rank": 40 + i, "ttl_min": 65, "do": m["do"], "why": m["why"],
             "push": not m["do"].lower().startswith("still")}
            for i, m in enumerate(res.get("moves") or [])]


def demo(snap, why):
    """No API call: the exact request it would send (key left out), then a canned answer labelled as one."""
    url, headers, body = build_request(snap)
    headers = {k: ("<ANTHROPIC_API_KEY>" if k == "x-api-key" else v) for k, v in headers.items()}
    extra = {k: v for k, v in body.items() if k not in ("system", "messages")}
    print(f"[floor coach demo: {why}; no API call, nothing sent]")
    print(f"--- request: POST {url}")
    print("--- headers: " + json.dumps(headers))
    print("--- body (besides system and messages): " + json.dumps(extra))
    print(f"--- system prompt ({PROMPT.relative_to(hubconfig.REPO)}):")
    print(body["system"].rstrip())
    print("--- user message:")
    print(body["messages"][0]["content"].rstrip())
    res = parse_answer(json.dumps(EXAMPLE_ANSWER))
    print("--- EXAMPLE ANSWER (canned, not from a model and not about tonight's numbers; shows the format the app gets):")
    print(res["headline"])
    print(text_of(res))


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="print the snapshot and the moves, send nothing")
    p.add_argument("--demo", action="store_true", help="no API call: print the request and a canned example answer")
    p.add_argument("--any-hour", action="store_true", help="run outside the night shift too")
    p.add_argument("--at", help='"YYYY-MM-DD HH:MM" to look at another time')
    p.add_argument("--snapshot-only", action="store_true")
    a = p.parse_args()
    now = datetime.strptime(a.at, "%Y-%m-%d %H:%M") if a.at else hubconfig.now_local()
    if not (a.at or a.any_hour or a.demo or a.snapshot_only) and not (now.hour >= ACTIVE_FROM or now.hour < ACTIVE_UNTIL):
        return 0
    snap = snapshot(now)
    if a.snapshot_only:
        print(snap)
        return 0
    key = os.environ.get("ANTHROPIC_API_KEY")
    if a.demo or not key:
        demo(snap, "--demo" if a.demo else "no ANTHROPIC_API_KEY")
        return 0
    try:
        res = ask(snap, key)
    except Exception as e:  # noqa: BLE001
        print(f"{now:%m/%d %H:%M} coach failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if a.dry_run:
        print(snap, "\n=====\n", res.get("headline"), "\n", text_of(res) if res["moves"] else "(no moves)")
        return 0
    hist, _ = earlier_moves(now)
    hist.append({"at": now.strftime("%Y-%m-%d %H:%M"), "headline": res.get("headline"), "moves": res["moves"]})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "history.json").write_text(json.dumps(hist[-30:], ensure_ascii=False, indent=1))
    try:  # The moves are the task list's coach tasks until the next hour; only new ones buzz ("Still: ..." don't)
        import do_now
        do_now.replace("coach", coach_tasks(res, now))
    except Exception as e:  # noqa: BLE001
        print(f"{now:%m/%d %H:%M} task list not updated: {type(e).__name__}: {e}", file=sys.stderr)
    if res["moves"]:
        import app_push
        app_push.alert(f"Floor coach · {now:%-I %p}", (res.get("headline") or "").strip() + "\n\n" + text_of(res),
                       key=f"coach-{now:%m%d%H}")
    print(f"{now:%m/%d %H:%M} {len(res['moves'])} moves: {res.get('headline')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
