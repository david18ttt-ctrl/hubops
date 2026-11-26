// Truck space: will tonight's booked trucks hold each station's boxes? Alerts the phone app early enough to
// get another truck (an extra truck usually shows up 2–5 hours after dispatch adds it, about 3 typical).
// - Every 15 minutes, all day: each station's boxes for its next truck, from the WMS (bags of 30+ packages
//   made after last night's cutoff + 15 minutes, up to tonight's). WMS bag counts match dispatch's own box
//   counts one-to-one.
// - Every half hour it keeps that count, so each night adds to the history the forecast learns from
//   (KV truckspace_curves). After each truck leaves, the hourly run rebuilds the forecast's numbers
//   (truckmodel.js: a self-judging mix, evaluated by walk-forward replay).
// - 9 AM: one alert with tonight's plan per station (boxes expected, trucks needed, trucks booked so far).
// - 11 AM–5 AM: an alert when a station is heading 5+ boxes past its booked trucks, with the time to ask
//   dispatch by. 1–4 over isn't worth a truck: they squeeze in or wait for the next night (SPARE).
//   Trucks come from dispatch's truck log (pushed to KV by the ops host), read every 15 minutes 7 AM–7 PM
//   (when dispatch books) and every 30 minutes at night, while it matters.
// - At most two messages per station per night: the first, and one more if it gets clearly worse.
import { HUB_ID, HUB_ID_KEY, TIME_ZONE, chicagoParts, operationWindow, wmsHeaders, wmsUrl } from "./hubtime.js";
import { addAlert } from "./appalert.js";
import { SLOTS, V3, addDays, buildModel, forecastFromModel } from "./truckmodel.js";
import { cityCheck } from "./truckcity.js";
import { TRUCK_NEWS_KEY, applyNews } from "./trucknews.js";
import { localCheck } from "./trucklocal.js";

const WMS_BAGS_MADE = "/api/bags/search";
// Dispatch's truck log, as the ops host pushes it: KV "truck_log" = {items: [{fields}]} for the current log and
// "truck_log_old" for an older log kept for history. The night is "Shift date" in the current log
// ("Departure date" in the old one); the current log has no "Cancelled" box, so a cancel is "cancel" typed
// in the Task cell.
export const TRUCK_LOG_KEY = "truck_log";
export const TRUCK_LOG_OLD_KEY = "truck_log_old";
const STATE_KEY = "truckspace";
// The station limits, as the ops host pushes them from the capacity sheet: {day, rows: [[name, cutoff, …,
// limit, …]], ai: {day, at, stations}} (ai = the limits reader's reading of the sheet's notes).
export const LIMITS_KEY = "station_limits";
export const CURVES_KEY = "truckspace_curves"; // {station: {night: {b: [boxes each half hour before departure], f: boxes at departure}}}
export const MODEL_KEY = "truckspace_model";
export const BOX_MIN = 30; // Packages before a bag counts as a box (smaller ones ride along)
// A full truck, in boxes. 53ft: dispatch's own counts on nights it logged the truck full; the busiest
// stations (BRO01, HRL01) fit about one more. Other stations use the usual 27.
export const TRUCK_BOXES = { "53FT": 27, "26FT": 11, "16FT": 4 };
// City trucks (truckcity.js) nearly always leave full at about 28 for every group.
// SGR01 (trucklocal.js): its 53fts carry a little more; 29 is planned.
const FULL_53FT = { AUS01: 27, BRO01: 28, HRL01: 28, PHX: 28, SEA: 28, MSP: 28, BOS: 28, STL: 28, SAN: 28, SGR01: 29 };
// Learned from dispatch's own box counts (learnFull / fullSizes below), set at the start of each check.
let learnedFull = {};
export const setFullSizes = (sizes) => { learnedFull = sizes || {}; };
export const truckBoxes = (station, type) => (TRUCK_BOXES[type] && type !== "53FT" ? TRUCK_BOXES[type]
  : learnedFull[station] ?? FULL_53FT[station] ?? TRUCK_BOXES["53FT"]);
const FULL_KEY = "truckspace_full"; // {station: {night: boxes on its fullest 53ft}}
const median = (xs) => {
  const v = [...xs].sort((a, b) => a - b);
  return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
};
// A full 53ft, learned: on a night a station had two or more trucks, its fullest 53ft was full (dispatch
// fills the 53s first). From dispatch's box counts. Kept per station and night; finished nights only.
export function learnFull(full, trucks, today) {
  let changed = false;
  const nights = {};
  for (const t of trucks) {
    if (t.cancelled || !t.day || !CUTOFF[t.station]) continue;
    const day = houstonDate(t.day);
    if (day < today) (nights[`${t.station}|${day}`] ||= []).push(t);
  }
  for (const [key, list] of Object.entries(nights)) {
    const loads = list.filter((t) => (t.type || "53FT") === "53FT" && t.boxes > 0).map((t) => t.boxes);
    if (list.length < 2 || !loads.length) continue;
    const [station, day] = key.split("|");
    const top = Math.max(...loads);
    if (full[station]?.[day] !== top) {
      (full[station] ||= {})[day] = top;
      changed = true;
    }
  }
  for (const nightsOf of Object.values(full)) {
    for (const old of Object.keys(nightsOf).sort().slice(0, -KEEP_NIGHTS)) { delete nightsOf[old]; changed = true; }
  }
  return changed;
}
// The size each station's 53ft is planned at: the median of its last 30 such nights, held to 25–31; with
// fewer than 5 nights, the default stays.
export function fullSizes(full) {
  const out = {};
  for (const [station, nights] of Object.entries(full || {})) {
    const loads = Object.keys(nights).sort().slice(-30).map((d) => nights[d]);
    if (loads.length >= 5) out[station] = Math.round(Math.min(31, Math.max(25, median(loads))));
  }
  return out;
}
// Cutoffs (the report's own win when it has one) and latest departures (dispatch's route rules).
export const CUTOFF = { AUS01: "20:30", ACT01: "20:30", VCT01: "20:30", BRO01: "21:30", HRL01: "22:00", SJT01: "23:30",
  GGG01: "0:00", TXK01: "0:00", SHV01: "1:00", AEX01: "2:00" };
export const DEPART = { AUS01: "22:00", ACT01: "22:00", VCT01: "22:00", BRO01: "23:00", HRL01: "23:30", SJT01: "1:00",
  GGG01: "2:00", TXK01: "1:30", SHV01: "2:00", AEX01: "3:30" };
