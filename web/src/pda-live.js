// Live numbers for the PDA speed board. The ops host's PDA job sends who signed for which PDA (it reads the
// sign-out sheet photos); this asks the WMS how much each PDA has scanned so far, each time the board is
// opened, and works out everyone's speed the same way the PDA job does. WMS answers are cached for a minute,
// so many people refreshing the page don't each hit the WMS. The 5-minute timer also saves a copy to KV.
import { HUB_ID, HUB_ID_KEY, TIME_ZONE, chicagoParts, operationWindow, wmsHeaders, wmsUrl } from "./hubtime.js";

const WMS_OPERATORS = "/api/sorter/operators";
const PAGE_TYPE_OPERATORS = 10; // Sorting area > sorted, by destination flow > per operator
const CACHE_SECONDS = 55; // Just under the page's 1-minute reload, so each reload gets new numbers
export const UNSIGNED_MIN = 50; // Parcels before a PDA nobody signed for is ranked (the PDA job's UNSIGNED_MIN)
export const GRACE_MINUTES = 45; // A PDA picked up this recently isn't called "not scanning" yet

export function tabOf(pda) {
  for (const [family, tab] of [["_VA_", "VA"], ["_VB_", "VB"], ["_VC_", "VC"]]) {
    if (pda.includes(family)) return tab;
  }
  return "OTHER";
}

export function shortPda(pda) {
  const sorter = /^HOU.H_sorter(\d+)$/.exec(pda);
  if (sorter) return "PDA " + String(+sorter[1]).padStart(3, "0");
  return pda.replace(/^HOU.H_/, "");
}

// Minutes since midnight of the operating day's date: 1:30 AM after midnight is 25 * 60 + 30.
export function shiftMinutes(date) {
  const now = chicagoParts(date);
  return +now.hour * 60 + +now.minute + (+now.hour < 6 ? 1440 : 0);
}

export function hourlyFrom(operators) {
  const hourly = {};
  for (const r of operators) {
    const hours = (r.dateList || []).slice(0, 24).map((h) => Number(h.waybillCount) || 0);
    hourly[String(r.destinCenterName || "")] = hours.concat(Array(24 - hours.length).fill(0));
  }
  return hourly;
}

// Rows like the PDA job's counts tab, without the day: [pda, parcels, bags, 24 hourly counts from 6 AM].
export function rowsFrom(operators) {
  const hourly = hourlyFrom(operators);
  return operators.map((r) => {
    const pda = String(r.destinCenterName || "");
    return [pda, Number(r.waybillCount) || 0, Number(r.bagCount) || 0, ...hourly[pda]];
  });
}

export const countsKey = (day) => "pda_counts:" + day;
const KEEP_SECONDS = 8 * 86400;

// Every 5 minutes: keeps a copy of today's per-PDA numbers, so a night isn't lost if the ops host's early
// morning save fails (the WMS view resets at 6 AM). Written only when the numbers changed.
export async function savePdaCounts(env, date = new Date()) {
  if (!env.WMS_TOKEN) return;
  const live = await fetchHourly(env, date, { fresh: true });
  const key = countsKey(live.day);
  const before = await env.PLAN.get(key, "json");
  if (before && JSON.stringify(before.rows) === JSON.stringify(live.rows)) return;
  // last: when each PDA's count last went up, as seen by these 5-minute checks (epoch ms). The
  // first copy of the day can't tell, so it starts empty.
  const last = { ...(before?.last || {}) };
  if (before) {
    const had = Object.fromEntries(before.rows.map((r) => [r[0], r[1]]));
    for (const r of live.rows) if (r[1] > (had[r[0]] || 0)) last[r[0]] = date.getTime();
  }
  await env.PLAN.put(key, JSON.stringify({ day: live.day, at: live.at, rows: live.rows, last }), { expirationTtl: KEEP_SECONDS });
}

// Minutes since each PDA last scanned: 0 if it has scanned since the last 5-minute copy.
export function minutesSinceScan(saved, liveRows, now) {
  const had = Object.fromEntries((saved?.rows || []).map((r) => [r[0], r[1]]));
  const result = {};
  for (const r of liveRows || []) {
    if (saved && r[1] > (had[r[0]] || 0)) result[r[0]] = 0;
    else if (saved?.last?.[r[0]]) result[r[0]] = Math.max(0, Math.floor((now - saved.last[r[0]]) / 60000));
  }
  return result;
}

