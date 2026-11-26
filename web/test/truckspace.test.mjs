import assert from "node:assert/strict";
import { CURVES_KEY, MODEL_KEY, alerts, assess, batchesOf, boardSnapshot, bookedFor, fullSizes, learnFull, setFullSizes, weeklyScore, withFactor, bulkArrival, bulkDelta, bulkNote, bulkOrder, withBulk, withoutBulk, cellText, clockMinutes, holdRoutes, isTruckSpaceTime, setBusyWait, truckReadDue, heldLeaving, heldAtDeparture, withHeld, HOLD_ASK_AFTER, morningPlan, recordCounts, refreshTruckModel,
  runTruckSpace, stationBoxes, stationNight, leftovers, withLeft, truckBoxes, truckRows, trucksFor, limitFor, mergeNews, packagesPerBox, trimNotes,
  usualPackagesPerBox } from "../src/truckspace.js";
import { SLOTS, V3, addDays, buildModel, forecastFromModel, forecaster } from "../src/truckmodel.js";
import { parseHoldOrder } from "../src/parse.js";

// The hub is UTC-6 in November. Operating day 11/09 starts 6 AM local.
const local = (text) => Date.parse(text.replace(" ", "T") + "-06:00");
const midnight = local("2026-11-09 00:00");

assert.equal(isTruckSpaceTime(15, 0), true);
assert.equal(isTruckSpaceTime(9, 45), true, "all day: the counts are the forecast's history");
assert.equal(isTruckSpaceTime(15, 5), false, "every 15 minutes");

// Dispatch's truck log: read at every check 7 AM–7 PM (a booking shows on the page 15 minutes later), every
// half hour at night, not 5–7 AM, and only while some station has boxes worth a truck.
const MIN = 60000;
assert.equal(truckReadDue(8, false, 60, 15 * MIN), true, "8:45 AM, read at 8:30");
assert.equal(truckReadDue(10, false, 60, 14.9 * MIN), true, "a check a few seconds early still reads");
assert.equal(truckReadDue(18, false, 60, 15 * MIN), true);
assert.equal(truckReadDue(19, false, 60, 15 * MIN), false, "from 7 PM, every half hour");
assert.equal(truckReadDue(19, false, 60, 30 * MIN), true);
assert.equal(truckReadDue(2, false, 60, 30 * MIN), true);
assert.equal(truckReadDue(6, false, 60, 120 * MIN), false, "5–7 AM: nobody books, no warnings");
assert.equal(truckReadDue(8, false, 12, 60 * MIN), false, "nothing worth a truck yet");
assert.equal(truckReadDue(8, false, 60, null), true, "never read");
assert.equal(truckReadDue(9, true, 0, 0), true, "the 9 AM plan always reads");
assert.equal(clockMinutes("20:30"), 20 * 60 + 30);
assert.equal(clockMinutes("1:00"), 25 * 60, "before noon is the next morning");
assert.equal(clockMinutes("0:00"), 24 * 60);

// Base cells come in several shapes.
assert.equal(cellText([{ text: "1:00", type: "text" }]), "1:00");
assert.equal(cellText({ value: [{ text: "x" }] }), "x");
const rows = truckRows([
  { fields: { "Departure date": midnight, Destination: "SJT01", "Truck type": "53FT" } },
  { fields: { "Departure date": midnight, Destination: "AUS01", "Truck type": "53FT" } },
  { fields: { "Departure date": midnight, Destination: "AUS01", "Truck type": "26FT", Cancelled: [{ text: "cancelled" }] } },
  { fields: { "Departure date": midnight - 86400000, Destination: "AUS01", "Truck type": "53FT" } },
  { fields: { Destination: "" } },
]);
assert.deepEqual(rows.map((r) => [r.station, r.type, r.cancelled]), [["SJT01", "53FT", false], ["AUS01", "53FT", false], ["AUS01", "26FT", true], ["AUS01", "53FT", false]]);

// Full trucks per station: AUS01's 53ft holds 27, BRO01's and HRL01's 28, the rest 27; a 26ft 11.
assert.equal(truckBoxes("AUS01", "53FT"), 27);
assert.equal(truckBoxes("BRO01", "53FT"), 28);
assert.equal(truckBoxes("TXK01", "53FT"), 27);
assert.equal(truckBoxes("HRL01", "26FT"), 11);
assert.equal(truckBoxes("SJT01", ""), 27, "no type filled in counts as a 53ft");
assert.deepEqual(trucksFor("AUS01", 60), [{ type: "53FT", n: 2 }, { type: "26FT", n: 1 }], "54 + a 26ft for 6 more");
assert.deepEqual(trucksFor("AUS01", 70), [{ type: "53FT", n: 3 }], "16 more is more than a 26ft");
assert.deepEqual(trucksFor("GGG01", 9), [{ type: "26FT", n: 1 }]);
assert.deepEqual(trucksFor("GGG01", 0), []);
assert.deepEqual(bookedFor(rows, "AUS01", "2026-11-09"), { trucks: [rows[1]], room: 27 }, "cancelled and yesterday's trucks aren't room");

// A station's night: from last night's cutoff + 15 minutes to tonight's, and its truck's latest departure.
const opDay = "2026-11-09";
let n = stationNight("AUS01", local("2026-11-09 15:00"), opDay, midnight);
assert.deepEqual(n, { day: "2026-11-09", depart: local("2026-11-09 22:00"), start: local("2026-11-08 20:45"), end: local("2026-11-09 20:45"),
  opStart: local("2026-11-09 06:00") });
n = stationNight("AUS01", local("2026-11-09 22:30"), opDay, midnight);
assert.equal(n.day, "2026-11-10", "after its truck left, the boxes are tomorrow's");
assert.equal(n.start, local("2026-11-09 20:45"));
n = stationNight("AEX01", local("2026-11-10 03:15"), opDay, midnight);
assert.equal(n.day, "2026-11-09", "AEX01's 11/09 truck leaves at 3:30 AM on 11/10");
assert.equal(n.end, local("2026-11-10 02:15"));
assert.equal(stationNight("AEX01", local("2026-11-10 03:45"), opDay, midnight).day, "2026-11-10");
assert.equal(stationNight("SJT01", local("2026-11-09 15:00"), opDay, midnight, "23:00").end, local("2026-11-09 23:15"), "the report's cutoff wins");

// Boxes: bags of 30+ packages in the window; held routes, the hub's own city stations and trunk lines aren't counted.
const bag = (station, n, at, route) => ({ destinCenterName: station, waybillCount: String(n), optCreateTime: local(at), exchangeBureauCode: route });
const bags = [
  ...Array.from({ length: 29 }, (_, i) => bag("SJT01", 200, `2026-11-09 ${19 + Math.floor(i / 10)}:${String((i % 10) * 5).padStart(2, "0")}`)),
  bag("SJT01", 5, "2026-11-09 23:29"), // too small to be a box
  bag("SJT01", 300, "2026-11-09 23:50"), // after the 23:30 cutoff + 15: tomorrow's truck
  bag("AUS01", 300, "2026-11-08 20:40"), // before last night's cutoff + 15: last night's truck
  bag("AUS01", 300, "2026-11-08 21:00"), // after it: tonight's
  bag("GGG01", 300, "2026-11-09 20:10", "GGG01-003"), // a held route stays off the truck
  bag("CXO01", 300, "2026-11-09 20:10"),
  bag("PHX.H", 300, "2026-11-09 20:10"),
];
const at = local("2026-11-09 20:00");
const stations = stationBoxes(bags, at, opDay, midnight, {}, new Set(["GGG01-003"]));
assert.equal(stations.SJT01.boxes, 29);
assert.equal(stations.AUS01.boxes, 1);
assert.equal(stations.GGG01.boxes, 0);
// What the held route bagged is kept apart, so the page can show the night both ways.
assert.deepEqual(stations.GGG01.held, { routes: ["GGG01-003"], boxes: 1, packages: 300 });
assert.equal(stations.SJT01.held, undefined);
assert.equal(stations.CXO01, undefined);
assert.equal(stations["PHX.H"], undefined);