// Boxes over the booked trucks that don't call for another truck (from dispatch's record: 5+ over, a 26ft
// was nearly always added; 1–4 over, rarely, and the rest squeezed in or waited for the next night).
export const SPARE = 4;
const EXTRA_TRUCK_MS = 3 * 3600000;
const KEEP_NIGHTS = 45;
const READ_EVERY_MS = 30 * 60000;
// Dispatch books the night's trucks by day (8:30–11 AM, extras until about 5:30 PM) and its page should show
// a booking at the next check, so 7 AM–7 PM the log is read at every 15-minute check.
const READ_EVERY_BOOKING_MS = 10 * 60000;
const bookingHours = (hour) => hour >= 7 && hour < 19;
const READ_WHEN_WAITING = 13; // Boxes expected at one station before the truck log is worth a read
const NO_TRUCK_MIN = 10;
const COULD_WITHIN_MS = 6 * 3600000; // "could run out" only this close to departure: earlier, the range is wide
const MAX_PER_STATION = 2;
const LOCAL = /^ST1[12]$/; // the hub's own city stations: CXO01 runs on shuttles, SGR01 has its own row (trucklocal.js)
const DAY = 86400000;
const SLOT_MS = 1800000;
// Routes a station asked the hub to hold, so their bags stay off the truck: KV "held_routes", {route: {since,
// why}}. Changed with an operator command ("hold st07 003" / "release st07 003", POST /ops/command) or by the
// chat reader when the team chat shows a hold (trucknews.js).
const HELD_KEY = "held_routes";
// The bulk shipper's daily truck (BULK-PHX): the shipper's own carrier from another region, about 5,500
// packages, scanned in here in the late afternoon or evening; it skips some days. It isn't in the hub's
// inbound truck list, and the WMS arrival list shows it only once it's scanned in; its orders keep coming
// in on skipped days too. So nothing says ahead of time whether it comes: from 2 PM each check looks for
// it. Once it's in, its size counts; if it hasn't come by 10:30 PM, it's out for tonight (freight that
// comes after 10 PM isn't finished that night). An operator can still say "bulk not coming" earlier.
// KV "bulk_today" = {day, coming, at (minutes after the day's midnight), size}, written only when
// something changes.
// How much it moves a station: on replayed nights each 1,000 bulk packages moved the later stations' final
// boxes by about 0.4 in the morning, fading to 0.1 by 3 hours before the truck. AUS01, VCT01 and ACT01
// (8:30 PM cutoff) showed no steady effect: the bulk freight mostly lands in their next night.
const BULK_KEY = "bulk_today";
const BULK_USUAL = 5500;
const BULK_WATCH_FROM = 14 * 60;
const BULK_WATCH_UNTIL = 26 * 60; // 2 AM
const BULK_FINISH = 22 * 60; // arrived after 10 PM: not finished tonight
const BULK_GIVE_UP = 22 * 60 + 30;
const BULK_EARLY = new Set(["AUS01", "VCT01", "ACT01"]);
const WMS_ARRIVALS = "/api/arrivals";
export function bulkPerThousand(name, slot) {
  if (BULK_EARLY.has(name) || LOCAL.test(name)) return 0;
  return slot >= 26 ? 0.43 : slot >= 14 ? 0.28 : slot >= 10 ? 0.21 : slot >= 6 ? 0.13 : 0.1;
}
// Tonight's BULK vs the usual, in thousands of packages: −5.5 when it isn't coming (told, or not in by
// 10:30 PM, or in after 10 PM), (size − 5,500) / 1,000 once it's in, 0 while it's still expected.
export function bulkDelta(bulk, day, minutes) {
  const mine = bulk?.day === day ? bulk : null;
  if (mine?.coming === false) return -BULK_USUAL / 1000;
  if (mine?.at != null) return mine.at <= BULK_FINISH ? (mine.size - BULK_USUAL) / 1000 : -BULK_USUAL / 1000;
  return minutes >= BULK_GIVE_UP ? -BULK_USUAL / 1000 : 0;
}
// A forecast moved by tonight's BULK for one station; never below the boxes already bagged.
export function withBulk(forecast, name, slot, boxes, delta) {
  const shift = bulkPerThousand(name, slot) * delta;
  if (!forecast || !shift) return forecast;
  const move = (x) => (x == null ? x : Math.max(boxes, x + shift));
  return { ...forecast, point: move(forecast.point), high: move(forecast.high), plan: move(forecast.plan), low: move(forecast.low), bulk: shift };
}
export const withoutBulk = (forecast, name, slot, boxes) => withBulk(forecast, name, slot, boxes, -BULK_USUAL / 1000);
const BULK_CUSTOMER = "c0ffee00000000000000000000000001"; // the WMS customer ids
const MARKET = "c0ffee00000000000000000000000002";
// A day's batches in the WMS arrival list for these customers, or every customer (ids null).
// The arrival list is slow to answer (tens of seconds), whatever the filter.
export async function arrivalRows(env, day, ids) {
  const rows = [];
  for (let pageNum = 1; pageNum <= 4; pageNum++) {
    const res = await fetch(wmsUrl(env, WMS_ARRIVALS), {
      method: "POST",
      headers: wmsHeaders(env),
      body: JSON.stringify({ pageNum, pageSize: 100, startCenterId: HUB_ID, arrivalBeginTime: `${day} 00:00:00`, arrivalEndTime: `${day} 23:59:59`,
        ...(ids ? { customerIdList: ids } : {}) }),
      signal: AbortSignal.timeout(90000),
    });
    const payload = await res.json().catch(() => ({}));
    if (payload.code !== 200) throw new Error(`WMS arrivals: code ${payload.code ?? res.status}`);
    const page = payload.data?.records || payload.data?.list || [];
    rows.push(...page);
    if (page.length < 100) break;
  }
  return rows;
}
// One customer's batches as [minutes after the day's midnight, packages], first in first.
export function batchesOf(rows, customer, dayMidnight) {
  return rows.filter((r) => String(r.customerName || "").toUpperCase() === customer && Number(r.waybillCount) > 1 && r.arrivalTime)
    .map((r) => [Math.round((r.arrivalTime - dayMidnight) / 60000), Number(r.waybillCount)]).sort((a, b) => a[0] - b[0]);
}
// BULK's truck among a day's arrivals: {at: minutes after that day's midnight, size} or null.
export function bulkFrom(rows, dayMidnight) {
  const batches = batchesOf(rows, "BULK-PHX", dayMidnight);
  return batches.length ? { at: batches[0][0], size: batches.reduce((a, b) => a + b[1], 0) } : null;
}
// BULK's truck for a day, asked by its customer number or (byName) from the whole day's list.
export async function bulkArrival(env, day, dayMidnight, byName = false) {
  return bulkFrom(await arrivalRows(env, day, byName ? null : [BULK_CUSTOMER]), dayMidnight);
}
// The marketplace customer's arrivals (its pickup trucks) are kept each day, KV "truckspace_hub" =
// {day: {tt: [[minutes, packages], …]}}. A marketplace-heavy day meant a little more at every station the
// next night (about +0.1 box per 1,000), but inside the mix it didn't beat the methods it has. Kept, to
// test again with more history.
const HUB_KEY = "truckspace_hub";

export const isTruckSpaceTime = (hour, minute) => minute % 15 === 0;
const alertHours = (hour) => hour >= 11 || hour < 5;
export const PLAN_HOUR = 9;
// Whether to read dispatch's truck log on this check: at the 9 AM plan; otherwise only while a station has
// boxes worth a truck, every check in the booking hours and every half hour through the warning hours.
export function truckReadDue(hour, planTime, busiest, sinceRead) {
  if (planTime) return true;
  if (busiest < READ_WHEN_WAITING || !(bookingHours(hour) || alertHours(hour))) return false;
  return sinceRead == null || sinceRead >= (bookingHours(hour) ? READ_EVERY_BOOKING_MS : READ_EVERY_MS);
}

// "20:30" → minutes after the night's midnight; before noon is the next morning ("1:00" → 25 h).
export function clockMinutes(text) {
  const m = /^(\d{1,2}):(\d{2})/.exec(String(text || ""));
  return m ? ((+m[1] < 12 ? 24 : 0) + +m[1]) * 60 + +m[2] : null;
}

// Text from a Base cell, whatever its shape: "53FT", [{text:"1:00"}], {value:[...]}.
export function cellText(cell) {
  if (cell == null) return "";
  if (Array.isArray(cell)) return cell.map((c) => (typeof c === "object" ? cellText(c.text ?? c.name ?? c.value) : String(c))).join("");
  if (typeof cell === "object") return cellText(cell.text ?? cell.name ?? cell.value);
  return String(cell);
}
const cellTime = (cell) => (typeof cell === "number" ? cell : Array.isArray(cell?.value) ? cell.value[0] : null);

// Dispatch's truck rows, cut down to what this needs.
export function truckRows(items) {
  return items.map(({ fields: f }) => ({
    day: cellTime(f["Departure date"] ?? f["Shift date"]),
    station: cellText(f.Destination).trim(),
    type: cellText(f["Truck type"]).trim().toUpperCase(),
    // Cancelled: the Cancelled box, or "cancel" typed where the task number goes.
    cancelled: /cancel|true|yes/i.test(cellText(f.Cancelled)) || /cancel/i.test(cellText(f.Task)),
    boxes: Number.parseInt(cellText(f.Boxes), 10) || null,
    task: (cellText(f.Task).match(/TT\d+/) || [])[0] || null,
  })).filter((t) => t.station);
}

// Hub calendar date of a time, "2026-11-09".
function houstonDate(ms) {
  const p = chicagoParts(new Date(ms));
  return `${p.year}-${p.month}-${p.day}`;
}
function clock(ms) {
  const p = chicagoParts(new Date(ms));
  const h = +p.hour;
  return `${h % 12 || 12}:${p.minute} ${h < 12 ? "AM" : "PM"}`;
}

// A station's current night: the first whose truck hasn't left yet. Its boxes are the bags made after
// last night's cutoff + 15 minutes, up to tonight's. opMidnight = midnight starting the operating day.
export function stationNight(name, now, opDay, opMidnight, cutoffText) {
  const cut = clockMinutes(cutoffText || CUTOFF[name]);
  const dep = clockMinutes(DEPART[name]) ?? (cut == null ? null : cut + 90);
  if (cut == null || dep == null) return null;
  for (let k = 0; k < 2; k++) {
    const midnight = opMidnight + k * DAY;
    const depart = midnight + dep * 60000;
    if (now <= depart) return { day: addDays(opDay, k), depart, start: midnight - DAY + (cut + 15) * 60000, end: midnight + (cut + 15) * 60000,
      opStart: midnight + 6 * 3600000 };
  }
  return null;
}

// Each station's boxes for its current night, and its packages in every bag (small ones ride along).
// {station: {day, depart, cutoff, boxes, packages}}
export function stationBoxes(bags, now, opDay, opMidnight, cutoffs = {}, held = new Set()) {
  const out = {};
  const names = new Set([...Object.keys(CUTOFF), ...Object.keys(cutoffs).filter((n) => /^[A-Z]{3}\d{2}$/.test(n))]);
  for (const name of names) {
    if (LOCAL.test(name)) continue;
    const night = stationNight(name, now, opDay, opMidnight, cutoffs[name]);
    if (night) out[name] = { ...night, cutoff: String(cutoffs[name] || CUTOFF[name] || ""), boxes: 0, packages: 0 };
  }
  // A held route's bags aren't the station's boxes, but they're counted apart (s.held), so the page can show
  // the night both ways when it's not sure the hold still stands.
  for (const route of [...held].sort()) {
    const s = out[route.split("-")[0]];
    if (s) (s.held ||= { routes: [], boxes: 0, packages: 0 }).routes.push(route);
  }
  for (const bag of bags) {
    const s = out[String(bag.destinCenterName || "")];
    const made = bag.optCreateTime || 0;
    if (!s || made <= s.start || made > s.end) continue;
    const n = Number(bag.waybillCount) || 0;
    const to = held.has(String(bag.exchangeBureauCode || "")) ? s.held : s;
    to.packages += n;
    if (n >= BOX_MIN) to.boxes += 1;
  }
  return out;
}

