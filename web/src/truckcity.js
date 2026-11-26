// City (trunk) trucks: will the 53fts booked for each city hold tonight's boxes? The same idea as the
// station trucks (truckspace.js), for the other hubs HOU.H ships to. What's different about cities:
// - Cities share trucks, so the forecast is per truck group, not per hub: PHX (+MKE), MSP (+BUF,
//   RIC, LIT), SEA (+PIT, JAX, PDX, RNO, COS, GRR), BOS (+ABQ, BOI), STL (+CMH, ICT),
//   SAN (+TUS). Dispatch names the trucks that way ("SEA.HPIT.H", "STL.HCMH.H", "SAN.HTUS.H").
// - City boxes are made 4 PM–5 AM, so a city night is the operating day, 6 AM to 6 AM, and its trucks carry
//   the next calendar date in dispatch's log (they leave from about 7 PM to the group's deadline the next
//   day: PHX 4 AM, SEA 9 AM, MSP 10 AM, BOS / STL noon, SAN 2 PM; dispatch's route rules).
// - A city box reaches 30 packages within minutes of its bag being made, so boxes are counted the way the
//   stations' are. But the WMS empties a bag's count once the other hub unpacks it (PHX the same night),
//   so every box counted for the night is remembered and the count never goes down.
// - Nearly every city truck leaves full (CITY_FULL).
// - Trucks are booked the afternoon before, when no city box exists yet. What says how big the night will
//   be is dispatch's own pickup forecast (posted about noon in dispatch's daily report): city boxes are the
//   hub's pickups going out. So the forecast is the stations' self-judging mix (truckmodel.js) on the cities'
//   own history, blended early in the night with boxes-per-forecast × today's pickup forecast.
// - Not counted: pass-through boxes (other hubs' boxes that change trucks here; they aren't bagged here, so
//   the WMS bag list doesn't have them) and boxes left from the night before, mostly yesterday's
//   left-behind boxes riding today.
import { MODEL_KIND, SLOTS, addDays, buildModel, forecastFromModel, forecaster } from "./truckmodel.js";

export const CITY_GROUPS = {
  PHX: { hubs: ["PHX.H", "MKE.H"], depart: "4:00" },
  SEA: { hubs: ["SEA.H", "PIT.H", "JAX.H", "PDX.H", "RNO.H", "COS.H", "GRR.H"], depart: "9:00" },
  // LIT.H (a newer, small destination) is taken to ride the MSP truck: a guess from the map, not confirmed.
  MSP: { hubs: ["MSP.H", "BUF.H", "RIC.H", "LIT.H"], depart: "10:00" },
  BOS: { hubs: ["BOS.H", "ABQ.H", "BOI.H"], depart: "12:00" },
  STL: { hubs: ["STL.H", "CMH.H", "ICT.H"], depart: "12:00" },
  // Pass-through boxes (other hubs' boxes that change trucks here) ride the SAN / TUS trucks and aren't
  // in the WMS list of bags made here. Dispatch's inbound sheet showed them for this group only, and it isn't
  // kept up, so they're only said on the row ("about 10 more ride these trucks"), not counted: a guess
  // shouldn't call for a truck by itself.
  SAN: { hubs: ["SAN.H", "TUS.H"], depart: "14:00", through: 10 },
};
// What the page calls each group: the hubs its trucks are named for.
export const CITY_NAMES = { PHX: "PHX", SEA: "SEA / PIT", MSP: "MSP", BOS: "BOS / ABQ", STL: "STL / CMH", SAN: "SAN / TUS" };
const HUB_GROUP = Object.fromEntries(Object.entries(CITY_GROUPS).flatMap(([g, { hubs }]) => hubs.map((h) => [h, g])));
export const cityGroupOf = (hub) => HUB_GROUP[hub] || null;
// A truck's group, from dispatch's destination ("PHX.H", "STL.HCMH.H", "ABQ.HBOS.H"): its first hub's.
export function truckGroup(destination) {
  const hubs = String(destination || "").match(/[A-Z]+\.H/g) || [];
  return hubs.length ? HUB_GROUP[hubs[0]] || null : null;
}
// Where the page's scale starts for cities (trucksboard.js): one notch toward "short on purpose" (trucks for
// the expected night, up to 8 boxes wait). Dispatch runs city trucks full and rolls the leftover boxes to the
// next day's truck; on replayed nights this left about twice the boxes of "normal" with well under half the
// empty spots.
export const CITY_LEAN = -1;
export const CITY_FULL = { "53FT": 28, "26FT": 11, "16FT": 4 };
export const cityTruckBoxes = (type) => CITY_FULL[type] ?? CITY_FULL["53FT"];
const DAY = 86400000;
const SLOT_MS = 1800000;
const median = (xs) => {
  const v = [...xs].sort((a, b) => a - b);
  return v.length ? (v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2) : null;
};