// A hold that isn't being followed: two of the route's bags, made on a later night than the hold was said,
// checked out of the hub. Bags of the night it was said, bags not scanned out and other routes don't count.
{
  const out = (station, route, made, left) => ({ destinCenterName: station, exchangeBureauCode: route, waybillCount: "300", optCreateTime: local(made), optCheckOutTime: left ? local(left) : null });
  const holds = { "GGG01-003": { since: "2026-11-08" }, "TXK01-012": { since: "2026-11-09" }, "AEX01-002": { since: "bad" } };
  const list = [
    out("GGG01", "GGG01-003", "2026-11-09 19:00", "2026-11-09 23:50"), out("GGG01", "GGG01-003", "2026-11-09 21:00", "2026-11-10 00:47"),
    out("GGG01", "GGG01-003", "2026-11-08 19:00", "2026-11-08 20:04"), // the night it was said: may have left before
    out("GGG01", "GGG01-004", "2026-11-09 19:00", "2026-11-09 23:50"),
    out("TXK01", "TXK01-012", "2026-11-09 19:00", "2026-11-09 23:00"), // same night as its hold
    out("TXK01", "TXK01-012", "2026-11-10 01:00", null), // not scanned out
  ];
  assert.deepEqual(heldLeaving(list, holds, opDay, midnight), { "GGG01-003": { n: 2, last: local("2026-11-10 00:47") } });
  assert.deepEqual(heldLeaving(list.slice(1), holds, opDay, midnight), {}, "one scan isn't enough");
  assert.deepEqual(heldLeaving(list, {}, opDay, midnight), {});
}

// Counts kept every half hour before departure; the departure count finishes the night.
{
  const state = {};
  const s = { AUS01: { day: "2026-11-09", depart: local("2026-11-09 22:00"), boxes: 40 } };
  assert.deepEqual(recordCounts(state, s, local("2026-11-09 21:00")), []);
  assert.equal(state.nights.AUS01.b[2], 40, "an hour before departure is slot 2");
  recordCounts(state, s, local("2026-11-09 21:15"));
  assert.equal(state.nights.AUS01.b.filter((x) => x != null).length, 1, "quarter past isn't a half-hour slot");
  s.AUS01.boxes = 52;
  const done = recordCounts(state, s, local("2026-11-09 22:00"));
  assert.equal(done.length, 1);
  assert.equal(done[0][1].b[0], 52);
  // The next check starts tomorrow's night without saving tonight's twice.
  assert.deepEqual(recordCounts(state, { AUS01: { day: "2026-11-10", depart: local("2026-11-10 22:00"), boxes: 0 } }, local("2026-11-09 22:15")), []);
  assert.equal(state.nights.AUS01.day, "2026-11-10");
}

// ---- The forecast (truckmodel.js): history from synthetic nights.
// Every night: 10 boxes 6 hours out, 30 at departure (AUS01); GGG01: 2 then 8.
function synthetic(nights, scale = () => 1) {
  const curves = { AUS01: {}, GGG01: {} };
  for (let i = nights; i >= 1; i--) {
    const day = addDays("2026-11-10", -i);
    const k = scale(i);
    const st01 = Array.from({ length: SLOTS }, (_, slot) => Math.round(Math.max(0, 30 * k - slot * (20 * k / 12))));
    const st07 = Array.from({ length: SLOTS }, (_, slot) => Math.round(Math.max(0, 8 * k - slot * 0.5)));
    curves.AUS01[day] = { b: st01, f: st01[0] };
    curves.GGG01[day] = { b: st07, f: st07[0] };
  }
  return curves;
}
{
  const curves = synthetic(20);
  const f = forecaster(curves);
  // Steady nights: every method agrees, so tonight's forecast at 6 h out with 10 boxes is 30.
  assert.equal(Math.round(f.mix("AUS01", "2026-11-10", 12, 10)), 30);
  // A busy night so far: the remaining boxes still come on top (36), tonight's pace scales them up (48),
  // and the "usual final" method (30) weighs in: every method was exactly right on these steady nights.
  assert.equal(Math.round(f.mix("AUS01", "2026-11-10", 12, 16)), 37);
  // The kept numbers give the same forecast as the full computation.
  const model = buildModel(curves, "2026-11-10");
  for (const [boxes, slot] of [[10, 12], [16, 12], [3, 30], [29, 1]]) {
    const full = f.mix("AUS01", "2026-11-10", slot, boxes);
    assert.ok(Math.abs(forecastFromModel(model, "AUS01", slot, boxes).point - full) < 0.01, `slot ${slot}`);
  }
  // Too few nights: no forecast.
  assert.equal(forecastFromModel(buildModel(synthetic(3), "2026-11-10"), "AUS01", 12, 10), null);
  // A hub-wide surge: the last 3 nights ran 30% heavy; the hub fix and the mix move toward it.
  const surge = synthetic(20, (i) => (i <= 3 ? 1.3 : 1));
  const s = forecaster(surge);
  assert.ok(s.hubFix("2026-11-10", 12) > 1.1, "rem10 ran low across stations lately");
  assert.ok(s.mix("AUS01", "2026-11-10", 12, 13) > 35, "the mix leans to what's been right");
  // The warning range comes from its own recent misses; steady nights keep it tight.
  const r = f.range("2026-11-10", 12);
  assert.ok(r.hi >= 0 && r.hi < 0.3 && r.lo <= 0 && r.plan >= 0 && r.plan <= r.hi, JSON.stringify(r));
  // Misses count in boxes over the square root of the forecast: GGG01 running 2 boxes over every night is a
  // small miss in that measure, so it doesn't widen AUS01's range much.
  const swing = synthetic(20);
  for (const [day, rec] of Object.entries(swing.GGG01)) swing.GGG01[day] = { ...rec, f: rec.f + 2 };
  const fs2 = forecaster(swing);
  assert.ok(fs2.range("2026-11-10", 12).hi < 1, "GGG01's +2 is a small miss at AUS01's size");
  // A model built the old way isn't used: the hourly run rebuilds it.
  assert.equal(forecastFromModel({ ...model, kind: "ratio" }, "AUS01", 12, 10), null);
  assert.ok(forecastFromModel(model, "AUS01", 12, 10).plan >= 30, "the plan number is kept for the 9 AM advice");
  // v3 (the stations' model): steady nights still give 30; the kept numbers match the full
  // computation; a season climbing 30% over the last 3 nights pulls the 9 AM forecast up more than v2 does.
  const v3 = forecaster(curves, V3);
  assert.equal(Math.round(v3.mix("AUS01", "2026-11-10", 20, 0)), 30);
  const m3 = buildModel(curves, "2026-11-10", V3);
  assert.equal(m3.kind, V3.kind);
  for (const [boxes, slot] of [[10, 12], [16, 20], [3, 30], [29, 1]]) {
    assert.ok(Math.abs(forecastFromModel(m3, "AUS01", slot, boxes).point - v3.mix("AUS01", "2026-11-10", slot, boxes)) < 0.02, `v3 slot ${slot}`);
  }
  assert.ok(forecaster(surge, V3).mix("AUS01", "2026-11-10", 20, 5) > s.mix("AUS01", "2026-11-10", 20, 5), "v3 leans into a climbing season");
}

// ---- Messages.
const ten = local("2026-11-09 16:00");
const st = (boxes, forecast, trucks, extra = {}) => ({ day: "2026-11-09", depart: local("2026-11-09 22:00"), boxes, forecast,
  booked: { trucks, room: trucks.reduce((a, t) => a + truckBoxes("AUS01", t.type), 0) }, ...extra });