// Where a station stands tonight: the forecast held to what the station will take (its limit from the
// capacity sheet, turned into boxes at tonight's packages per box), the trucks that fits, and what to tell
// dispatch. status, the same rule as the warnings: "none" (no truck booked), "short" (heading 5+ boxes past
// the booked trucks), "maybe" (could reach 5+ past them), "few" (could be 1–4 past: fine), "ok".
// planShort: the 9 AM advice's own call, from the plan number.
// Packages per box, to turn a limit in packages into boxes: tonight's boxes so far blended with the station's
// usual (median of its last 7 nights, s.ppbUsual), as if the usual were 15 boxes already made. Early boxes are
// often light (leftovers, part-filled), so tonight's own number alone runs well off the night's final in the
// morning and often too light; the blend is closer and rarely too light.
const PACKAGES_PER_BOX = 360; // Before a station has a week of nights saved
const USUAL_WEIGHT = 15;
export function packagesPerBox(s) {
  const usual = s.ppbUsual > 0 ? s.ppbUsual : PACKAGES_PER_BOX;
  return ((s.packages || 0) + usual * USUAL_WEIGHT) / ((s.boxes || 0) + USUAL_WEIGHT);
}
export function assess(name, s) {
  const room = s.booked?.room ?? 0;
  const trucks = s.booked?.trucks || [];
  const ppb = packagesPerBox(s);
  const cap = s.cap > 0 ? s.cap : null;
  const takes = cap == null ? Infinity : cap / ppb;
  const f = s.forecast;
  const out = { room, trucks, ppb, cap, takes, overCapNow: cap != null && (s.packages || 0) > cap };
  if (!f) return { ...out, status: trucks.length ? "unknown" : "none" };
  const point = Math.min(f.point, takes), high = Math.min(f.high, takes), plan = Math.min(f.plan ?? f.high, takes);
  const expected = (s.packages || 0) + Math.max(0, f.point - s.boxes) * ppb;
  const over = room + SPARE + 1;
  const status = !trucks.length ? "none" : point >= over ? "short" : high >= over ? "maybe" : high > room ? "few" : "ok";
  return { ...out, point, high, plan, expected, overCap: cap != null && expected > cap, hold: Math.max(0, f.point - takes),
    need: trucksFor(name, Math.max(1, Math.ceil(plan) - SPARE)), status, planShort: plan >= over };
}

// What a night's truck count should be for `boxes`: 53fts at the station's full size, plus a 26ft for a
// small remainder. [{type, n}]
export function trucksFor(station, boxes) {
  const full = truckBoxes(station, "53FT");
  let n53 = Math.floor(boxes / full);
  const rest = boxes - n53 * full;
  const plan = [];
  if (rest > TRUCK_BOXES["26FT"]) n53 += 1;
  if (n53) plan.push({ type: "53FT", n: n53 });
  if (rest > 0 && rest <= TRUCK_BOXES["26FT"]) plan.push({ type: "26FT", n: 1 });
  return plan;
}
const truckWords = (trucks) => {
  const counts = {};
  for (const t of trucks) counts[t.type || "53FT"] = (counts[t.type || "53FT"] || 0) + (t.n || 1);
  return Object.entries(counts).map(([type, n]) => `${n}× ${type.replace("FT", "ft")}`).join(" + ");
};
const round = (x) => Math.round(x);

// ---- Station limits.
// What a station will take on its night `day`, in packages (null = no limit), and where that came from:
// - "sheet": the number on that day's capacity sheet (report.rows / report.day);
// - "ai": the limits reader's reading of the sheet (report.ai). Before the day's sheet is out (late
//   morning) the night plans with yesterday's reading of what tomorrow takes, so a one-day cut isn't
//   carried over. A station with no number yet ("/", "update this afternoon") gets the number the reader
//   expects to apply.
// - "chat": something newer from the team chat (limit_news, found by the ops host's chat reader), e.g.
//   "ACT01 flooded after the storm, capacity now 3k". It wins when it came after the reader's pass for that day.
export const NEWS_KEY = "limit_news"; // {items: [{key, station, night, limit, why, quote, chat, sent}]}
export function limitFor(name, day, report = {}, news = []) {
  const row = (report.rows || []).find((r) => r?.[0] === name);
  const sheetNum = row && typeof row[4] === "number" && row[4] > 0 ? row[4] : null;
  const ai = report.ai?.day && report.ai.day === report.day ? report.ai : null; // a reading of that report's sheet
  const read = ai?.stations?.[name];
  const k = (x) => (x == null || !Number.isFinite(+x) ? null : Math.round(+x * 1000));
  let out = { cap: sheetNum, from: "sheet" };
  if (report.day === day) {
    if (sheetNum == null && read?.use_now != null) out = { cap: k(read.use_now), from: "ai" };
  } else if (read && report.day && addDays(report.day, 1) === day && read.plan_tomorrow !== undefined) {
    out = { cap: k(read.plan_tomorrow), from: "ai" };
  }
  if (read) Object.assign(out, { kind: read.kind || null, note: read.note || "", why: read.why || "" });
  const after = ai && report.day === day ? ai.at || 0 : 0;
  const latest = (news || []).filter((n) => n.station === name && n.night === day && (n.sent || 0) > after)
    .sort((a, b) => a.sent - b.sent).pop();
  if (latest) {
    out = { ...out, cap: latest.limit == null ? null : k(latest.limit), from: "chat", why: latest.why || "",
      quote: latest.quote || "", chat: latest.chat || "", sent: latest.sent };
  }
  return out;
}

// Keeps what the chat reader sent: new items added (same message and station = replaced), the last 3 nights.
export function mergeNews(old, items, today) {
  const keep = new Map();
  for (const n of [...(old?.items || []), ...(items || [])]) {
    if (!n || !/^[A-Z]{3}\d{2}$/.test(String(n.station)) || !/^\d{4}-\d{2}-\d{2}$/.test(String(n.night))) continue;
    if (n.night < addDays(today, -2)) continue;
    const limit = n.limit == null ? null : Number(n.limit);
    if (limit != null && !(limit >= 0 && limit < 1000)) continue;
    keep.set(`${n.key || ""}|${n.station}|${n.night}`, { key: String(n.key || "").slice(0, 200), station: n.station, night: n.night, limit,
      why: String(n.why || "").slice(0, 200), quote: String(n.quote || "").slice(0, 300), chat: String(n.chat || "").slice(0, 100),
      sent: Number(n.sent) || 0 });
  }
  return { items: [...keep.values()].sort((a, b) => a.sent - b.sent).slice(-200) };
}

// More truck than the limit lets the station take: booked room is a whole truck (a 26ft, 11 boxes) or more
// past what the capped forecast needs, and without the limit the booking would be about right. Once per
// station-night and limit, in the warning hours (11 AM on) until 3 hours before the truck.
export function trimNotes(stations, said, now) {
  const lines = [];
  const next = { ...said };
  for (const [name, s] of Object.entries(stations).sort()) {
    const f = s.forecast;
    if (!f || !s.limit?.cap || now > s.depart - EXTRA_TRUCK_MS) continue;
    const a = assess(name, s);
    if (!a.trucks.length || !(f.plan > a.takes)) continue;
    const needRoom = a.need.reduce((sum, x) => sum + truckBoxes(name, x.type) * x.n, 0);
    if (a.room - needRoom < TRUCK_BOXES["26FT"]) continue;
    const key = `${name}|${s.day}|trim`;
    if (said[key] === s.limit.cap) continue;
    const src = s.limit.from === "chat" ? `chat${s.limit.chat ? ` (${s.limit.chat})` : ""}: ${s.limit.why}` : s.limit.why ? `sheet: ${s.limit.why}` : "sheet";
    lines.push(`${name} ${clock(s.depart)}: limit ${fmtK(s.limit.cap)} ≈ ${Math.floor(a.takes)} boxes (${src}) · expect ${round(a.point)} · ` +
      `booked ${truckWords(a.trucks)} = ${a.room} · enough: ${truckWords(a.need)}`);
    next[key] = s.limit.cap;
  }
  return { lines, said: next };
}
const fmtK = (n) => `${Math.round(n / 100) / 10}k`;

// The ops host's chat reader at /limit-news/<LIMIT_NEWS_KEY>: POST {items} adds what it found; GET returns the
// limits as the worker has them, so the reader can say what changed.
export async function handleLimitNews(request, env) {
  const url = new URL(request.url);
  if (!url.pathname.startsWith("/limit-news/")) return null;
  const key = url.pathname.slice("/limit-news/".length);
  if (!env.LIMIT_NEWS_KEY || env.LIMIT_NEWS_KEY.length < 16 || key !== env.LIMIT_NEWS_KEY) return new Response("Not found", { status: 404 });
  const today = operationWindow(new Date())[0].slice(0, 10);
  if (request.method === "POST") {
    const body = await request.json().catch(() => null);
    if (!body || !Array.isArray(body.items)) return new Response("Bad request", { status: 400 });
    const merged = mergeNews(await env.PLAN.get(NEWS_KEY, "json"), body.items, today);
    if (body.items.length) await env.PLAN.put(NEWS_KEY, JSON.stringify(merged));
    // What else the reader judged: holds, the bulk truck, a peak day, things that need attention
    // (trucknews.js). Applied here, and summed up in one alert.
    const { lines } = await applyNews(env, body, today, Date.now(), clock);
    if (lines.length) {
      await addAlert(env, { title: "Truck news", text: lines.join("\n") })
        .catch((error) => console.error("truck news alert:", error));
    }
    return Response.json({ ok: true, kept: merged.items.length, told: lines.length });
  }
  const limits = (await env.PLAN.get(LIMITS_KEY, "json")) || {};
  return Response.json({ today, day: limits.day || null, rows: limits.rows || [], ai: limits.ai || null,
    news: (await env.PLAN.get(NEWS_KEY, "json"))?.items || [],
    held: (await env.PLAN.get(HELD_KEY, "json")) || {}, bulk: await env.PLAN.get(BULK_KEY, "json"),
    calendar: (await env.PLAN.get(CALENDAR_KEY, "json")) || {},
    truck_news: ((await env.PLAN.get(TRUCK_NEWS_KEY, "json"))?.items || []).slice(-20).map((n) => ({ kind: n.kind, did: n.did, summary: n.summary, night: n.night })),
  }, { headers: { "cache-control": "no-store" } });
}

// The news handed to the ops host's daily limits job with the capacity sheet, last two nights, newest last.
export async function newsForJob(env, today) {
  const items = ((await env.PLAN.get(NEWS_KEY, "json"))?.items || []).filter((n) => n.night >= addDays(today, -1)).slice(-40);
  return JSON.stringify(items.map((n) => ({ station: n.station, night: n.night, limit_k: n.limit, why: n.why, quote: n.quote,
    chat: n.chat, sent: new Date(n.sent).toLocaleString("en-US", { timeZone: TIME_ZONE }) })));
}