export async function fetchHourly(env, date = new Date(), { fresh = false } = {}) {
  const [start, end] = operationWindow(date);
  const cacheKey = new Request("https://pda-live.cache/" + start.slice(0, 10));
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const cached = cache && !fresh && (await cache.match(cacheKey));
  if (cached) return { day: start.slice(0, 10), ...(await cached.json()) };
  const res = await fetch(wmsUrl(env, WMS_OPERATORS), {
    method: "POST",
    headers: wmsHeaders(env),
    body: JSON.stringify({ pageType: PAGE_TYPE_OPERATORS, isSummary: 2, centerId: HUB_ID,
      centerIdEncryption: HUB_ID_KEY, startTime: start, endTime: end, timeZone: TIME_ZONE,
      startTimeFj: start, endTimeFj: end }),
  });
  const payload = await res.json().catch(() => ({}));
  if (payload.code !== 200) throw new Error(`WMS PDA numbers failed (code ${payload.code ?? res.status})`);
  const parts = chicagoParts(date);
  const live = { hourly: hourlyFrom(payload.data || []), rows: rowsFrom(payload.data || []), at: `${parts.hour}:${parts.minute}` };
  if (cache) {
    await cache.put(cacheKey, new Response(JSON.stringify(live), {
      headers: { "content-type": "application/json", "cache-control": `max-age=${CACHE_SECONDS}` } }));
  }
  return { day: start.slice(0, 10), ...live };
}

// The box each PDA last bagged into, and that box's route (AUS01-018, CXO01-030 …), for "last scanned".
// The scan history answers searches with no package numbers only with extra request headers (a page tag
// and the time-zone ones), kept in the WMS_SCAN_HEADERS secret as JSON. Without the secret, or if the WMS
// refuses, the board just shows no box.
const WMS_SCANS = "/api/scans/history";
const WMS_BAGS = "/api/bags/sorted";
const BOX_PAGES = 4; // 500 bagging scans a page, newest first: about the last half hour at a busy time
const TAG_HEADERS = ["auth-tag-x", "user-time-zone", "timezone", "date-time-format", "lang"];

