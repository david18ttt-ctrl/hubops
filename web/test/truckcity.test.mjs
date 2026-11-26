import assert from "node:assert/strict";
import { CITY_CURVES_KEY, CITY_MODEL_KEY, CITY_PICKUP_KEY, PICKUP_REPORT_KEY, blendCity, weekdayRem, cityBooked, cityBoxes, cityCheck, cityForecast, cityNight, citySlot, pickupDue, pickupFrom,
  pickupPoint, pickupStats, refreshCityModel, truckGroup } from "../src/truckcity.js";
import { boardSnapshot, finishNight, recordCounts, truckRows } from "../src/truckspace.js";
import { SLOTS, addDays } from "../src/truckmodel.js";

const local = (text) => Date.parse(text.replace(" ", "T") + "-06:00");
const opDay = "2026-11-11";
const midnight = local("2026-11-11 00:00");
const houstonDate = (ms) => new Date(ms - 6 * 3600000).toISOString().slice(0, 10);

// Which group a truck is for: its first hub. Stations and Houston's own aren't cities.
assert.equal(truckGroup("PHX.H"), "PHX");
assert.equal(truckGroup("STL.HCMH.H"), "STL");
assert.equal(truckGroup("ABQ.HBOS.H"), "BOS");
assert.equal(truckGroup("SAN.HTUS.H"), "SAN");
assert.equal(truckGroup("TUS.H"), "SAN");
assert.equal(truckGroup("SEA.HPIT.H"), "SEA");
assert.equal(truckGroup("AUS01"), null);
assert.equal(truckGroup("HOU.H"), null);
// Hub names of any length, alone or run together the way dispatch writes them (not just three letters).
assert.equal(truckGroup("RIC.H"), "MSP");
assert.equal(truckGroup("BOI.HABQ.H"), "BOS");
assert.equal(truckGroup(["PHX.H"]), "PHX");

// A city night is the operating day (6 AM to 6 AM); it stays the current one until the group's deadline the
// next day, and never ends before 6:30 AM (PHX's 4 AM deadline comes before its last boxes).
const night = (g, at) => cityNight(g, local(at), opDay, midnight);
assert.equal(night("MSP", "2026-11-11 15:00").day, "2026-11-11");
assert.equal(night("MSP", "2026-11-11 15:00").start, local("2026-11-11 06:00"));
assert.equal(night("MSP", "2026-11-11 15:00").end, local("2026-11-12 06:00"));
assert.equal(night("MSP", "2026-11-11 15:00").depart, local("2026-11-12 10:00"));
assert.equal(night("MSP", "2026-11-11 09:30").day, "2026-11-10", "9:30 AM: last night's MSP truck hasn't left");
assert.equal(night("MSP", "2026-11-11 10:15").day, "2026-11-11");
assert.equal(night("SAN", "2026-11-11 13:30").day, "2026-11-10", "TUS's deadline is 2 PM");
assert.equal(cityNight("PHX", local("2026-11-12 05:00"), "2026-11-11", midnight).day, "2026-11-11", "5 AM: PHX boxes are still being made");
assert.equal(cityNight("PHX", local("2026-11-12 06:45"), "2026-11-12", midnight + 86400000).day, "2026-11-12");
// The forecast's clock: half hours before 6 AM.
assert.equal(citySlot(night("MSP", "2026-11-11 14:00"), local("2026-11-11 14:00")), 32);
assert.equal(citySlot(night("MSP", "2026-11-11 14:00"), local("2026-11-12 02:00")), 8);
assert.equal(citySlot(night("MSP", "2026-11-11 14:00"), local("2026-11-12 07:00")), 0);

// Boxes: bags of 30+ packages made in the night, by truck group. A box stays counted after WMS empties its
// bag at the other hub; a new night starts from nothing.
const bag = (hub, n, at, no) => ({ destinCenterName: hub, waybillCount: String(n), optCreateTime: local(at), packageNo: no });
const seen = {};
let bags = [bag("PHX.H", 300, "2026-11-11 19:00", "A1"), bag("MKE.H", 280, "2026-11-11 19:30", "A2"), bag("PHX.H", 12, "2026-11-11 19:40", "A3"),
  bag("CMH.H", 300, "2026-11-11 20:00", "B1"), bag("STL.H", 300, "2026-11-10 23:00", "B0"), bag("AUS01", 300, "2026-11-11 19:00", "S1"), bag("HOU.H", 300, "2026-11-11 19:00", "H1")];
