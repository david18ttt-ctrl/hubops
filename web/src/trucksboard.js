// Dispatch's truck page: GET /trucks/<TRUCKS_VIEW_KEY>. One row per station for its next truck, in
// departure order, in plain words: status, what to do (which truck to add, by when), boxes expected, trucks
// booked, how full they'd be, boxes so far, and the station's limit (the capacity sheet).
// The numbers are the truck-space check's own (truckspace.js, every 15 minutes, kept in KV), so a view
// costs one KV read and no WMS call.

const STATE_KEY = "truckspace";

export async function handleTrucksBoard(request, env) {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/trucks/") || request.method !== "GET") return null;
  const key = url.pathname.slice("/trucks/".length);
  if (!env.TRUCKS_VIEW_KEY || env.TRUCKS_VIEW_KEY.length < 16 || key !== env.TRUCKS_VIEW_KEY) {
    return new Response("Not found", { status: 404 });
  }
  // No chat news in this page, not even hidden in its data: it's shared with dispatch, and the chat is internal.
  const state = await env.PLAN.get(STATE_KEY, "json");
  return new Response(publicPage(trucksPage(state?.board || null, state?.city?.board || null, null)), {
    headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex" },
  });
}

// The served page without its whole-line code comments: they're for developers, not for the shared page.
export function publicPage(html) {
  return html.replace(/^[ \t]*[/][/].*\n/gm, "");
}