const two53 = [{ type: "53FT" }, { type: "53FT" }];
{
  // Heading past the room: ask by departure − 3 hours.
  let r = alerts({ AUS01: st(30, { point: 60.4, low: 56, high: 66.2 }, two53) }, {}, ten);
  assert.deepEqual(r.lines, ["AUS01 10:00 PM: expect 60 (56–67), room 54 (2× 53ft), short 6. Add 1× 26ft by 7:00 PM."]);
  assert.equal(alerts({ AUS01: st(30, { point: 60.4, low: 56, high: 66.2 }, two53) }, r.said, ten).lines.length, 0, "said once");
  // Clearly worse (4+ boxes more): said again, but never more than twice a night.
  r = alerts({ AUS01: st(40, { point: 65, low: 61, high: 70 }, two53) }, r.said, ten);
  assert.equal(r.lines.length, 1);
  assert.equal(alerts({ AUS01: st(58, { point: 75, low: 70, high: 80 }, two53) }, r.said, ten).lines.length, 0);
  // Could run out: only within 6 hours of departure, and when the high end is 5+ past the room.
  const could = st(30, { point: 50, low: 45, high: 60 }, two53);
  assert.equal(alerts({ AUS01: could }, {}, local("2026-11-09 15:00")).lines.length, 0, "7 hours out: too early for a maybe");
  assert.match(alerts({ AUS01: could }, {}, ten).lines[0], /^AUS01 10:00 PM: expect 50, up to 60; room 54 \(2× 53ft\)\. Backup 1× 26ft by 7:00 PM\.$/);
  assert.equal(alerts({ AUS01: st(30, { point: 50, low: 45, high: 58 }, two53) }, {}, ten).lines.length, 0, "58 vs 54: 4 over squeeze in or wait");
  // 1–4 over: no truck call, even at the high end of the forecast or already over.
  assert.equal(alerts({ AUS01: st(30, { point: 58, low: 55, high: 58.9 }, two53) }, {}, ten).lines.length, 0, "heading 4 over");
  assert.equal(alerts({ AUS01: st(58, { point: 58, low: 58, high: 58.5 }, two53) }, {}, ten).lines.length, 0, "already 4 over");
  // Already 5+ over the room.
  assert.match(alerts({ AUS01: st(59, { point: 60, low: 59, high: 63 }, two53) }, {}, ten).lines[0], /^AUS01 10:00 PM: 59 bagged, room 54 \(2× 53ft\)\. Add 1× 26ft by 7:00 PM\.$/);
  // Nothing booked, within 10 hours of the truck.
  assert.match(alerts({ AUS01: st(5, { point: 20, low: 17, high: 24 }, []) }, {}, ten).lines[0], /^AUS01 10:00 PM: expect 20 \(17–24\), no truck booked\. Book by 7:00 PM\.$/);
  // Past the ask-by time: say it may come late.
  assert.match(alerts({ AUS01: st(40, { point: 60, low: 56, high: 66 }, two53) }, {}, local("2026-11-09 20:00")).lines[0], /Add 1× 26ft now \(may arrive late\)\.$/);
  // Fits: nothing.
  assert.equal(alerts({ AUS01: st(20, { point: 40, low: 36, high: 45 }, two53) }, {}, ten).lines.length, 0);
}
{
  // The 9 AM plan, in departure order. Trucks are sized for the plan number less 4 boxes that may wait.
  const lines = morningPlan({
    GGG01: { depart: local("2026-11-10 02:00"), forecast: { point: 8, low: 6, high: 10.2, plan: 9 }, booked: { trucks: [{ type: "26FT" }], room: 11 } },
    AUS01: st(10, { point: 58, low: 53, high: 63.5, plan: 61 }, two53),
    BRO01: { depart: local("2026-11-09 23:00"), forecast: { point: 29, low: 26, high: 32.4, plan: 30.5 }, booked: { trucks: [{ type: "53FT" }], room: 28 } },
  });
  assert.deepEqual(lines, [
    "Truck plan 9 AM (expected boxes, likely range; another truck at 5+ boxes over):",
    "AUS01 10:00 PM · expect 58 (53–64) · booked 2× 53ft = 54 · need 2× 53ft + 1× 26ft · NEED TRUCK",
    "BRO01 11:00 PM · expect 29 (26–33) · booked 1× 53ft = 28 · need 1× 53ft · nearly full",
    "GGG01 2:00 AM · expect 8 (6–11) · booked 1× 26ft = 11 · need 1× 26ft",
  ]);
}

{
  // A station's limit (the capacity sheet) caps what the trucks are sized for: BRO01 takes 15,000 packages, about 41
  // boxes at 360 a box, so a forecast of 50 calls for one more truck for 41, not 50, and 9 boxes wait.
  const st04 = { day: "2026-11-09", depart: local("2026-11-09 23:00"), boxes: 20, packages: 7200, cap: 15000,
    forecast: { point: 50, low: 46, high: 56, plan: 53 }, booked: { trucks: [{ type: "53FT" }], room: 28 } };
  const a = assess("BRO01", st04);
  assert.equal(Math.round(a.takes), 42);
  assert.equal(a.status, "short");
  assert.equal(a.overCap, true);
  assert.equal(Math.round(a.hold), 8);
  assert.deepEqual(a.need, [{ type: "53FT", n: 1 }, { type: "26FT", n: 1 }]);
  // With room for what the station takes, no truck call even though the forecast is over: the rest waits.
  const roomy = { ...st04, booked: { trucks: [{ type: "53FT" }, { type: "26FT" }], room: 39 } };
  assert.equal(assess("BRO01", roomy).status, "few");
  assert.equal(alerts({ BRO01: roomy }, {}, local("2026-11-09 19:00")).lines.length, 0);
  // No limit given: the forecast as it is.
  assert.equal(assess("BRO01", { ...st04, cap: null }).takes, Infinity);
  // The page's numbers, in departure order.
  const snap = boardSnapshot({ BRO01: st04, AUS01: st(30, { point: 50, low: 45, high: 60, plan: 55 }, two53) }, ten, ten - 600000);
  assert.deepEqual(snap.rows.map((r) => r.name), ["AUS01", "BRO01"]);
  assert.deepEqual(snap.rows[1].booked, [["53FT", 1]]);
  assert.deepEqual(snap.rows[1].need, [["53FT", 1], ["26FT", 1]]);
  assert.equal(snap.rows[1].status, "short");
  assert.equal(snap.rows[1].askBy, local("2026-11-09 20:00"));
  assert.equal(snap.rows[0].status, "maybe", "50 expected on 54 of room, could reach 60");
  // What the top of the range would take, sized like the trucks to order (4 may wait): 60 − 4 = 56, 2×53 + a 26ft.
  assert.deepEqual(snap.rows[0].needHigh, [["53FT", 2], ["26FT", 1]]);
  assert.equal(snap.trucksAt, ten - 600000);
  assert.equal(snap.rows[0].held, null, "no held route: nothing extra on the row");
  // A held route: the row keeps the night without it, and next to it the night with it on the truck. Its
  // boxes so far grow the way the station's are expected to (4 → 12.5, so 1 held box → about 3).
  const st07 = { day: "2026-11-12", depart: local("2026-11-13 02:00"), boxes: 4, packages: 730, cap: null,
    forecast: { point: 12.5, low: 9.2, high: 19.7, plan: 16.6 }, booked: { trucks: [{ type: "26FT" }], room: 11 },
    held: { routes: ["GGG01-003"], boxes: 1, packages: 300, since: "2026-11-08", ask: true } };
  assert.equal(Math.round(heldAtDeparture(st07) * 10) / 10, 3.1);
  assert.equal(withHeld(st07).boxes, 5);
  assert.equal(withHeld(st07).packages, 1030);
  const row = boardSnapshot({ GGG01: st07 }, ten, ten).rows[0];
  assert.equal(row.point, 12.5);
  assert.equal(row.status, "maybe");
  assert.deepEqual(row.held, { routes: ["GGG01-003"], since: "2026-11-08", ask: true, boxes: 1, packages: 300,
    point: 15.6, low: 12.3, high: 22.8, plan: 19.7, status: "maybe", need: [["53FT", 1]] });
  // Nothing bagged on the held route yet, or too few boxes to judge its growth: just what it has so far.
  assert.equal(heldAtDeparture({ ...st07, boxes: 2 }), 1);
  assert.equal(heldAtDeparture({ ...st07, held: { ...st07.held, boxes: 0 } }), 0);
  // No forecast yet (more than a day before the truck): the held block is still there, without numbers.
  assert.equal(boardSnapshot({ GGG01: { ...st07, forecast: null } }, ten, ten).rows[0].held.point, null);
}