let groups = cityBoxes(bags, local("2026-11-11 21:00"), opDay, midnight, seen);
assert.deepEqual([groups.PHX.boxes, groups.STL.boxes, groups.MSP.boxes], [2, 1, 0]);
assert.equal(groups.PHX.packages, 592);
assert.equal(groups.STL.name, "STL / CMH");
bags = [bag("PHX.H", 0, "2026-11-11 19:00", "A1"), bag("MKE.H", 280, "2026-11-11 19:30", "A2"), bag("PHX.H", 250, "2026-11-11 23:00", "A4")];
groups = cityBoxes(bags, local("2026-11-12 01:00"), opDay, midnight, seen);
assert.equal(groups.PHX.boxes, 3, "A1 was unpacked at PHX: still a box that was made tonight");
assert.equal(groups.STL.boxes, 1, "kept, though the bag is no longer in the list");
groups = cityBoxes([], local("2026-11-12 16:00"), "2026-11-12", midnight + 86400000, seen);
assert.equal(groups.PHX.boxes, 0);
assert.equal(groups.PHX.day, "2026-11-12");

// Trucks: dispatch dates a city night's trucks the next calendar day. Cancelled ones aren't room, whichever
// way dispatch marked it.
const rows = truckRows([
  { fields: { "Departure date": local("2026-11-12 00:00"), Destination: ["PHX.H"], "Truck type": "53FT" } },
  { fields: { "Departure date": local("2026-11-12 00:00"), Destination: ["PHX.H"], "Truck type": "26FT" } },
  { fields: { "Departure date": local("2026-11-12 00:00"), Destination: ["STL.H", "CMH.H"], "Truck type": "53FT" } },
  { fields: { "Departure date": local("2026-11-12 00:00"), Destination: ["MSP.H"], "Truck type": "53FT", Task: [{ text: "cancelled", type: "text" }] } },
  { fields: { "Departure date": local("2026-11-12 00:00"), Destination: ["MSP.H"], "Truck type": "53FT", Cancelled: "yes" } },
  { fields: { "Departure date": local("2026-11-11 00:00"), Destination: ["PHX.H"], "Truck type": "53FT" } },
]);
assert.equal(cityBooked(rows, "PHX", opDay, houstonDate).room, 39);
assert.equal(cityBooked(rows, "STL", opDay, houstonDate).room, 28);
assert.equal(cityBooked(rows, "MSP", opDay, houstonDate).room, 0, "both cancelled");

// The pickup forecast: read from dispatch's daily report (pushed to KV) once it's posted, looked for every half hour from 10 AM.
const utc = (day) => Date.parse(`${day}T00:00:00Z`);
assert.equal(pickupFrom([{ fields: { Date: utc("2026-11-11"), "Pickup forecast": 97336 } }, { fields: { Date: utc("2026-11-10"), "Pickup forecast": 163925 } }], "2026-11-11"), 97336);
assert.equal(pickupFrom([{ fields: { Date: utc("2026-11-11") } }, { fields: { Date: utc("2026-11-10"), "Pickup forecast": 163925 } }], "2026-11-11"), null, "not posted yet");
assert.equal(pickupDue(9, 30, false), false);
assert.equal(pickupDue(12, 0, false), true);
assert.equal(pickupDue(12, 15, false), false);
assert.equal(pickupDue(12, 0, true), false, "already have it");

