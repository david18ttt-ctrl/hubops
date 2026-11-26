// Demo mode (DEMO = "true", local dev only): fills KV with synthetic, internally consistent data so every page
// renders without the ops host or a WMS. The truck numbers are not hard-coded: synthetic history goes through
// the real forecast (truckmodel.js) and the real board code (truckspace.js, truckcity.js, trucklocal.js).
// Everything is generated relative to "now", so the pages look live; it is re-seeded every few minutes.
import { addAlertTo, ALERTS_KEY, houstonNow } from "./appalert.js";
import { operationWindow } from "./hubtime.js";
import { SLOTS, V3, addDays, buildModel, forecastFromModel } from "./truckmodel.js";
import { boardSnapshot, bookedFor, bulkNote, finishNight, limitFor, recordCounts, stationBoxes, stationNight, trucksFor } from "./truckspace.js";
import { CITY_GROUPS, CITY_MODEL_KEY, CITY_PICKUP_KEY, cityCheck, cityNight } from "./truckcity.js";
import { localCheck } from "./trucklocal.js";
const DEMO_STATIONS = ["AUS01", "ACT01", "VCT01", "BRO01", "HRL01", "SJT01", "GGG01", "TXK01", "SHV01", "AEX01", "CXO01", "SGR01"];

export const DEMO_KEYS = {
  APP_VIEW_KEY: "demo-app-0000000000",
  TRUCKS_VIEW_KEY: "demo-trucks-00000000",
  UNSORTED_VIEW_KEY: "demo-unsorted-000000",
  PDA_VIEW_KEY: "demo-pda-00000000000",
  APP_PUSH_KEY: "demo-push-key",
  LABEL_SCAN_KEY: "demo-scan-0000000000",
  LABEL_PRINT_KEY: "demo-print-000000000",
};
const SEEDED_KEY = "demo_seeded_at";
const RESEED_MS = 5 * 60000;
const DAY = 86400000;
const SLOT_MS = 1800000;

// The env with demo view keys and tab links filled in where no real ones are set.
export function demoEnv(env) {
  const out = { ...env };
  for (const [k, v] of Object.entries(DEMO_KEYS)) if (!out[k]) out[k] = v;
  if (!out.APP_LINKS) {
    out.APP_LINKS = JSON.stringify([{ name: "Unsorted", icon: "▤", url: "/u/" + out.UNSORTED_VIEW_KEY },
      { name: "Trucks", icon: "⛟", url: "/trucks/" + out.TRUCKS_VIEW_KEY }, { name: "PDA", icon: "▦", url: "/pda/" + out.PDA_VIEW_KEY }]);
  }
  return out;
}

export function demoIndex(env) {
  const link = (path, name, what) => `<li><a href="${path}">${name}</a> · ${what}</li>`;
  return new Response(`<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hub ops demo</title><style>body{font:16px/1.5 -apple-system,Arial,sans-serif;max-width:640px;margin:32px auto;padding:0 16px;color:#262626}a{color:#d4380d;font-weight:600}</style></head>
<body><h1>Hub ops · demo data</h1><p>Synthetic data for a fictional hub ("HOU.H"), regenerated every few minutes.</p><ul>
${link("/a/" + env.APP_VIEW_KEY + "/", "Phone app", "tasks, stuck chutes, alerts, feeders, scanner, the pages below as tabs")}
${link("/trucks/" + env.TRUCKS_VIEW_KEY, "Truck page", "truck-space forecast per station and city group")}
${link("/u/" + env.UNSORTED_VIEW_KEY, "Unsorted board", "inbound truckloads not sorted yet, oldest first")}
${link("/pda/" + env.PDA_VIEW_KEY, "PDA board", "PDA scan speed and error rates per person")}
${link("/s/" + env.LABEL_SCAN_KEY, "Label station", "scan a package, the box's label prints (hubops/label_station.py + agent/label_agent.py)")}
${link("/s/" + env.LABEL_SCAN_KEY + "/stickers", "Sticker page", "chute stickers, plain codes and time labels to the label printer")}
${link("/p/" + env.LABEL_PRINT_KEY, "Print station", "the print queue's page; the print agent polls /p/<key>/next")}
</ul></body></html>`, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } });
}