// A group's current night: the first whose trucks' deadline hasn't passed. day = the operating day its boxes
// are made in; start/end = 6 AM to 6 AM; depart = the deadline the next day. opMidnight = the midnight that
// starts operating day opDay. PHX's deadline (4 AM) comes before its last boxes are made (they ride a later
// PHX truck), so no night ends before 6:30 AM, once its final count is kept.
export const cityNight = (group, now, opDay, opMidnight) => opNight(CITY_GROUPS[group].depart, now, opDay, opMidnight);
// The same for any destination whose night is the operating day, given its deadline the next morning ("4:00").
export function opNight(departText, now, opDay, opMidnight) {
  const [h, m] = departText.split(":").map(Number);
  for (let k = -1; k < 2; k++) {
    const start = opMidnight + k * DAY + 6 * 3600000;
    const depart = opMidnight + (k + 1) * DAY + (h * 60 + m) * 60000;
    if (now <= Math.max(depart, start + DAY + SLOT_MS)) return { day: addDays(opDay, k), start, end: start + DAY, depart };
  }
  return null;
}
// The forecast's clock for a city night: half hours before 6 AM, when the night's boxes are all made.
export const citySlot = (night, now) => Math.min(SLOTS - 1, Math.max(0, Math.round((night.end - now) / SLOT_MS)));

// Each group's boxes for its current night. `seen` = {group: {day, bags: [bag numbers already counted]}} is
// kept between checks and updated here: a box stays counted after the WMS empties its bag at the other hub.
export function cityBoxes(bags, now, opDay, opMidnight, seen = {}) {
  const out = {};
  for (const group of Object.keys(CITY_GROUPS)) {
    const night = cityNight(group, now, opDay, opMidnight);
    if (!night) continue;
    const kept = seen[group]?.day === night.day ? seen[group].bags : [];
    out[group] = { ...night, name: CITY_NAMES[group], boxes: 0, packages: 0, counted: new Set(kept) };
  }
  for (const bag of bags) {
    const s = out[HUB_GROUP[String(bag.destinCenterName || "")]];
    const made = bag.optCreateTime || 0;
    if (!s || made <= s.start || made > s.end) continue;
    const n = Number(bag.waybillCount) || 0;
    s.packages += n;
    if (n >= 30) s.counted.add(String(bag.packageNo ?? `${bag.destinCenterName}|${made}`));
  }
  for (const [group, s] of Object.entries(out)) {
    s.boxes = s.counted.size;
    seen[group] = { day: s.day, bags: [...s.counted] };
    delete s.counted;
  }
  return out;
}

// The trucks dispatch booked for a group's night: its rows dated the day after the operating day.
export function cityBooked(trucks, group, day, houstonDate) {
  const date = addDays(day, 1);
  const mine = (trucks || []).filter((t) => !t.cancelled && t.day && truckGroup(t.station) === group && houstonDate(t.day) === date);
  return { trucks: mine, room: mine.reduce((sum, t) => sum + cityTruckBoxes(t.type), 0) };
}

