import assert from "node:assert/strict";
import vm from "node:vm";
import { handleTrucksBoard, publicPage, trucksPage } from "../src/trucksboard.js";

const local = (text) => Date.parse(text.replace(" ", "T") + "-06:00");
const KEY = "k".repeat(24);
const board = {
  at: local("2026-11-10 15:30"), trucksAt: local("2026-11-10 15:15"), spare: 4, bulk: { state: "in", at: 18 * 60 + 19, size: 6716 }, calendar: 1.3,
  rows: [
    { name: "AUS01", cutoff: "20:30", depart: local("2026-11-10 22:00"), askBy: local("2026-11-10 19:00"), boxes: 38, packages: 13300,
      point: 53, low: 45, high: 72, plan: 61, status: "short", booked: [["53FT", 2]], room: 54, need: [["53FT", 2], ["26FT", 1]], am: { point: 50, need: [["53FT", 2]] },
      full53: 27, cap: 22000, expected: 18550, overCap: false, overCapNow: false, hold: 0, takes: 62.9,
      limit: { from: "sheet", kind: "cut", note: "truck 320 late, no ETA, limit volume", why: "one-night cut for a late truck" } },
    { name: "BRO01", cutoff: "21:30", depart: local("2026-11-10 23:00"), askBy: local("2026-11-10 20:00"), boxes: 29, packages: 10400,
      point: 50, low: 46, high: 56, plan: 53, status: "short", booked: [["53FT", 1]], room: 28, need: [["53FT", 1], ["26FT", 1]],
      full53: 28, cap: 15000, expected: 18000, overCap: true, overCapNow: false, hold: 8.3, takes: 41.7,
      limit: { from: "chat", why: "warehouse flooded", quote: "BRO01 warehouse flooded, capacity now 15k", chat: "Sorting production", sent: local("2026-11-10 14:00") } },
    { name: "GGG01", cutoff: "0:00", depart: local("2026-11-11 02:00"), askBy: local("2026-11-10 23:00"), boxes: 4, packages: 1500,
      point: null, low: null, high: null, plan: null, status: "unknown", booked: [["26FT", 1]], room: 11, need: [], full53: 27,
      cap: null, expected: null, overCap: false, overCapNow: false, hold: null, takes: null },
  ],
};
board.rows.push({ ...board.rows[0], name: "HRL01", status: "maybe", askBy: local("2026-11-10 20:30"), overCap: false, needHigh: [["53FT", 3]] });
board.rows[0].needHigh = [["53FT", 2], ["26FT", 1]]; // the same trucks as its "to order": nothing more to say
const store = new Map([["truckspace", JSON.stringify({ board })]]);
const env = { TRUCKS_VIEW_KEY: KEY, PLAN: { get: async (k, type) => (store.has(k) ? JSON.parse(store.get(k)) : null) } };
const get = (path) => handleTrucksBoard(new Request("https://x.test" + path), env);

// Only the right key opens it; other paths aren't this page's.
assert.equal(await get("/pda/abc"), null);
assert.equal((await get("/trucks/wrong")).status, 404);
assert.equal((await handleTrucksBoard(new Request("https://x.test/trucks/" + KEY), { ...env, TRUCKS_VIEW_KEY: "short" })).status, 404);
const res = await get("/trucks/" + KEY);
assert.equal(res.status, 200);
const html = await res.text();
assert.match(html, /"name":"AUS01"/);