// Writes the demo data to KV unless it was written in the last few minutes.
export async function seedDemo(env, now = Date.now()) {
  const last = Number(await env.PLAN.get(SEEDED_KEY)) || 0;
  if (now - last < RESEED_MS) return false;
  const seed = await demoSeed(now);
  for (const [key, value] of Object.entries(seed)) await env.PLAN.put(key, value);
  await env.PLAN.put(SEEDED_KEY, String(now));
  return true;
}

// ---- The data. {KV key: string or ArrayBuffer value}.
function rng(seed) { // mulberry32: the same day gives the same numbers
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const gauss = (r) => Math.sqrt(-2 * Math.log(r() || 1e-9)) * Math.cos(2 * Math.PI * r());
const WEEKDAY = [0.7, 1.25, 1.1, 1.0, 1.0, 0.95, 0.85]; // Sun … Sat
const weekday = (day) => WEEKDAY[new Date(`${day}T12:00:00Z`).getUTCDay()];
const STATION_BASE = { AUS01: 74, ACT01: 31, VCT01: 26, BRO01: 69, HRL01: 81, SJT01: 44, GGG01: 21, TXK01: 36, SHV01: 28, AEX01: 23 };
const CITY_BASE = { PHX: 58, SEA: 118, MSP: 92, BOS: 71, STL: 64, SAN: 55 };
const PER_BOX = 330;
// Share of a night's boxes made by `hours` before the departure (stations) or before 6 AM (cities).
const doneBy = (hours, center, width, floor) => {
  const f = (x) => 1 / (1 + Math.exp((x - center) / width));
  return Math.min(1, f(hours) / f(floor));
};
const stationShare = (hours) => doneBy(hours, 7, 2.2, 1.5);
const cityShare = (hours) => doneBy(hours, 6, 2.4, 1);

function curvesFor(bases, share, opDay, r) {
  const curves = {};
  for (const [name, base] of Object.entries(bases)) {
    curves[name] = {};
    for (let back = 35; back >= 1; back--) {
      const day = addDays(opDay, -back);
      const f = Math.max(3, Math.round(base * weekday(day) * (1 + 0.08 * gauss(r)) * (1 + 0.004 * (35 - back))));
      const b = Array.from({ length: SLOTS }, (_, slot) => Math.round(f * share(slot / 2)));
      curves[name][day] = { b, f, pk: f * PER_BOX + Math.round(400 * gauss(r)) };
    }
  }
  return curves;
}

const memoryKv = (data) => ({ get: async (k) => data[k] ?? null, put: async (k, v) => { data[k] = typeof v === "string" ? JSON.parse(v) : v; } });

export async function demoSeed(now = Date.now()) {
  const ct = (ms) => houstonNow(ms);
  const opDay = operationWindow(new Date(now))[0].slice(0, 10);
  const offset = Date.parse(ct(now).replace(" ", "T") + "Z") - now; // hub time minus UTC
  const opMidnight = Date.parse(`${opDay}T00:00:00Z`) - offset;
  const minutes = (now - opMidnight) / 60000;
  const r = rng(Number(opDay.replace(/-/g, "")));
  const ymd = opDay.slice(2).replace(/-/g, "");
  const out = {};
  const put = (k, v) => { out[k] = typeof v === "string" || v instanceof ArrayBuffer ? v : JSON.stringify(v); };

  // ---- Stations: synthetic history → the real model → tonight's boxes from synthetic bags → the real board.
  const curves = curvesFor(STATION_BASE, stationShare, opDay, r);
  const model = buildModel(curves, opDay, V3);
  const bags = [];
  const trucks = [];
  const finals = {};
  let seq = 1;
  const bagNo = (chute) => `BG01${ymd}${String(seq++).padStart(4, "0")}-${String(chute).padStart(3, "0")}`;
  for (const [name, base] of Object.entries(STATION_BASE)) {
    const night = stationNight(name, now, opDay, opMidnight);
    if (!night) continue;
    const final = Math.round(base * weekday(night.day) * (1 + 0.1 * gauss(r)));
    finals[name] = final;
    const made = Math.round(final * stationShare(Math.max(0, (night.depart - now) / 3600000)));
    const chute = 80 + Number(name.slice(2)) * 7;
    for (let i = 0; i < made; i++) {
      const at = night.start + ((Math.min(now, night.end) - night.start) * (i + 0.5)) / Math.max(1, made);
      const route = `${name}-${String(1 + (i % 4)).padStart(3, "0")}`;
      bags.push({ packageNo: bagNo(chute), destinCenterName: name, exchangeBureauCode: route, waybillCount: String(260 + Math.floor(150 * r())), optCreateTime: Math.round(at) });
    }
    bags.push({ packageNo: bagNo(chute), destinCenterName: name, exchangeBureauCode: `${name}-001`, waybillCount: "12", optCreateTime: now - 600000 });
    // Booked trucks: about right for most stations; one station short, one with nothing booked yet.
    let plan = trucksFor(name, Math.max(1, final - 4)).flatMap((x) => Array(x.n).fill(x.type));
    if (name === "HRL01" && plan.length > 1) plan = plan.slice(1);
    if (name === "SHV01") plan = [];
    const dayMs = Date.parse(`${night.day}T12:00:00Z`);
    plan.forEach((type, i) => trucks.push({ day: dayMs, station: name, type, cancelled: false, boxes: null, task: `TT${night.day.slice(2).replace(/-/g, "")}${String(100 + i + Number(name.slice(2)) * 10).padStart(4, "0")}` }));
  }
  // A held route, said two nights ago and not repeated since: the page asks whether it still stands.
  const holds = { "GGG01-003": { since: addDays(opDay, -2), why: "station asked to hold" } };
  const stations = stationBoxes(bags, now, opDay, opMidnight, {}, new Set(Object.keys(holds)));
  const limits = { day: opDay, rows: [["ACT01", "20:30", 0, 0, 9000], ["TXK01", "0:00", 0, 0, 14000]] };
  for (const [name, s] of Object.entries(stations)) {
    s.limit = limitFor(name, s.day, limits, []);
    s.cap = s.limit.cap;
    if (s.held) { s.held.since = holds[s.held.routes[0]]?.since || null; s.held.ask = true; }
    const ahead = (s.depart - now) / SLOT_MS;
    if (ahead < SLOTS - 0.5) s.forecast = forecastFromModel(model, name, Math.max(0, Math.round(ahead)), s.boxes);
    s.booked = bookedFor(trucks, name, s.day);
  }
  const bulk = minutes < 16 * 60 ? { day: opDay } : { day: opDay, coming: true, at: 16 * 60 + 20, size: 5340 };
  const state = { version: 2, readAt: now - 7 * 60000, trucks };
  state.board = { ...boardSnapshot(stations, now, state.readAt), bulk: bulkNote(bulk, opDay, minutes), calendar: null };

  // SGR01, the hub's own city station (trucklocal.js), on its own night.
  const local = {};
  const localNight = { start: opMidnight + 6 * 3600000 };
  const localBoxes = Math.round(150 * Math.min(1, Math.max(0, (now - localNight.start) / (20 * 3600000))));
  for (let i = 0; i < localBoxes; i++) {
    bags.push({ packageNo: bagNo(211), destinCenterName: "SGR01", waybillCount: String(240 + Math.floor(120 * r())), optCreateTime: localNight.start + 60000 + i * 400000 });
  }
  for (let i = 0; i < 6; i++) trucks.push({ day: Date.parse(`${opDay}T12:00:00Z`), station: "SGR01", type: "53FT", cancelled: false, boxes: null, task: `TT${ymd}${String(900 + i).padStart(4, "0")}` });
  const tools = { bookedFor, boardSnapshot, recordCounts, finishNight, houstonDate: (ms) => ct(ms).slice(0, 10) };
  const { rows: localRows } = await localCheck({ PLAN: memoryKv(local) }, null, { bags, t: now, opDay, opMidnight, trucks, cutoffs: {}, limit: null }, tools);
  state.board.rows = [...state.board.rows, ...localRows].sort((a, b) => a.depart - b.depart || a.name.localeCompare(b.name));

  // City groups: their own history and model, then the real city check on synthetic bags.
  const cityCurves = curvesFor(CITY_BASE, cityShare, opDay, r);
  const pickup = {};
  for (let back = 35; back >= 0; back--) pickup[addDays(opDay, -back)] = Math.round(42000 * weekday(addDays(opDay, -back)) * (1 + 0.05 * gauss(r)));
  const cityKv = { [CITY_MODEL_KEY]: { ...buildModel(cityCurves, opDay), pk: {}, wk: {} }, [CITY_PICKUP_KEY]: pickup };
  const cityBags = [];
  for (const [group, base] of Object.entries(CITY_BASE)) {
    const night = cityNight(group, now, opDay, opMidnight);
    if (!night) continue;
    const made = Math.round(base * weekday(night.day) * cityShare(Math.max(0, (night.end - now) / 3600000)));
    const hubs = CITY_GROUPS[group].hubs;
    for (let i = 0; i < made; i++) {
      cityBags.push({ packageNo: bagNo(50 + i % 9), destinCenterName: hubs[i % Math.min(2, hubs.length)], waybillCount: String(90 + Math.floor(120 * r())),
        optCreateTime: Math.round(night.start + ((Math.min(now, night.end) - night.start) * (i + 0.5)) / Math.max(1, made)) });
    }
    const n = Math.max(1, Math.ceil((base * weekday(night.day) * 1.06) / 28) - (group === "SEA" ? 1 : 0)); // SEA one short
    const dest = hubs.length > 1 ? hubs[0] + hubs[1] : hubs[0];
    for (let i = 0; i < n; i++) trucks.push({ day: Date.parse(`${addDays(night.day, 1)}T12:00:00Z`), station: dest, type: "53FT", cancelled: false, boxes: null, task: null });
  }
  state.city = await cityCheck({ PLAN: memoryKv(cityKv) }, null,
    { bags: cityBags, t: now, opDay, opMidnight, hour: 0, minute: 1, trucks, readAt: state.readAt }, tools);
  put("truckspace", state);
  put("held_routes", holds);
  put("bulk_today", bulk);
  put("station_limits", limits);

  // ---- Phone app: floor card, chutes, feeders, tasks, alerts.
  const at = (minAgo) => ct(now - minAgo * 60000);
  const hourName = (ms) => { const h = +ct(ms).slice(11, 13); return `${h % 12 || 12} ${h < 12 ? "AM" : "PM"}`; };
  const hourLabel = (ms) => `${hourName(ms - 3600000)}–${hourName(ms)}`;
  put("app_floor", {
    updated: at(4),
    speed: { per_hour: 11840, normal: 12600, hour: hourLabel(now), minutes: 60, vendor_a: 6120, vendor_b: 5720, rej: 2.4, night: 61250 },
    freight: { in_full: 18420, updated: at(6), trucks: [{ from: "PHX.H", in_full: 7210, codes: ["0031", "0044"] }, { from: "BOS.H", in_full: 6650, codes: ["0102"] },
      { from: "Pickup", in_full: 4560 }],
      not_unloaded: [{ from: "MSP.H", pcs: 3980, gaylords: 14, arrived: at(95) }], not_unloaded_pcs: 3980, tonight: 22400, later: 3100 },
    trucks: { posted: at(130), coming: 14800, list: [{ eta: at(-35), route: "SEA.H", pkgs: 6200 }, { eta: at(-80), route: "SAN.H", pkgs: 4100 },
      { eta: at(-190), route: "STL.H", pkgs: 4500 }] },
  });
  const chuteWhere = (c) => `${DEMO_STATIONS[c % 9]}-00${1 + (c % 4)}`;
  put("app_chutes", {
    updated: at(1),
    live: [{ chute: "118", where: chuteWhere(118), start: at(9), last: at(0.5), rejects: 23, boxes: 41, alerted: true },
      { chute: "093", where: chuteWhere(93), start: at(4), last: at(1.5), rejects: 7, boxes: 12, alerted: false }],
    history: [{ chute: "141", where: chuteWhere(141), start: at(190), last: at(176), cleared: true, minutes: 14, boxes: 38 },
      { chute: "087", where: chuteWhere(87), start: at(120), last: at(111), cleared: true, minutes: 9, boxes: 17 },
      { chute: "118", where: chuteWhere(118), start: at(9), last: at(0.5), cleared: false, minutes: 9, boxes: 41 }],
    labels: { open: 2, merged: 3, filled: 1, left: 1, flagged: 7, shift: [9, 412], last_card: hourName(now - 3600000),
      chutes: [{ chute: "104", where: "VCT01-002", bags: [{ bag: `BG01${ymd}0412-104`, start: at(160).slice(11, 16), end: at(120).slice(11, 16), hour: hourName(now - 3600000), pcs: 18, then: 14 }] },
        { chute: "129", where: "SJT01-001", bags: [{ bag: `BG01${ymd}0533-129`, start: at(90).slice(11, 16), end: "", hour: hourName(now), pcs: 9, then: 9 }] }] },
  });
  const feedHour = (ago, k) => {
    const stations = Array.from({ length: 30 }, (_, i) => {
      const s = String(i + 1), normal = 380 + Math.round(90 * Math.sin(i)), fed = Math.round(normal * k * (0.8 + 0.35 * r()));
      const rejNormal = Math.round((2 + 1.2 * r()) * 10) / 10, rej = Math.round((rejNormal + gauss(r) * 0.9) * 10) / 10;
      return { s, fed, normal, rej: Math.max(0, rej), rej_normal: rejNormal, slow: fed < 0.6 * normal, high: rej >= 4 && rej >= 1.6 * rejNormal };
    });
    stations[15] = { ...stations[15], rej: 7.8, high: true, every_night: true };
    stations[22] = { ...stations[22], fed: 170, slow: true };
    const fed = stations.reduce((a, x) => a + x.fed, 0), normal = stations.reduce((a, x) => a + x.normal, 0);
    const flags = stations.filter((x) => x.slow || x.high);
    return { hour: hourLabel(now - ago * 3600000), updated: at(ago * 60 + 6), fed, normal, rej: 2.6, stations,
      text: flags.map((x) => x.slow ? `Slow: ${x.s}: ${x.fed} (normal ${x.normal})` : `Rejects: ${x.s}: ${x.rej}%`).join("\n") };
  };
  const hours = [feedHour(0, 1), feedHour(1, 1.02), feedHour(2, 0.97)];
  put("app_feeder", { ...hours[0], earlier: hours.slice(1),
    night: { hours: 3, per_hour: Math.round(hours.reduce((a, h) => a + h.fed, 0) / 3), normal: hours[0].normal, rej: 2.5, stations: hours[0].stations } });
  put("app_donow", {
    updated: at(2),
    tasks: [
      { id: "chute|118", source: "chute", do: "Clear chute 118 (ACT01-004): 41 boxes going around", why: "Rejecting for 9 min; the belt fills up if it keeps going", since: at(8),
        extra: { fixed: { chute: "118", start: at(9) } } },
      { id: "dock|HRL01", source: "dock", do: "Pull 6 HRL01 boxes to door 17", why: "HRL01's truck is at the door with room for 6 more", since: at(14),
        extra: { more: "Door 17 · HRL01 53ft · loaded 21 of 27\nBoxes ready: chutes 115, 116, 117" } },
      { id: "label|104", source: "label", do: "Relabel the bag on chute 104", why: "Two labels printed for one chute within 15 minutes", since: at(25) },
    ],
    cleared: [{ how: "done", gone: at(40), do: "Bring 10 empty gaylords to chutes 80–95" }, { how: "fixed", gone: at(75), do: "Clear chute 141 (GGG01-002)" }],
  });
  let alerts = [];
  const alert = (minAgo, a) => { alerts = addAlertTo(alerts, a, now - minAgo * 60000); };
  alert(300, { title: "Truck space", text: "Truck plan 9 AM (expected boxes, likely range; another truck at 5+ boxes over):\n" +
    state.board.rows.filter((x) => x.point != null).slice(0, 4).map((x) => `${x.name} · expect ${Math.round(x.point)} · booked ${x.room}`).join("\n") });
  alert(140, { title: "Feed stations", text: hours[1].text });
  alert(60, { title: "Chat Q", text: "Dispatch asked: how many HRL01 boxes are left for the last truck?", reply: "About 6 boxes left, they'll be ready in 20 minutes.",
    link: "https://chat.example.com/open?conversation=demo-dispatch" });
  alert(20, { title: "Stuck chute", text: "Chute 118 (ACT01-004): 23 rejects in 9 min, 41 boxes going around." });
  alerts.forEach((a) => { a.pushed = true; });
  put(ALERTS_KEY, alerts);

  // ---- Unsorted board.
  const truck = (i, from, arrivedAgo, state, full, total) => {
    const task = `TT${ymd}${String(3300 + i).padStart(4, "0")}`;
    const gaylords = Array.from({ length: 6 }, (_, k) => {
      const of = 280 + ((i * 37 + k * 53) % 160), left = k < full ? of - (k * 7) % 40 : (k * 11) % 30;
      return { bag: `GL${ymd}${String(i * 100 + k).padStart(5, "0")}`, code: String(i * 100 + k).padStart(4, "0"), left, of, full: k < full,
        noscan: state === "not unloaded", to: [DEMO_STATIONS[(i + k) % 9], "SGR01"] };
    });
    const left = gaylords.reduce((a, b) => a + b.left, 0);
    return { task, from, arrived: at(arrivedAgo), where: i % 2 ? "Building 1" : "Building 2?", state, kind: "", total, left, gaylords: gaylords.length, open: full,
      in_full: gaylords.filter((b) => b.full).reduce((a, b) => a + b.left, 0), unloaded: [at(arrivedAgo - 8), at(arrivedAgo - 50)], by: ["Unload crew 2"],
      noscan_full: state === "unloaded" && i === 2 ? 1 : 0, bags: gaylords };
  };
  const board = { updated: at(3), day: opDay, wms: { waiting: 23110, older: 1840 },
    trucks: [truck(1, "BOS.H", 26 * 60, "unloaded", 3, 2400), truck(2, "PHX.H", 7 * 60, "unloaded", 4, 2650), truck(3, "MSP.H", 95, "not unloaded", 6, 1980)],
    almost: [{ task: `TT${ymd}3299`, from: "STL.H", arrived: at(31 * 60), left: 6, total: 2210, gaylords: 7, where: "Building 1" }] };
  put("unsorted_board", board);
  const csv = board.trucks.map((t) => `#${t.task}\ngaylord,parcels left,stations\n` + t.bags.map((b) => `${b.bag},${b.left},${b.to.join(" ")}`).join("\n")).join("\n");
  if (typeof CompressionStream !== "undefined") {
    put("unsorted_lists_gz", await new Response(new Blob([csv]).stream().pipeThrough(new CompressionStream("gzip"))).arrayBuffer());
  }

  // ---- PDA board.
  const NAMES = { VA: ["Sam Ortiz", "Ana Reyes", "Luis Moreno", "Kim Tran"], VB: ["Dee Carter", "Joel Price", "Nina Lopez"], VC: ["Raj Patel", "Eva Brooks"] };
  const tabs = {}, history = {};
  for (const [tab, names] of Object.entries(NAMES)) {
    tabs[tab] = { people: names.map((name, k) => {
      const rate = 380 + Math.round(240 * r());
      const hourly = Array.from({ length: 24 }, (_, c) => (c >= 9 && c <= 18 ? Math.round(rate * (0.8 + 0.3 * r())) : 0));
      return { name, tab, id: `E${tab}${k}`, written: name.split(" ")[0], pdas: [`${tab}_${String(k + 1).padStart(2, "0")}`],
        parcels: hourly.reduce((a, b) => a + b, 0), hours: hourly.filter(Boolean).length, hourly, lastHour: 18 };
    }).sort((a, b) => b.parcels / b.hours - a.parcels / a.hours), idle: [], notes: [] };
  }
  tabs.OTHER = { people: [], idle: [], notes: [] };
  for (let back = 1; back <= 10; back++) {
    history[addDays(opDay, -back)] = Object.values(tabs).flatMap((t) => t.people).map((p) => [p.name, p.tab, p.id, Math.round(p.parcels * (0.85 + 0.3 * r())), p.hours]);
  }
  put("pda_board", { updated: ct(opMidnight + 26.6 * 3600000 < now ? opMidnight + 26.6 * 3600000 : now - 3600000), day: opDay, live: false, tabs });
  put("pda_history", history);
  put("pda_errors", { updated: at(300), days: Object.fromEntries(Object.keys(history).slice(0, 7).map((day) =>
    [day, Object.values(tabs).flatMap((t) => t.people).map((p) => [p.name, p.tab, p.id, Math.round(p.parcels * 1.4), Math.round(3 * r()), Math.round(2 * r() * 10) / 10, "checked"])])) });
  return out;
}