export function trucksPage(board, city = null, news = null) {
  const newsData = JSON.stringify(news).replace(/</g, "\\u003c");
  // A limit set from a chat message is marked "note", and the chat's and sender's names are left out: this page
  // is shared with dispatch, and where the worker reads its news is internal.
  const hide = (k, v) => (k === "from" && v === "chat" ? "note" : k === "chat" || k === "sender" ? undefined : v);
  const data = JSON.stringify(board, hide).replace(/</g, "\\u003c");
  const cityData = JSON.stringify(city, hide).replace(/</g, "\\u003c");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light">
<meta http-equiv="refresh" content="300">
<title>Station trucks · HOU.H</title>
<style>
:root { color-scheme: light; --bg: #f7f8fa; --card: #fff; --text: #262626; --muted: #8c8c8c; --line: #e8e8e8; --accent: #ff4800;
        --top: #001529; --topsub: #a3b1bf;
        --ok: #389e0d; --ok-bg: #f6ffed; --tight: #ad6800; --tight-bg: #fffbe6; --risk: #d4380d; --risk-bg: #fff2e8;
        --bad: #cf1322; --bad-bg: #fff1f0; --cap: #531dab; --cap-bg: #f9f0ff; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.4 "Helvetica Neue", Helvetica, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif; }
header { background: var(--top); color: #fff; display: flex; align-items: center; gap: 12px;
         padding: 12px max(16px, calc((100% - 1440px) / 2 + 16px)); }
header > div { flex: 1; min-width: 0; }
h1 { font-size: 19px; font-weight: 600; margin: 0; }
.sub { color: var(--topsub); font-size: 13px; margin-top: 2px; }
button { font: inherit; cursor: pointer; }
main { padding: 0 16px 32px; max-width: 1440px; margin: 0 auto; }
.strip { background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 10px 12px; margin-top: 12px; font-size: 14px; }
.strip div { margin: 2px 0; }
.strip b { font-weight: 700; }
.strip .stale { color: #fff; background: var(--bad); font-weight: 700; padding: 6px 8px; border-radius: 3px; margin-bottom: 6px; }
.wrap { overflow-x: auto; margin-top: 12px; background: var(--card); border: 1px solid var(--line); border-radius: 4px; }
table { border-collapse: collapse; width: 100%; min-width: 1120px; font-variant-numeric: tabular-nums; }
th { text-align: left; font-size: 12px; font-weight: 600; color: var(--muted); padding: 8px 10px; border-bottom: 1px solid var(--line); background: #fafafa; vertical-align: bottom; }
td { padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: middle; white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
td small { display: block; color: var(--muted); font-size: 12px; }
td.st { position: sticky; left: 0; background: var(--card); font-weight: 700; font-size: 15px; }
td.st small { font-weight: 400; }
.big { font-size: 15px; font-weight: 600; }
.tag { display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 13px; font-weight: 700; }
.tag.ok { color: var(--ok); background: var(--ok-bg); } .tag.tight { color: var(--tight); background: var(--tight-bg); }
.tag.risk { color: var(--risk); background: var(--risk-bg); } .tag.short, .tag.none { color: var(--bad); background: var(--bad-bg); }
.tag.unknown { color: var(--muted); background: var(--bg); }
.do { font-weight: 600; } .do.short, .do.none { color: var(--bad); } .do.risk { color: var(--risk); }
.fill { display: flex; align-items: center; gap: 8px; }
.fill .bar { position: relative; width: 80px; height: 12px; background: var(--bg); border: 1px solid var(--line); border-radius: 3px; overflow: hidden; }
.fill .bar i { position: absolute; top: 0; bottom: 0; left: 0; }
.fill .bar i.ok { background: #95de64; } .fill .bar i.tight { background: #ffd666; } .fill .bar i.over { background: #ff7875; }
.over { color: var(--bad); font-weight: 700; } .free { color: var(--ok); }
.cap { color: var(--cap); font-weight: 700; }
td small.alt { color: var(--text); } td small.alt.q { color: var(--bad); font-weight: 600; }
.alert { display: inline-block; margin-left: 6px; width: 18px; height: 18px; line-height: 18px; text-align: center; border-radius: 50%;
         background: var(--bad); color: #fff; font-size: 12px; font-weight: 700; vertical-align: 2px; }
.asks { margin-top: 12px; background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 10px 12px; font-size: 14px; }
.asks div { margin: 3px 0; }
.asks .q { color: var(--bad); border-left: 4px solid var(--bad); padding-left: 8px; }
.asks .q b { font-weight: 700; }
h2 { font-size: 16px; font-weight: 600; margin: 20px 0 0; }
.scale { background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 8px 12px; margin-top: 12px; font-size: 13px; }
.scale .ends { display: flex; align-items: center; gap: 10px; }
.scale .ends span { flex: none; color: var(--muted); font-size: 12px; }
.scale input { flex: 1; min-width: 120px; max-width: 420px; accent-color: var(--accent); }
.scale .now { margin-top: 2px; } .scale .now b { font-weight: 700; }
.sub2 { color: var(--muted); font-size: 12px; margin-top: 2px; }
.notes { margin-top: 12px; font-size: 12px; color: var(--muted); }
.notes div { margin: 2px 0; }
.empty { background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 14px; margin-top: 12px; color: var(--muted); }
</style>
</head>
<body>
<header><div><h1 id="title"></h1><div class="sub" id="sub"></div></div></header>
<main id="main"></main>
<script>
const B = ${data};
const C = ${cityData};
const N = ${newsData};
const fmt = (n) => Math.round(n).toLocaleString("en-US");
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const TZ = "America/Chicago";
const clock12 = (ms) => new Date(ms).toLocaleTimeString("en-US", { timeZone: TZ, hour: "numeric", minute: "2-digit" });
const clock = clock12;
// A time with its weekday when it isn't today ("Fri 1:00 AM"), so a row for tomorrow night's truck reads as tomorrow's.
const dayOf = (ms) => new Date(ms).toLocaleDateString("en-CA", { timeZone: TZ });
const when = (ms) => (dayOf(ms) === dayOf(Date.now()) ? "" :
  new Date(ms).toLocaleDateString("en-US", { timeZone: TZ, weekday: "short" }) + " ") + clock(ms);
const STALE_MS = 45 * 60000; // three missed 15-minute checks
// Minutes after the operating day's midnight (the bulk truck's arrival) as a clock time.
const minutesTime = (m) => { const h = Math.floor(m / 60) % 24, mm = String(m % 60).padStart(2, "0");
  return (h % 12 || 12) + ":" + mm + (h < 12 ? " AM" : " PM"); };
const TEXT = {
  en: {
    title: "Station trucks · HOU.H",
    sub: (t, tr) => "Updated " + t + " · dispatch's truck list read " + (tr || "–") + " · refreshes every 15 min",
    cols: ["Station", "Status", "What to do", "Total trucks tonight<br>(incl. booked)", "Trucks booked", "Boxes expected<br>at departure", "Truck fill<br>(expected ÷ space)", "Boxes<br>so far", "Station limit"],
    status: { ok: "Enough trucks", tight: "Nearly full", risk: "May need truck", short: "Need truck", none: "No truck booked", unknown: "No forecast" },
    add: (t, by) => "Add " + t + " by " + by, addNow: (t) => "Add " + t + " now",
    maybe: (t, by) => "Maybe " + t + ", decide by " + by, maybeNow: (t) => "Maybe " + t + ", decide now",
    book: (t, by) => "Book " + t + " by " + by, bookNow: (t) => "Book " + t + " now",
    likely: (a, b) => "likely " + a + "–" + b, holds: (n) => "holds " + n + " boxes",
    over: (n) => n + (n === 1 ? " box over" : " boxes over"), free: (n) => n + (n === 1 ? " box free" : " boxes free"), full: "exactly full",
    am: (t) => "9 AM: " + t, ifHigh: (t) => t + " if it runs high",
    leaves: (t, c) => "leaves " + t + " · cutoff " + c, pkg: (n) => fmt(n) + " pkg",
    limit: (p, cap) => p + "% of " + fmt(cap) + " pkg", overLimit: (n) => "~" + n + " boxes over limit, extra truck won't help",
    noLimit: "no limit", truck: (type, n) => n + "×" + type.replace("FT", "ft"),
    srcNote: (t, why, quote) => "Update " + t + ": " + why, srcAi: (why) => "until the capacity sheet is out: " + why,
    srcSheet: { cut: (n) => "cut: " + n, raised: (n) => "raised: " + n, no_number: (n) => "no number: " + n },
    stripAdd: "Add trucks:", stripMaybe: "Maybe add:", stripNone: "No extra trucks needed right now.",
    bulk: { expected: "Bulk truck: not in yet (usually 4–9 PM)", in: (n) => "Bulk truck: in at " + n.time + ", " + fmt(n.size) + " pkg",
      late: "Bulk truck: not in by 10 PM, left out tonight", no: "Bulk truck: not coming today, left out" },
    peak: (f) => "Peak day: boxes still to come ×" + f,
    stale: (t) => "Not updating: these numbers are from " + t + ". Don't plan from them.",
    counting: (codes, text) => "With route " + codes + ": " + text,
    heldSoFar: (n, codes) => "+" + n + " held (" + codes + ")", alertTip: "Open question, see below the table",
    leftIn: (n) => "incl. " + n + " left from last night", leftOn: (n) => n + " of them on tonight's trucks already",
    leftUnsure: (n) => "+" + n + " more not signed out, not counted (likely left unscanned)",
    askHeld: (name, codes, since, a, b) => "<b>Question · " + name + ": is route " + codes + " still held?</b> Marked held since " + since +
      ", not confirmed since. Still held: expect " + a + " boxes. Back on the truck: expect " + b + ".",
    noteHeld: (name, codes, since) => name + ": route " + codes + " held since " + since + ", not counted.",
    notes: [
      "Boxes: gaylords/bags of 30+ packages for that truck, from WMS. Expected: forecast for when the truck leaves; it lands in the \\"likely\\" range about 85% of nights.",
      "Truck space: 53ft ≈ 27 boxes (BRO01, HRL01 28; SGR01 29), 26ft ≈ 11. Up to 4 boxes over usually still fits or waits a night; 5+ over needs another truck.",
      "Act-by time: 3 hours before the truck leaves (an extra truck takes about 3 hours to arrive).",
      "Station limit: the capacity sheet's daily package limit (out late morning; before that, the number expected for the day). A limit change applies right away. Boxes over it wait, however many trucks there are.",
      "Left from last night: boxes made for last night's truck that weren't signed out, counted in when they can't have gone (still on the chute, or made after the last truck was loaded) or when last night's trucks left full or the station reached its limit. Otherwise they're shown, not counted.",
    ],
    empty: "No numbers yet.",
    noForecast: "no forecast: fill is boxes so far",
    scaleLow: "Short on purpose", scaleHigh: "Extra safe",
    scaleName: ["Short on purpose", "A little lean", "Normal", "A little safe", "Extra safe"],
    scaleSays: ["trucks for a low night; up to 12 boxes wait for the next truck", "trucks for the expected night; up to 8 boxes wait",
      "trucks for a likely-high night; up to 4 boxes wait", "trucks for the top of the likely range; up to 2 boxes wait",
      "trucks for past the top of the range; nothing waits"],
    scaleFor: { stations: "Station trucks", cities: "City trucks" },
    scaleNote: "The scale (above each table) sets how the trucks are sized, from short on purpose (full trucks, leftover boxes wait) to extra safe (room to spare). It changes the status, what to do and trucks to order on this screen only. On past nights, cities one step lean left about 5 boxes a night per group; at normal about 3, with 6 more empty spots.",
    newsTitle: "Updates, last 24 hours", newsNone: "",
    feedStale: (t) => "",
    newsDid: (did) => "Applied: " + did + ".", newsYou: "",
    cityTitle: "City trucks", citySub: "Boxes made tonight (4 PM–5 AM) for each city's trucks. Not counted: pass-through boxes and boxes left from last night.",
    cityCols: ["Cities", "Status", "What to do", "Total trucks tonight<br>(incl. booked)", "Trucks booked", "Boxes expected<br>by 6 AM", "Truck fill<br>(expected ÷ space)", "Boxes<br>so far", "Pickup forecast<br>(today)"],
    lastTruck: (t) => "last truck by " + t, allMade: "all made", noPickup: "not posted yet",
    through: (n) => "+ about " + n + " pass-through boxes ride these, not counted",
    stripCityAdd: "Cities, add trucks:", stripCityMaybe: "Cities, maybe add:",
    cityNote: "City trucks: 53ft ≈ 28 boxes. The forecast uses the last weeks' city nights and dispatch's pickup forecast (posted about noon). Act-by time: 3 hours before the group's last truck.",
  },
};
const L = () => TEXT.en;
const trucks = (list) => (list || []).map(([type, n]) => L().truck(type, n)).join(" + ");
// The forecast's status: "maybe" (top of the range 5+ over) and "few" (top of the range 1–4 over).
// Without a forecast, the row still says when the boxes already made no longer fit the booked trucks.
const overNow = (r) => r.point == null && r.booked.length > 0 && r.boxes >= r.room + (r.spare ?? SPARE) + 1;
const statusOf = (r) => (r.point == null ? (overNow(r) ? "short" : "unknown") : r.status === "maybe" ? "risk" : r.status === "few" ? "tight" : r.status);
// The extra truck for boxes the booked trucks can't take: a 26ft for up to 11, else 53fts (and a 26ft for a small rest).
function extraTruck(r, boxes) {
  const n = Math.max(1, Math.ceil(boxes));
  if (n <= 11) return L().truck("26FT", 1);
  const full = r.full53 || 27;
  let n53 = Math.floor(n / full); const rest = n - n53 * full;
  if (rest > 11) n53 += 1;
  return [n53 ? L().truck("53FT", n53) : "", rest > 0 && rest <= 11 ? L().truck("26FT", 1) : ""].filter(Boolean).join(" + ");
}
// The truck to add is judged the way the row itself was (the same standard as the estimate, up to 4 wait),
// with no extra safety on top:
// - "Need truck" is called because the expected boxes are 5+ past the booked trucks, so the truck to add is
//   the one that takes the expected boxes with up to 4 waiting: 77 expected on 65 of room is
//   77 − 4 − 65 = 8, a 26ft (not a 53ft sized for the likely-high night).
// - "May need truck" means the expected boxes fit and only a high night wouldn't, so the truck named is the
//   one the trucks to order are sized for (the likely-high night, 4 waiting), not the very top of the range.
const SPARE = (B && B.spare) || 4;
function whatToDo(r, s) {
  const T = L(), late = Date.now() >= r.askBy, by = when(r.askBy);
  const spare = r.spare ?? SPARE, expected = r.sizeS ?? r.point ?? r.boxes, likelyHigh = r.sizeT ?? r.plan ?? r.high;
  if (s === "none") { const t = trucks(r.need) || extraTruck(r, likelyHigh ?? expected); return late ? T.bookNow(t) : T.book(t, by); }
  if (s === "short") { const t = extraTruck(r, expected - spare - r.room); return late ? T.addNow(t) : T.add(t, by); }
  if (s === "risk") { const t = extraTruck(r, likelyHigh - spare - r.room); return late ? T.maybeNow(t) : T.maybe(t, by); }
  return "";
}
// ---- The scale: how the trucks are sized, from short on purpose (−2) to extra safe (+2). 0 is the check's own
// rule (trucks for the 70th-percentile night, 4 boxes may wait; "need truck" when the expected boxes are 5+
// past the booked trucks, "may need" when the top of the range is). Each step moves one notch along the
// forecast's own numbers (low 15% · expected · plan 70% · high 85% · beyond) and changes how many boxes may
// wait: 12 · 8 · 4 · 2 · 0. On replayed nights, −1 left cities about twice the boxes of 0 with well under half
// the empty spots, while stations did best at 0. Kept per viewer; the page's defaults: stations 0, cities −1
// (dispatch runs city trucks full and rolls leftovers).
const WAIT = { "-2": 12, "-1": 8, "0": 4, "1": 2, "2": 0 };
const scale = { stations: (B && B.lean) || 0, cities: C && C.lean != null ? C.lean : -1 };
try { for (const k of ["stations", "cities"]) { const v = localStorage.getItem("trucks-scale-" + k); if (v != null && WAIT[String(+v)] != null) scale[k] = +v; } } catch (e) {}
function trucksList(r, boxes) {
  const full = r.full53 || 27;
  let n53 = Math.floor(boxes / full); const rest = boxes - n53 * full;
  if (rest > 11) n53 += 1;
  return [...(n53 ? [["53FT", n53]] : []), ...(rest > 0 && rest <= 11 ? [["26FT", 1]] : [])];
}
// A row as a setting sees it: its status, trucks to order and the sizes the truck call uses.
function sized(r, k) {
  if (!k || r.point == null) return r;
  const step = Math.max(0, r.high - (r.plan ?? r.high));
  const q = [r.low - (r.point - r.low) / 2, r.low, r.point, r.plan ?? r.high, r.high, r.high + step, r.high + 2 * step].map((x) => Math.max(x, r.boxes || 0));
  const spare = WAIT[String(k)], over = r.room + spare + 1;
  const S = q[2 + k], T = q[3 + k], U = q[4 + k];
  const status = !r.booked.length ? "none" : S >= over ? "short" : U >= over ? "maybe" : U > r.room ? "few" : "ok";
  return { ...r, status, spare, sizeS: S, sizeT: T, sizeU: U, need: trucksList(r, Math.max(1, Math.ceil(T) - spare)), needHigh: trucksList(r, Math.max(1, Math.ceil(U) - spare)) };
}
function scaleBar(which) {
  const T = L(), k = scale[which];
  return '<div class="scale"><div class="ends"><span>' + T.scaleLow + '</span><input type="range" min="-2" max="2" step="1" value="' + k + '" data-scale="' + which +
    '" aria-label="' + esc(T.scaleFor[which]) + '"><span>' + T.scaleHigh + '</span></div><div class="now" id="scale-' + which + '">' + scaleLine(which, k) + "</div></div>";
}
const scaleLine = (which, k) => { const T = L(); return esc(T.scaleFor[which]) + ": <b>" + T.scaleName[k + 2] + "</b> · " + T.scaleSays[k + 2]; };
function fill(r, s) {
  const T = L();
  if ((r.point == null && !r.boxes) || !r.booked.length || !r.room) return "–";
  const expected = Math.round(r.point ?? r.boxes); // no forecast: the boxes made so far
  const pct = Math.round((100 * expected) / r.room);
  const diff = expected - r.room;
  const cls = diff >= 5 ? "over" : pct > 95 || s === "tight" || s === "risk" ? "tight" : "ok";
  return '<div class="fill"><div class="bar"><i class="' + cls + '" style="width:' + Math.min(100, pct) + '%"></i></div><div><b>' + pct + "%</b><small" +
    (diff > 0 ? ' class="over">' + T.over(diff) : diff < 0 ? ' class="free">' + T.free(-diff) : ">" + T.full) + "</small></div></div>";
}
// A station's held routes as short codes ("003, 004"), and the day the hold was marked ("M/D").
const heldCodes = (h) => h.routes.map((x) => x.split("-")[1] || x).join(", ");
const shortDay = (day) => (day ? +day.slice(5, 7) + "/" + +day.slice(8, 10) : "?");
// The row as it would be with its held routes on the truck.
const counted = (r) => ({ ...r, point: r.held.point, low: r.held.low ?? r.held.point, high: r.held.high, plan: r.held.plan, status: r.held.status, need: r.held.need, needHigh: null,
  boxes: r.boxes + (r.held.boxes || 0) });
function row(raw, city) {
  const k = scale[city ? "cities" : "stations"];
  const r = sized(raw, k);
  const T = L(), s = statusOf(r);
  const todo = whatToDo(r, s);
  const h = r.held && r.held.point != null ? r.held : null;
  const alt = h ? ' class="alt' + (h.ask ? " q" : "") + '"' : "";
  const withHeld = h ? sized(counted(raw), k) : null;
  const todoHeld = h ? whatToDo(withHeld, statusOf(withHeld)) : "";
  return "<tr>" +
    '<td class="st">' + esc(r.name) + (r.held && r.held.ask ? '<span class="alert" title="' + esc(T.alertTip) + '">!</span>' : "") + "<small>" + esc(city ? T.lastTruck(when(r.depart)) : T.leaves(when(r.depart), r.cutoff || "?")) + "</small></td>" +
    '<td><span class="tag ' + s + '">' + T.status[s] + "</span></td>" +
    '<td class="do ' + s + '">' + todo + (todoHeld && todoHeld !== todo ? "<small" + alt + ">" + T.counting(heldCodes(h), todoHeld) + "</small>" : "") + "</td>" +
    "<td>" + (r.point == null ? "–" : '<span class="big">' + trucks(r.need) + "</span>" +
      (r.needHigh && trucks(r.needHigh) !== trucks(r.need) ? "<small>" + T.ifHigh(trucks(r.needHigh)) + "</small>" : "") +
      (r.am && r.am.need.length ? "<small>" + T.am(trucks(r.am.need)) + "</small>" : "")) + "</td>" +
    "<td>" + (r.booked.length ? trucks(r.booked) + "<small>" + T.holds(r.room) + "</small>" : "–") + "</td>" +
    "<td>" + (r.point == null ? "–<small>" + T.noForecast + "</small>" : '<span class="big">' + Math.round(r.point) + "</span><small>" + (r.made ? T.allMade : T.likely(Math.round(r.low), Math.ceil(r.high))) + "</small>" +
      (r.through ? "<small>" + T.through(r.through) + "</small>" : "") +
      (r.left && r.left.boxes ? "<small>" + T.leftIn(r.left.boxes) + "</small>" : "") +
      (h ? "<small" + alt + ">" + T.counting(heldCodes(h), Math.round(h.point)) + "</small>" : "")) + "</td>" +
    "<td>" + fill(r, s) + "</td>" +
    '<td><span class="big">' + r.boxes + "</span><small>" + T.pkg(r.packages) + "</small>" +
      (r.held && r.held.boxes ? "<small>" + T.heldSoFar(r.held.boxes, heldCodes(r.held)) + "</small>" : "") +
      (r.left && r.left.boxes ? "<small>" + T.leftIn(r.left.boxes) + (r.left.onTonight ? " · " + T.leftOn(r.left.onTonight) : "") + "</small>" : "") +
      (r.left && r.left.unsure ? "<small>" + T.leftUnsure(r.left.unsure) + "</small>" : "") + "</td>" +
    (city ? "<td>" + (r.pickup ? T.pkg(r.pickup) : T.noPickup) + "</td></tr>" : "") +
    (city ? "" : "<td>" + (r.cap ? (r.expected != null ? T.limit(Math.round((100 * r.expected) / r.cap), r.cap) : fmt(r.cap)) +
      (r.overCap ? '<small class="cap">' + T.overLimit(Math.ceil(r.hold)) + "</small>" : "") : T.noLimit) + limitSource(r, T) + "</td>" +
    "</tr>");
}
// Where the limit came from, when it's not simply the sheet's number for the day: a chat message, the expected
// number before her sheet is out, or her note on a cut.
function limitSource(r, T) {
  const l = r.limit;
  if (!l) return "";
  let text = "";
  if (l.from === "note") text = T.srcNote(l.sent ? clock(l.sent) : "", l.why, l.quote);
  else if (l.from === "ai") text = T.srcAi(l.why);
  else if (T.srcSheet[l.kind] && (l.note || l.why)) text = T.srcSheet[l.kind](l.why || l.note);
  return text ? "<small>" + esc(text) + "</small>" : "";
}
// Under the table: a hold that may be over, as a question with the night both ways; a fresh hold, as a note.
function asks() {
  const T = L();
  const lines = B.rows.filter((r) => r.held).map((r) => {
    const codes = heldCodes(r.held), since = shortDay(r.held.since);
    return r.held.ask && r.point != null && r.held.point != null
      ? '<div class="q">' + T.askHeld(esc(r.name), esc(codes), since, Math.round(r.point), Math.round(r.held.point)) + "</div>"
      : "<div>" + esc(T.noteHeld(r.name, codes, since)) + "</div>";
  });
  return lines.length ? '<div class="asks">' + lines.join("") + "</div>" : "";
}
// What the chat reader found for the trucks: each message's gist and what the check did with it. Turned off
// on this page (it's shared with dispatch); kept for an internal view.
function newsBlock() {
  return "";
  const T = L();
  if (!N || !N.items || !N.items.length) return "";
  return "<h2>" + T.newsTitle + '</h2><div class="asks">' + N.items.map((n) =>
    "<div><b>" + esc(when(n.sent)) + "</b> · " + esc(n.summary) +
    ' <span class="sub2">(' + esc([n.sender, n.chat].filter(Boolean).join(", ")) + ")</span>" +
    (n.did ? " <b>" + esc(T.newsDid(n.did)) + "</b>" : "") + (n.attention ? ' <b style="color:var(--bad)">' + T.newsYou + "</b>" : "") + "</div>").join("") + "</div>";
}
function render() {
  const T = L();
  document.getElementById("title").textContent = T.title;
  document.getElementById("sub").textContent = B ? T.sub(when(B.at), B.trucksAt ? when(B.trucksAt) : "") : "";
  if (!B || !B.rows || !B.rows.length) { document.getElementById("main").innerHTML = '<div class="empty">' + T.empty + "</div>"; return; }
  const line = (r) => "<b>" + esc(r.name) + "</b> " + whatToDo(r, statusOf(r)).replace(/^(Add |Book |Maybe )/, "");
  const stationRows = B.rows.map((r) => sized(r, scale.stations));
  const add = stationRows.filter((r) => ["short", "none"].includes(statusOf(r)));
  const maybe = stationRows.filter((r) => statusOf(r) === "risk");
  const cities = C && C.rows ? C.rows : [];
  const cityRows = cities.map((r) => sized(r, scale.cities));
  const cityAdd = cityRows.filter((r) => ["short", "none"].includes(statusOf(r)));
  const cityMaybe = cityRows.filter((r) => statusOf(r) === "risk");
  const strip = [];
  if (Date.now() - B.at > STALE_MS) strip.push('<div class="stale">' + esc(T.stale(when(B.at))) + "</div>");
  // (No chat-reader warning here: this page is shared with dispatch, and how it gets its news is internal.)
  if (add.length) strip.push('<div style="color:var(--bad)"><b>' + T.stripAdd + "</b> " + add.map(line).join(" · ") + "</div>");
  if (maybe.length) strip.push('<div style="color:var(--risk)"><b>' + T.stripMaybe + "</b> " + maybe.map(line).join(" · ") + "</div>");
  if (cityAdd.length) strip.push('<div style="color:var(--bad)"><b>' + T.stripCityAdd + "</b> " + cityAdd.map(line).join(" · ") + "</div>");
  if (cityMaybe.length) strip.push('<div style="color:var(--risk)"><b>' + T.stripCityMaybe + "</b> " + cityMaybe.map(line).join(" · ") + "</div>");
  if (!add.length && !maybe.length && !cityAdd.length && !cityMaybe.length) strip.push("<div>" + T.stripNone + "</div>");
  if (B.bulk) strip.push("<div>" + (B.bulk.state === "in" ? T.bulk.in({ size: B.bulk.size, time: minutesTime(B.bulk.at) }) : T.bulk[B.bulk.state]) + "</div>");
  if (B.calendar) strip.push("<div>" + T.peak(B.calendar) + "</div>");
  document.getElementById("main").innerHTML = '<div class="strip">' + strip.join("") + "</div>" + scaleBar("stations") +
    '<div class="wrap"><table><thead><tr>' + T.cols.map((c) => "<th>" + c + "</th>").join("") +
    "</tr></thead><tbody>" + B.rows.map((r) => row(r)).join("") + "</tbody></table></div>" + asks() +
    (cities.length ? "<h2>" + T.cityTitle + '</h2><div class="sub2">' + T.citySub + "</div>" + scaleBar("cities") + '<div class="wrap"><table><thead><tr>' +
      T.cityCols.map((c) => "<th>" + c + "</th>").join("") + "</tr></thead><tbody>" + cities.map((r) => row(r, true)).join("") + "</tbody></table></div>" : "") + newsBlock() +
    '<div class="notes">' + T.notes.concat(cities.length ? [T.cityNote] : [], [T.scaleNote]).map((x) => "<div>" + x + "</div>").join("") + "</div>";
}
document.addEventListener("input", (e) => {
  const which = e.target && e.target.dataset ? e.target.dataset.scale : null;
  if (which) document.getElementById("scale-" + which).innerHTML = scaleLine(which, +e.target.value);
});
document.addEventListener("change", (e) => {
  const which = e.target && e.target.dataset ? e.target.dataset.scale : null;
  if (!which) return;
  scale[which] = +e.target.value;
  try { localStorage.setItem("trucks-scale-" + which, String(scale[which])); } catch (err) {}
  render();
});
render();
</script>
</body>
</html>`;
}
