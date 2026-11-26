// SGR01: the hub's own city station, which goes out by truck like the others: several 53fts a night, dated
// the operating day like every station truck, leaving from the evening to a 4 AM deadline.
// It runs on the stations' own code (same boxes rule, same forecast mix, same truck rule), with four
// differences:
// - its night is the operating day, 6 AM to 6 AM, the way the cities' is, not "up to the cutoff + 15 minutes":
//   a good share of its boxes are made in the last hours before 4 AM and still ride that morning's last trucks;
// - its own history and model (KV trucklocal_curves / trucklocal_model), so the stations' tested forecast,
//   which corrects itself hub-wide across the ten stations, isn't moved by a station twice their size;
// - its bags are unpacked at SGR01 the same night (a short drive), and the WMS empties a bag's count then, so
//   the boxes it has counted are remembered, the way the cities' are (truckcity.js);
// - its range is its own (how far the forecast was off on its last 20 nights), not the ten stations'.
// Its nights swing widely and much of the volume comes late, so on replayed nights the forecast was not good
// enough to tell dispatch what to book. It is built and kept in step but not shown (LOCAL_FORECAST): the row
// gives the boxes made so far against the trucks booked, and says so when they no longer fit. Every night is
// still recorded, so it can be tested again with more history.
export const LOCAL_FORECAST = false;
// CXO01 isn't here: its trucks don't follow its boxes (they run as shuttles between the buildings).
import { MODEL_KIND, SLOTS, addDays, buildModel, forecastFromModel, forecaster } from "./truckmodel.js";
import { citySlot, opNight } from "./truckcity.js";

export const LOCAL_STATIONS = { SGR01: { cutoff: "3:00", depart: "4:00" } };
export const LOCAL_CURVES_KEY = "trucklocal_curves"; // {SGR01: {night: {b: [boxes each half hour before 6 AM], f}}}
export const LOCAL_MODEL_KEY = "trucklocal_model";
const SLOT_MS = 1800000;

// The forecast for one night from the history itself (the lab's test, and the range).
export const localPoint = (f, curves, name, day, slot, boxes) => f.mix(name, day, slot, boxes);
// How far the forecast was off over the last 20 nights at this slot, the way the stations' range is kept
// (boxes over the square root of the forecast; hi / lo = 85th / 15th percentile, plan = the 70th). One
// station has one night a night, so it looks back twice as far as the stations do.
export function localRange(f, curves, name, day, slot) {
  const z = [];
  for (let back = 1; back <= 20; back++) {
    const r = curves[name]?.[addDays(day, -back)];
    if (!r || r.f == null || r.b[slot] == null) continue;
    const p = localPoint(f, curves, name, addDays(day, -back), slot, r.b[slot]);
    if (p != null) z.push((r.f - p) / Math.sqrt(Math.max(p, 4)));
  }
  if (z.length < 8) return { hi: 1.5, lo: -1.5, plan: 0.8 };
  z.sort((a, b) => a - b);
  const q = (x) => { const pos = (z.length - 1) * x, lo = Math.floor(pos), hi = Math.ceil(pos); return z[lo] + (z[hi] - z[lo]) * (pos - lo); };
  const r4 = (x) => Math.round(x * 1e4) / 1e4;
  return { hi: r4(Math.max(0, q(0.85))), lo: r4(Math.min(0, q(0.15))), plan: r4(Math.max(0, q(0.7))) };
}

// Rebuilds the forecast's numbers after a new night finishes (the hourly run). Returns true when rebuilt.
export async function refreshLocalModel(env) {
  const curves = (await env.PLAN.get(LOCAL_CURVES_KEY, "json")) || {};
  let latest = "", ver = 0;
  for (const nights of Object.values(curves)) for (const day of Object.keys(nights)) { ver++; if (day > latest) latest = day; }
  if (!latest) return false;
  const old = await env.PLAN.get(LOCAL_MODEL_KEY, "json");
  if (old && old.ver === ver && old.latest === latest && old.kind === MODEL_KIND) return false;
  const day = addDays(latest, 1);
  const f = forecaster(curves);
  const range = {};
  for (const name of Object.keys(LOCAL_STATIONS)) range[name] = Array.from({ length: SLOTS }, (_, slot) => localRange(f, curves, name, day, slot));
  await env.PLAN.put(LOCAL_MODEL_KEY, JSON.stringify({ ...buildModel(curves, day), ver, latest, range }));
  return true;
}