// ---- The pickup forecast as a method.
// A group's boxes per pickup-forecast package (median of its last 7 nights that had a forecast, 4 needed),
// and the usual share of the night's boxes made by each slot (mean of the last 10 nights).
export function pickupStats(curves, pickup, group, day) {
  const ratios = [];
  const share = Array(SLOTS).fill(0);
  let nights = 0;
  for (let back = 1; back < 40 && (ratios.length < 7 || nights < 10); back++) {
    const d = addDays(day, -back);
    const r = curves[group]?.[d];
    if (!r || !(r.f > 0)) continue;
    if (ratios.length < 7 && pickup[d] > 0) ratios.push(r.f / pickup[d]);
    if (nights < 10) {
      nights++;
      for (let slot = 0; slot < SLOTS; slot++) share[slot] += (r.b[slot] ?? r.f) / r.f;
    }
  }
  if (ratios.length < 4 || !nights) return null;
  return { ratio: median(ratios), share: share.map((x) => Math.round((1000 * x) / nights) / 1000) };
}
// The night's boxes from today's pickup forecast: the boxes so far plus the forecast's share still to come.
export function pickupPoint(stats, pickupToday, slot, boxes) {
  if (!stats || !(pickupToday > 0)) return null;
  return boxes + Math.max(0, stats.ratio * pickupToday * (1 - stats.share[slot]));
}
// The same weekday's usual: the boxes that were still to come after each slot on this weekday one and two
// weeks ago (city nights swing with the weekday: Sundays run far lighter than Mondays).
export function weekdayRem(curves, group, day) {
  const rows = [7, 14].map((k) => curves[group]?.[addDays(day, -k)]).filter((r) => r && r.f != null);
  if (!rows.length) return null;
  return Array.from({ length: SLOTS }, (_, slot) => Math.round((10 * rows.reduce((a, r) => a + Math.max(0, r.f - (r.b[slot] ?? 0)), 0)) / rows.length) / 10);
}
// The cities' forecast: three views of the night, blended. Walked forward over several weeks of
// group-nights (each from earlier nights only), the blend missed by less than any single view at 2 PM and
// matched the best single view by 10 PM and 2 AM, about the same in both halves of the test.
// A view that's missing (no forecast posted yet, no such weekday saved) leaves its weight to the others.
export const CITY_WEIGHTS = { pickup: 0.4, weekday: 0.3, mix: 0.3 };
export function blendCity(mix, pk, wk) {
  const parts = [[mix, CITY_WEIGHTS.mix], [pk, CITY_WEIGHTS.pickup], [wk, CITY_WEIGHTS.weekday]].filter(([x]) => x != null);
  const total = parts.reduce((a, [, w]) => a + w, 0);
  return parts.length ? parts.reduce((a, [x, w]) => a + x * w, 0) / total : null;
}
// The blend for one group-night from the history itself (offline tests, and the range below).
export function cityPoint(f, curves, pickup, group, day, slot, boxes) {
  const mix = f.mix(group, day, slot, boxes);
  if (mix == null) return null;
  const wk = weekdayRem(curves, group, day);
  return blendCity(mix, pickupPoint(pickupStats(curves, pickup, group, day), pickup[day], slot, boxes), wk ? boxes + wk[slot] : null);
}
// How far the blend was off over the last 10 nights (every group, same slot), as boxes over the square root
// of the forecast, the way the stations' range is kept: hi / lo = 85th / 15th percentile, plan = the 70th.
export function cityRange(f, curves, pickup, day, slot) {
  const z = [];
  for (let back = 1; back <= 10; back++) {
    const d = addDays(day, -back);
    for (const group of Object.keys(CITY_GROUPS)) {
      const r = curves[group]?.[d];
      if (!r || r.f == null || r.b[slot] == null) continue;
      const p = cityPoint(f, curves, pickup, group, d, slot, r.b[slot]);
      if (p != null) z.push((r.f - p) / Math.sqrt(Math.max(p, 4)));
    }
  }
  if (z.length < 15) return { hi: 1.5, lo: -1.5, plan: 0.8 };
  z.sort((x, y) => x - y);
  const q = (x) => { const pos = (z.length - 1) * x, lo = Math.floor(pos), hi = Math.ceil(pos); return z[lo] + (z[hi] - z[lo]) * (pos - lo); };
  const r4 = (x) => Math.round(x * 1e4) / 1e4;
  return { hi: r4(Math.max(0, q(0.85))), lo: r4(Math.min(0, q(0.15))), plan: r4(Math.max(0, q(0.7))) };
}

