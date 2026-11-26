// The PDA speed board. The ops host's PDA job sends its numbers to POST /pda-data (with PDA_PUSH_KEY) after
// every sweep; the page is GET /pda/<PDA_VIEW_KEY>. Each day's per-person numbers are kept here for good,
// for the week / month / all-time leaderboards. While the shift is on, the page asks the WMS for live PDA
// numbers (cached a minute) so speeds are current, not as of the last sweep.
import { UNSIGNED_MIN, applyLive, countsKey, fetchHourly, fetchLastBoxes, minutesSinceScan, shiftMinutes } from "./pda-live.js";
import { operationWindow } from "./hubtime.js";

const BOARD_KEY = "pda_board";
const HISTORY_KEY = "pda_history";
// Missed scans and mis-sorts per person per night, from the ops host's quality job (POST /pda-errors
// with QUALITY_PUSH_KEY): { updated, days: { "2026-11-05": [[name, crew, key, parcels, missed, missorts, match]] } }.
const ERRORS_KEY = "pda_errors";
// Pictures of each handwritten name, cut from the sign-out sheet photos by the PDA job (POST /pda-names with
// PDA_PUSH_KEY): one entry per picture, "pda_name_img:<id>" = {tab, jpeg}, the who-is-who index
// {board person key: [[id, day, written], …]}, and the list of ids that have a picture.
const NAMES_INDEX_KEY = "pda_name_index";
const NAMES_IDS_KEY = "pda_name_ids";
const nameImageKey = (id) => "pda_name_img:" + id;

// Who a /pda/<key> link is for: everyone (the main key), one company, or no one.
function viewer(env, key) {
  if (env.PDA_VIEW_KEY && key === env.PDA_VIEW_KEY) return { ok: true, company: null };
  const company = companyFor(env, key);
  return { ok: Boolean(company), company };
}

export async function handleDashboard(request, env) {
  const url = new URL(request.url);
  if (url.pathname === "/pda-data" && request.method === "POST") {
    if (!env.PDA_PUSH_KEY || request.headers.get("x-pda-key") !== env.PDA_PUSH_KEY) {
      return new Response("Forbidden", { status: 403 });
    }
    const body = await request.json();
    const history = (await env.PLAN.get(HISTORY_KEY, "json")) || {};
    Object.assign(history, body.history || {});
    delete body.history;
    await env.PLAN.put(HISTORY_KEY, JSON.stringify(history));
    await env.PLAN.put(BOARD_KEY, JSON.stringify(body));
    return new Response("OK");
  }
  if (url.pathname === "/pda-errors" && request.method === "POST") {
    if (!env.QUALITY_PUSH_KEY || request.headers.get("x-pda-key") !== env.QUALITY_PUSH_KEY) {
      return new Response("Forbidden", { status: 403 });
    }
    const body = await request.json();
    const saved = (await env.PLAN.get(ERRORS_KEY, "json")) || { days: {} };
    // A night sent again replaces that night; older nights stay.
    const merged = { updated: body.updated || saved.updated || "", days: { ...saved.days, ...(body.days || {}) } };
    await env.PLAN.put(ERRORS_KEY, JSON.stringify(merged));
    return new Response("OK");
  }
  if (url.pathname === "/pda-names" && request.method === "POST") {
    if (!env.PDA_PUSH_KEY || request.headers.get("x-pda-key") !== env.PDA_PUSH_KEY) {
      return new Response("Forbidden", { status: 403 });
    }
    const body = await request.json();
    const ids = new Set((await env.PLAN.get(NAMES_IDS_KEY, "json")) || []);
    for (const [id, img] of Object.entries(body.images || {})) {
      if (!/^[0-9a-f]{16}$/.test(id) || !img?.jpeg) continue;
      await env.PLAN.put(nameImageKey(id), JSON.stringify({ tab: String(img.tab || "OTHER"), jpeg: img.jpeg }));
      ids.add(id);
    }
    await env.PLAN.put(NAMES_IDS_KEY, JSON.stringify([...ids]));
    if (body.index) await env.PLAN.put(NAMES_INDEX_KEY, JSON.stringify(body.index));
    return new Response("OK");
  }
  // One name picture, for a link that may see it: a company's link only gets its own people's.
  const picture = /^\/pda\/([^/]+)\/name\/([0-9a-f]{16})$/.exec(url.pathname);
  if (picture && request.method === "GET") {
    const { ok, company } = viewer(env, picture[1]);
    const img = ok ? await env.PLAN.get(nameImageKey(picture[2]), "json") : null;
    if (!img || (company && img.tab !== company)) return new Response("Not found", { status: 404 });
    const bytes = Uint8Array.from(atob(img.jpeg), (c) => c.charCodeAt(0));
    return new Response(bytes, { headers: { "content-type": "image/jpeg", "cache-control": "private, max-age=86400", "x-robots-tag": "noindex" } });
  }
  // The PDA job fetches the 5-minute copy of a day's PDA numbers if its own save before 6 AM failed.
  if (url.pathname.startsWith("/pda-counts/") && request.method === "GET") {
    if (!env.PDA_PUSH_KEY || request.headers.get("x-pda-key") !== env.PDA_PUSH_KEY) {
      return new Response("Forbidden", { status: 403 });
    }
    const saved = await env.PLAN.get(countsKey(url.pathname.slice("/pda-counts/".length)));
    return saved ? new Response(saved, { headers: { "content-type": "application/json" } }) : new Response("Not found", { status: 404 });
  }
  if (url.pathname.startsWith("/pda/") && request.method === "GET") {
    const key = url.pathname.slice("/pda/".length);
    const everyone = Boolean(env.PDA_VIEW_KEY) && key === env.PDA_VIEW_KEY;
    const company = everyone ? null : companyFor(env, key);
    if (!everyone && !company) {
      return new Response("Not found", { status: 404 });
    }
    let board = (await env.PLAN.get(BOARD_KEY, "json")) || null;
    let history = (await env.PLAN.get(HISTORY_KEY, "json")) || {};
    const today = operationWindow(new Date())[0].slice(0, 10);
    if (env.WMS_TOKEN && (!board || board.day !== today)) {
      // No sign-in sheets read for today yet: show today's PDAs live from WMS, without names.
      board = (await unsignedToday(env, board, today)) || (board && { ...board, live: false });
    } else if (board?.live && board.signins && env.WMS_TOKEN) {
      const now = new Date();
      const saved = await env.PLAN.get(countsKey(board.day), "json");
      let live = null;
      try {
        live = await fetchHourly(env, now);
        if (live.day !== board.day) live = null;
      } catch (error) {
        console.error(error); // WMS didn't answer: use the 5-minute copy, or else the last sweep's numbers
      }
      const rows = live?.rows || saved?.rows;
      if (rows) {
        const hourly = Object.fromEntries(rows.map((r) => [r[0], r.slice(3)]));
        let boxes = {};
        try { boxes = await fetchLastBoxes(env, now); } catch (error) { console.error(error); } // No box shown, nothing else changes
        const result = applyLive(board, hourly, shiftMinutes(now), live?.at || saved.at, minutesSinceScan(saved, rows, now.getTime()), boxes);
        board = result.board;
        history = { ...history, [board.day]: result.historyDay };
      }
    }
    const errors = (await env.PLAN.get(ERRORS_KEY, "json")) || null;
    const shown = company ? onlyCompany(board, history, errors, company) : { board, history, errors };
    const names = await namePictures(env, company);
    return new Response(page(shown.board, shown.history, shown.errors, company, names), {
      headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "x-robots-tag": "noindex" },
    });
  }
  return null;
}