// Tonight's booked trucks for a station-night (cancelled ones aren't room) and their room in boxes.
// A truck that has already left holds only what it took: its empty spots went with it (two full 53fts gone
// in the afternoon and a half-empty 53ft gone at night leave far less room than "three 53fts" says).
// fullRoom = every truck at its full size, what last night's "did the trucks leave full" judgement needs.
export function bookedFor(trucks, station, day) {
  const mine = trucks.filter((t) => t.station === station && !t.cancelled && t.day && houstonDate(t.day) === day);
  const fullRoom = mine.reduce((sum, t) => sum + truckBoxes(station, t.type), 0);
  const gone = mine.filter((t) => t.gone && t.boxes != null);
  if (!gone.length) return { trucks: mine, room: fullRoom };
  const room = mine.reduce((sum, t) => sum + (t.gone && t.boxes != null ? t.boxes : truckBoxes(station, t.type)), 0);
  return { trucks: mine, room, fullRoom, gone: { trucks: gone.length, boxes: gone.reduce((a, t) => a + t.boxes, 0) } };
}
// "4× 53ft, 2 gone with 34" for the warnings.
const bookedWords = (booked) => truckWords(booked.trucks) + (booked.gone ? `, ${booked.gone.trucks} gone with ${booked.gone.boxes}` : "");

// The 9 AM plan: one line per station, in departure order. Trucks are sized for the plan number less the
// boxes that may wait (SPARE); "short" when the booked trucks would leave 5+.
export function morningPlan(stations) {
  const lines = Object.entries(stations).filter(([, s]) => s.forecast && s.forecast.high >= 1)
    .sort((a, b) => a[1].depart - b[1].depart)
    .map(([name, s]) => {
      const f = s.forecast;
      const a = assess(name, s);
      const booked = a.trucks.length ? `${truckWords(a.trucks)} = ${a.room}` : "none";
      const tag = a.planShort ? " · NEED TRUCK" : a.high >= a.room + SPARE + 1 ? " · may need truck" : a.high > a.room ? " · nearly full" : "";
      const cap = a.overCap ? ` · over limit ~${Math.ceil(a.hold)}` : "";
      return `${name} ${clock(s.depart)} · expect ${round(f.point)} (${round(f.low)}–${Math.ceil(f.high)})${leftWords(s)} · booked ${booked} · need ${truckWords(a.need) || "none"}${tag}${cap}`;
    });
  return lines.length ? ["Truck plan 9 AM (expected boxes, likely range; another truck at 5+ boxes over):", ...lines] : [];
}

// A hold that isn't being followed lifts itself: the WMS stamps a bag when it's checked out of the hub (loaded
// on its truck), so a held route whose bags, made on a later night than the hold was said, are being checked
// out is riding the truck. Two such bags are enough. Not every bag gets the scan, so no scan proves nothing:
// an old hold with no scans stays a question on the page. {route: {n, last}}
export function heldLeaving(bags, holds, opDay, opMidnight) {
  const out = {};
  for (const [route, hold] of Object.entries(holds || {})) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(hold?.since))) continue;
    const days = Math.round((Date.parse(`${hold.since}T12:00:00Z`) - Date.parse(`${opDay}T12:00:00Z`)) / DAY);
    const after = opMidnight + (days + 1) * DAY + 6 * 3600000; // 6 AM, the start of the night after the hold was said
    const left = bags.filter((b) => String(b.exchangeBureauCode || "") === route && (b.optCreateTime || 0) > after && b.optCheckOutTime > b.optCreateTime)
      .map((b) => b.optCheckOutTime);
    if (left.length >= 2) out[route] = { n: left.length, last: Math.max(...left) };
  }
  return out;
}

// A hold nobody has repeated for this many nights is in doubt: the page asks. Holds are usually for a night
// ("hold it a day", "don't release tomorrow's 003"), and nobody announces when one ends.
// Saying the hold again ("hold st07 003") restarts the count.
export const HOLD_ASK_AFTER = 2;
// The boxes a station's held routes would add at departure if they went on the truck after all: what they've
// bagged so far, grown the way the station's own boxes are expected to grow once it has 3.
export function heldAtDeparture(s) {
  const now = s.held?.boxes || 0;
  return s.boxes >= 3 && s.forecast?.point > s.boxes ? now * (s.forecast.point / s.boxes) : now;
}
// The station with its held routes counted in: its boxes, packages and forecast.
export function withHeld(s) {
  const add = heldAtDeparture(s);
  const f = s.forecast;
  const up = (x) => (x == null ? x : x + add);
  return { ...s, boxes: s.boxes + s.held.boxes, packages: (s.packages || 0) + s.held.packages,
    forecast: f ? { ...f, point: up(f.point), low: up(f.low), high: up(f.high), plan: up(f.plan) } : f };
}

// ---- Boxes left from last night.
// A station's night counts the bags made after last night's cutoff, so a box made for last night that didn't
// leave is in nobody's count, yet it needs room on tonight's trucks.
// "Not signed out" alone isn't proof the box is here: not every bag gets the sign-out scan. So each box made
// for last night (made after the night before's cutoff, up to last night's) and not signed out by 6 AM is:
// - counted when it can't have gone: still open on its chute (the newest bag there: the WMS leaves the close
//   time blank on many finished bags, so a blank alone means nothing), or closed after last night's last
//   sign-out (the last truck was already loaded), or last night had no truck at all (trucks booked but not
//   one sign-out scan proves nothing, so then only the next rule can count it);
// - counted when last night's trucks left full (signed-out boxes within 2 a truck of their room) or the
//   station reached its limit (signed-out packages 90%+ of it);
// - otherwise not counted, shown as "not signed out" (the trucks had room, so it most likely left unscanned).
// Older boxes (made two or more nights ago) still not signed out are never counted (two nights of trucks
// passed them by: most likely a missed scan or a split label), and neither are customer pre-sort bags
// (the customer's own bag numbers, often re-bagged into a BG… bag) or held routes. Any of these that gets
// signed out after 6 AM went on tonight's trucks, so it counts for sure. Boxes = 30+ packages (BOX_MIN).
// `judge`: last night's shipping, kept from the first look after 6 AM, because a station unpacking a bag
// empties its count in the WMS ({shipped, shippedPkgs, lastLoad}, highest seen).
export function leftovers(name, s, bags, prev, judge = {}, held = new Set()) {
  const boundary = s.opStart, prevBoundary = boundary - DAY, prevStart = s.start - DAY;
  const seen = new Set();
  const mine = bags.filter((b) => String(b.destinCenterName || "") === name && !seen.has(b.packageNo) && seen.add(b.packageNo));
  const pk = (b) => Number(b.waybillCount) || 0;
  const shippedLast = mine.filter((b) => b.optCheckOutTime >= prevBoundary && b.optCheckOutTime < boundary && (pk(b) === 0 || pk(b) >= BOX_MIN));
  const j = {
    shipped: Math.max(judge.shipped || 0, shippedLast.length),
    shippedPkgs: Math.max(judge.shippedPkgs || 0, shippedLast.reduce((a, b) => a + pk(b), 0)),
    lastLoad: Math.max(judge.lastLoad || 0, ...shippedLast.map((b) => b.optCheckOutTime)),
  };
  const room = prev.room || 0, nTrucks = prev.trucks || 0;
  const full = room > 0 && j.shipped >= room - 2 * nTrucks;
  const capHit = prev.cap > 0 && j.shippedPkgs >= 0.9 * prev.cap;
  const out = { boxes: 0, packages: 0, unsure: 0, unsurePackages: 0, onTonight: 0, full, capHit, judge: j };
  // The bag each chute is filling now: its newest (BG…-<chute>, any destination).
  const chuteOf = (b) => (/^BG/.test(String(b.packageNo || "")) ? String(b.packageNo).split("-").pop() : null);
  const newest = {};
  for (const b of bags) {
    const c = chuteOf(b);
    if (c && (b.optCreateTime || 0) > (newest[c]?.optCreateTime || 0)) newest[c] = b;
  }
  for (const b of mine) {
    const made = b.optCreateTime || 0, n = pk(b);
    if (made > s.start || !n) continue; // tonight's own bag (counted already), or an empty one
    if (b.optCheckOutTime && b.optCheckOutTime < boundary) continue; // left on an earlier truck
    const add = (sure) => {
      if (sure) { out.packages += n; if (n >= BOX_MIN) out.boxes += 1; }
      else if (n >= BOX_MIN) { out.unsure += 1; out.unsurePackages += n; }
    };
    if (held.has(String(b.exchangeBureauCode || ""))) continue;
    if (b.optCheckOutTime >= boundary) { add(true); if (n >= BOX_MIN) out.onTonight += 1; continue; } // on tonight's truck
    const presort = !/^BG/.test(String(b.packageNo || ""));
    if (made <= prevStart || presort) { add(false); continue; }
    const closed = b.optFinishStatusTime;
    const openNow = !closed && newest[chuteOf(b)]?.packageNo === b.packageNo;
    // No sign-out scans at all for last night's trucks proves nothing either way (a truck can go out unscanned).
    // No truck booked and none signed out: nothing went. (Sign-outs with no booking: room unknown, judged as below.)
    const cantHaveGone = openNow || (!room && !j.shipped) || (closed > 0 && j.lastLoad > 0 && closed > j.lastLoad);
    add(cantHaveGone || full || capHit);
  }
  return out;
}

// The station with last night's boxes counted in: they're boxes already made, so the forecast moves by them.
export function withLeft(s) {
  const n = s.left?.boxes || 0;
  if (!s.left || (!n && !s.left.packages)) return s;
  const f = s.forecast;
  const up = (x) => (x == null ? x : x + n);
  return { ...s, boxes: s.boxes + n, packages: (s.packages || 0) + s.left.packages,
    forecast: f ? { ...f, point: up(f.point), low: up(f.low), high: up(f.high), plan: up(f.plan) } : f };
}