{
  // BULK not coming: the later stations' forecasts come down (about 2.4 boxes early in the day, 0.5 at the
  // end), never below the boxes already bagged; AUS01, VCT01 and ACT01 don't change.
  const f = { point: 30, low: 26, high: 35, plan: 32 };
  assert.equal(Math.round(withoutBulk(f, "SJT01", 30, 10).point * 10) / 10, 27.6);
  assert.equal(Math.round(withoutBulk(f, "SJT01", 4, 10).point * 10) / 10, 29.5);
  assert.equal(withoutBulk(f, "AUS01", 30, 10), f);
  assert.equal(withoutBulk({ point: 11, low: 10, high: 12, plan: 11 }, "AEX01", 30, 10).point, 10);
  // The chat sets it for today's operating day, and can ask.
  const kv = new Map();
  const env = { PLAN: { get: async (k) => (kv.has(k) ? JSON.parse(kv.get(k)) : null), put: async (k, v) => kv.set(k, v) } };
  const at = new Date(local("2026-11-10 10:00"));
  assert.match(await bulkOrder(env, { ask: true }, at), /^BULK 11\/10: not in WMS yet\./);
  assert.match(await bulkOrder(env, { coming: false }, at), /^BULK 11\/10: marked not coming\./);
  assert.deepEqual(JSON.parse(kv.get("bulk_today")), { day: "2026-11-10", coming: false });
  assert.match(await bulkOrder(env, { ask: true }, new Date(local("2026-11-11 02:00"))), /^BULK 11\/10: marked not coming\. Later stations/);
  assert.match(await bulkOrder(env, { ask: true }, new Date(local("2026-11-11 07:00"))), /^BULK 11\/11: not in WMS yet\./);
  // Without anyone telling it: BULK as usual until it's in; then its size; not in by 10:30 PM, or in after
  // 10 PM, means out for tonight.
  const day = "2026-11-10";
  assert.equal(bulkDelta(null, day, 18 * 60), 0);
  assert.equal(bulkDelta({ day }, day, 22 * 60 + 15), 0);
  assert.equal(bulkDelta({ day }, day, 22 * 60 + 30), -5.5);
  assert.equal(bulkDelta(null, day, 25 * 60), -5.5, "1 AM, still not in");
  assert.equal(bulkDelta({ day, at: 18 * 60 + 19, size: 6716, coming: true }, day, 23 * 60), 1.216);
  assert.equal(bulkDelta({ day, at: 22 * 60 + 40, size: 6716, coming: true }, day, 23 * 60), -5.5, "after 10 PM: not finished tonight");
  assert.equal(bulkDelta({ day: "2026-11-09", coming: false }, day, 9 * 60), 0, "yesterday's news");
  assert.equal(Math.round(withBulk(f, "SJT01", 14, 10, 1.216).point * 100) / 100, 30.34);
  assert.deepEqual(bulkNote({ day, at: 1099, size: 6716 }, day, 23 * 60), { state: "in", at: 1099, size: 6716 });
  assert.deepEqual(bulkNote({ day }, day, 23 * 60), { state: "late" });
  assert.deepEqual(bulkNote(null, day, 15 * 60), { state: "expected" });
  // Reading it from WMS's arrival list: BULK-PHX's batches only, the first scan-in time.
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    assert.equal(body.arrivalBeginTime, "2026-11-10 00:00:00");
    assert.deepEqual(body.customerIdList, ["c0ffee00000000000000000000000001"]);
    return new Response(JSON.stringify({ code: 200, data: { records: [
      { customerName: "BULK-PHX", waybillCount: 400, arrivalTime: local("2026-11-10 18:32") },
      { customerName: "BULK-PHX", waybillCount: 6316, arrivalTime: local("2026-11-10 18:19") },
      { customerName: "Marketco", waybillCount: 900, arrivalTime: local("2026-11-10 12:00") },
      { customerName: "BULK-PHX", waybillCount: 1, arrivalTime: local("2026-11-10 09:00") }] } }));
  };
  assert.deepEqual(await bulkArrival({ WMS_TOKEN: "t" }, day, local("2026-11-10 00:00")), { at: 18 * 60 + 19, size: 6716 });
  globalThis.fetch = async (url, init) => {
    assert.equal(JSON.parse(init.body).customerIdList, undefined, "by name: the whole list");
    return new Response(JSON.stringify({ code: 200, data: { records: [] } }));
  };
  assert.equal(await bulkArrival({ WMS_TOKEN: "t" }, day, local("2026-11-10 00:00"), true), null);
  globalThis.fetch = realFetch;
}

{
  // Real 53ft sizes from dispatch's box counts: on nights with two or more trucks, the fullest 53ft.
  const day = (d) => local(`${d} 00:00`);
  const trucks = [];
  for (const [d, a, b] of [["2026-11-02", 27, 30], ["2026-11-03", 16, 23], ["2026-11-05", 28, 20], ["2026-11-06", 23, 25], ["2026-11-07", 24, 30], ["2026-11-09", 30, 22]]) {
    trucks.push({ day: day(d), station: "HRL01", type: "53FT", boxes: a }, { day: day(d), station: "HRL01", type: "53FT", boxes: b });
  }
  trucks.push({ day: day("2026-11-08"), station: "HRL01", type: "53FT", boxes: 29 }); // one truck: says nothing about full
  trucks.push({ day: day("2026-11-10"), station: "HRL01", type: "53FT", boxes: 12 }, { day: day("2026-11-10"), station: "HRL01", type: "26FT", boxes: 3 }); // tonight: not finished
  const full = {};
  assert.equal(learnFull(full, trucks, "2026-11-10"), true);
  assert.deepEqual(full.HRL01, { "2026-11-02": 30, "2026-11-03": 23, "2026-11-05": 28, "2026-11-06": 25, "2026-11-07": 30, "2026-11-09": 30 });
  assert.equal(learnFull(full, trucks, "2026-11-10"), false, "nothing new");
  assert.deepEqual(fullSizes(full), { HRL01: 29 });
  assert.deepEqual(fullSizes({ BRO01: { a: 31, b: 33, c: 32, d: 34, e: 35 } }), { BRO01: 31 }, "held to 31");
  assert.deepEqual(fullSizes({ BRO01: { a: 30, b: 31 } }), {}, "too few nights");
  setFullSizes({ HRL01: 29 });
  assert.equal(truckBoxes("HRL01", "53FT"), 29);
  assert.equal(truckBoxes("HRL01", "26FT"), 11);
  setFullSizes({});
  assert.equal(truckBoxes("HRL01", "53FT"), 28);
  assert.equal(truckRows([{ fields: { Destination: "HRL01", "Truck type": "53FT", Boxes: [{ text: "27", type: "text" }] } }])[0].boxes, 27);
  // Marketplace's batches from the arrival list.
  assert.deepEqual(batchesOf([{ customerName: "Marketco", waybillCount: 900, arrivalTime: local("2026-11-10 22:05") },
    { customerName: "Marketco", waybillCount: 1, arrivalTime: local("2026-11-10 12:00") }, { customerName: "BULK-PHX", waybillCount: 500, arrivalTime: local("2026-11-10 18:00") }],
  "MARKETCO", local("2026-11-10 00:00")), [[1325, 900]]);
  // Peak days scale only the boxes still to come.
  assert.deepEqual(withFactor({ point: 30, low: 26, high: 35, plan: 32 }, 10, 1.5), { point: 40, low: 34, high: 47.5, plan: 43, factor: 1.5 });
  assert.equal(withFactor({ point: 30 }, 10, 1).point, 30);
  // The weekly scorecard.
  const curves = { HRL01: {}, GGG01: {}, VCT01: {} };
  const p = (x) => Object.assign(Array(48).fill(null), { 10: x });
  curves.HRL01["2026-11-10"] = { b: [], f: 62, am: { point: 60, plan: 64, need: ["53FT", "53FT", "26FT"] }, p: p(61), kinds: ["53FT", "53FT"], room5: 56, warned: { times: 1, rank: 3, room: 56 } };
  curves.HRL01["2026-11-11"] = { b: [], f: 70, am: { point: 58, plan: 62, need: ["53FT", "53FT"] }, p: p(66), kinds: ["53FT", "53FT", "26FT"], room5: 56 };
  curves.GGG01["2026-11-11"] = { b: [], f: 9, am: { point: 10, plan: 11, need: ["26FT"] }, p: p(9), kinds: ["26FT"], room5: 11 };
  curves.VCT01["2026-11-11"] = { b: [], f: 30, am: { point: 12, plan: 14, need: ["26FT"] }, kinds: ["53FT"] };
  curves.HRL01["2026-11-01"] = { b: [], f: 99, am: { point: 1, need: [] } }; // more than a week back
  const week = weeklyScore(curves, "2026-11-16");
  assert.deepEqual(week, [
    "Truck forecast scorecard, 11/9–11/15 (4 station-nights):",
    "- 9 AM forecast off by 8.3 boxes on average; 5 hours before the truck, 1.7.",
    "- 9 AM truck call: right 50%, short (5+ left) 50%, one too many 0%. Dispatch's final bookings: 75% / 25% / 0%.",
    "- Warnings: 1 sent, 1 for nights that did run 5+ boxes over the trucks booked then; 1 night ran 5+ over with no warning (HRL01 11/11).",
    "- Biggest 9 AM misses: VCT01 11/11 said 12, came 30; HRL01 11/11 said 58, came 70; HRL01 11/10 said 60, came 62.",
  ]);
  assert.deepEqual(weeklyScore({}, "2026-11-16"), []);
}