// Each labor vendor's own board: GET /pda/<its key>, with the keys in the PDA_COMPANY_KEYS secret as
// JSON, {"VA": "…", "VB": "…", "VC": "…"}. It's the same page with the same live numbers, but the
// data is cut down to that company here, before it's sent, so no other company's names or numbers are
// anywhere in the page.
const COMPANIES = ["VA", "VB", "VC"];

export function companyFor(env, key) {
  if (!key || !env.PDA_COMPANY_KEYS) return null;
  let keys;
  try { keys = JSON.parse(env.PDA_COMPANY_KEYS); } catch { return null; }
  const found = Object.entries(keys || {}).find(([company, k]) => COMPANIES.includes(company) && typeof k === "string" && k.length >= 16 && k === key);
  return found ? found[0] : null;
}

export function onlyCompany(board, history, errors, company) {
  const mine = (days, col) => Object.fromEntries(Object.entries(days || {}).map(([day, rows]) => [day, rows.filter((r) => r[col] === company)]));
  let shownBoard = null;
  if (board) {
    // signins lists everyone who signed for a PDA, every company: the page doesn't need it.
    const { signins, ...rest } = board;
    shownBoard = { ...rest, tabs: { [company]: board.tabs?.[company] || { people: [], idle: [], notes: [] } } };
  }
  return {
    board: shownBoard,
    history: mine(history, 1),
    errors: errors && { updated: errors.updated, days: mine(errors.days, 1) },
  };
}

// {board person key: [[picture id, day, written], …]} for the pictures this link may see.
export async function namePictures(env, company) {
  const index = (await env.PLAN.get(NAMES_INDEX_KEY, "json")) || {};
  const have = new Set((await env.PLAN.get(NAMES_IDS_KEY, "json")) || []);
  const out = {};
  for (const [person, list] of Object.entries(index)) {
    if (company && !person.endsWith("|" + company)) continue;
    const pictures = (list || []).filter((x) => have.has(x[0]));
    if (pictures.length) out[person] = pictures;
  }
  return out;
}

// Today's board before any sign-in sheet is read: every PDA that has scanned is "name?", live from
// WMS. Null when nothing has scanned yet (early morning), so the last shift's board stays up.
export async function unsignedToday(env, lastBoard, today, now = new Date()) {
  let live = null;
  try {
    live = await fetchHourly(env, now);
    if (live.day !== today) live = null;
  } catch (error) {
    console.error(error);
  }
  const saved = await env.PLAN.get(countsKey(today), "json"); // The 5-minute copy, if WMS didn't answer
  const rows = live?.rows || saved?.rows;
  if (!rows || !rows.some((r) => String(r[0]).startsWith("HOU.H_") && r.slice(3).reduce((a, b) => a + b, 0) >= UNSIGNED_MIN)) {
    return null;
  }
  const hourly = Object.fromEntries(rows.map((r) => [r[0], r.slice(3)]));
  const base = { day: today, live: true, noSheets: true, signins: [], updated: lastBoard?.updated || "", tabs: {} };
  let boxes = {};
  try { boxes = await fetchLastBoxes(env, now); } catch (error) { console.error(error); }
  return applyLive(base, hourly, shiftMinutes(now), live?.at || saved.at, minutesSinceScan(saved, rows, now.getTime()), boxes).board;
}