const leftWords = (s) => (s.left?.boxes ? ` (incl. ${s.left.boxes} left from last night)` : "");

// What dispatch's page shows (trucksboard.js), kept in the truck-space state: one row per station, in
// departure order, every number behind the advice. Trucks as [[type, count]].
export function boardSnapshot(stations, now, trucksAt) {
  const r1 = (x) => (x == null || !Number.isFinite(x) ? null : Math.round(x * 10) / 10);
  const kinds = (list) => Object.entries((list || []).reduce((m, t) => ({ ...m, [t.type || "53FT"]: (m[t.type || "53FT"] || 0) + (t.n || 1) }), {}));
  const rows = Object.entries(stations).sort((a, b) => a[1].depart - b[1].depart || a[0].localeCompare(b[0])).map(([name, s]) => {
    const a = assess(name, s);
    const f = s.forecast;
    return { name, day: s.day, cutoff: s.cutoff, depart: s.depart, askBy: s.depart - EXTRA_TRUCK_MS, boxes: s.boxes, packages: s.packages || 0,
      point: r1(f?.point), low: r1(f?.low), high: r1(f?.high), plan: r1(a.plan), status: a.status,
      booked: kinds(a.trucks), room: a.room, need: kinds(a.need), full53: truckBoxes(name, "53FT"),
      // The trucks the top of the likely range would take, sized the way `need` is (4 boxes may wait).
      needHigh: a.high == null ? null : kinds(trucksFor(name, Math.max(1, Math.ceil(a.high) - SPARE))),
      cap: a.cap, expected: a.expected == null ? null : Math.round(a.expected), overCap: Boolean(a.overCap), overCapNow: a.overCapNow,
      am: s.am ? { point: s.am.point, need: kinds((s.am.need || []).map((type) => ({ type }))) } : null,
      hold: r1(a.hold), takes: r1(a.takes), ppb: Math.round(a.ppb), held: heldRow(name, s, r1, kinds),
      // Last night's boxes counted in (boxes, point, range and packages above include them), and the ones not
      // signed out that aren't counted (most likely went out unscanned).
      left: s.left && (s.left.boxes || s.left.unsure) ? { boxes: s.left.boxes, packages: s.left.packages, unsure: s.left.unsure,
        onTonight: s.left.onTonight } : null,
      limit: s.limit ? { from: s.limit.from, kind: s.limit.kind || null, note: s.limit.note || "", why: s.limit.why || "",
        quote: s.limit.quote || "", chat: s.limit.chat || "", sent: s.limit.sent || null } : null };
  });
  return { at: now, trucksAt: trucksAt || null, spare: SPARE, lean: 0, rows };
}

// A row's held routes, and the night as it would be with them on the truck (the same rule as the row itself).
function heldRow(name, s, r1, kinds) {
  if (!s.held) return null;
  const a = assess(name, withHeld(s));
  const f = withHeld(s).forecast;
  return { routes: s.held.routes, since: s.held.since || null, ask: Boolean(s.held.ask), boxes: s.held.boxes, packages: s.held.packages,
    point: r1(a.point ?? f?.point), low: r1(f?.low), high: r1(a.high ?? f?.high), plan: r1(a.plan), status: a.status, need: kinds(a.need) };
}

// The warnings due now, and the updated "already said" list ({"station|night": {rank, point, times}}).
// The truck to add, sized like the page's "What to do" (trucksboard extraTruck): boxes past the room with up
// to 4 waiting; a 26ft for up to 11, else 53fts (and a 26ft for a small rest), so "short 8" is a 26ft.
function addWords(name, boxes) {
  const n = Math.max(1, Math.ceil(boxes));
  return truckWords(n <= TRUCK_BOXES["26FT"] ? [{ type: "26FT", n: 1 }] : trucksFor(name, n));
}

export function alerts(stations, said, now) {
  const lines = [];
  const next = { ...said };
  for (const [name, s] of Object.entries(stations).sort()) {
    const f = s.forecast;
    if (!f || now > s.depart) continue;
    const { room, trucks } = s.booked;
    const askBy = s.depart - EXTRA_TRUCK_MS;
    const when = now < askBy ? `by ${clock(askBy)}` : "now (may arrive late)";
    const range = `${round(f.low)}–${Math.ceil(f.high)}`;
    let line = null, rank = 0;
    const over = room + SPARE + 1; // 5+ boxes past the booked trucks
    const a = assess(name, s); // a.point / a.high: held to what the station will take
    if (room > 0 && Math.min(s.boxes, a.takes) >= over) {
      rank = 4;
      line = `${name} ${clock(s.depart)}: ${s.boxes} bagged${leftWords(s)}, room ${room} (${bookedWords(s.booked)}). Add ${addWords(name, Math.max(s.boxes, a.point) - SPARE - room)} ${when}.`;
    } else if (!trucks.length && f.point >= NO_TRUCK_MIN && s.depart - now <= 10 * 3600000) {
      rank = 3;
      line = `${name} ${clock(s.depart)}: expect ${round(f.point)} (${range})${leftWords(s)}, no truck booked. Book ${when}.`;
    } else if (room > 0 && a.point >= over) {
      rank = 3;
      line = `${name} ${clock(s.depart)}: expect ${round(f.point)} (${range})${leftWords(s)}, room ${room} (${bookedWords(s.booked)}), short ${round(a.point - room)}. Add ${addWords(name, a.point - SPARE - room)} ${when}.`;
    } else if (room > 0 && a.high >= over && s.depart - now <= COULD_WITHIN_MS) {
      rank = 2;
      line = `${name} ${clock(s.depart)}: expect ${round(f.point)}, up to ${Math.ceil(f.high)}${leftWords(s)}; room ${room} (${bookedWords(s.booked)}). Backup ${addWords(name, a.plan - SPARE - room)} ${when}.`;
    }
    if (!line) continue;
    const key = `${name}|${s.day}`;
    const before = said[key];
    if ((before?.times || 0) >= MAX_PER_STATION) continue;
    if (before && rank <= before.rank && f.point < before.point + 4) continue;
    lines.push(line);
    next[key] = { rank, point: Math.round(f.point * 10) / 10, times: (before?.times || 0) + 1, room };
  }
  return { lines, said: next };
}

// The WMS per-station view (sorting area, branch lines): packages waiting here to be sorted for each
// station, and still on the way. Saved every half hour with the boxes, so it can be tested as a forecast
// method once there's history (much of what's on the way doesn't make that night's truck).
// {station: [waiting, coming]}
const WMS_PIPELINE = "/api/sorter/pipeline";
export async function fetchPipeline(env, date) {
  const [start, end] = operationWindow(date);
  const res = await fetch(wmsUrl(env, WMS_PIPELINE), {
    method: "POST",
    headers: wmsHeaders(env),
    body: JSON.stringify({ pageType: 7, curFlag: 1, isSummary: 2, trunkFlag: 2, pageNum: 1, pageSize: 50, centerId: HUB_ID,
      centerIdEncryption: HUB_ID_KEY, startTime: start, endTime: end, timeZone: TIME_ZONE, startTimeFj: start, endTimeFj: end }),
    signal: AbortSignal.timeout(30000),
  });
  const payload = await res.json().catch(() => ({}));
  if (payload.code !== 200) throw new Error(`WMS pipeline: code ${payload.code ?? res.status}`);
  const out = {};
  for (const r of payload.data?.records || payload.data?.list || []) {
    if (r.destinCenterName) out[r.destinCenterName] = [Number(r.allShouldSortVotes) || 0, Number(r.estimatedArriveVotes) || 0];
  }
  return out;
}

// Peak days: KV "truckspace_calendar" = {night: factor}, e.g. {"2026-11-30": 1.3} when head office or the team
// chat says a day will run that much above normal. Only the boxes still to come are scaled. Empty unless
// someone sets it. For single unusual days, or the first night or two of a jump, never a whole season: on
// replayed nights a right entry on a one-night spike cut the miss by about a quarter and even a wrong one
// helped, while an entry on a normal night or on every night of a season made it worse, since the mix
// learns a new level by itself.
const CALENDAR_KEY = "truckspace_calendar";
export function withFactor(forecast, boxes, factor) {
  if (!forecast || !factor || factor === 1) return forecast;
  const scale = (x) => (x == null ? x : boxes + Math.max(0, x - boxes) * factor);
  return { ...forecast, point: scale(forecast.point), high: scale(forecast.high), plan: scale(forecast.plan), low: scale(forecast.low), factor };
}

// ---- Keeping each night's boxes for the forecast.
// Tonight's half-hour counts live in the state; a finished night goes into the curves.
export function finishNight(curves, name, rec) {
  const b = rec.b;
  let f = b[0];
  for (let slot = 0; f == null && slot < SLOTS; slot++) f = b[slot];
  if (f == null) return false;
  const keep = { b, f };
  for (const k of ["p", "h", "w", "room", "kinds", "room5", "am", "warned", "pk", "q", "lb"]) if (rec[k] != null) keep[k] = rec[k];
  (curves[name] ||= {})[rec.day] = keep;
  const days = Object.keys(curves[name]).sort();
  for (const old of days.slice(0, Math.max(0, days.length - KEEP_NIGHTS))) delete curves[name][old];
  return true;
}