// Boxes per forecast package, and the usual share of the night made by each slot.
const flat = (f) => ({ f, b: Array.from({ length: SLOTS }, (_, slot) => (slot >= 26 ? 0 : slot <= 2 ? f : Math.round((f * (26 - slot)) / 24))) });
const curves = { PHX: {}, MSP: {} };
const pickup = {};
for (let k = 1; k <= 12; k++) {
  const d = addDays("2026-11-12", -k);
  pickup[d] = 100000 + 5000 * (k % 3);
  curves.PHX[d] = flat(Math.round(pickup[d] / 1000));
  curves.MSP[d] = flat(Math.round(pickup[d] / 2500));
}
const stats = pickupStats(curves, pickup, "PHX", "2026-11-12");
assert.equal(Math.round(stats.ratio * 100000) / 100, 1, "1 box per 1,000 forecast packages");
assert.equal(stats.share[32], 0, "2 PM: nothing made yet");
assert.equal(stats.share[0], 1);
assert.equal(pickupStats({ PHX: {} }, pickup, "PHX", "2026-11-12"), null, "no history: no pickup method");
// 2 PM, forecast 150,000: 150 boxes. At midnight (slot 12), with 100 made and 58% usually made by then: 100 + 42% of 150.
assert.equal(Math.round(pickupPoint(stats, 150000, 32, 0)), 150);
assert.equal(Math.round(pickupPoint(stats, 150000, 12, 100)), Math.round(100 + 150 * (1 - stats.share[12])));
assert.equal(pickupPoint(stats, null, 32, 0), null, "forecast not posted");
// The weekday's usual: what was still to come at each slot on this weekday one and two weeks ago.
const wk = weekdayRem(curves, "PHX", "2026-11-12");
assert.equal(wk[32], curves.PHX["2026-11-05"].f, "only 11/05 is saved (12 nights back): its whole night was still to come at 2 PM");
assert.equal(wk[0], 0, "6 AM: nothing still to come");
assert.equal(weekdayRem({ PHX: {} }, "PHX", "2026-11-12"), null);
// The blend: 40% pickup, 30% weekday, 30% the mix; a missing view leaves its weight to the others.
assert.equal(blendCity(100, 150, 120), 0.3 * 100 + 0.4 * 150 + 0.3 * 120);
assert.equal(blendCity(100, null, 120), 110);
assert.equal(Math.round(blendCity(100, 150, null) * 100), Math.round(((0.3 * 100 + 0.4 * 150) / 0.7) * 100));
assert.equal(blendCity(100, null, null), 100);