// Tonight's forecast: the mix on SGR01's own history, with its own range.
export function localForecast(model, name, day, slot, boxes) {
  const mix = forecastFromModel(model, name, slot, boxes);
  if (!mix) return null;
  const at = model.range?.[name]?.[slot];
  if (!at) return mix;
  const spread = Math.sqrt(Math.max(mix.point, 4));
  return { point: mix.point, high: mix.point + at.hi * spread + 0.5, low: Math.max(boxes, mix.point + at.lo * spread - 0.5), plan: mix.point + at.plan * spread + 0.5 };
}

// One 15-minute check, from the bags and trucks the stations' check already read. `local` is state.local;
// tools = the stations' helpers. Returns the kept state and the rows for the stations' table.
export async function localCheck(env, local, ctx, tools) {
  const { bags, t, opDay, opMidnight, trucks, cutoffs, limit } = ctx;
  const next = local?.version === 1 ? local : { version: 1 };
  next.seen ||= {};
  const model = await env.PLAN.get(LOCAL_MODEL_KEY, "json");
  const stations = {};
  for (const [name, { cutoff, depart }] of Object.entries(LOCAL_STATIONS)) {
    const cut = String(cutoffs?.[name] || cutoff);
    const night = opNight(depart, t, opDay, opMidnight);
    if (!night) continue;
    const counted = new Set(next.seen[name]?.day === night.day ? next.seen[name].bags : []);
    let packages = 0;
    for (const bag of bags) {
      const made = bag.optCreateTime || 0;
      if (bag.destinCenterName !== name || made <= night.start || made > night.end) continue;
      const n = Number(bag.waybillCount) || 0;
      packages += n;
      if (n >= 30) counted.add(String(bag.packageNo ?? made));
    }
    next.seen[name] = { day: night.day, bags: [...counted] };
    // Packages: what the WMS shows now can be less than was bagged (unpacked bags read 0); the most seen is kept.
    const most = next.pk?.[name]?.day === night.day ? Math.max(next.pk[name].n, packages) : packages;
    (next.pk ||= {})[name] = { day: night.day, n: most };
    const s = { ...night, cutoff: cut, boxes: counted.size, packages: most };
    // After 6 AM the night's boxes are all made: the count is the answer.
    s.forecast = t >= s.end ? { point: s.boxes, low: s.boxes, high: s.boxes, plan: s.boxes, done: true }
      : ctx.forecast ?? LOCAL_FORECAST ? ((s.end - t) / SLOT_MS < SLOTS - 0.5 ? localForecast(model, name, s.day, citySlot(s, t), s.boxes) : null) : null;
    if (model?.ppb?.[name]) s.ppbUsual = model.ppb[name];
    s.limit = limit ? limit(name, s.day) : null;
    s.cap = s.limit?.cap ?? null;
    s.booked = tools.bookedFor(trucks, name, s.day);
    stations[name] = s;
  }
  // The history, on the night's own clock (half hours before 6 AM).
  const holder = { nights: next.nights };
  const finished = tools.recordCounts(holder, Object.fromEntries(Object.entries(stations).map(([name, s]) => [name, { ...s, depart: s.end }])), t);
  next.nights = holder.nights;
  if (finished.length) {
    const curves = (await env.PLAN.get(LOCAL_CURVES_KEY, "json")) || {};
    if (finished.map(([name, rec]) => tools.finishNight(curves, name, rec)).some(Boolean)) await env.PLAN.put(LOCAL_CURVES_KEY, JSON.stringify(curves));
  }
  return { local: next, rows: tools.boardSnapshot(stations, t, null).rows.map((r) => ({ ...r, made: t >= stations[r.name].end })) };
}