// Keeps this check's counts; returns the station-nights that just finished (to save in the curves).
export function recordCounts(state, stations, now) {
  state.nights ||= {};
  const finished = [];
  for (const [name, s] of Object.entries(stations)) {
    let rec = state.nights[name];
    if (rec && rec.day !== s.day) {
      if (!rec.saved) finished.push([name, rec]);
      rec = null;
    }
    rec ||= state.nights[name] = { day: s.day, b: Array(SLOTS).fill(null) };
    const ahead = (s.depart - now) / SLOT_MS;
    const slot = Math.round(ahead);
    if (Math.abs(ahead - slot) < 0.05 && slot >= 0 && slot < SLOTS) {
      rec.b[slot] = s.boxes;
      const r1 = (x) => Math.round(x * 10) / 10;
      if (s.forecast) {
        (rec.p ||= Array(SLOTS).fill(null))[slot] = r1(s.forecast.point);
        (rec.h ||= Array(SLOTS).fill(null))[slot] = r1(s.forecast.high);
      }
      if (s.pipe) (rec.w ||= Array(SLOTS).fill(null))[slot] = s.pipe;
      rec.pk = s.packages || 0; // packages in its bags; the last one is at departure (packages per box)
      // Each half hour's packages too, to test later how light the early boxes run (open bags still filling).
      (rec.q ||= Array(SLOTS).fill(null))[slot] = s.packages || 0;
      if (slot === 10 && s.booked) rec.room5 = s.booked.room; // 5 hours before the truck
      // Last night's boxes counted in tonight (withLeft); f stays the night's own boxes, so the forecast keeps
      // learning from those, and the scorecard judges the trucks against f + lb.
      if (s.left) rec.lb = s.left.boxes;
    }
    if (s.booked?.trucks?.length) {
      rec.room = s.booked.room;
      rec.kinds = s.booked.trucks.map((x) => x.type || "53FT");
    }
    if (slot === 0 && rec.b[0] != null && !rec.saved) {
      finished.push([name, rec]);
      rec.saved = true;
    }
  }
  return finished;
}

// Rebuilds the forecast's numbers after new nights finish (the hourly run). Returns true when rebuilt.
export async function refreshTruckModel(env) {
  const curves = (await env.PLAN.get(CURVES_KEY, "json")) || {};
  let latest = "", ver = 0;
  for (const nights of Object.values(curves)) {
    for (const day of Object.keys(nights)) { ver++; if (day > latest) latest = day; }
  }
  if (!latest) return false;
  const old = await env.PLAN.get(MODEL_KEY, "json");
  if (old && old.ver === ver && old.latest === latest && old.kind === V3.kind) return false;
  const model = buildModel(curves, addDays(latest, 1), V3);
  await env.PLAN.put(MODEL_KEY, JSON.stringify({ ...model, ver, latest, ppb: usualPackagesPerBox(curves) }));
  return true;
}

// Each station's usual packages per box: the median of its last 7 nights with 5+ boxes at departure.
export function usualPackagesPerBox(curves) {
  const out = {};
  for (const [name, nights] of Object.entries(curves || {})) {
    const xs = Object.keys(nights).sort().map((d) => nights[d]).filter((r) => r.pk > 0 && r.f >= 5).slice(-7).map((r) => r.pk / r.f).sort((a, b) => a - b);
    if (xs.length >= 3) out[name] = Math.round(xs.length % 2 ? xs[(xs.length - 1) / 2] : (xs[xs.length / 2 - 1] + xs[xs.length / 2]) / 2);
  }
  return out;
}

let BUSY_WAIT_MS = 2000;
export const setBusyWait = (ms) => { BUSY_WAIT_MS = ms; };
async function bagsSince(env, fromText, toText, bySignOut = false) {
  // A few thousand bags by the end of a busy night (two nights' worth at most); 20 pages leaves room for a
  // peak day of twice that.
  const bags = [];
  for (let pageNum = 1; pageNum <= 20; pageNum++) {
    // WMS answers "busy" (code 500) now and then and is fine a moment later: up to three tries a page.
    let payload = {}, status = 0;
    for (let attempt = 0; attempt < 3 && payload.code !== 200 && payload.code !== 401; attempt++) {
      if (attempt) await new Promise((resolve) => setTimeout(resolve, BUSY_WAIT_MS));
      const res = await fetch(wmsUrl(env, WMS_BAGS_MADE), {
        method: "POST",
        headers: wmsHeaders(env),
        // bySignOut: bags signed out (checked out to a truck) in the window, whenever they were made.
        body: JSON.stringify({ packageNoList: [], ...(bySignOut ? { checkOutBeginTime: fromText, checkOutEndTime: toText } : { createBeginTime: fromText, createEndTime: toText }),
          startCenterId: HUB_ID, pageNum, pageSize: 1000 }),
      }).catch(() => null);
      status = res?.status ?? 0;
      payload = res ? await res.json().catch(() => ({})) : {};
    }
    if (payload.code !== 200) throw new Error(`WMS bags failed (code ${payload.code ?? status})`);
    const list = payload.data?.list || [];
    bags.push(...list);
    if (!list.length || bags.length >= Number(payload.data?.total || 0)) return bags;
  }
  console.error(`WMS bags: more than ${bags.length} in the window, the rest weren't read`);
  return bags;
}

// The newest rows, by departure date: tonight's station trucks and tomorrow's-dated city trucks come first.
// Both logs are read: the current one for the nights it has, the old one for the nights before (the learned
// truck sizes look back 30 nights). A night in both counts the current log only.
async function readTrucks(env) {
  const [fresh, old] = await Promise.all([env.PLAN.get(TRUCK_LOG_KEY, "json"), env.PLAN.get(TRUCK_LOG_OLD_KEY, "json").catch(() => null)]);
  if (!fresh) throw new Error("no truck log in KV yet");
  return mergeTruckLogs(truckRows(fresh.items || []), truckRows(old?.items || []));
}
// The current log has no box counts typed in. The WMS keeps every truck's loaded bag count (loadBagCount in
// its truck tasks), the same number dispatch used to copy, so a gone truck with no box count takes the WMS
// count. One request a day.
const WMS_TASKS = "/api/trucks/tasks";
const TMS_HUB_ID = 1;
export async function loadedCounts(env, fromDay, toDay) {
  const out = {};
  for (let pageNum = 1; pageNum <= 3; pageNum++) {
    const res = await fetch(wmsUrl(env, WMS_TASKS), {
      method: "POST",
      headers: wmsHeaders(env, { lang: "en_US", "User-Time-Zone": TIME_ZONE }),
      body: JSON.stringify({ data: { originTransitIdList: [TMS_HUB_ID], timeType: 1, startLocalTime: `${fromDay} 00:00:00`, endLocalTime: `${toDay} 23:59:59` }, pageNum, pageSize: 200 }),
      signal: AbortSignal.timeout(60000),
    });
    const payload = await res.json().catch(() => ({}));
    if (payload.code !== 200) throw new Error(`WMS truck tasks: code ${payload.code ?? res.status}`);
    const page = payload.data?.records || [];
    for (const r of page) if (r.taskNo && r.actualDepartureTime && r.loadBagCount > 0) out[r.taskNo] = r.loadBagCount;
    if (page.length < 200) break;
  }
  return out;
}
// A truck the WMS shows as left (departure time + bags loaded) is gone: it keeps dispatch's box count if
// typed, else the WMS count.
export const withLoaded = (trucks, loaded) => trucks.map((t) => (!t.task || !loaded?.[t.task] ? t : { ...t, gone: true, boxes: t.boxes || loaded[t.task] }));
// The loaded counts kept: the last 4 days of task numbers (TT2611200095 = made 2026-11-20).
const keepLoaded = (loaded, opDay) => Object.fromEntries(Object.entries(loaded || {}).filter(([task]) => {
  const m = /^TT(\d{2})(\d{2})(\d{2})/.exec(task);
  return !m || `20${m[1]}-${m[2]}-${m[3]}` >= addDays(opDay, -3);
}));
export function mergeTruckLogs(fresh, old) {
  const nights = new Set(fresh.filter((t) => t.day).map((t) => houstonDate(t.day)));
  return [...fresh, ...old.filter((t) => t.day && !nights.has(houstonDate(t.day)))];
}

const stamp = (p) => `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:00`;