// company: set on a labor company's own board (see onlyCompany): just that company, no tabs.
export function page(board, history, errors = null, company = null, names = {}) {
  // </script> inside a name can't end the script early.
  const data = JSON.stringify({ board, history, errors, company, names }).replace(/</g, "\\u003c");
  const heading = (company ? { VA: "Vendor A", VB: "Vendor B", VC: "Vendor C" }[company] + " · " : "") + "PDA speed board";
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<meta name="color-scheme" content="light">
<title>${heading}</title>
<style>
:root { color-scheme: light; --bg: #f7f8fa; --card: #fff; --text: #262626; --muted: #8c8c8c; --line: #e8e8e8; --accent: #ff4800;
        --soft: #fff1e6; --top: #001529; --topsub: #a3b1bf;
        --note: #fff7f0; --noteline: #ffd8bf; --q: #d48806; --idle: #f5222d; }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
       font: 14px/1.5 "Helvetica Neue", Helvetica, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", Arial, sans-serif; }
/* Navy top bar */
header { background: var(--top); color: #fff; display: flex; align-items: center; gap: 12px;
         padding: 14px max(16px, calc((100% - 760px) / 2 + 16px)); }
header > div { flex: 1; min-width: 0; }
h1 { font-size: 20px; font-weight: 600; margin: 0; color: #fff; }
.sub { color: var(--topsub); font-size: 13px; margin-top: 2px; }
/* Tabs: white strip, the open one orange with an orange underline */
nav { position: sticky; top: 0; z-index: 2; background: var(--card); border-bottom: 1px solid var(--line); }
nav div { display: flex; gap: 4px; padding: 0 16px; overflow-x: auto; max-width: 760px; margin: 0 auto; }
button { font: inherit; cursor: pointer; }
nav button { border: 0; border-bottom: 2px solid transparent; background: none; color: var(--text);
             padding: 12px 14px 10px; font-size: 15px; white-space: nowrap; }
nav button:hover { color: var(--accent); }
nav button.on { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.seg button { border: 1px solid var(--line); background: var(--card); color: var(--text); padding: 6px 15px; font-size: 14px; }
.seg button.on { background: var(--soft); border-color: var(--accent); color: var(--accent); font-weight: 600; }
main { padding: 0 16px 36px; max-width: 760px; margin: 0 auto; }
section { background: var(--card); border: 1px solid var(--line); border-radius: 4px; padding: 14px 16px; margin-top: 12px;
          box-shadow: 0 1px 2px rgba(0, 21, 41, .04); }
/* Section titles with the orange bar on the left */
h2 { font-size: 16px; font-weight: 600; margin: 0 0 4px; padding-left: 10px; border-left: 4px solid var(--accent); line-height: 1.2; }
.explain { color: var(--muted); font-size: 13px; margin: 0 0 6px; }
.row { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; column-gap: 8px; row-gap: 4px;
       align-items: center; padding: 8px 0; border-top: 1px solid var(--line); }
.row:first-of-type { border-top: 0; }
.rank { color: var(--muted); font-size: 12px; text-align: right; font-variant-numeric: tabular-nums; }
.name { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.meta { color: var(--muted); font-size: 12px; font-weight: 400; white-space: normal; }
.rate { text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.rate b { font-size: 15px; } .rate span { display: block; color: var(--muted); font-size: 12px; }
.bar { grid-column: 2 / 4; height: 9px; background: var(--line); border-radius: 5px; overflow: hidden; }
.bar i { display: block; height: 100%; border-radius: 5px; }
.q .rank, .q .name, .q .who { color: var(--q); }
.who { font-weight: 600; }
.idle .rank, .idle .name { color: var(--idle); }
.notes { background: var(--note); border-color: var(--noteline); }
.notes p { margin: 6px 0; } .notes b { font-weight: 600; }
.seg { display: flex; gap: 6px; margin: 8px 0 4px; }
.seg button { flex: 1; padding: 6px 8px; white-space: normal; border-radius: 4px; }
.empty { color: var(--muted); font-size: 14px; padding: 6px 0; }
.help { color: var(--muted); font-size: 12px; margin: 16px 2px 0; }
.row.click { cursor: pointer; border-radius: 4px; }
.row.click:hover { background: color-mix(in srgb, var(--accent) 7%, transparent); }
.backdrop { position: fixed; inset: 0; z-index: 10; background: rgba(0, 0, 0, .45); display: flex; align-items: flex-end; justify-content: center; }
.sheet { background: var(--card); color: var(--text); width: 100%; max-width: 760px; max-height: 92vh; overflow: auto;
         border-radius: 8px 8px 0 0; padding: 14px 16px 22px; box-sizing: border-box; }
@media (min-width: 700px) { .backdrop { align-items: center; } .sheet { border-radius: 8px; } }
.sheet-head { display: flex; align-items: flex-start; gap: 10px; }
.sheet-head h3 { margin: 0; font-size: 18px; }
.sheet-head .meta { font-size: 13px; margin-top: 2px; }
.close { border: 0; background: var(--line); color: var(--text); width: 32px; height: 32px; border-radius: 4px; font-size: 18px; line-height: 1; cursor: pointer; }
.stats { display: flex; gap: 18px; flex-wrap: wrap; margin: 10px 0 4px; }
.stats div { font-size: 12px; color: var(--muted); } .stats b { display: block; font-size: 18px; color: var(--text); font-variant-numeric: tabular-nums; }
.chart { position: relative; margin-top: 6px; }
.chart svg { display: block; width: 100%; height: auto; touch-action: pan-y; }
.chart text { fill: var(--muted); font-size: 12px; font-family: inherit; }
.modes { display: flex; gap: 6px; padding: 10px 16px 0; max-width: 760px; margin: 0 auto; }
.modes button { flex: 1; border: 1px solid var(--line); background: var(--card); color: var(--text); padding: 8px 10px;
                font-size: 15px; border-radius: 4px; }
.modes button.on { background: var(--accent); border-color: var(--accent); color: #fff; font-weight: 600; }
.star { color: var(--q); font-weight: 700; }
.warn { color: var(--q); font-size: 13px; margin: 10px 0 0; }
.names { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.names figure { margin: 0; border: 1px solid var(--line); border-radius: 4px; padding: 4px; max-width: calc(50% - 4px); background: #fff; }
.names img { display: block; max-width: 100%; height: auto; }
.names figcaption { font-size: 12px; color: var(--muted); margin-top: 2px; }
h4 { font-size: 14px; margin: 14px 0 0; }
.tip { position: absolute; pointer-events: none; background: var(--text); color: var(--card); font-size: 12px; padding: 4px 8px;
       border-radius: 4px; white-space: nowrap; transform: translate(-50%, -115%); display: none; }
</style>
</head>
<body>
<header><div><h1 id="title">${heading}</h1><div class="sub" id="sub"></div></div></header>
<nav><div class="modes" id="modes"></div><div id="tabs"></div></nav>
<main id="main"></main>
<div id="detail"></div>
<script>
const DATA = ${data};
// Every word on the page, in one place.
const clockOf = (h, m) => (h % 12 || 12) + ":" + String(m).padStart(2, "0") + (h < 12 ? " AM" : " PM");
const TEXT = {
  en: {
    htmlLang: "en", title: "PDA speed board",
    tabs: { ALL: "All", VA: "Vendor A", VB: "Vendor B", VC: "Vendor C", OTHER: "Other" },
    periods: { "7": "Last 7 days", "30": "Last 30 days", all: "All time" },
    active: { all: "Everyone", recent: "Scanned in the last hour", quiet: "Not in the last hour" },
    views: { day: "Day", week: "Week", month: "Month" },
    weekdays: ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"],
    noData: "No data yet.", noOne: "No one yet.", perHour: "/hr", secEach: " sec each", nameQ: "name?",
    clock: (hm) => { const [h, m] = hm.split(":").map(Number); return clockOf(h, m); },
    sub: (live, names) => "Live from WMS at " + live + " · names from sheets read at " + names,
    subNoSheets: (live) => "Live from WMS at " + live + " · no sign-in sheets posted yet, so no names",
    updated: (day, time) => "Updated " + day + " at " + time,
    allMeans: " · All = Vendors A, B and C",
    parcelsIn: (p, h) => p + " parcels in " + h + " h",
    wrote: (w) => 'wrote "' + w + '"',
    lastScanned: (when, box) => "last scanned " + (box ? box + " " : "") + when,
    ago: (m) => (m < 5 ? "<5 min ago" : m < 60 ? m + " min ago" : Math.floor(m / 60) + " h " + (m % 60) + " min ago"),
    hourRange: (col) => {
      const h = (6 + col) % 24, next = (h + 1) % 24, label = (x) => x % 12 || 12, half = (x) => (x < 12 ? "AM" : "PM");
      return half(h) === half(next) ? label(h) + "–" + label(next) + " " + half(next) : label(h) + " " + half(h) + "–" + label(next) + " " + half(next);
    },
    hourTick: (col) => { const h = (6 + col) % 24; return (h % 12 || 12) + (h < 12 ? "a" : "p"); },
    shiftTitle: (live, day) => (live ? "Today's shift" : "Last shift") + " (" + day + "): fastest to slowest",
    shiftExplain: 'Parcels per hour while their PDA was scanning. Short green bar = fast, long red bar = slow. A PDA in orange with "name?" is being used, but nobody signed for it.',
    idle: "signed out, but it scanned nothing",
    wrong: "What looks wrong",
    boardTitle: "Average speed leaderboard",
    boardExplain: (since) => "Average parcels per hour over every day they used a PDA" + (since ? " since " + since : "") + ".",
    daysParcels: (d, p) => d + (d === 1 ? " day" : " days") + " · " + p + " parcels",
    help: "Tap anyone to see their speed hour by hour, or day by day over a week or a month. Names are the sheet reader's best reading of the handwritten sign-out sheets, matched to attendance when it can. Speed counts only hours the PDA actually scanned, so it works before attendance comes in.",
    nobodySigned: "nobody signed for this PDA", close: "Close",
    speed: "Speed", parcels: "Parcels", hoursScanning: "Hours scanning", bestHour: "Best hour", average: "Average", daysWorked: "Days worked",
    dayExplain: (live, day) => (live ? "Today" : day) + ": parcels scanned in each hour. An hour they only worked part of shows lower.",
    rangeExplain: (count) => "Their speed (parcels per hour) each day of the last " + count + " days. Gaps are days they didn't work.",
    hourTip: (range, p) => range + ": " + p + " parcels",
    dayTip: (rate, p, h) => ": " + rate + "/hr · " + p + " parcels in " + h + " h",
    off: ": didn't work",
    modes: { speed: "Speed", errors: "Error rates" },
    errorTitle: "PDA error board",
    errorsHead: "Missed scans and mis-sorts per 10,000 parcels",
    errorsExplain: (since, avg) => "Each person's missed bagging scans and mis-sorts" + (since ? " since " + since : "") +
      ", per 10,000 parcels they scanned. Highest first." + (avg != null ? " Crew average: " + avg + " per 10k." : ""),
    perTenK: " /10k", missedN: (n) => n + " missed", missortN: (n) => n + " mis-sorted",
    nightsParcels: (d, p) => d + (d === 1 ? " night" : " nights") + " · " + p + " parcels",
    nameGuess: "name not confirmed",
    notEnough: "* Not ranked yet: under 10,000 parcels. One or two misses move these rates a lot, so don't judge them yet.",
    errorsHelp: "A miss counts for whoever had the PDA then (from the timesheet or sign-out sheet): a bagging scan into the wrong station's bag, a 'bagging failed' that still left that night, a check-in with no bagging scan, or, for a package no one scanned, the PDA that bagged its box-mates in the same section. When two PDAs bagged those back and forth, each gets its share (like 0.7 and 0.3). A whole bag sent to the wrong place, or a box no one had to sort, isn't anyone's, and neither is a moment two people could have had the PDA. “Name not confirmed” means the sheet reader read the handwriting but nobody has checked it.",
    noErrors: "No error numbers yet.",
    errorsUpdated: (d) => "Error numbers updated " + d,
    errorRate: (p) => "Error rate, " + p.toLowerCase(), errorCounts: (m, s) => m + " missed · " + s + " mis-sorted",
    onSheet: "Their name on the sign-out sheets", noSheetPics: "No pictures of their name from the sign-out sheets yet.",
    sheetPic: (day, w) => day + ' · wrote "' + w + '"',
  },
};
const L = () => TEXT.en;
// A labor company's own board has only its own tab, and its name in the title.
const COMPANY = DATA.company;
const COMPANY_NAMES = { VA: "Vendor A", VB: "Vendor B", VC: "Vendor C" };
const titled = (t) => (COMPANY ? COMPANY_NAMES[COMPANY] + " · " + t : t);
const TABS = COMPANY ? [COMPANY] : ["ALL", "VA", "VB", "VC", "OTHER"];
const NAMES = new Proxy({}, { get: (_, k) => L().tabs[k] });
const PERIODS = ["7", "30", "all"];
let tab = (location.hash.slice(1).replace(/^errors-?/, "") || TABS[0]).toUpperCase();
if (!TABS.includes(tab)) tab = TABS[0];
let mode = location.hash.startsWith("#errors") ? "errors" : "speed";
let period = "7";
try { period = localStorage.getItem("pda-period") || "7"; } catch (e) {}
let active = "all";
try { active = localStorage.getItem("pda-active") || "all"; } catch (e) {}

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const n = (v) => Number(v).toLocaleString("en-US");
const md = (day) => { const [, m, d] = day.split("-"); return +m + "/" + +d; };
// "<5 min ago" while it's scanning (the copies are 5 minutes apart), else the minutes.
const ago = (m) => L().ago(m);
// Hour column 0 is 6 AM: "3–4 PM", "11 AM–12 PM". Used when the 5-minute copies have no time yet.
const hourRange = (col) => L().hourRange(col);
const tabsFor = (t) => (t === "ALL" ? ["VA", "VB", "VC"] : [t]);

// Short green bars for fast people, long red bars for slow: the bar is seconds per parcel, so a
// 200/hr person's bar is 3 times a 600/hr person's. The full width is twice the median, so one very
// slow person doesn't squash everyone else's bar; anyone slower than that just fills the bar.
function barScale(list) {
  const secs = list.map((p) => 3600 / p.rate).sort((a, b) => a - b);
  const lo = secs[0], median = secs[Math.floor(secs.length / 2)];
  const full = Math.max(Math.min(2 * median, secs[secs.length - 1]), lo + 0.1);
  return (p) => {
    const sec = 3600 / p.rate;
    const t = Math.min(Math.max((sec - lo) / (full - lo), 0), 1);
    return { width: Math.max(4, Math.min(sec / full, 1) * 100), color: "hsl(" + Math.round(120 * (1 - t)) + " 68% 45%)", sec };
  };
}

// scale: the list the bars are measured against, so filtering doesn't change anyone's bar.
function speedRows(list, withTab, scale = list) {
  if (!list.length) return '<div class="empty">' + L().noOne + "</div>";
  const bar = barScale(scale);
  // A PDA nobody signed for shows the PDA's name, with "name?" where the PDA usually goes.
  return list.map((p, i) => {
    const b = bar(p);
    return '<div class="row click' + (p.unsigned ? " q" : "") + '" data-key="' + esc(p.key) + '"><div class="rank">' + (p.rank ?? i + 1) + '</div><div class="name">' + esc(p.name) +
      '<div class="meta">' + [withTab ? esc(NAMES[p.tab]) : "", p.unsigned ? '<span class="who">' + L().nameQ + "</span>" : esc(p.where), esc(p.detail)]
        .filter(Boolean).join(" · ") + '</div></div>' +
      '<div class="rate"><b>' + n(Math.round(p.rate)) + L().perHour + "</b><span>" + b.sec.toFixed(1) + L().secEach + "</span></div>" +
      '<div class="bar"><i style="width:' + b.width + '%;background:' + b.color + '"></i></div></div>';
  }).join("");
}

// Scanned in the last hour: from the 5-minute copies, or else the hour column it last scanned in.
function scannedLastHour(p, nowCol) {
  if (p.lastScan != null) return p.lastScan < 60;
  return p.lastHour != null && nowCol != null && p.lastHour >= nowCol;
}
const ACTIVE = ["all", "recent", "quiet"];

function today(board) {
  const tabs = tabsFor(tab);
  const people = tabs.flatMap((t) => (board.tabs[t]?.people || []).map((p) => ({
    name: p.name, tab: p.tab, rate: p.parcels / p.hours, where: p.pdas.join(", "), unsigned: p.unsigned,
    lastScan: p.lastScan, lastHour: p.lastHour, lastBox: p.lastBox, key: keyOf(p),
    detail: L().parcelsIn(n(p.parcels), p.hours) + (p.written && p.written !== p.name ? " · " + L().wrote(p.written) : "") +
      (!board.live ? "" : p.lastScan != null ? " · " + L().lastScanned(ago(p.lastScan), p.lastBox)
        : p.lastHour != null ? " · " + L().lastScanned(hourRange(p.lastHour), p.lastBox) : ""),
  }))).sort((a, b) => b.rate - a.rate).map((p, i) => ({ ...p, rank: i + 1 }));
  let idle = tabs.flatMap((t) => (board.tabs[t]?.idle || []).map((u) => ({ ...u, tab: t })));
  const nowCol = board.liveAt ? (+board.liveAt.slice(0, 2) - 6 + 24) % 24 : null;
  const filtering = board.live && nowCol != null;
  let shown = people;
  if (filtering && active !== "all") {
    shown = people.filter((p) => scannedLastHour(p, nowCol) === (active === "recent"));
    if (active === "recent") idle = [];
  }
  const notes = tabs.flatMap((t) => (board.tabs[t]?.notes || []).map((x) => ({ text: x, tab: t })));
  let html = "<section><h2>" + esc(L().shiftTitle(board.live, md(board.day))) + '</h2><p class="explain">' + esc(L().shiftExplain) + "</p>" +
    (filtering ? '<div class="seg">' + ACTIVE.map((k) => '<button data-active="' + k + '"' + (k === active ? ' class="on"' : "") + ">" + L().active[k] +
      (k === "all" ? "" : " (" + people.filter((p) => scannedLastHour(p, nowCol) === (k === "recent")).length + ")") + "</button>").join("") + "</div>" : "") +
    speedRows(shown, tab === "ALL", people);
  html += idle.map((u) => '<div class="row idle"><div class="rank">0</div><div class="name">' + esc(u.name) +
    '<div class="meta">' + (tab === "ALL" ? NAMES[u.tab] + " · " : "") + esc(u.pda) + " · " + L().idle + '</div></div><div class="rate"></div></div>').join("");
  html += "</section>";
  if (notes.length) {
    html += '<section class="notes"><h2>' + L().wrong + "</h2>" +
      notes.map((x) => "<p>" + (tab === "ALL" ? "<b>" + NAMES[x.tab] + ":</b> " : "") + esc(x.text) + "</p>").join("") + "</section>";
  }
  return html;
}

function leaderboard() {
  const days = Object.keys(DATA.history).sort();
  const last = days[days.length - 1];
  let from = "";
  if (last && period !== "all") {
    const d = new Date(last + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() - (+period - 1));
    from = d.toISOString().slice(0, 10);
  }
  const people = {};
  for (const day of days) {
    if (day < from) continue;
    for (const [name, t, id, parcels, hours] of DATA.history[day]) {
      if (!tabsFor(tab).includes(t) || !hours) continue;
      const key = (id || name.toLowerCase()) + "|" + t;
      const p = (people[key] ||= { name, tab: t, parcels: 0, hours: 0, days: 0, key });
      p.parcels += parcels; p.hours += hours; p.days += 1; p.name = name;
    }
  }
  const list = Object.values(people).map((p) => ({ ...p, rate: p.parcels / p.hours,
    detail: L().daysParcels(p.days, n(p.parcels)) })).sort((a, b) => b.rate - a.rate);
  return '<section class="board-avg"><h2>' + L().boardTitle + '</h2><div class="seg">' +
    PERIODS.map((k) => '<button data-period="' + k + '"' + (k === period ? ' class="on"' : "") + ">" + L().periods[k] + "</button>").join("") +
    '</div><p class="explain">' + esc(L().boardExplain(from ? md(from) : "")) + "</p>" +
    speedRows(list, tab === "ALL") + "</section>";
}

// ---- Error rates: missed scans and mis-sorts per 10,000 parcels, per person, from the quality job.
const ENOUGH = 10000; // Parcels before a rate means much; under it, an asterisk and a warning
// A miss bagged back and forth by two PDAs is split between them, so counts can be 0.7, 12.3 …
const dec = (v) => (Math.round(v * 10) / 10).toLocaleString("en-US");
function errorsView() {
  const E = DATA.errors;
  if (!E || !E.days || !Object.keys(E.days).length) return '<section><div class="empty">' + L().noErrors + "</div></section>";
  const days = Object.keys(E.days).sort();
  const last = days[days.length - 1];
  let from = "";
  if (period !== "all") {
    const d = new Date(last + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() - (+period - 1));
    from = d.toISOString().slice(0, 10);
  }
  const people = {};
  for (const day of days) {
    if (day < from) continue;
    for (const [name, crew, key, parcels, missed, missorts, match] of E.days[day]) {
      if (!tabsFor(tab).includes(crew)) continue;
      const p = (people[key + "|" + crew] ||= { name, crew, key, parcels: 0, missed: 0, missorts: 0, nights: 0, guessed: false });
      p.parcels += parcels; p.missed += missed; p.missorts += missorts; p.nights += 1; p.name = name;
      p.guessed ||= !["checked", "high", "medium"].includes(match); // Any unconfirmed night shows it
    }
  }
  const list = Object.values(people).filter((p) => p.parcels > 0)
    .map((p) => ({ ...p, rate: ((p.missed + p.missorts) * 10000) / p.parcels }))
    .sort((a, b) => b.rate - a.rate || b.parcels - a.parcels);
  const totalP = list.reduce((a, p) => a + p.parcels, 0), totalE = list.reduce((a, p) => a + p.missed + p.missorts, 0);
  const avg = totalP ? (totalE * 10000) / totalP : null;
  // Bar: the rate against twice the crew average; green under the average, red over it.
  const full = Math.max(1, 2 * (avg || 1), ...list.filter((p) => p.parcels >= ENOUGH).map((p) => p.rate));
  const row = (p, rank) => {
    const t = avg ? Math.min(p.rate / (2 * avg), 1) : 0;
    const short = p.parcels < ENOUGH;
    const open = boardKeyFor(p.key, p.crew);
    return '<div class="row' + (open ? ' click" data-key="' + esc(open) + '"' : '"') + '><div class="rank">' + rank + '</div><div class="name">' + esc(p.name) +
      (short ? ' <span class="star">*</span>' : "") + '<div class="meta">' +
      [tab === "ALL" ? esc(NAMES[p.crew]) : "", L().nightsParcels(p.nights, n(p.parcels)),
       L().missedN(dec(p.missed)) + " · " + L().missortN(dec(p.missorts)), p.guessed ? L().nameGuess : ""].filter(Boolean).join(" · ") +
      '</div></div><div class="rate"><b>' + p.rate.toFixed(1) + L().perTenK + "</b><span>" + dec(p.missed + p.missorts) + "</span></div>" +
      '<div class="bar"><i style="width:' + Math.max(3, Math.min(p.rate / full, 1) * 100) + "%;background:hsl(" +
      Math.round(120 * (1 - t)) + ' 68% 45%)"></i></div></div>';
  };
  // Ranked: people with enough parcels for the rate to mean something. Under 10,000 parcels, one or two
  // misses swing the rate wildly (135 parcels and 8 misses is "593 per 10k"), so they're listed after.
  const ranked = list.filter((p) => p.parcels >= ENOUGH), few = list.filter((p) => p.parcels < ENOUGH);
  const rows = ranked.map((p, i) => row(p, String(i + 1))).join("") +
    (few.length ? '<p class="warn">' + esc(L().notEnough) + "</p>" + few.map((p) => row(p, "*")).join("") : "");
  return "<section><h2>" + L().errorsHead + '</h2><div class="seg">' +
    PERIODS.map((k) => '<button data-period="' + k + '"' + (k === period ? ' class="on"' : "") + ">" + L().periods[k] + "</button>").join("") +
    '</div><p class="explain">' + esc(L().errorsExplain(from ? md(from) : "", avg == null ? null : avg.toFixed(1))) + "</p>" +
    (rows || '<div class="empty">' + L().noOne + "</div>") + "</section>";
}

// One person's errors over the error board's period: {parcels, missed, missorts, rate} or null. An
// error row's key is the employee ID, or "name|PDA" for a name nobody confirmed.
const whoOf = (key) => String(key).split("|")[0].toLowerCase();
function errorStatsFor(key) {
  const E = DATA.errors;
  if (!E || !E.days || !key) return null;
  const [who, tab] = [whoOf(key), key.split("|").pop()];
  const days = Object.keys(E.days).sort();
  let from = "";
  if (period !== "all" && days.length) {
    const d = new Date(days[days.length - 1] + "T12:00:00Z");
    d.setUTCDate(d.getUTCDate() - (+period - 1));
    from = d.toISOString().slice(0, 10);
  }
  let parcels = 0, missed = 0, missorts = 0;
  for (const day of days) {
    if (day < from) continue;
    for (const [_name, crew, ekey, n, m, s] of E.days[day]) {
      if (whoOf(ekey) === who && (crew === tab || tab === "OTHER")) { parcels += n; missed += m; missorts += s; }
    }
  }
  return parcels ? { parcels, missed, missorts, rate: ((missed + missorts) * 10000) / parcels } : null;
}
// The speed board's key for an error row's person, so tapping it opens the same details.
function boardKeyFor(ekey, crew) {
  const who = whoOf(ekey);
  const keys = new Set();
  for (const rows of Object.values(DATA.history)) for (const [name, t, id] of rows) keys.add((id || name.toLowerCase()) + "|" + t);
  for (const t of Object.keys(DATA.board?.tabs || {})) for (const p of DATA.board.tabs[t].people || []) keys.add(keyOf(p));
  // Employee IDs keep their case in board keys; names are lower case. Compare without case.
  const all = [...keys], lower = (k) => k.toLowerCase();
  return [who + "|" + crew, who + "|other"].map((w) => all.find((k) => lower(k) === lower(w))).find(Boolean)
    || all.find((k) => lower(k).startsWith(who + "|")) || "";
}

// ---- One person's chart: tap a row. Day = their parcels each hour; Week / Month = their average
// speed each day they worked. Which one is open survives the page's 1-minute reload.
const keyOf = (p) => (p.unsigned ? "pda:" + p.name : (p.id || p.name.toLowerCase())) + "|" + p.tab;
const VIEWS = ["day", "week", "month"];
let openKey = null, view = "day", chart = null;
try { const o = JSON.parse(sessionStorage.getItem("pda-open") || "null"); if (o) { openKey = o.key; view = o.view; } } catch (e) {}
const saveOpen = () => { try { sessionStorage.setItem("pda-open", JSON.stringify(openKey ? { key: openKey, view } : null)); } catch (e) {} };
const hourLabel = (col) => L().hourTick(col);

function todayRow(key) {
  for (const t of Object.keys(DATA.board?.tabs || {})) {
    const p = (DATA.board.tabs[t].people || []).find((x) => keyOf(x) === key);
    if (p) return p;
  }
  return null;
}

function historyOf(key) {
  const days = {};
  let who = null;
  for (const [day, rows] of Object.entries(DATA.history)) {
    for (const [name, t, id, parcels, hours] of rows) {
      if ((id || name.toLowerCase()) + "|" + t === key && hours) { days[day] = { parcels, hours }; who = { name, tab: t }; }
    }
  }
  return { days, who };
}

// points: [{label, tip, y (null = didn't work)}]. One line, so no legend: the heading names it.
function lineChart(points, label) {
  // Drawn at the sheet's real width, so the labels stay 12px on a phone.
  const W = Math.round(Math.min(728, Math.max(280, (window.innerWidth || 640) - 32))), H = W < 500 ? 200 : 230, L = 44, R = 14, T = 14, B = 28;
  const ys = points.map((p) => p.y).filter((y) => y != null);
  const raw = Math.max(1, ...ys);
  const base = Math.pow(10, Math.floor(Math.log10(raw / 4)));
  const step = [1, 2, 5, 10].map((m) => m * base).find((s) => raw / s <= 4);
  const top = Math.ceil(raw / step) * step;
  const x = (i) => L + (points.length === 1 ? (W - L - R) / 2 : (i * (W - L - R)) / (points.length - 1));
  const y = (v) => T + (1 - v / top) * (H - T - B);
  let svg = '<svg viewBox="0 0 ' + W + " " + H + '" role="img" aria-label="' + esc(label) + '">';
  for (let v = 0; v <= top; v += step) {
    svg += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="var(--line)" stroke-width="1"/>' +
      '<text x="' + (L - 8) + '" y="' + (y(v) + 4) + '" text-anchor="end">' + n(v) + "</text>";
  }
  const every = Math.ceil(points.length / Math.max(2, Math.floor((W - L - R) / 72)));
  points.forEach((p, i) => {
    if (i % every === 0 || i === points.length - 1) {
      svg += '<text x="' + x(i) + '" y="' + (H - 8) + '" text-anchor="' + (i === 0 ? "start" : i === points.length - 1 ? "end" : "middle") + '">' + esc(p.label) + "</text>";
    }
  });
  // The line breaks over days they didn't work; each run of points gets a light area under it.
  const runs = [];
  let cur = [];
  points.forEach((p, i) => { if (p.y == null) { if (cur.length) runs.push(cur); cur = []; } else cur.push(i); });
  if (cur.length) runs.push(cur);
  for (const run of runs) {
    const line = run.map((i, k) => (k ? "L" : "M") + x(i) + " " + y(points[i].y)).join(" ");
    if (run.length > 1) {
      svg += '<path d="' + line + " L" + x(run[run.length - 1]) + " " + y(0) + " L" + x(run[0]) + " " + y(0) +
        ' Z" fill="var(--accent)" fill-opacity="0.12"/>';
    }
    svg += '<path d="' + line + '" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>';
  }
  points.forEach((p, i) => {
    if (p.y != null) svg += '<circle cx="' + x(i) + '" cy="' + y(p.y) + '" r="4" fill="var(--accent)" stroke="var(--card)" stroke-width="2"/>';
  });
  svg += '<line class="cross" x1="0" x2="0" y1="' + T + '" y2="' + (H - B) + '" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>';
  svg += '<circle class="hot" r="6" fill="var(--accent)" stroke="var(--card)" stroke-width="2" visibility="hidden"/></svg>';
  chart = { points, xs: points.map((_, i) => x(i)), ys: points.map((p) => (p.y == null ? null : y(p.y))), W };
  return '<div class="chart">' + svg + '<div class="tip"></div></div>';
}

function detail() {
  if (!openKey) return "";
  const board = DATA.board;
  const now = todayRow(openKey);
  const { days, who } = historyOf(openKey);
  if (!now && !who) { openKey = null; saveOpen(); return ""; }
  const name = now?.name || who.name, tabName = NAMES[now?.tab || who.tab];
  const views = VIEWS.filter((k) => (k === "day" ? Boolean(now?.hourly) : !now?.unsigned));
  if (!views.length) return "";
  if (!views.includes(view)) view = views[0];
  let points = [], stats = [], explain = "";
  if (view === "day") {
    const hourly = now.hourly;
    const active = hourly.map((v, i) => (v ? i : -1)).filter((i) => i >= 0);
    const from = Math.max(0, (active[0] ?? 0) - 1), to = Math.min(23, (active[active.length - 1] ?? 0) + 1);
    for (let c = from; c <= to; c++) points.push({ label: hourLabel(c), y: hourly[c], tip: L().hourTip(hourRange(c), n(hourly[c])) });
    const best = active.reduce((b, c) => (hourly[c] > hourly[b] ? c : b), active[0]);
    stats = [[L().speed, n(Math.round(now.parcels / now.hours)) + L().perHour], [L().parcels, n(now.parcels)], [L().hoursScanning, String(now.hours)]];
    if (best != null) stats.push([L().bestHour, hourRange(best)]);
    explain = L().dayExplain(board.live, md(board.day));
  } else {
    const count = view === "week" ? 7 : 30;
    const end = new Date((board?.day || Object.keys(DATA.history).sort().pop()) + "T12:00:00Z");
    let parcels = 0, hours = 0, worked = 0;
    for (let k = count - 1; k >= 0; k--) {
      const d = new Date(end);
      d.setUTCDate(d.getUTCDate() - k);
      const day = d.toISOString().slice(0, 10), rec = days[day];
      const label = (view === "week" ? L().weekdays[d.getUTCDay()] + " " : "") + md(day);
      if (rec) { parcels += rec.parcels; hours += rec.hours; worked += 1; }
      points.push({ label, y: rec ? Math.round(rec.parcels / rec.hours) : null,
        tip: label + (rec ? L().dayTip(n(Math.round(rec.parcels / rec.hours)), n(rec.parcels), rec.hours) : L().off) });
    }
    stats = [[L().average, hours ? n(Math.round(parcels / hours)) + L().perHour : "–"], [L().daysWorked, String(worked)], [L().parcels, n(parcels)]];
    explain = L().rangeExplain(count);
  }
  const err = errorStatsFor(openKey);
  if (err) stats.push([L().errorRate(L().periods[period]), err.rate.toFixed(1) + L().perTenK]);
  const pictures = (DATA.names || {})[openKey] || [];
  const base = location.pathname.replace(/[/]+$/, ""); // (No backslashes: this script sits in a template string)
  const sheet = "<h4>" + esc(L().onSheet) + "</h4>" + (pictures.length
    ? '<div class="names">' + pictures.map(([id, day, written]) => '<figure><img src="' + esc(base + "/name/" + id) +
        '" alt="' + esc(written) + '" loading="lazy" onerror="this.parentNode.remove()"><figcaption>' +
        esc(L().sheetPic(md(day), written)) + "</figcaption></figure>").join("") + "</div>"
    : '<p class="explain">' + esc(L().noSheetPics) + "</p>");
  return '<div class="backdrop" data-close="1"><div class="sheet" role="dialog" aria-label="' + esc(name) + '">' +
    '<div class="sheet-head"><div style="flex:1;min-width:0"><h3>' + esc(name) + '</h3><div class="meta">' + esc(tabName) +
    (now ? " · " + esc(now.pdas.join(", ")) : "") + (now?.unsigned ? " · " + L().nobodySigned : "") + "</div></div>" +
    '<button class="close" data-close="1" aria-label="' + L().close + '">×</button></div>' +
    '<div class="seg">' + views.map((k) => '<button data-view="' + k + '"' + (k === view ? ' class="on"' : "") + ">" + L().views[k] + "</button>").join("") + "</div>" +
    '<div class="stats">' + stats.map(([k, v]) => "<div><b>" + esc(v) + "</b>" + esc(k) + "</div>").join("") + "</div>" +
    (err ? '<p class="explain">' + esc(L().errorCounts(dec(err.missed), dec(err.missorts))) + "</p>" : "") +
    '<p class="explain">' + esc(explain) + "</p>" + lineChart(points, name + ": " + explain) + sheet + "</div></div>";
}

// Crosshair + tooltip: follows the pointer (or a tap) to the nearest point that has a value.
function hover(e) {
  const box = e.target.closest ? e.target.closest(".chart") : null;
  if (!box || !chart) return;
  const svg = box.querySelector("svg"), r = svg.getBoundingClientRect();
  const px = ((e.clientX - r.left) / r.width) * chart.W;
  let best = -1;
  chart.xs.forEach((x, i) => { if (chart.ys[i] != null && (best < 0 || Math.abs(x - px) < Math.abs(chart.xs[best] - px))) best = i; });
  if (best < 0) return;
  const cross = svg.querySelector(".cross"), hot = svg.querySelector(".hot"), tip = box.querySelector(".tip");
  cross.setAttribute("x1", chart.xs[best]); cross.setAttribute("x2", chart.xs[best]); cross.setAttribute("visibility", "visible");
  hot.setAttribute("cx", chart.xs[best]); hot.setAttribute("cy", chart.ys[best]); hot.setAttribute("visibility", "visible");
  const scale = r.width / chart.W;
  tip.textContent = chart.points[best].tip;
  tip.style.display = "block";
  tip.style.left = Math.min(Math.max(chart.xs[best] * scale, 80), r.width - 80) + "px";
  tip.style.top = chart.ys[best] * scale + "px";
}
document.addEventListener("pointermove", hover);
document.addEventListener("pointerdown", hover);

function render() {
  const board = DATA.board;
  document.documentElement.lang = L().htmlLang;
  const title = titled(mode === "errors" ? L().errorTitle : L().title);
  document.title = title;
  document.getElementById("title").textContent = title;
  document.getElementById("modes").innerHTML = ["speed", "errors"].map((k) =>
    '<button data-mode="' + k + '"' + (k === mode ? ' class="on"' : "") + ">" + L().modes[k] + "</button>").join("");
  document.getElementById("tabs").innerHTML = COMPANY ? "" : TABS.map((k) =>
    '<button data-tab="' + k + '"' + (k === tab ? ' class="on"' : "") + ">" + L().tabs[k] + "</button>").join("");
  document.getElementById("sub").textContent = !board ? L().noData
    : (board.liveAt && board.noSheets ? L().subNoSheets(L().clock(board.liveAt))
      : board.liveAt ? L().sub(L().clock(board.liveAt), L().clock(board.updated.slice(11)))
      : L().updated(md(board.updated.slice(0, 10)), L().clock(board.updated.slice(11)))) +
      (tab === "ALL" ? L().allMeans : "");
  if (mode === "errors") {
    document.getElementById("sub").textContent = (DATA.errors?.updated ? L().errorsUpdated(DATA.errors.updated) : L().noErrors) +
      (tab === "ALL" ? L().allMeans : "");
    document.getElementById("main").innerHTML = errorsView() + '<p class="help">' + esc(L().errorsHelp) + "</p>";
    document.getElementById("detail").innerHTML = detail();
    document.body.style.overflow = openKey ? "hidden" : "";
    return;
  }
  document.getElementById("main").innerHTML = (board ? today(board) : "") + leaderboard() +
    '<p class="help">' + esc(L().help) + "</p>";
  document.getElementById("detail").innerHTML = detail();
  document.body.style.overflow = openKey ? "hidden" : "";
}

document.addEventListener("click", (e) => {
  const row = e.target.closest("[data-key]"), v = e.target.closest("[data-view]"), close = e.target.closest("[data-close]");
  if (v) { view = v.dataset.view; saveOpen(); render(); return; }
  if (close && (e.target === close || close.classList.contains("close"))) { openKey = null; saveOpen(); render(); return; }
  if (row) {
    openKey = row.dataset.key;
    view = row.closest(".board-avg") ? "week" : "day"; // From the leaderboard, start on the week
    saveOpen(); render(); return;
  }
  const m = e.target.closest("[data-mode]");
  if (m) { mode = m.dataset.mode; history.replaceState(null, "", "#" + (mode === "errors" ? "errors-" : "") + tab.toLowerCase()); render(); return; }
  const t = e.target.closest("[data-tab]"), p = e.target.closest("[data-period]"), a = e.target.closest("[data-active]");
  if (a) { active = a.dataset.active; try { localStorage.setItem("pda-active", active); } catch (err) {} render(); }
  if (t) { tab = t.dataset.tab; history.replaceState(null, "", "#" + (mode === "errors" ? "errors-" : "") + tab.toLowerCase()); render(); }
  if (p) { period = p.dataset.period; try { localStorage.setItem("pda-period", period); } catch (err) {} render(); }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openKey) { openKey = null; saveOpen(); render(); } });
render();
// While the page is open and showing, it reloads every minute for fresh numbers.
setInterval(() => { if (!document.hidden) location.reload(); }, 60 * 1000);
</script>
</body>
</html>`;
}