// ---- Running it (called from truckspace.js's 15-minute check; everything of the cities' lives in state.city,
// and KV "truckcity_curves" / "truckcity_model" / "truckcity_pickup").
export const CITY_CURVES_KEY = "truckcity_curves"; // {group: {night: {b: [boxes each half hour before 6 AM], f}}}
export const CITY_MODEL_KEY = "truckcity_model";
export const CITY_PICKUP_KEY = "truckcity_pickup"; // {day: dispatch's pickup forecast for that day}
// Dispatch's daily report rows, pushed by the ops host: KV "pickup_report" = {items: [{fields: {Date, ...}}]}.
export const PICKUP_REPORT_KEY = "pickup_report";
const PICKUP_FIELD = "Pickup forecast";
const KEEP_DAYS = 60;

// Today's pickup forecast among the report's newest rows (its dates are midnight UTC of the day), or null.
export function pickupFrom(items, day) {
  for (const { fields } of items || []) {
    const stamp = fields?.Date, value = fields?.[PICKUP_FIELD];
    if (typeof stamp === "number" && typeof value === "number" && value > 0 && new Date(stamp).toISOString().slice(0, 10) === day) return value;
  }
  return null;
}
export async function readPickup(env, day) {
  const report = await env.PLAN.get(PICKUP_REPORT_KEY, "json");
  return pickupFrom(report?.items, day);
}
// When to look for today's pickup forecast: every half hour from 10 AM until it's in (dispatch posts it
// about noon; the day's trucks are booked from about 2 PM).
export const pickupDue = (hour, minute, have) => !have && hour >= 10 && minute % 30 === 0;

// Rebuilds the cities' forecast numbers after new nights finish (the hourly run). Returns true when rebuilt.
export async function refreshCityModel(env) {
  const curves = (await env.PLAN.get(CITY_CURVES_KEY, "json")) || {};
  let latest = "", ver = 0;
  for (const nights of Object.values(curves)) for (const day of Object.keys(nights)) { ver++; if (day > latest) latest = day; }
  if (!latest) return false;
  const pickup = (await env.PLAN.get(CITY_PICKUP_KEY, "json")) || {};
  const pkVer = Object.keys(pickup).length;
  const old = await env.PLAN.get(CITY_MODEL_KEY, "json");
  if (old && old.ver === ver && old.latest === latest && old.kind === MODEL_KIND && old.pkVer === pkVer) return false;
  const day = addDays(latest, 1);
  const pk = {}, wk = {};
  for (const group of Object.keys(CITY_GROUPS)) {
    pk[group] = pickupStats(curves, pickup, group, day);
    wk[group] = weekdayRem(curves, group, day);
  }
  // The blend's own range, per slot.
  const f = forecaster(curves);
  const range = Array.from({ length: SLOTS }, (_, slot) => cityRange(f, curves, pickup, day, slot));
  await env.PLAN.put(CITY_MODEL_KEY, JSON.stringify({ ...buildModel(curves, day), ver, latest, pk, wk, range, pkVer }));
  return true;
}