export async function runTruckSpace(env, date = new Date()) {
  const now = chicagoParts(date);
  const hour = +now.hour;
  if (!env.WMS_TOKEN || !isTruckSpaceTime(hour, +now.minute)) return;
  const [start] = operationWindow(date);
  const opDay = start.slice(0, 10);
  const t = date.getTime();
  const opStart = Date.parse(start.replace(" ", "T") + "Z") + (t - Date.parse(stamp(now).replace(" ", "T") + "Z"));
  const opMidnight = opStart - 6 * 3600000;
  const minutes = (t - opMidnight) / 60000;
  const halfHour = +now.minute % 30 === 0;
  // The WMS arrival list, alongside the bag count (it is slow): the bulk truck from 2 PM until it's in (after
  // 10:30 PM with nothing found by its customer id, one look by name too, in case the WMS numbered it anew),
  // and the marketplace customer every half hour from 8 AM to 2 AM. The station pipeline every half hour.
  let bulk = await env.PLAN.get(BULK_KEY, "json");
  if (bulk?.day !== opDay) bulk = { day: opDay };
  const lookForBulk = bulk.at == null && bulk.coming !== false && minutes >= BULK_WATCH_FROM && minutes <= BULK_WATCH_UNTIL;
  const byName = lookForBulk && minutes >= BULK_GIVE_UP && !bulk.byName;
  const watchMarketplace = halfHour && minutes >= 8 * 60 && minutes <= BULK_WATCH_UNTIL;
  const ids = [...(lookForBulk ? [BULK_CUSTOMER] : []), ...(watchMarketplace ? [MARKET] : [])];
  const arrivalsLook = byName || ids.length ? arrivalRows(env, opDay, byName ? null : ids).catch((error) => { console.error(error); return undefined; }) : null;
  const pipeLook = halfHour ? fetchPipeline(env, date).catch((error) => { console.error(error); return null; }) : null;
  // From noon the day before the operating day: covers every station's window, last night's cutoffs included.
  const bags = await bagsSince(env, stamp(chicagoParts(new Date(opStart - 18 * 3600000))), stamp(now));
  // The day before that too (from noon two days back), only for last night's leftover boxes (leftovers()):
  // a box made for last night can have been opened the afternoon before. A WMS hiccup here keeps the last
  // check's leftovers.
  // And every bag signed out since 6 AM yesterday, whenever it was made (a truck can carry boxes made the
  // night before last): last night's trucks as they really left, and leftovers already on tonight's.
  const older = await Promise.all([
    bagsSince(env, stamp(chicagoParts(new Date(opStart - 42 * 3600000))), stamp(chicagoParts(new Date(opStart - 18 * 3600000)))),
    bagsSince(env, stamp(chicagoParts(new Date(opStart - DAY))), stamp(now), true),
  ]).then((parts) => parts.flat()).catch((error) => { console.error("older bags:", error); return null; });

  let state = (await env.PLAN.get(STATE_KEY, "json")) || {};
  const before = JSON.stringify(state);
  if (state.version !== 2) state = { version: 2 }; // the old pace forecast's state
  // The station limits as the ops host last pushed them: rows [name, cutoff, …, limit, …], the day they're
  // for, and the limits reader's reading of the sheet.
  const limits = (await env.PLAN.get(LIMITS_KEY, "json")) || {};
  const report = limits.rows || [];
  const news = (await env.PLAN.get(NEWS_KEY, "json"))?.items || [];
  const cutoffs = Object.fromEntries(report.map((row) => [row[0], row[1]]));
  const holds = (await env.PLAN.get(HELD_KEY, "json")) || {};
  // A hold whose boxes are leaving the hub anyway is over: lifted here, and the alert says so.
  const lifted = heldLeaving(bags, holds, opDay, opMidnight);
  for (const route of Object.keys(lifted)) delete holds[route];
  if (Object.keys(lifted).length) await env.PLAN.put(HELD_KEY, JSON.stringify(holds));
  const stations = stationBoxes(bags, t, opDay, opMidnight, cutoffs, new Set(Object.keys(holds)));
  for (const [name, s] of Object.entries(stations)) {
    s.limit = limitFor(name, s.day, limits, news);
    s.cap = s.limit.cap;
    // Each night's limit, kept for judging its leftovers the next day (did the station reach it?).
    if (s.cap > 0) (state.caps ||= {})[`${name}|${s.day}`] = s.cap;
    if (s.held) {
      s.held.since = s.held.routes.map((route) => String(holds[route]?.since || "")).filter(Boolean).sort()[0] || null;
      s.held.ask = Boolean(s.held.since) && s.day >= addDays(s.held.since, HOLD_ASK_AFTER);
    }
  }
  // Dispatch's box counts are collected to learn each station's real 53ft size, but not used yet: on replayed
  // nights the learned size was no closer than the fixed one (half-empty 53fts on two-truck nights pull it
  // down). Recheck with more history.
  const full = (await env.PLAN.get(FULL_KEY, "json")) || {};

  // A WMS hiccup (undefined) just means no news this time.
  const rows = arrivalsLook ? await arrivalsLook : undefined;
  if (rows && lookForBulk) {
    const found = bulkFrom(rows, opMidnight);
    if (found || byName) {
      bulk = found ? { ...bulk, ...found, coming: true } : { ...bulk, byName: true };
      await env.PLAN.put(BULK_KEY, JSON.stringify(bulk));
    }
  }
  if (rows && (watchMarketplace || byName)) {
    const tt = batchesOf(rows, "MARKETCO", opMidnight);
    const hub = (await env.PLAN.get(HUB_KEY, "json")) || {};
    if (tt.length && JSON.stringify(hub[opDay]?.tt) !== JSON.stringify(tt)) {
      hub[opDay] = { ...hub[opDay], tt };
      for (const old of Object.keys(hub).sort().slice(0, -KEEP_NIGHTS)) delete hub[old];
      await env.PLAN.put(HUB_KEY, JSON.stringify(hub));
    }
  }
  const pipe = pipeLook ? await pipeLook : null;

  const model = await env.PLAN.get(MODEL_KEY, "json");
  for (const [name, s] of Object.entries(stations)) if (model?.ppb?.[name]) s.ppbUsual = model.ppb[name];
  const delta = bulkDelta(bulk, opDay, minutes);
  const calendar = (await env.PLAN.get(CALENDAR_KEY, "json")) || {};
  for (const [name, s] of Object.entries(stations)) {
    const ahead = (s.depart - t) / SLOT_MS;
    const slot = Math.max(0, Math.round(ahead));
    if (ahead < SLOTS - 0.5) s.forecast = forecastFromModel(model, name, slot, s.boxes);
    if (delta && s.day === opDay) s.forecast = withBulk(s.forecast, name, slot, s.boxes, delta);
    if (calendar[s.day]) s.forecast = withFactor(s.forecast, s.boxes, calendar[s.day]);
    if (pipe?.[name]) s.pipe = pipe[name];
  }
  // The 9 AM plan: at 9:00, or at the next check in that hour if 9:00's didn't go through.
  const planTime = hour === PLAN_HOUR && state.planDay !== opDay;
  const busiest = Math.max(0, ...Object.values(stations).map((s) => Math.max(s.boxes, s.forecast?.high || 0)));
  const readNow = truckReadDue(hour, planTime, busiest, state.trucks ? t - (state.readAt || 0) : null);
  if (readNow) {
    // A truck-log hiccup isn't worth losing the check: the last read stays (the page shows when it was).
    const read = await readTrucks(env).catch((error) => { console.error("truck log:", error); return null; });
    if (read) {
      state.trucks = withLoaded(read, state.loaded);
      state.readAt = t;
      if (learnFull(full, state.trucks, opDay)) await env.PLAN.put(FULL_KEY, JSON.stringify(full));
    }
  }
  // Which trucks have gone and with how many boxes (one WMS request): every half hour 11 AM–5 AM, when station
  // trucks leave (the early stations from about 11 AM, the last ones after midnight), and once a day otherwise.
  if (state.trucks && (state.loadedDay !== opDay || (halfHour && (hour >= 11 || hour < 5)))) {
    const got = await loadedCounts(env, addDays(opDay, -2), addDays(opDay, 1)).catch((error) => { console.error("WMS loads:", error); return null; });
    if (got) {
      state.loaded = { ...keepLoaded(state.loaded, opDay), ...got };
      state.loadedDay = opDay;
      state.trucks = withLoaded(state.trucks, state.loaded);
    }
  }
  // Last night's boxes still here (leftovers()), from the booked trucks, the limit and the sign-out scans.
  state.lastNight ||= {};
  const heldSet = new Set(Object.keys(holds));
  for (const [name, s] of Object.entries(stations)) {
    s.booked = bookedFor(state.trucks || [], name, s.day);
    const prevDay = addDays(s.day, -1), key = `${name}|${prevDay}`, kept = state.lastNight[name]?.day === prevDay ? state.lastNight[name] : null;
    if (older) {
      const pb = bookedFor(state.trucks || [], name, prevDay);
      const trim = state.said?.[`${key}|trim`];
      const cap = state.caps?.[key] ?? (typeof trim === "number" ? trim : null);
      s.left = leftovers(name, s, [...older, ...bags], { room: pb.fullRoom ?? pb.room, trucks: pb.trucks.length, cap }, kept?.judge || {}, heldSet);
      const { judge, ...left } = s.left;
      state.lastNight[name] = { day: prevDay, judge, left };
    } else if (kept?.left) {
      s.left = { ...kept.left };
    }
  }
  for (const key of Object.keys(state.caps || {})) if (key.split("|")[1] < addDays(opDay, -3)) delete state.caps[key];
  // What the trucks are judged on: tonight's own boxes and forecast plus last night's leftovers. The history
  // (recordCounts) keeps the night's own numbers, so the forecast keeps learning what it always did.
  const view = Object.fromEntries(Object.entries(stations).map(([name, s]) => [name, withLeft(s)]));
  for (const [name, s] of Object.entries(stations)) {
    // The 9 AM call (what to book for the night), made at 9 AM and kept with the night after that. Its point is
    // the night's own forecast (scored against its own boxes); its trucks count the leftovers too.
    if (planTime && s.forecast) {
      const a = assess(name, view[name]);
      // lb/capped: the leftovers it counted then and whether a station limit trimmed the plan, so the score can
      // tell a forecast miss from leftovers that showed up later or a limit that held freight on purpose.
      s.am = { point: Math.round(s.forecast.point * 10) / 10, plan: Math.round(a.plan * 10) / 10,
        need: a.need.flatMap((x) => Array(x.n).fill(x.type)), lb: s.left?.boxes || 0, ...(Number.isFinite(a.takes) && a.takes < (view[name].forecast?.plan ?? 0) ? { capped: Math.round(a.takes) } : {}) };
    } else if (state.nights?.[name]?.day === s.day && state.nights[name].am) {
      s.am = state.nights[name].am;
    }
    view[name].am = s.am;
  }
  state.board = { ...boardSnapshot(view, t, state.readAt), bulk: bulkNote(bulk, opDay, minutes),
    calendar: calendar[opDay] || null };
  // SGR01 (trucklocal.js): a row in the stations' table, from the same bags and truck log, with its own
  // history. A problem there never stops the stations' check either.
  try {
    const { local, rows } = await localCheck(env, state.local, { bags, t, opDay, opMidnight, trucks: state.trucks || [], cutoffs,
      limit: (name, day) => limitFor(name, day, limits, news) }, { bookedFor, boardSnapshot, recordCounts, finishNight });
    state.local = local;
    state.board.rows = [...state.board.rows, ...rows].sort((a, b) => a.depart - b.depart || a.name.localeCompare(b.name));
  } catch (error) {
    console.error("SGR01:", error);
  }
  // The city trucks (truckcity.js), from the same bags and truck log. A problem there never stops the
  // stations' check: the cities' rows just stay as they were.
  try {
    state.city = await cityCheck(env, state.city, { bags, t, opDay, opMidnight, hour, minute: +now.minute, trucks: state.trucks || [], readAt: state.readAt },
      { boardSnapshot, recordCounts, finishNight, houstonDate });
  } catch (error) {
    console.error("cities:", error);
  }

  const lines = Object.entries(lifted).map(([route, x]) =>
    `Hold lifted: ${route}. ${x.n} of its boxes were checked out of the hub (last ${clock(x.last)}), so it's counted for the truck again.`);
  const saidBefore = JSON.stringify(state.said || {}), planDayBefore = state.planDay;
  if (planTime) {
    lines.push(...morningPlan(view));
    state.planDay = opDay;
  } else if (alertHours(hour) && state.trucks) {
    const result = alerts(view, state.said || {}, t);
    lines.push(...(result.lines.length ? [`Truck space ${clock(t)}:`, ...result.lines] : []));
    state.said = result.said;
    const trim = trimNotes(view, state.said, t);
    lines.push(...(trim.lines.length ? [`${lines.length ? "\n" : ""}Fewer trucks needed (station limit) ${clock(t)}:`, ...trim.lines] : []));
    state.said = trim.said;
  }

  // The history: this check's counts and forecasts, the 9 AM call and any warning, kept per station-night;
  // finished nights go into the curves (the hourly run rebuilds the forecast) and the scorecard.
  const finished = recordCounts(state, stations, t);
  for (const [name, s] of Object.entries(stations)) {
    const rec = state.nights?.[name];
    if (!rec || rec.day !== s.day) continue;
    if (planTime && s.am) rec.am = s.am;
    const said = state.said?.[`${name}|${s.day}`];
    if (said) rec.warned = said;
  }
  let curves = null;
  if (finished.length) {
    curves = (await env.PLAN.get(CURVES_KEY, "json")) || {};
    if (finished.map(([name, rec]) => finishNight(curves, name, rec)).some(Boolean)) await env.PLAN.put(CURVES_KEY, JSON.stringify(curves));
  }
  if (planTime && new Date(`${opDay}T12:00:00Z`).getUTCDay() === 1) {
    const week = weeklyScore(curves || (await env.PLAN.get(CURVES_KEY, "json")) || {}, opDay);
    if (week.length) lines.push("", ...week);
  }

  // Forget "already said" for nights that are over.
  for (const key of Object.keys(state.said || {})) if (key.split("|")[1] < addDays(opDay, -2)) delete state.said[key];
  if (lines.length) {
    try {
      await addAlert(env, { title: "Truck space", text: lines.join("\n") });
    } catch (error) {
      // The message didn't go out: the page still gets this check's numbers, and nothing counts as said, so
      // the next check sends it.
      console.error("truck space alert:", error);
      state.said = JSON.parse(saidBefore);
      state.planDay = planDayBefore;
      for (const [name, rec] of Object.entries(state.nights || {})) {
        const was = state.said[`${name}|${rec.day}`];
        if (was) rec.warned = was; else delete rec.warned;
        if (planTime) delete rec.am;
      }
    }
  }
  if (JSON.stringify(state) !== before) await env.PLAN.put(STATE_KEY, JSON.stringify(state));
}