// Run the page's script with a small stand-in for the browser.
function render(language) {
  const els = {};
  const el = () => ({ textContent: "", innerHTML: "" });
  const sandbox = {
    document: { documentElement: {}, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} },
    localStorage: { getItem: () => language, setItem: () => {} }, navigator: { language: "en-US" }, Date, Math, String, JSON,
  };
  vm.runInNewContext(/<script>([\s\S]*)<\/script>/.exec(html)[1], sandbox);
  return els;
}
const realNow = Date.now;
Date.now = () => local("2026-11-10 15:30");
const en = render("en");
// Where a limit came from: the sheet's note on a cut, or the chat message that changed it (without the chat's name).
assert.match(en.main.innerHTML, /<small>cut: one-night cut for a late truck<\/small>/);
assert.match(en.main.innerHTML, /<small>Update 2:00 PM: warehouse flooded<\/small>/);
assert.doesNotMatch(html, /Sorting production/, "the chat's name never reaches the page");
assert.equal(en.title.textContent, "Station trucks · HOU.H");
assert.match(en.sub.textContent, /^Updated 3:30 PM · dispatch's truck list read 3:15 PM/);
const tr = (html, name) => html.split("<tbody>")[1].split("<tr>").find((x) => x.includes(">" + name + "<"));
// The top says what to do, in plain words.
assert.match(en.main.innerHTML, /Add trucks:<\/b> <b>AUS01<\/b> 1×26ft by 7:00 PM · <b>BRO01<\/b> 1×53ft by 8:00 PM/);
assert.match(en.main.innerHTML, /Maybe add:<\/b> <b>HRL01<\/b> 1×26ft, decide by 8:30 PM/);
assert.match(en.main.innerHTML, /Bulk truck: in at 6:19 PM, 6,716 pkg/);
assert.match(en.main.innerHTML, /Peak day: boxes still to come ×1\.3/);
// Each row: status, what to do, expected with its likely range, trucks booked, how full, boxes so far, limit.
const st01 = tr(en.main.innerHTML, "AUS01");
assert.match(st01, /leaves 10:00 PM · cutoff 20:30/);
assert.match(st01, /tag short">Need truck</);
assert.match(st01, /Add 1×26ft by 7:00 PM/);
assert.match(st01, /<span class="big">53<\/span><small>likely 45–72<\/small>/);
assert.match(st01, /2×53ft<small>holds 54 boxes<\/small>/);
assert.match(st01, /<b>98%<\/b><small class="free">1 box free<\/small>/);
// Trucks to order for the expected boxes, with the 9 AM call under it.
assert.match(st01, /<span class="big">2×53ft \+ 1×26ft<\/span><small>9 AM: 2×53ft<\/small>/);
assert.match(st01, /<span class="big">38<\/span><small>13,300 pkg<\/small>/);
assert.match(st01, /84% of 22,000 pkg/);
const st04 = tr(en.main.innerHTML, "BRO01");
assert.match(st04, /<b>179%<\/b><small class="over">22 boxes over<\/small>/);
assert.match(st04, /~9 boxes over limit, extra truck won't help/);
assert.match(tr(en.main.innerHTML, "GGG01"), /tag unknown">No forecast</);
assert.match(tr(en.main.innerHTML, "GGG01"), /no limit/);
assert.match(tr(en.main.innerHTML, "HRL01"), /tag risk">May need truck</);
// Under the trucks to order: the trucks the top of the range would take, in full (not "+ one more" on top of
// the line above), and only when that's different.
assert.match(tr(en.main.innerHTML, "HRL01"), /<span class="big">2×53ft \+ 1×26ft<\/span><small>3×53ft if it runs high<\/small>/);
assert.doesNotMatch(st01, /if it runs high/);
// The truck to add is judged like the estimate, with 4 boxes allowed to wait. "May need": the truck the
// likely-high night would take (61 − 4 on 54 of room: 3 more, a 26ft), not the very top of the range (72).
assert.match(tr(en.main.innerHTML, "HRL01"), /Maybe 1×26ft, decide by 8:30 PM/);
// "Need truck": the truck that takes the expected boxes (BRO01: 50 − 4 on 28 of room is 18, a 53ft).
assert.match(st04, /Add 1×53ft by 8:00 PM/);
assert.doesNotMatch(en.main.innerHTML, /\b(fine|squeeze|worth|I'll|Risk|Margin|Tight)\b/, "plain words only");
// A truck that leaves after midnight, and its act-by time on another day, carry the weekday.
assert.match(tr(en.main.innerHTML, "GGG01"), /leaves Wed 2:00 AM · cutoff 0:00/);
assert.doesNotMatch(en.main.innerHTML, /class="stale"/, "fresh numbers: no warning");
// After a truck leaves, its row is tomorrow night's: the day shows on the time it leaves and the act-by time.
Date.now = () => local("2026-11-09 22:40");
const early = render("en");
assert.match(tr(early.main.innerHTML, "AUS01"), /leaves Tue 10:00 PM/);
assert.match(tr(early.main.innerHTML, "AUS01"), /Add 1×26ft by Tue 7:00 PM/);
// Numbers more than 45 minutes old (the checks stopped): a warning on top, with when they're from.
Date.now = () => local("2026-11-10 16:20");
assert.match(render("en").main.innerHTML, /class="stale">Not updating: these numbers are from 3:30 PM\. Don't plan from them\.</);
Date.now = () => local("2026-11-11 08:00");
assert.match(render("en").main.innerHTML, /these numbers are from Tue 3:30 PM/);
assert.match(render("en").sub.textContent, /^Updated Tue 3:30 PM/);
// A held route that may be over (not repeated for two nights): a red mark by the station, the night both
// ways in the row, and the question under the table. A fresh hold is only a note there.
{
  const st07 = { ...board.rows[2], point: 12.5, low: 9.2, high: 19.7, plan: 16.6, status: "ok", need: [["26FT", 1]], boxes: 4, packages: 730,
    held: { routes: ["GGG01-003"], since: "2026-11-08", ask: true, boxes: 1, packages: 300, point: 16.4, low: 12.3, high: 22.8, plan: 19.7, status: "short", need: [["53FT", 1]] } };
  const fresh = { ...board.rows[1], held: { routes: ["BRO01-002", "BRO01-007"], since: "2026-11-10", ask: false, boxes: 0, packages: 0, point: 50, low: 46, high: 56, plan: 53, status: "short", need: [["53FT", 1], ["26FT", 1]] } };
  const page = (language) => {
    const els = {}; const el = () => ({ textContent: "", innerHTML: "" });
    vm.runInNewContext(/<script>([\s\S]*)<\/script>/.exec(trucksPage({ ...board, rows: [board.rows[0], fresh, st07] }))[1], {
      document: { documentElement: {}, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} },
      localStorage: { getItem: () => language, setItem: () => {} }, navigator: { language: "en-US" }, Date, Math, String, JSON });
    return els.main.innerHTML;
  };
  Date.now = () => local("2026-11-10 15:30");
  const e = page("en"), row3 = tr(e, "GGG01");
  assert.match(row3, />GGG01<span class="alert" title="Open question, see below the table">!<\/span>/);
  assert.match(row3, /<span class="big">13<\/span><small>likely 9–20<\/small><small class="alt q">With route 003: 16<\/small>/);
  assert.match(row3, /<small class="alt q">With route 003: Add 1×26ft by 11:00 PM<\/small>/);
  assert.match(row3, /<small>\+1 held \(003\)<\/small>/);
  assert.match(e, /<div class="q"><b>Question · GGG01: is route 003 still held\?<\/b> Marked held since 11\/8, not confirmed since\. Still held: expect 13 boxes\. Back on the truck: expect 16\.<\/div>/);
  assert.doesNotMatch(tr(e, "BRO01"), /class="alert"/, "a fresh hold: no red mark");
  assert.match(e, /<div>BRO01: route 002, 007 held since 11\/10, not counted\.<\/div>/);
  assert.doesNotMatch(tr(e, "AUS01"), /With route|held/);
}
// The city trucks: their own table under the stations', the same columns with the pickup forecast in place
// of the station limit, and their truck calls in the strip on top.
{
  const north = { name: "PHX", group: "PHX", day: "2026-11-10", cutoff: "", depart: local("2026-11-11 04:00"), askBy: local("2026-11-11 01:00"), boxes: 40, packages: 11000,
    point: 112, low: 98, high: 131, plan: 121, status: "short", booked: [["53FT", 3]], room: 84, need: [["53FT", 4], ["26FT", 1]], full53: 28, made: false, pickup: 163925,
    cap: null, expected: 31000, overCap: false, overCapNow: false, hold: 0, takes: null, am: null, limit: null, held: null };
  const east = { ...north, name: "MSP", group: "MSP", depart: local("2026-11-10 10:00"), askBy: local("2026-11-10 07:00"), boxes: 46, point: 46, low: 46, high: 46, plan: 46,
    status: "ok", booked: [["53FT", 2]], room: 56, need: [["53FT", 2]], made: true, pickup: null };
  const page = (language, city, saved = {}) => {
    const els = {}; const el = () => ({ textContent: "", innerHTML: "" });
    vm.runInNewContext(/<script>([\s\S]*)<\/script>/.exec(trucksPage(board, city))[1], {
      document: { documentElement: {}, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} },
      localStorage: { getItem: (k) => (k === "trucks-lang" ? language : saved[k] ?? null), setItem: () => {} }, navigator: { language: "en-US" }, Date, Math, String, JSON });
    return els.main.innerHTML;
  };
  Date.now = () => local("2026-11-10 15:30");
  const cityBoard = { at: local("2026-11-10 15:30"), lean: -1, rows: [north, east] };
  // With the scale at normal for cities, the check's own rule: 112 expected, 4 may wait, 84 booked: 24 more, a 53ft.
  const e = page("en", cityBoard, { "trucks-scale-cities": "0" });
  assert.match(e, /<h2>City trucks<\/h2>/);
  assert.match(e, /Cities, add trucks:<\/b> <b>PHX<\/b> 1×53ft by Wed 1:00 AM/);
  assert.match(e, /City trucks: <b>Normal<\/b> · trucks for a likely-high night; up to 4 boxes wait/);
  // ---- The scale. Cities start one step toward "short on purpose": trucks for the expected night (112), 8 may
  // wait: 4×53ft to order. A truck is called for because even a low night (98) is 9+ past the 3 booked (84),
  // and the truck to add is the one that takes that: 98 − 8 − 84 = 6, a 26ft.
  const lean = page("en", cityBoard);
  assert.match(lean, /value="-1" data-scale="cities"/);
  assert.match(lean, /City trucks: <b>A little lean<\/b> · trucks for the expected night; up to 8 boxes wait/);
  assert.match(lean, /Cities, add trucks:<\/b> <b>PHX<\/b> 1×26ft by Wed 1:00 AM/);
  const leanNorth = lean.split("<h2>City trucks</h2>")[1].split("<tr>").find((x) => x.includes(">PHX<"));
  assert.match(leanNorth, /<span class="big">4×53ft<\/span><small>4×53ft \+ 1×26ft if it runs high<\/small>/);
  // Stations start at normal, whatever the cities' setting.
  assert.match(lean, /value="0" data-scale="stations"/);
  assert.match(lean, /Add trucks:<\/b> <b>AUS01<\/b> 1×26ft by 7:00 PM · <b>BRO01<\/b> 1×53ft by 8:00 PM/);
  // Short on purpose: trucks for a low night (98), 12 may wait: the 3 booked 53fts (84) do, so no truck call.
  const shortest = page("en", cityBoard, { "trucks-scale-cities": "-2" });
  assert.doesNotMatch(shortest, /Cities, add trucks/);
  assert.match(shortest.split("<h2>City trucks</h2>")[1].split("<tr>").find((x) => x.includes(">PHX<")), /tag risk">May need truck<.*<span class="big">3×53ft \+ 1×26ft<\/span>/);
  // Extra safe: trucks for past the top of the range (131 + 10), nothing waits; the truck to add takes the top of
  // the range itself (131 − 84 booked = 47 more).
  const safest = page("en", cityBoard, { "trucks-scale-cities": "2" });
  assert.match(safest.split("<h2>City trucks</h2>")[1].split("<tr>").find((x) => x.includes(">PHX<")), /<span class="big">5×53ft \+ 1×26ft<\/span>/);
  assert.match(safest, /Cities, add trucks:<\/b> <b>PHX<\/b> 2×53ft by Wed 1:00 AM/);
  // A station moved to "a little lean": AUS01's 53 expected with 8 allowed to wait fits its 54 booked.
  const stationLean = page("en", cityBoard, { "trucks-scale-stations": "-1" });
  assert.match(stationLean, /Station trucks: <b>A little lean<\/b>/);
  assert.doesNotMatch(stationLean, /<b>AUS01<\/b> 1×26ft by 7:00 PM/);
  // A saved setting that isn't one of the five is ignored.
  assert.match(page("en", cityBoard, { "trucks-scale-cities": "9" }), /value="-1" data-scale="cities"/);
  const cityTable = e.split("<h2>City trucks</h2>")[1];
  const d = cityTable.split("<tr>").find((x) => x.includes(">PHX<"));
  assert.match(d, /last truck by Wed 4:00 AM/);
  assert.match(d, /tag short">Need truck</);
  assert.match(d, /<span class="big">112<\/span><small>likely 98–131<\/small>/);
  assert.match(d, /3×53ft<small>holds 84 boxes/);
  assert.match(d, /<td>163,925 pkg<\/td><\/tr>/);
  assert.doesNotMatch(d, /no limit|cutoff/);
  const w = cityTable.split("<tr>").find((x) => x.includes(">MSP<"));
  assert.match(w, /<span class="big">46<\/span><small>all made<\/small>/);
  assert.match(w, /<td>not posted yet<\/td>/);
  assert.match(e, /City trucks: 53ft ≈ 28 boxes/);
  // No city numbers yet: the page is the stations' alone, as before.
  assert.doesNotMatch(page("en", null), /City trucks/);
}
// Chat news and the chat-reader warning never show on this shared page.
{
  const news = { items: [{ kind: "hold", did: "holding GGG01-003 (not counted for the truck)", summary: "Station says GGG01 route 003 must not be sent.", quote: "route 003 still being sent, hold it",
    chat: "Station Dispatch", sender: "Station Seven Lead", sent: local("2026-11-10 14:18"), attention: false },
    { kind: "truck", did: "", summary: "Dispatcher says AUS01's second truck broke down.", quote: "AUS01 second truck broke down", chat: "Sorting team", sender: "Night Dispatcher", sent: local("2026-11-10 13:00"), attention: true }],
    stale: { newest: local("2026-11-10 05:45") } };
  const page = (language) => {
    const els = {}; const el = () => ({ textContent: "", innerHTML: "" });
    vm.runInNewContext(/<script>([\s\S]*)<\/script>/.exec(trucksPage(board, null, news))[1], {
      document: { documentElement: {}, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} },
      localStorage: { getItem: () => language, setItem: () => {} }, navigator: { language: "en-US" }, Date, Math, String, JSON });
    return els.main.innerHTML;
  };
  Date.now = () => local("2026-11-10 15:30");
  // The page is shared with dispatch: no chat news, no chat-reader warning, no senders, even when news exists.
  const html = page("en");
  assert.doesNotMatch(html, /Station Seven Lead|Night Dispatcher|Station Dispatch|Applied/);
  // Whole-line code comments are stripped from the served page.
  const served = publicPage(trucksPage(board, null, null));
  assert.doesNotMatch(served, /^\s*[/][/] What the chat reader found/m);
}
// A row with no forecast (SGR01, whose forecast isn't good enough to show): the boxes made so far against
// the trucks booked, and a truck call once they no longer fit.
{
  const st12 = { ...board.rows[2], name: "SGR01", cutoff: "3:00", depart: local("2026-11-11 04:00"), askBy: local("2026-11-11 01:00"), boxes: 120, packages: 33000,
    booked: [["53FT", 5]], room: 145, full53: 29, cap: 65000, expected: null };
  const page = (rows) => {
    const els = {}; const el = () => ({ textContent: "", innerHTML: "" });
    vm.runInNewContext(/<script>([\s\S]*)<\/script>/.exec(trucksPage({ ...board, rows }))[1], {
      document: { documentElement: {}, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} },
      localStorage: { getItem: (k) => (k === "trucks-lang" ? "en" : null), setItem: () => {} }, navigator: { language: "en-US" }, Date, Math, String, JSON });
    return els.main.innerHTML;
  };
  Date.now = () => local("2026-11-10 23:30");
  const fits = tr(page([st12]), "SGR01");
  assert.match(fits, /tag unknown">No forecast</);
  assert.match(fits, /–<small>no forecast: fill is boxes so far<\/small>/);
  assert.match(fits, /<b>83%<\/b><small class="free">25 boxes free<\/small>/);
  assert.match(fits, /5×53ft<small>holds 145 boxes/);
  const over = page([{ ...st12, boxes: 152 }]);
  assert.match(tr(over, "SGR01"), /tag short">Need truck<.*Add 1×26ft by Wed 1:00 AM/, "152 made on 145 of room: 3 more than the 4 that may wait");
  assert.match(over, /Add trucks:<\/b> <b>SGR01<\/b> 1×26ft by Wed 1:00 AM/);
}
Date.now = realNow;

// Before the first check: an empty page that says so.
assert.match(trucksPage(null), /const B = null;/);
console.log("trucksboard tests passed");