// ---- The whole run, with KV and the WMS mocked.
{
  const store = new Map([["station_limits", JSON.stringify({ rows: [["AUS01", "20:30"]] })]]);
  const curves = synthetic(20);
  store.set(CURVES_KEY, JSON.stringify(curves));
  const env = {
    WMS_TOKEN: "t",
    PLAN: { get: async (k, type) => {
      if (k === "truck_log") calls.push("kv:truck_log");
      return store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null;
    },
      put: async (k, v) => {
        if (k === "app_alerts") { // alerts go to the phone app: record the newest one like a sent message
          if (alertsDown) throw new Error("KV busy");
          calls.push(JSON.parse(v).at(-1).text);
        }
        store.set(k, v);
      } },
  };
  // The hourly run builds the forecast's numbers once, and again only when a night is added.
  assert.equal(await refreshTruckModel(env), true);
  assert.equal(JSON.parse(store.get(MODEL_KEY)).day, "2026-11-10");
  assert.equal(await refreshTruckModel(env), false);
  const today = "2026-11-10";
  const mid = local("2026-11-10 00:00");
  // Tonight like the usual night: 10 boxes made 11 AM–4 PM, 20 more by the 8:30 PM cutoff.
  const live = [
    ...Array.from({ length: 10 }, (_, i) => ({ destinCenterName: "AUS01", waybillCount: "300", optCreateTime: local(`${today} 11:00`) + i * 1740000 })),
    ...Array.from({ length: 20 }, (_, i) => ({ destinCenterName: "AUS01", waybillCount: "300", optCreateTime: local(`${today} 16:01`) + i * 780000 })),
  ];
  const calls = [];
  let alertsDown = false;
  globalThis.fetch = async (url, init) => {
    const body = init?.body ? JSON.parse(init.body) : {};
    calls.push(String(url).replace(/^https?:\/\/[^/]+/, ""));
    if (String(url).includes("/api/bags/search")) {
      if (body.checkOutEndTime) { // signed out since 6 AM yesterday: last night's trucks and tonight's
        const out = live.filter((b) => b.optCheckOutTime >= local(body.checkOutBeginTime.slice(0, 16)) && b.optCheckOutTime <= local(body.checkOutEndTime.slice(0, 16)));
        return new Response(JSON.stringify({ code: 200, data: { total: out.length, list: body.pageNum === 1 ? out : [] } }));
      }
      const made = live.filter((b) => b.optCreateTime <= local(body.createEndTime.slice(0, 16)) && b.optCreateTime >= local(body.createBeginTime.slice(0, 16)));
      return new Response(JSON.stringify({ code: 200, data: { total: made.length, list: body.pageNum === 1 ? made : [] } }));
    }
    if (String(url).includes("/api/arrivals")) return new Response(JSON.stringify({ code: 200, data: { records: [
      { customerName: "BULK-PHX", waybillCount: 6716, arrivalTime: local(`${today} 15:40`) },
      { customerName: "Marketco", waybillCount: 2400, arrivalTime: local(`${today} 12:10`) }] } }));
    if (String(url).includes("/api/sorter/pipeline")) {
      assert.equal(body.trunkFlag, 2);
      return new Response(JSON.stringify({ code: 200, data: { records: [{ destinCenterName: "AUS01", allShouldSortVotes: 4200, estimatedArriveVotes: 1800 }] } }));
    }
    throw new Error("unexpected " + url);
  };
  // Dispatch's truck log, as the ops host pushes it to KV: tonight's 26ft and five earlier nights of two 53fts.
  store.set("truck_log", JSON.stringify({ items: [
    { fields: { "Departure date": mid, Destination: "AUS01", "Truck type": "26FT" } },
    ...[1, 2, 3, 4, 5].flatMap((k) => [{ fields: { "Departure date": mid - k * 86400000, Destination: "AUS01", "Truck type": "53FT", Boxes: "29" } },
      { fields: { "Departure date": mid - k * 86400000, Destination: "AUS01", "Truck type": "53FT", Boxes: "20" } }])] }));
  // 9 AM, but the alert can't be saved: the page still gets the check's numbers, and the plan isn't
  // counted as sent, so the next check sends it.
  {
    const real = globalThis.fetch;
    alertsDown = true;
    await runTruckSpace(env, new Date(local(`${today} 09:00`)));
    alertsDown = false;
    const kept = JSON.parse(store.get("truckspace"));
    assert.equal(kept.board.at, local(`${today} 09:00`));
    assert.equal(kept.planDay, undefined, "not sent: not marked as sent");
    assert.equal(kept.nights.AUS01.am, undefined);
    globalThis.fetch = real;
    calls.length = 0;
  }
  // 9 AM: the plan, after one truck-log read.
  await runTruckSpace(env, new Date(local(`${today} 09:00`)));
  const plan = calls.find((c) => c.startsWith("Truck plan"));
  assert.ok(plan && plan.includes("AUS01 10:00 PM · expect 30 (30–31) · booked 1× 26ft = 11 · need 1× 53ft · NEED TRUCK"), calls.join("\n"));
  assert.equal(calls.filter((c) => c === "kv:truck_log").length, 1, "one truck-log read");
  // The page shows the 9 AM call ("trucks to order") from 9 AM on.
  assert.deepEqual(JSON.parse(store.get("truckspace")).board.rows.find((r) => r.name === "AUS01").am, { point: 30, need: [["53FT", 1]] });
  // The 9 AM call is kept for the scorecard; dispatch's box counts are collected (not used yet).
  assert.deepEqual(JSON.parse(store.get("truckspace")).nights.AUS01.am, { point: 30, plan: 30.5, need: ["53FT"], lb: 0 });
  assert.deepEqual(JSON.parse(store.get("truckspace_full")).AUS01, Object.fromEntries([5, 4, 3, 2, 1].map((k) => [addDays(today, -k), 29])));
  // 9:15: nothing to send before 11 AM. The truck log is read again (dispatch is booking: the page shows a
  // new truck at the next check), and no alert goes out.
  calls.length = 0;
  await runTruckSpace(env, new Date(local(`${today} 09:15`)));
  assert.equal(calls.filter((c) => c === "kv:truck_log").length, 1);
  assert.equal(calls.some((c) => c.startsWith("Truck")), false);
  assert.equal(JSON.parse(store.get("truckspace")).readAt, local(`${today} 09:15`));
  // No truck log to read (9:30): the check still finishes with the last read, and the page says when that was.
  {
    const log = store.get("truck_log");
    store.delete("truck_log");
    await runTruckSpace(env, new Date(local(`${today} 09:30`)));
    const kept = JSON.parse(store.get("truckspace"));
    assert.equal(kept.board.at, local(`${today} 09:30`), "the numbers are this check's");
    assert.equal(kept.readAt, local(`${today} 09:15`), "the truck log is still the 9:15 read");
    assert.deepEqual(kept.board.rows.find((r) => r.name === "AUS01").booked, [["26FT", 1]]);
    store.set("truck_log", log);
  }
  // WMS says "busy" once (9:45): the bag list is asked again and the check goes through.
  {
    const real = globalThis.fetch;
    let busy = 1;
    globalThis.fetch = async (url, init) => (String(url).includes("/api/bags/search") && busy-- > 0
      ? new Response(JSON.stringify({ code: 500, msg: "Le serveur est anormal" })) : real(url, init));
    setBusyWait(1);
    await runTruckSpace(env, new Date(local(`${today} 09:45`)));
    assert.equal(JSON.parse(store.get("truckspace")).board.at, local(`${today} 09:45`));
    globalThis.fetch = real;
  }
  // Late morning: the ops host pushes today's limits and the limits reader's reading of them; the next check
  // plans with them.
  store.set("station_limits", JSON.stringify({ day: today, rows: [["AUS01", "20:30", 0, 0, 28000, 0]],
    ai: { day: today, at: local(`${today} 11:57`), stations: { AUS01: { kind: "normal", use_now: 28, plan_tomorrow: 28, note: "", why: "usual" } } } }));
  await runTruckSpace(env, new Date(local(`${today} 12:00`)));
  const limitRow = JSON.parse(store.get("truckspace")).board.rows.find((r) => r.name === "AUS01");
  assert.deepEqual([limitRow.limit.from, limitRow.cap], ["sheet", 28000]);
  // A hold marked last night is still taken as it is; one nobody has repeated for two nights is in doubt, and
  // the page asks about it.
  const heldRow = () => JSON.parse(store.get("truckspace")).board.rows.find((r) => r.name === "AUS01").held;
  assert.equal(heldRow(), null);
  store.set("held_routes", JSON.stringify({ "AUS01-005": { since: addDays(today, -1), why: "chat" } }));
  await runTruckSpace(env, new Date(local(`${today} 12:30`)));
  assert.deepEqual([heldRow().routes, heldRow().since, heldRow().ask], [["AUS01-005"], addDays(today, -1), false]);
  store.set("held_routes", JSON.stringify({ "AUS01-005": { since: addDays(today, -HOLD_ASK_AFTER), why: "chat" } }));
  await runTruckSpace(env, new Date(local(`${today} 12:45`)));
  assert.equal(heldRow().ask, true);
  store.delete("held_routes");
  // (Noon already warned about AUS01; forget that so 4 PM shows the warning as the first one.)
  const kept = JSON.parse(store.get("truckspace"));
  delete kept.said;
  store.set("truckspace", JSON.stringify(kept));
  // 4 PM: 10 boxes, heading for about 30 against one 26ft (11): a warning, with the ask-by time.
  calls.length = 0;
  await runTruckSpace(env, new Date(local(`${today} 16:00`)));
  const dm = calls.find((c) => c.startsWith("Truck space"));
  assert.ok(dm && /AUS01 10:00 PM: expect 30 \(\d+–\d+\), room 11 \(1× 26ft\), short 19\. Add 1× 53ft by 7:00 PM\./.test(dm), calls.join("\n"));
  // The page's numbers are kept with the state: AUS01's boxes, the forecast and the 26ft booked.
  const board = JSON.parse(store.get("truckspace")).board;
  assert.equal(board.rows.find((r) => r.name === "AUS01").boxes, 10);
  assert.deepEqual(board.rows.find((r) => r.name === "AUS01").booked, [["26FT", 1]]);
  assert.equal(board.rows.find((r) => r.name === "AUS01").status, "short");
  // The bulk truck was looked for after 2 PM and found (in at 3:40 PM, 6,716 packages); before 2 PM it wasn't looked for.
  assert.deepEqual(board.bulk, { state: "in", at: 15 * 60 + 40, size: 6716 });
  assert.deepEqual(JSON.parse(store.get("bulk_today")), { day: today, at: 940, size: 6716, coming: true });
  assert.equal(calls.filter((c) => c.includes("/api/arrivals")).length, 1);
  // Marketplace's batches are kept for the day; the station pipeline is kept with the half-hour counts.
  assert.deepEqual(JSON.parse(store.get("truckspace_hub"))[today], { tt: [[730, 2400]] });
  assert.deepEqual(JSON.parse(store.get("truckspace")).nights.AUS01.w[12], [4200, 1800]);
  // The departure check finishes the night into the curves; the next hourly run rebuilds.
  await runTruckSpace(env, new Date(local(`${today} 22:00`)));
  const saved = JSON.parse(store.get(CURVES_KEY)).AUS01[today];
  assert.equal(saved.f, 30);
  assert.equal(saved.b[12], 10, "4 PM is 6 hours before 10 PM: slot 12");
  // What the scorecard needs went into the curves with the night.
  assert.deepEqual(saved.w[12], [4200, 1800]);
  assert.equal(saved.p[12], 30);
  assert.deepEqual(saved.am.need, ["53FT"]);
  assert.equal(saved.warned.room, 11);
  assert.deepEqual(saved.kinds, ["26FT"]);
  assert.equal(await refreshTruckModel(env), true);
  assert.equal(JSON.parse(store.get(MODEL_KEY)).day, "2026-11-11");
  // Not on the quarter hour: nothing at all.
  calls.length = 0;
  await runTruckSpace(env, new Date(local(`${today} 16:05`)));
  assert.equal(calls.length, 0);
}