// The week's scorecard, sent Mondays with the 9 AM plan: how the forecast and its truck calls did on the
// last 7 finished nights (5+ boxes left = short, the way dispatch works), next to dispatch's final bookings.
const md = (day) => `${+day.slice(5, 7)}/${+day.slice(8, 10)}`;
function judgeTrucks(name, kinds, final) {
  const room = kinds.reduce((a, k) => a + truckBoxes(name, k), 0);
  if (final > room + SPARE) return "short";
  return kinds.some((k) => final <= room - truckBoxes(name, k) + SPARE) ? "too many" : "right";
}
export function weeklyScore(curves, today) {
  const from = addDays(today, -7);
  const rows = [];
  for (const [name, nights] of Object.entries(curves || {})) {
    for (const [day, r] of Object.entries(nights)) if (day >= from && day < today && r.f != null) rows.push({ name, day, ...r });
  }
  const am = rows.filter((r) => r.am);
  if (!am.length) return [];
  const avg = (xs) => (xs.reduce((a, b) => a + b, 0) / xs.length).toFixed(1);
  const five = rows.filter((r) => r.p?.[10] != null);
  const pct = (list, v) => Math.round((100 * list.filter((x) => x === v).length) / list.length);
  // Every station counts. Held routes are already out of the boxes (held_routes), so a hold doesn't hurt the score.
  const calls = am;
  const mine = calls.filter((r) => r.am.need?.length).map((r) => judgeTrucks(r.name, r.am.need, r.f + (r.lb || 0)));
  const disp = calls.filter((r) => r.kinds?.length).map((r) => judgeTrucks(r.name, r.kinds, r.f + (r.lb || 0)));
  const warned = rows.filter((r) => r.warned?.times);
  const good = warned.filter((r) => r.f + (r.lb || 0) >= (r.warned.room ?? 0) + SPARE + 1).length;
  const missed = rows.filter((r) => !r.warned?.times && r.room5 != null && r.f + (r.lb || 0) >= r.room5 + SPARE + 1);
  const worst = [...am].sort((a, b) => Math.abs(b.am.point - b.f) - Math.abs(a.am.point - a.f)).slice(0, 3);
  const lines = [`Truck forecast scorecard, ${md(from)}–${md(addDays(today, -1))} (${rows.length} station-nights):`,
    `- 9 AM forecast off by ${avg(am.map((r) => Math.abs(r.am.point - r.f)))} boxes on average` +
      (five.length ? `; 5 hours before the truck, ${avg(five.map((r) => Math.abs(r.p[10] - r.f)))}.` : ".")];
  if (mine.length && disp.length) {
    lines.push(`- 9 AM truck call: right ${pct(mine, "right")}%, short (5+ left) ${pct(mine, "short")}%, one too many ${pct(mine, "too many")}%. ` +
      `Dispatch's final bookings: ${pct(disp, "right")}% / ${pct(disp, "short")}% / ${pct(disp, "too many")}%.`);
  }
  lines.push(`- Warnings: ${warned.length} sent, ${good} for nights that did run 5+ boxes over the trucks booked then; ` +
    `${missed.length} night${missed.length === 1 ? "" : "s"} ran 5+ over with no warning${missed.length ? ` (${missed.map((r) => `${r.name} ${md(r.day)}`).join(", ")})` : ""}.`);
  lines.push(`- Biggest 9 AM misses: ${worst.map((r) => `${r.name} ${md(r.day)} said ${Math.round(r.am.point)}, came ${r.f}`).join("; ")}.`);
  return lines;
}

// What the page says about BULK tonight: {state: "expected" | "in" | "late" | "no", at, size}.
export function bulkNote(bulk, day, minutes) {
  const mine = bulk?.day === day ? bulk : {};
  if (mine.coming === false) return { state: "no" };
  if (mine.at != null) return { state: mine.at <= BULK_FINISH ? "in" : "late", at: mine.at, size: mine.size };
  return { state: minutes >= BULK_GIVE_UP ? "late" : "expected" };
}

// An operator's bulk-truck news for today's operating day. Returns the reply.
export async function bulkOrder(env, order, date = new Date()) {
  const day = operationWindow(date)[0].slice(0, 10);
  const md = day.slice(5).replace("-", "/");
  if (order.ask) {
    const now = await env.PLAN.get(BULK_KEY, "json");
    if (now?.day === day && now.at != null) {
      const h = Math.floor(now.at / 60), m = String(now.at % 60).padStart(2, "0");
      return `BULK ${md}: in at ${h % 12 || 12}:${m} ${h % 24 < 12 ? "AM" : "PM"}, ${now.size.toLocaleString("en-US")} pkg.`;
    }
    if (now?.day === day && now.coming === false) return `BULK ${md}: marked not coming. Later stations −1–2 boxes each.`;
    return `BULK ${md}: not in WMS yet. Checked every 15 min from 2 PM; excluded if not in by 10:30 PM.`;
  }
  await env.PLAN.put(BULK_KEY, JSON.stringify({ day, coming: order.coming }));
  return order.coming === false
    ? `BULK ${md}: marked not coming. BRO01, HRL01, SJT01, TXK01, GGG01, SHV01, AEX01 −1–2 boxes each. "bulk coming" undoes it.`
    : `BULK ${md}: marked coming.`;
}

// An operator's hold / release / list. Returns the reply.
export async function holdRoutes(env, order, date = new Date()) {
  const held = (await env.PLAN.get(HELD_KEY, "json")) || {};
  const p = chicagoParts(date);
  const lines = [];
  if (order.action === "hold") {
    for (const route of order.routes) held[route] = { since: `${p.year}-${p.month}-${p.day}`, why: "chat" };
    lines.push(`Holding ${order.routes.join(", ")}: those bags won't count toward the truck space. Say "release ${order.routes[0].slice(0, 4).toLowerCase()} ${order.routes[0].slice(-3)}" when it can go again.`);
  } else if (order.action === "release") {
    const was = order.routes.filter((route) => held[route]);
    for (const route of order.routes) delete held[route];
    lines.push(was.length ? `Released ${was.join(", ")}: those bags count toward the truck space again.` : `${order.routes.join(", ")} wasn't held.`);
  }
  if (order.action !== "list") await env.PLAN.put(HELD_KEY, JSON.stringify(held));
  const now = Object.entries(held).map(([route, h]) => `${route} (since ${h.since.slice(5).replace("-", "/")})`);
  lines.push(now.length ? `Held now: ${now.join(", ")}.` : "No routes are held.");
  return lines.join("\n");
}