const stamp = (date) => {
  const p = chicagoParts(date);
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:00`;
};

export async function fetchLastBoxes(env, date = new Date()) {
  if (!env.WMS_SCAN_HEADERS || !env.WMS_TOKEN) return {};
  const cacheKey = new Request("https://pda-live.cache/boxes");
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const cached = cache && (await cache.match(cacheKey));
  if (cached) return cached.json();
  let tag;
  try { tag = JSON.parse(env.WMS_SCAN_HEADERS); } catch { return {}; }
  const headers = wmsHeaders(env);
  for (const k of TAG_HEADERS) if (tag[k]) headers[k] = tag[k];
  const last = {};
  for (let page = 1; page <= BOX_PAGES; page++) {
    const res = await fetch(wmsUrl(env, WMS_SCANS), { method: "POST", headers, body: JSON.stringify({
      pageNum: page, pageSize: 500, operateNoType: "10", operateNos: [], operateType: 217, operatorDeptId: HUB_ID,
      operateTimeStart: stamp(new Date(date.getTime() - 90 * 60000)), operateTimeEnd: stamp(new Date(date.getTime() + 60000)) }) });
    const payload = await res.json().catch(() => ({}));
    if (payload.code !== 200) break;
    const rows = payload.data?.list || [];
    for (const r of rows) {
      const who = String(r.operatorName || "");
      if (who.startsWith("HOU.H_") && !last[who]) last[who] = { bag: r.bagNo || "", time: r.operateTime || "" };
    }
    if (rows.length < 500) break;
  }
  // Each box's route, from today's sorted-bags list (the one the hourly report reads).
  const routes = {};
  if (Object.keys(last).length) {
    const [start, end] = operationWindow(date);
    const res = await fetch(wmsUrl(env, WMS_BAGS), { method: "POST", headers: wmsHeaders(env),
      body: JSON.stringify({ centerId: HUB_ID, centerIdEncryption: HUB_ID_KEY, startTime: start, endTime: end, timeZone: TIME_ZONE,
        startTimeFj: start, endTimeFj: end, pageNum: 1, pageSize: 5000, destinCenterIdList: [], pageType: 7 }) });
    const payload = await res.json().catch(() => ({}));
    for (const b of payload.data?.records || []) routes[b.bagNo] = b.destinAreaCode || b.destinCenterName || "";
  }
  const boxes = {};
  for (const [pda, v] of Object.entries(last)) boxes[pda] = { box: routes[v.bag] || "", bag: v.bag, time: v.time };
  if (cache) {
    await cache.put(cacheKey, new Response(JSON.stringify(boxes), {
      headers: { "content-type": "application/json", "cache-control": `max-age=${CACHE_SECONDS}` } }));
  }
  return boxes;
}

// The last hour column (0 = 6 AM) in which a PDA scanned anything, or null. `from` is where `part` starts.
export function lastActiveHour(part, from) {
  for (let i = part.length - 1; i >= 0; i--) if (part[i]) return from + i;
  return null;
}
// Same as the PDA job's took_at: the start of their shift (6 AM day, 3 PM night), or the time they wrote
// if that's earlier. Later written times are too often misread, and breaks leave gaps in the scans.
export function tookAt(out, shift) {
  if (out == null) return 0;
  return Math.min(startHour(out), shift === "night" ? NIGHT_STARTS : 0);
}
const NIGHT_STARTS = 9; // Hour column 9 is 3 PM
const startHour = (out) => (out == null ? 0 : Math.min(Math.max(Math.floor((out - 360) / 60), 0), 23));
const sum = (list) => list.reduce((a, b) => a + b, 0);

// board.signins: [pda, name, written, employee id, out minutes or null], from the PDA job.
// Returns the board with each tab's people (fastest first, PDAs nobody signed for included) and
// idle names worked out from the live numbers, plus that day's leaderboard entries.
export function applyLive(board, hourly, nowMinutes, at, lastScan = {}, lastBox = {}) {
  const byPda = {};
  for (const [pda, name, written, id, out, shift] of board.signins) {
    (byPda[pda] ||= []).push({ name, written, id, out, from: tookAt(out, shift, hourly[pda] || Array(24).fill(0)) });
  }
  const people = {};
  const idle = [];
  for (const [pda, users] of Object.entries(byPda)) {
    const hours = hourly[pda] || Array(24).fill(0);
    const tab = tabOf(pda);
    users.sort((a, b) => a.from - b.from);
    users.forEach((u, i) => {
      // Each person's parcels start when they took the PDA (like the PDA job's person_stats).
      const from = u.from;
      const part = hours.slice(from, i + 1 < users.length ? users[i + 1].from : 24);
      const lastHour = lastActiveHour(part, from);
      const key = (u.id || u.name.toLowerCase()) + "|" + tab;
      const p = (people[key] ||= { name: u.name, written: u.written, tab, id: u.id, pdas: [], parcels: 0, hours: 0, hourly: Array(24).fill(0) });
      p.pdas.push(shortPda(pda));
      part.forEach((v, k) => { p.hourly[from + k] += v; }); // Their parcels in each hour, for the chart
      if (i + 1 === users.length && lastScan[pda] != null) p.lastScan = Math.min(p.lastScan ?? Infinity, lastScan[pda]);
      if (i + 1 === users.length && lastBox[pda]?.box) p.lastBox = lastBox[pda].box;
      if (lastHour != null) p.lastHour = Math.max(p.lastHour ?? -1, lastHour);
      p.parcels += sum(part);
      p.hours += part.filter(Boolean).length;
      const justTaken = u.out != null && nowMinutes - u.out < GRACE_MINUTES;
      if (!justTaken && sum(hours.slice(u.from)) === 0) idle.push({ pda: shortPda(pda), name: u.written, tab });
    });
  }
  const ranked = Object.values(people).filter((p) => p.hours);
  // Nobody's name on it: a PDA no one signed for, or its scans from before the first person took it.
  for (const [pda, all] of Object.entries(hourly)) {
    if (!pda.startsWith("HOU.H_")) continue; // Not a PDA: the sorter is in the WMS list too
    const users = byPda[pda];
    const hours = users ? all.slice(0, Math.min(...users.map((u) => u.from))) : all;
    if (sum(hours) < UNSIGNED_MIN) continue;
    ranked.push({ name: shortPda(pda), written: "", tab: tabOf(pda), id: "", unsigned: true,
      lastScan: users ? undefined : lastScan[pda], lastHour: lastActiveHour(hours, 0),
      lastBox: users ? undefined : lastBox[pda]?.box || undefined,
      pdas: [shortPda(pda)], parcels: sum(hours), hours: hours.filter(Boolean).length,
      hourly: hours.concat(Array(24 - hours.length).fill(0)) });
  }
  ranked.sort((a, b) => b.parcels / b.hours - a.parcels / a.hours);
  const tabs = {};
  for (const tab of ["VA", "VB", "VC", "OTHER"]) {
    tabs[tab] = { ...(board.tabs[tab] || { notes: [] }),
      people: ranked.filter((p) => p.tab === tab), idle: idle.filter((x) => x.tab === tab) };
  }
  const historyDay = ranked.filter((p) => !p.unsigned).map((p) => [p.name, p.tab, p.id, p.parcels, p.hours]);
  return { board: { ...board, tabs, liveAt: at }, historyDay };
}