// A group's forecast for tonight: the blend of the mix on the cities' history, today's pickup forecast and
// the weekday's usual, with the blend's own range. After 6 AM the night's boxes are all made: the count is
// the answer.
export function cityForecast(model, group, night, now, boxes, pickupToday) {
  if (now >= night.end) return { point: boxes, low: boxes, high: boxes, plan: boxes, done: true };
  const slot = citySlot(night, now);
  const mix = forecastFromModel(model, group, slot, boxes);
  if (!mix) return null;
  // The weekday and pickup numbers are for the night the model was built for.
  const mine = model.day === night.day;
  const pk = mine ? pickupPoint(model.pk?.[group], pickupToday, slot, boxes) : null;
  const wk = mine && model.wk?.[group] ? boxes + model.wk[group][slot] : null;
  const point = Math.max(boxes, blendCity(mix.point, pk, wk));
  const at = model.range?.[slot];
  const shift = point - mix.point;
  if (!at) return { point, high: Math.max(point, mix.high + shift), low: Math.max(boxes, mix.low + shift), plan: Math.max(point, mix.plan + shift) };
  const spread = Math.sqrt(Math.max(point, 4));
  return { point, high: point + at.hi * spread + 0.5, low: Math.max(boxes, point + at.lo * spread - 0.5), plan: point + at.plan * spread + 0.5,
    pickup: pk == null ? null : Math.round(pk * 10) / 10, weekday: wk == null ? null : Math.round(wk * 10) / 10, mix: Math.round(mix.point * 10) / 10 };
}

// One 15-minute check for the cities, from the bags and trucks the stations' check already read. `city` is
// state.city (kept by the caller); tools = the stations' own helpers (assess rule, page rows, half-hour
// history), so cities are judged exactly the way stations are.
export async function cityCheck(env, city, ctx, tools) {
  const { bags, t, opDay, opMidnight, hour, minute, trucks, readAt } = ctx;
  const next = city?.version === 1 ? city : { version: 1 };
  next.seen ||= {};
  const groups = cityBoxes(bags, t, opDay, opMidnight, next.seen);
  let pickup = (await env.PLAN.get(CITY_PICKUP_KEY, "json")) || {};
  if (pickupDue(hour, minute, pickup[opDay] > 0)) {
    const value = await readPickup(env, opDay).catch((error) => { console.error(error); return null; });
    if (value) {
      pickup = { ...pickup, [opDay]: value };
      for (const old of Object.keys(pickup).sort().slice(0, -KEEP_DAYS)) delete pickup[old];
      await env.PLAN.put(CITY_PICKUP_KEY, JSON.stringify(pickup));
    }
  }
  const model = await env.PLAN.get(CITY_MODEL_KEY, "json");
  for (const [group, s] of Object.entries(groups)) {
    s.cutoff = "";
    s.forecast = cityForecast(model, group, s, t, s.boxes, pickup[s.day]);
    s.booked = cityBooked(trucks, group, s.day, tools.houstonDate);
  }
  // The history, on the cities' clock (half hours before 6 AM); a finished night goes into the curves.
  const holder = { nights: next.nights };
  const finished = tools.recordCounts(holder, Object.fromEntries(Object.entries(groups).map(([g, s]) => [g, { ...s, depart: s.end }])), t);
  next.nights = holder.nights;
  if (finished.length) {
    const curves = (await env.PLAN.get(CITY_CURVES_KEY, "json")) || {};
    if (finished.map(([name, rec]) => tools.finishNight(curves, name, rec)).some(Boolean)) await env.PLAN.put(CITY_CURVES_KEY, JSON.stringify(curves));
  }
  const rows = tools.boardSnapshot(groups, t, readAt).rows.map((r) => ({ ...r, group: r.name, name: CITY_NAMES[r.name], made: t >= groups[r.name].end,
    pickup: pickup[r.day] ?? null, through: CITY_GROUPS[r.name].through || 0 }));
  next.board = { at: t, lean: CITY_LEAN, rows };
  return next;
}