// Holds from an operator command: saved in KV, read by the next check.
{
  const store = new Map();
  const env = { PLAN: { get: async (k, type) => (store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null), put: async (k, v) => store.set(k, v) } };
  const at = new Date(local("2026-11-09 23:00"));
  let reply = await holdRoutes(env, parseHoldOrder("hold ggg 003"), at);
  assert.match(reply, /^Holding GGG01-003/);
  assert.match(reply, /Held now: GGG01-003 \(since 11\/09\)\./);
  assert.deepEqual(Object.keys(JSON.parse(store.get("held_routes"))), ["GGG01-003"]);
  assert.match(await holdRoutes(env, parseHoldOrder("what routes are held?"), at), /^Held now: GGG01-003/);
  reply = await holdRoutes(env, parseHoldOrder("release ggg 003"), at);
  assert.match(reply, /^Released GGG01-003/);
  assert.match(reply, /No routes are held\./);
  assert.match(await holdRoutes(env, parseHoldOrder("release txk 012"), at), /^TXK01-012 wasn't held\./);
  assert.deepEqual(parseHoldOrder("don't send GGG01-003"), { action: "hold", routes: ["GGG01-003"] });
  assert.equal(parseHoldOrder("lower act to 3k"), null);
}
{
  // Packages per box: tonight's boxes blended with the station's usual, as if the usual were 15 boxes made.
  assert.equal(Math.round(packagesPerBox({ boxes: 14, packages: 2000, ppbUsual: 356 })), 253);
  assert.equal(Math.round(packagesPerBox({ boxes: 0, packages: 0 })), 360, "no history: 360");
  assert.equal(Math.round(packagesPerBox({ boxes: 60, packages: 18000, ppbUsual: 360 })), 312, "a full night counts most");
  // BRO01 at 9 AM: its 15,000 limit is about 42 boxes at its usual 356, right on the line between 1×53 + 1×26
  // (39 + 4 that can wait) and 2×53. A few slightly light early boxes tip it; the usual keeps it on the right side.
  // Here 14 boxes at 343 each (own boxes alone: 15,000 ≈ 44 boxes → 2×53); blended with 356: 1×53 + 1×26.
  const early = { day: "2026-11-11", depart: local("2026-11-11 23:00"), boxes: 14, packages: 4800, ppbUsual: 356, cap: 15000,
    forecast: { point: 39.5, low: 35, high: 51.5, plan: 46.7 }, booked: { trucks: [], room: 0 } };
  assert.deepEqual(assess("BRO01", early).need, [{ type: "53FT", n: 1 }, { type: "26FT", n: 1 }], "1×53 + 1×26");
  assert.deepEqual(trucksFor("BRO01", Math.ceil(15000 / 343) - 4), [{ type: "53FT", n: 2 }], "own boxes alone: 2×53");
  // Usual packages per box: median of the last 7 nights with 5+ boxes.
  const nights = Object.fromEntries(["01", "02", "03", "04"].map((d, i) => [`2026-11-${d}`, { f: 40, pk: [12000, 14000, 16000, 100][i] }]));
  nights["2026-11-04"].f = 2; // too few boxes to count
  assert.deepEqual(usualPackagesPerBox({ BRO01: nights, ACT01: { "2026-11-01": { f: 9, pk: 3000 } } }), { BRO01: 350 });
}

{
  // Which limit a station-night plans with.
  const rows = [["AUS01", "20:30", 0, 0, 28000, 0], ["GGG01", "0:00", 0, 0, "N/A", 0], ["ACT01", "20:30", 0, 0, 7000, 0]];
  const ai = { day: "2026-11-10", at: local("2026-11-10 11:57"), stations: {
    AUS01: { kind: "cut", use_now: 22, plan_tomorrow: 28, note: "truck 320 late, no ETA, limit volume", why: "one-night cut for a late truck" },
    GGG01: { kind: "no_number", use_now: 5.5, plan_tomorrow: 5.5, note: "update this afternoon", why: "usual 5.5k until updated" },
    ACT01: { kind: "normal", use_now: 7, plan_tomorrow: null, note: "", why: "Sunday: no maximum" } } };
  const report = { day: "2026-11-10", rows: [["AUS01", "20:30", 0, 0, 22000, 0], ...rows.slice(1)], ai };
  // That day: the sheet's number as on the report; a station with no number gets the reader's likely one.
  assert.deepEqual([limitFor("AUS01", "2026-11-10", report).cap, limitFor("AUS01", "2026-11-10", report).from], [22000, "sheet"]);
  assert.equal(limitFor("AUS01", "2026-11-10", report).kind, "cut");
  assert.deepEqual([limitFor("GGG01", "2026-11-10", report).cap, limitFor("GGG01", "2026-11-10", report).from], [5500, "ai"]);
  // The next morning, before the day's sheet: a one-day cut isn't carried over; null = no maximum.
  assert.deepEqual([limitFor("AUS01", "2026-11-11", report).cap, limitFor("AUS01", "2026-11-11", report).from], [28000, "ai"]);
  assert.equal(limitFor("ACT01", "2026-11-11", report).cap, null);
  // No reading of that report's sheet (it failed): the sheet's number, as before.
  assert.equal(limitFor("AUS01", "2026-11-11", { ...report, ai: { ...ai, day: "2026-11-09" } }).cap, 22000);
  assert.equal(limitFor("AUS01", "2026-11-11", {}).cap, null, "nothing known: no limit");
  // Chat: newer than the reading wins, for its night only; older news was already in the reader's pass.
  const news = [
    { station: "ACT01", night: "2026-11-10", limit: 3, why: "warehouse flooded", quote: "ACT01 flooded after the storm, capacity now 3k", chat: "Sorting production", sent: local("2026-11-10 14:00") },
    { station: "AUS01", night: "2026-11-10", limit: 30, why: "older", sent: local("2026-11-10 10:00") },
    { station: "BRO01", night: "2026-11-11", limit: 0, why: "not shipping", sent: local("2026-11-10 22:00") },
  ];
  const st02 = limitFor("ACT01", "2026-11-10", report, news);
  assert.deepEqual([st02.cap, st02.from, st02.quote], [3000, "chat", "ACT01 flooded after the storm, capacity now 3k"]);
  assert.equal(limitFor("ACT01", "2026-11-11", report, news).from, "ai", "the flood news was for 11/10");
  assert.equal(limitFor("AUS01", "2026-11-10", report, news).cap, 22000, "before the reader's pass: already in it");
  assert.equal(limitFor("BRO01", "2026-11-11", report, news).cap, 0, "a station not shipping takes nothing");
  // What the chat reader sends is checked and kept for three nights, one entry per message and station.
  const merged = mergeNews({ items: news }, [
    { key: "m1", station: "ACT01", night: "2026-11-10", limit: 2.5, sent: local("2026-11-10 15:00") },
    { key: "m1", station: "ACT01", night: "2026-11-10", limit: 2, sent: local("2026-11-10 15:00") },
    { station: "st02", night: "2026-11-10", limit: 1 }, { station: "ACT01", night: "11/10", limit: 1 },
    { station: "ACT01", night: "2026-11-10", limit: 5000 }, { station: "ACT01", night: "2026-11-05", limit: 1 },
  ], "2026-11-10");
  assert.deepEqual(merged.items.map((n) => [n.station, n.limit]), [["AUS01", 30], ["ACT01", 3], ["ACT01", 2], ["BRO01", 0]]);
}

{
  // Fewer trucks: BRO01 booked 2×53ft (56 boxes) but its 15,000 limit takes about 42, so 1×53 + 1×26 does.
  const st04 = { day: "2026-11-11", depart: local("2026-11-11 23:00"), boxes: 30, packages: 10680, ppbUsual: 356, cap: 15000,
    limit: { cap: 15000, from: "sheet", why: "" }, forecast: { point: 45, low: 40, high: 55, plan: 50 },
    booked: { trucks: [{ type: "53FT" }, { type: "53FT" }], room: 56 } };
  const out = trimNotes({ BRO01: st04 }, {}, local("2026-11-11 12:00"));
  assert.equal(out.lines.length, 1);
  assert.match(out.lines[0], /^BRO01 11:00 PM: limit 15k ≈ 42 boxes \(sheet\) · expect 42 · booked 2× 53ft = 56 · enough: 1× 53ft \+ 1× 26ft$/);
  assert.equal(trimNotes({ BRO01: st04 }, out.said, local("2026-11-11 12:15")).lines.length, 0, "once per limit");
  const chatLimit = { ...st04, cap: 3000, limit: { cap: 3000, from: "chat", chat: "Sorting production", why: "not shipping much" } };
  assert.match(trimNotes({ BRO01: chatLimit }, out.said, local("2026-11-11 13:00")).lines[0], /limit 3k ≈ 8 boxes \(chat \(Sorting production\): not shipping much\)/);
  // Not when the limit isn't what makes it, nor 3 hours before the truck, nor when the booking fits.
  assert.equal(trimNotes({ BRO01: { ...st04, cap: 40000, limit: { cap: 40000, from: "sheet" } } }, {}, local("2026-11-11 12:00")).lines.length, 0);
  assert.equal(trimNotes({ BRO01: st04 }, {}, local("2026-11-11 20:30")).lines.length, 0);
  assert.equal(trimNotes({ BRO01: { ...st04, booked: { trucks: [{ type: "53FT" }, { type: "26FT" }], room: 39 } } }, {}, local("2026-11-11 12:00")).lines.length, 0);
}

// Last night's leftover boxes. HRL01's night of 11/09 (window 11/08 22:15 – 11/09 22:15), judged on 11/10.
{
  const night = stationNight("HRL01", local("2026-11-10 10:00"), "2026-11-10", local("2026-11-10 00:00"));
  assert.equal(night.opStart, local("2026-11-10 06:00"));
  let k = 0;
  const b = (made, pcs, { closed, out, chute = 110 + (k % 20), route, pre } = {}) => ({ packageNo: (pre ? "CP138-" : "BG01261109") + String(k++).padStart(5, "0") + "-" + chute,
    destinCenterName: "HRL01", waybillCount: pcs, optCreateTime: local(made), optFinishStatusTime: closed ? local(closed) : undefined,
    optCheckOutTime: out ? local(out) : undefined, exchangeBureauCode: route || "" });
  // Last night's 3×53ft: 26 + 26 + 27 boxes signed out, the last at 11:20 PM (one made two nights back).
  const shipped = [...Array.from({ length: 78 }, (_, i) => b("2026-11-09 12:00", 350, { closed: "2026-11-09 18:00", out: `2026-11-09 ${i < 26 ? "15" : i < 52 ? "22" : "23"}:10` })),
    b("2026-11-08 09:00", 400, { closed: "2026-11-08 15:00", out: "2026-11-09 23:20" })];
  const left = [
    b("2026-11-09 16:00", 400, { closed: "2026-11-09 20:00" }), // closed before the last truck, not signed out
    b("2026-11-09 17:00", 380, { closed: "2026-11-10 00:30" }), // closed after the last truck was loaded
    b("2026-11-09 18:00", 300), // blank close time, not the chute's newest: a finished bag WMS didn't stamp
    b("2026-11-09 21:00", 120, { chute: 499 }), // blank close time and its chute's newest: still filling there
    b("2026-11-09 19:00", 12, { closed: "2026-11-09 21:00" }), // too small to be a box: its packages count
    b("2026-11-09 19:30", 0, { closed: "2026-11-09 21:00" }), // empty
    b("2026-11-08 18:00", 410, { closed: "2026-11-08 21:00" }), // made two nights back: not counted
    b("2026-11-09 03:00", 90, { closed: "2026-11-09 05:00", pre: true }), // customer pre-sort bag: not counted
    b("2026-11-09 14:00", 300, { closed: "2026-11-09 19:00", route: "HRL01-107" }), // held route: neither
    b("2026-11-09 15:00", 330, { closed: "2026-11-09 19:00", out: "2026-11-10 17:00" }), // went on tonight's truck
    b("2026-11-10 08:00", 200, { closed: "2026-11-10 09:00" }), // tonight's own box: counted already
  ];
  // Every other chute's newest bag is today's, so their blank close times are just missing stamps.
  const today = Array.from({ length: 20 }, (_, i) => b("2026-11-10 09:30", 40, { chute: 110 + i }));
  const all = [...shipped, ...left, ...today];
  const full = leftovers("HRL01", night, all, { room: 84, trucks: 3, cap: 25000 }, {}, new Set(["HRL01-107"]));
  assert.equal(full.full, true, "79 signed out on 84 of room, 3 trucks: full");
  assert.equal(full.boxes, 5, "before and after the last truck, the unstamped one, the one still filling, the one on tonight's truck");
  assert.equal(full.packages, 400 + 380 + 300 + 120 + 12 + 330);
  assert.equal(full.unsure, 2, "two nights back and the pre-sort bag: shown, not counted");
  assert.equal(full.onTonight, 1);
  // Trucks with room and the limit not reached: only what can't have gone counts.
  const roomy = leftovers("HRL01", night, all, { room: 108, trucks: 4, cap: 40000 }, {}, new Set(["HRL01-107"]));
  assert.equal(roomy.full, false);
  assert.equal(roomy.capHit, false);
  assert.equal(roomy.boxes, 3, "closed after the last truck, still filling, on tonight's truck");
  assert.equal(roomy.unsure, 4, "closed before the last truck and the unstamped one join the two older ones");
  // The limit reached counts them all (a station far over its limit kept boxes with half a truck empty).
  assert.equal(leftovers("HRL01", night, all, { room: 108, trucks: 4, cap: 25000 }, {}, new Set(["HRL01-107"])).boxes, 5);
  // No truck booked and none signed out: everything made for last night is here.
  const none = leftovers("HRL01", night, [...left, ...today], { room: 0, trucks: 0, cap: null }, {}, new Set());
  assert.equal(none.boxes, 6, "the held route counts when nothing says it's held");
  // A truck that left with no booking in the log: room unknown, nothing assumed.
  assert.equal(leftovers("HRL01", night, all, { room: 0, trucks: 0, cap: null }, {}, new Set(["HRL01-107"])).boxes, 3);
  // Last night's shipping is kept at its highest: a station unpacking its bags empties their counts in WMS.
  const emptied = all.map((x) => (x.optCheckOutTime && x.optCheckOutTime < night.opStart ? { ...x, waybillCount: 0 } : x));
  const kept = leftovers("HRL01", night, emptied, { room: 84, trucks: 3, cap: 25000 }, full.judge, new Set(["HRL01-107"]));
  assert.equal(kept.judge.shippedPkgs, full.judge.shippedPkgs);
  assert.equal(kept.boxes, 5);
  // Counted in: the boxes and the whole range move by the leftovers; nothing changes without any.
  const s = { boxes: 10, packages: 3000, forecast: { point: 50, low: 45, high: 61, plan: 55 }, left: { boxes: 5, packages: 1542 } };
  assert.deepEqual(withLeft(s).forecast, { point: 55, low: 50, high: 66, plan: 60 });
  assert.equal(withLeft(s).boxes, 15);
  assert.equal(withLeft(s).packages, 4542);
  const z = { ...s, left: { boxes: 0, packages: 0 } };
  assert.equal(withLeft(z), z, "no leftovers: the station as it is");
}

console.log("truckspace tests passed");

// The current truck log (Shift date) wins its nights; the old log fills nights before it.
{
  const { mergeTruckLogs } = await import("../src/truckspace.js");
  const at = (d) => Date.parse(`${d}T06:00:00Z`); // midnight at the hub
  const fresh = truckRows([{ fields: { "Shift date": at("2026-11-16"), Destination: ["AUS01"], "Truck type": "53FT", Task: [{ text: "TT2611160311", type: "text" }] } },
    { fields: { "Shift date": at("2026-11-15"), Destination: ["AUS01"], "Truck type": "26FT" } }]);
  const old = truckRows([{ fields: { "Departure date": at("2026-11-15"), Destination: ["AUS01"], "Truck type": "53FT" } },
    { fields: { "Departure date": at("2026-11-14"), Destination: ["AUS01"], "Truck type": "53FT" } }]);
  const all = mergeTruckLogs(fresh, old);
  assert.equal(all.length, 3);
  assert.equal(all.filter((t) => t.day === at("2026-11-15")).map((t) => t.type).join(), "26FT");
  assert.equal(fresh[0].station, "AUS01");
}
console.log("truck log merge passed");

// Box counts from the WMS when dispatch leaves Boxes blank (the current log).
{
  const { truckRows: rowsOf, withLoaded } = await import("../src/truckspace.js");
  const rows = rowsOf([
    { fields: { "Shift date": 1794812400000, Destination: ["AUS01"], "Truck type": "53FT", Task: [{ text: "TT2611170320", type: "text" }] } },
    { fields: { "Shift date": 1794812400000, Destination: ["AUS01"], "Truck type": "53FT", Boxes: 26, Task: [{ text: "TT2611170321", type: "text" }] } },
    { fields: { "Shift date": 1794812400000, Destination: ["HRL01"], "Truck type": "53FT" } },
  ]);
  assert.equal(rows[0].task, "TT2611170320");
  assert.equal(rows[2].task, null);
  const got = withLoaded(rows, { TT2611170320: 15, TT2611170321: 99 });
  assert.equal(got[0].boxes, 15); // blank Boxes takes the WMS count
  assert.equal(got[1].boxes, 26); // dispatch's own count stays
  assert.equal(got[2].boxes, null);
  assert.equal(withLoaded(rows, undefined)[0].boxes, null);
  assert.equal(got[0].gone, true);
  assert.equal(got[2].gone, undefined, "no MT number: not known to have left");
  // A gone truck holds what it took (26 + 28 full, then a 26ft with 11 and a 53ft with 8).
  const day = Date.parse("2026-11-19T12:00:00-06:00");
  const st05 = [
    { station: "HRL01", day, type: "53FT", task: "TT1", boxes: null },
    { station: "HRL01", day, type: "53FT", task: "TT2", boxes: null },
    { station: "HRL01", day, type: "26FT", task: "TT3", boxes: null },
    { station: "HRL01", day, type: "53FT", task: "TT4", boxes: null },
  ];
  assert.deepEqual(bookedFor(st05, "HRL01", "2026-11-19"), { trucks: st05, room: 95 }, "none gone: full room");
  const late = withLoaded(st05, { TT1: 26, TT2: 28, TT3: 11 });
  const b = bookedFor(late, "HRL01", "2026-11-19");
  assert.equal(b.room, 26 + 28 + 11 + 28, "last 53ft still to go: full size");
  assert.equal(b.fullRoom, 95);
  assert.deepEqual(b.gone, { trucks: 3, boxes: 65 });
  assert.equal(bookedFor(withLoaded(st05, { TT1: 26, TT2: 28, TT3: 11, TT4: 8 }), "HRL01", "2026-11-19").room, 73);
}
console.log("WMS loaded counts passed");