// The whole check, the way the stations' check calls it: boxes, the forecast, trucks, rows for the page.
{
  const store = new Map([[CITY_CURVES_KEY, JSON.stringify(curves)], [CITY_PICKUP_KEY, JSON.stringify(pickup)]]);
  const env = { PLAN: { get: async (k, type) => (store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null), put: async (k, v) => store.set(k, v) } };
  assert.equal(await refreshCityModel(env), true);
  assert.equal(await refreshCityModel(env), false, "nothing new: not rebuilt");
  const model = JSON.parse(store.get(CITY_MODEL_KEY));
  assert.equal(model.day, "2026-11-12");
  assert.ok(model.pk.PHX.ratio > 0);
  assert.equal(model.wk.PHX.length, SLOTS);
  assert.equal(model.range.length, SLOTS);
  assert.ok(model.range[32].hi >= 0 && model.range[32].lo <= 0);
  const reads = [];
  store.set(PICKUP_REPORT_KEY, JSON.stringify({ items: [{ fields: { Date: utc("2026-11-12"), "Pickup forecast": 150000 } }] }));
  const get = env.PLAN.get;
  env.PLAN.get = async (k, type) => { if (k === PICKUP_REPORT_KEY) reads.push(k); return get(k, type); };
  const tools = { boardSnapshot, recordCounts, finishNight, houstonDate };
  const day = "2026-11-12", mid = local("2026-11-12 00:00");
  const tonight = Array.from({ length: 60 }, (_, i) => bag("PHX.H", 300, "2026-11-12 18:00", `D${i}`)).map((b, i) => ({ ...b, optCreateTime: b.optCreateTime + i * 300000 }));
  const trucks = truckRows([{ fields: { "Departure date": local("2026-11-13 00:00"), Destination: ["PHX.H"], "Truck type": "53FT" } }, { fields: { "Departure date": local("2026-11-13 00:00"), Destination: ["PHX.H"], "Truck type": "53FT" } }]);
  const ctx = (at, list) => ({ bags: list, t: local(at), opDay: day, opMidnight: mid, hour: +at.slice(11, 13), minute: +at.slice(14, 16), trucks, readAt: local(at) });
  // 2:30 PM: no box yet. The pickup forecast is read once (150,000 → about 150 boxes; the usual night is ~105).
  let city = await cityCheck(env, undefined, ctx("2026-11-12 14:30", []), tools);
  assert.equal(reads.length, 1);
  assert.equal(JSON.parse(store.get(CITY_PICKUP_KEY))[day], 150000);
  let phx = city.board.rows.find((r) => r.group === "PHX");
  assert.equal(phx.name, "PHX");
  assert.equal(phx.boxes, 0);
  assert.equal(phx.pickup, 150000);
  assert.ok(phx.point > 115 && phx.point < 140, `between the usual and the pickup forecast: ${phx.point}`);
  assert.deepEqual(phx.booked, [["53FT", 2]]);
  assert.equal(phx.room, 56);
  assert.equal(phx.status, "short");
  assert.equal(phx.need[0][0], "53FT");
  assert.equal(phx.askBy, local("2026-11-13 01:00"), "3 hours before PHX's 4 AM deadline");
  assert.deepEqual(city.board.rows.map((r) => r.group), ["PHX", "SEA", "MSP", "BOS", "STL", "SAN"], "in deadline order");
  // The SAN trucks also carry about 10 pass-through boxes a day: said on the row, not counted.
  assert.deepEqual(city.board.rows.map((r) => r.through), [0, 0, 0, 0, 0, 10]);
  // 3 PM: the forecast is in, so it isn't read again.
  city = await cityCheck(env, city, ctx("2026-11-12 15:00", []), tools);
  assert.equal(reads.length, 1);
  // 11 PM: 60 boxes made; kept on the half hour for the history.
  city = await cityCheck(env, city, ctx("2026-11-12 23:00", tonight), tools);
  assert.equal(city.board.rows[0].boxes, 60);
  assert.equal(city.nights.PHX.b[14], 60, "11 PM is 14 half hours before 6 AM");
  // 6 AM: the night is finished and goes into the cities' history; the row stays (last night's trucks).
  city = await cityCheck(env, city, { ...ctx("2026-11-13 06:00", tonight), opDay: day, opMidnight: mid }, tools);
  assert.equal(JSON.parse(store.get(CITY_CURVES_KEY)).PHX[day].f, 60);
  // 6:15 AM (a new operating day): PHX's row is still last night's, all made; its count is the answer.
  city = await cityCheck(env, city, { ...ctx("2026-11-13 06:15", tonight), opDay: "2026-11-13", opMidnight: mid + 86400000 }, tools);
  phx = city.board.rows.find((r) => r.group === "PHX");
  assert.deepEqual([phx.day, phx.made, phx.boxes, phx.point, phx.high], [day, true, 60, 60, 60]);
  assert.equal(phx.status, "few", "60 boxes on 56 of room: 4 over rides or waits, so nearly full, no truck call");
  // 6:45 AM: PHX moves on to tonight; MSP (10 AM deadline) is still on last night.
  city = await cityCheck(env, city, { ...ctx("2026-11-13 06:45", tonight), opDay: "2026-11-13", opMidnight: mid + 86400000 }, tools);
  assert.equal(city.board.rows.find((r) => r.group === "PHX").day, "2026-11-13");
  assert.equal(city.board.rows.find((r) => r.group === "PHX").boxes, 0);
  assert.equal(city.board.rows.find((r) => r.group === "MSP").day, day);
  // After 6 AM a forecast is just the count.
  assert.deepEqual(cityForecast(null, "MSP", cityNight("MSP", local("2026-11-13 08:00"), "2026-11-13", mid + 86400000), local("2026-11-13 08:00"), 31, null),
    { point: 31, low: 31, high: 31, plan: 31, done: true });
}
console.log("truckcity tests passed");
