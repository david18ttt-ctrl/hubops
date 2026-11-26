import assert from "node:assert/strict";
import { handleDashboard } from "../src/dashboard.js";

const store = new Map();
const env = {
  PDA_PUSH_KEY: "push", PDA_VIEW_KEY: "view",
  PLAN: { get: async (k, type) => (store.has(k) ? (type === "json" ? JSON.parse(store.get(k)) : store.get(k)) : null),
          put: async (k, v) => store.set(k, v) },
};
const push = (body, key = "push") => handleDashboard(new Request("https://hubops.example/pda-data", {
  method: "POST", headers: { "x-pda-key": key, "content-type": "application/json" }, body: JSON.stringify(body) }), env);
const view = (key) => handleDashboard(new Request(`https://hubops.example/pda/${key}`), env);

// Other paths are left to the other handlers.
assert.equal(await handleDashboard(new Request("https://hubops.example/", { method: "POST", body: "{}" }), env), null);
assert.equal((await push({}, "wrong")).status, 403);
assert.equal((await view("wrong")).status, 404);

const board = { updated: "2026-11-04 02:40", day: "2026-11-03", live: false,
  tabs: { VA: { people: [{ name: "Lupe </script> Alcala", tab: "VA", pdas: ["VA_03"], parcels: 2555, hours: 7 }], unsigned: [], idle: [], notes: [] } } };
assert.equal((await push({ ...board, history: { "2026-11-02": [["A", "VA", "", 10, 1]], "2026-11-03": [["B", "VA", "", 20, 1]] } })).status, 200);
// A later push replaces the days it sends and keeps the older ones.
await push({ ...board, history: { "2026-11-03": [["C", "VA", "", 30, 1]] } });
assert.deepEqual(Object.keys(JSON.parse(store.get("pda_history"))).sort(), ["2026-11-02", "2026-11-03"]);
assert.equal(JSON.parse(store.get("pda_history"))["2026-11-03"][0][0], "C");
assert.equal(JSON.parse(store.get("pda_board")).history, undefined);

const res = await view("view");
assert.equal(res.status, 200);
const html = await res.text();
assert.ok(html.includes("PDA speed board"));
assert.ok(!html.includes("Lupe </script>"), "a name can't close the script tag");
// No sign-in sheets for today yet, but PDAs are scanning: today's board, live, without names.
const { operationWindow } = await import("../src/hubtime.js");
const today = operationWindow(new Date())[0].slice(0, 10);
const hours = (n) => Array.from({ length: 24 }, (_, i) => ({ waybillCount: i < 3 ? n : 0 }));
let wmsRows = [
  { destinCenterName: "HOU.H_VB_09", waybillCount: 600, bagCount: 0, dateList: hours(200) },
  { destinCenterName: "HOU.H_VA_03", waybillCount: 30, bagCount: 0, dateList: hours(10) },   // under 50: not ranked yet
  { destinCenterName: "SORTER_1", waybillCount: 900, bagCount: 0, dateList: hours(300) },   // not a PDA
];
globalThis.fetch = async () => new Response(JSON.stringify({ code: 200, data: wmsRows }));
const withWms = { ...env, WMS_TOKEN: "t" };
const viewLive = async () => {
  const text = await (await handleDashboard(new Request("https://hubops.example/pda/view"), withWms)).text();
  return JSON.parse(text.split("\n").find((l) => l.startsWith("const DATA = ")).slice(13, -1));
};
let shown = (await viewLive()).board;
assert.equal(shown.day, today);
assert.equal(shown.live, true);
assert.equal(shown.noSheets, true);
const ranked = Object.values(shown.tabs).flatMap((t) => t.people);
assert.deepEqual(ranked.map((p) => [p.name, p.unsigned, p.parcels]), [["VB_09", true, 600]]);
// Nothing scanned yet today (early morning): the last shift stays up, not called "today".
wmsRows = [];
shown = (await viewLive()).board;
assert.equal(shown.day, "2026-11-03");
assert.equal(shown.live, false);
// WMS down and no 5-minute copy: same.
globalThis.fetch = async () => { throw new Error("down"); };
shown = (await viewLive()).board;
assert.equal(shown.day, "2026-11-03");
console.log("dashboard.test.mjs passed");

// ---- Error rates tab: quality.py pushes nights with QUALITY_PUSH_KEY; the page gets them.
{
  const vm = await import("node:vm");
  const qenv = { ...env, QUALITY_PUSH_KEY: "q" };
  const pushErrors = (body, key = "q") => handleDashboard(new Request("https://hubops.example/pda-errors", {
    method: "POST", headers: { "x-pda-key": key, "content-type": "application/json" }, body: JSON.stringify(body) }), qenv);
  assert.equal((await pushErrors({ days: {} }, "push")).status, 403, "the speed board's key isn't the error key");
  assert.equal((await handleDashboard(new Request("https://hubops.example/pda-errors", { method: "POST", body: "{}" }), env)).status, 403);
  await pushErrors({ updated: "2026-11-08 08:00", days: {
    "2026-11-04": [["Ana", "VB", "E1", 9000, 3, 0, "high"]],
    "2026-11-05": [["Ana", "VB", "E1", 4000, 1, 1, "high"], ["Bo </script>", "VA", "bo", 2000, 4, 0, "guess"]] } });
  // Sending a night again replaces it and keeps the others.
  await pushErrors({ updated: "2026-11-08 09:00", days: { "2026-11-05": [["Ana", "VB", "E1", 4000, 2, 1, "high"]] } });
  const saved = JSON.parse(store.get("pda_errors"));
  assert.deepEqual(Object.keys(saved.days).sort(), ["2026-11-04", "2026-11-05"]);
  assert.equal(saved.days["2026-11-05"].length, 1);
  assert.equal(saved.updated, "2026-11-08 09:00");
  const text = await (await handleDashboard(new Request("https://hubops.example/pda/view"), qenv)).text();
  const script = text.split("<script>")[1].split("</script>")[0];
  new vm.Script(script); // The page's script still compiles with the error tab in it
  assert.ok(script.includes('"pda_errors"') === false && text.includes('"days":{"2026-11-04"'));
  // Run the page in a tiny fake browser, on the error tab, and check the asterisk and warning.
  const els = {};
  const el = () => ({ innerHTML: "", textContent: "", style: {} });
  const doc = { documentElement: {}, body: { style: {} }, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} };
  const sandbox = { document: doc, location: { hash: "#errors-all", reload() {} }, history: { replaceState() {} },
    localStorage: { getItem: () => null, setItem() {} }, sessionStorage: { getItem: () => null, setItem() {} },
    navigator: { language: "en" }, window: { innerWidth: 400 }, setInterval: () => 0 };
  vm.runInNewContext(script, sandbox);
  const main = els.main.innerHTML;
  assert.ok(main.includes("Missed scans and mis-sorts per 10,000 parcels"));
  assert.ok(main.includes("Ana") && main.includes("13,000 parcels"), main);
  assert.ok(!main.includes('Ana <span class="star">'), "13,000 parcels is enough data");
  assert.equal(els.title.textContent, "PDA error board");
  // Under 10,000 parcels: not ranked. Listed after the ranking under the warning, with an asterisk; a guessed name says so.
  await pushErrors({ days: { "2026-11-06": [["Cy", "VA", "cy", 2500, 2, 0, "guess"]] } });
  const text2 = await (await handleDashboard(new Request("https://hubops.example/pda/view"), qenv)).text();
  const els2 = {};
  const doc2 = { ...doc, getElementById: (id) => (els2[id] ||= el()) };
  vm.runInNewContext(text2.split("<script>")[1].split("</script>")[0], { ...sandbox, document: doc2 });
  assert.ok(els2.main.innerHTML.includes('Cy <span class="star">*</span>'));
  assert.ok(els2.main.innerHTML.includes("Not ranked yet"));
  assert.ok(els2.main.innerHTML.includes("name not confirmed"));
  const m2 = els2.main.innerHTML;
  assert.ok(m2.indexOf("Ana") < m2.indexOf("Not ranked yet") && m2.indexOf("Not ranked yet") < m2.indexOf("Cy"),
    "Cy's rate is higher, but 2,500 parcels is too few to rank: listed after Ana, under the warning");
  assert.ok(m2.includes('<div class="rank">1</div><div class="name">Ana') && m2.includes('<div class="rank">*</div><div class="name">Cy'));
}
console.log("dashboard tests passed");

// ---- Each labor company's own board: /pda/<its key> shows only that company, and the page holds
// nothing about the others (not even in the data behind the page).
{
  const vm = await import("node:vm");
  const { companyFor, onlyCompany } = await import("../src/dashboard.js");
  const keys = { VA: "va-key-0123456789abcdef", VB: "vb-key-0123456789abcdef", VC: "vc-key-0123456789abcdef" };
  const cenv = { ...env, QUALITY_PUSH_KEY: "q", PDA_COMPANY_KEYS: JSON.stringify(keys) };
  assert.equal(companyFor(cenv, keys.VB), "VB");
  assert.equal(companyFor(cenv, "view"), null);
  assert.equal(companyFor({ ...env, PDA_COMPANY_KEYS: '{"VA":"short"}' }, "short"), null, "a too-short key never opens a board");
  assert.equal(companyFor({ ...env, PDA_COMPANY_KEYS: "not json" }, keys.VA), null);
  assert.equal(companyFor({ ...env, PDA_COMPANY_KEYS: JSON.stringify({ OTHER: keys.VA }) }, keys.VA), null, "only vendors A, B and C get one");
  assert.equal((await handleDashboard(new Request("https://hubops.example/pda/" + keys.VA), env)).status, 404, "no keys set: no company boards");

  store.set("pda_board", JSON.stringify({ updated: "2026-11-04 02:40", day: "2026-11-03", live: false,
    signins: [["HOU.H_VA_03", "Nina Vale", "Nina", "", 0, "night"], ["HOU.H_VB_09", "Hank Bell", "Hank", "", 0, "night"]],
    tabs: {
      VA: { people: [{ name: "Nina Vale", tab: "VA", pdas: ["VA_03"], parcels: 2000, hours: 5 }], idle: [], notes: ["Nina note"] },
      VB: { people: [{ name: "Hank Bell", tab: "VB", pdas: ["VB_09"], parcels: 900, hours: 3 }], idle: [{ pda: "VB_02", name: "Hal Idle" }], notes: ["Hank note"] },
      VC: { people: [{ name: "Flo Cole", tab: "VC", pdas: ["VC_01"], parcels: 800, hours: 2 }], idle: [], notes: [] },
      OTHER: { people: [{ name: "PDA 007", tab: "OTHER", pdas: ["PDA 007"], parcels: 100, hours: 1 }], idle: [], notes: [] },
    } }));
  store.set("pda_history", JSON.stringify({ "2026-11-02": [["Nina Vale", "VA", "", 400, 1], ["Hank Bell", "VB", "", 300, 1], ["Flo Cole", "VC", "", 200, 1]] }));
  store.set("pda_errors", JSON.stringify({ updated: "2026-11-08 09:00", days: {
    "2026-11-02": [["Nina Vale", "VA", "m", 4000, 1, 0, "high"], ["Hank Bell", "VB", "h", 3000, 2, 1, "high"]] } }));

  const open = async (key) => {
    const res = await handleDashboard(new Request("https://hubops.example/pda/" + key), cenv);
    assert.equal(res.status, 200);
    return res.text();
  };
  const run = (text, hash = "") => {
    const els = {};
    const el = () => ({ innerHTML: "", textContent: "", style: {} });
    const doc = { documentElement: {}, body: { style: {} }, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} };
    vm.runInNewContext(text.split("<script>")[1].split("</script>")[0], { document: doc, location: { hash, reload() {} },
      history: { replaceState() {} }, localStorage: { getItem: () => null, setItem() {} }, sessionStorage: { getItem: () => null, setItem() {} },
      navigator: { language: "en" }, window: { innerWidth: 400 }, setInterval: () => 0 });
    return els;
  };
  const others = { VA: ["Hank", "Hal Idle", "Flo Cole", "PDA 007"], VB: ["Nina", "Flo Cole", "PDA 007"], VC: ["Nina", "Hank", "Hal Idle", "PDA 007"] };
  const own = { VA: ["Nina Vale", "Nina note"], VB: ["Hank Bell", "Hal Idle", "Hank note"], VC: ["Flo Cole"] };
  for (const company of ["VA", "VB", "VC"]) {
    const text = await open(keys[company]);
    for (const name of others[company]) assert.ok(!text.includes(name), company + "'s page must not hold " + name);
    for (const name of own[company]) assert.ok(text.includes(name), company + "'s page shows " + name);
    // A hash asking for another tab, or for All, still shows only their own company.
    for (const hash of ["", "#all", "#vb", "#va", "#errors-all"]) {
      const els = run(text, hash);
      assert.equal(els.tabs.innerHTML, "", "no tabs on a company's board");
      // Vendor C has no error numbers in this data, so its error tab is empty.
      if (!(company === "VC" && hash.startsWith("#errors"))) assert.ok(els.main.innerHTML.includes(own[company][0]), company + " " + hash);
      for (const name of others[company]) assert.ok(!els.main.innerHTML.includes(name), company + " " + hash + " " + name);
      assert.ok(!els.sub.textContent.includes("All ="), "no All = … line");
    }
    assert.ok(run(text).title.textContent.startsWith({ VA: "Vendor A", VB: "Vendor B", VC: "Vendor C" }[company] + " · "));
    assert.ok(text.includes("<title>" + { VA: "Vendor A", VB: "Vendor B", VC: "Vendor C" }[company] + " · PDA speed board</title>"));
  }
  // The main board still shows everyone, with its tabs.
  const all = await open("view");
  for (const name of ["Nina Vale", "Hank Bell", "Flo Cole"]) assert.ok(all.includes(name));
  assert.ok(run(all).tabs.innerHTML.includes('data-tab="VB"'));
  // Nothing sent for a company with no board yet: an empty board, not an error.
  assert.deepEqual(onlyCompany(null, {}, null, "VC"), { board: null, history: {}, errors: null });
}
console.log("company board tests passed");

// ---- Name pictures: pda.py sends them; a link only gets its own company's; tapping a name shows them.
{
  const vm = await import("node:vm");
  const keys = { VA: "va-key-0123456789abcdef", VB: "vb-key-0123456789abcdef", VC: "vc-key-0123456789abcdef" };
  const nenv = { ...env, PDA_COMPANY_KEYS: JSON.stringify(keys) };
  const post = (body, key = "push") => handleDashboard(new Request("https://hubops.example/pda-names", {
    method: "POST", headers: { "x-pda-key": key, "content-type": "application/json" }, body: JSON.stringify(body) }), nenv);
  assert.equal((await post({}, "wrong")).status, 403);
  const jpeg = Buffer.from([0xff, 0xd8, 0xff, 0xd9]).toString("base64");
  assert.equal((await post({
    images: { "aaaaaaaaaaaaaaaa": { tab: "VA", jpeg }, "bbbbbbbbbbbbbbbb": { tab: "VB", jpeg }, "not-an-id": { tab: "VA", jpeg } },
    index: { "E1|VA": [["aaaaaaaaaaaaaaaa", "2026-11-09", "Ana"], ["cccccccccccccccc", "2026-11-08", "Ana"]],
             "luis|VB": [["bbbbbbbbbbbbbbbb", "2026-11-09", "Luis"]] } })).status, 200);
  const get = (key, id) => handleDashboard(new Request(`https://hubops.example/pda/${key}/name/${id}`), nenv);
  const ok = await get("view", "aaaaaaaaaaaaaaaa");
  assert.equal(ok.status, 200);
  assert.equal(ok.headers.get("content-type"), "image/jpeg");
  assert.equal((await ok.arrayBuffer()).byteLength, 4);
  assert.equal((await get(keys.VA, "aaaaaaaaaaaaaaaa")).status, 200);
  assert.equal((await get(keys.VB, "aaaaaaaaaaaaaaaa")).status, 404, "Vendor B's link can't load VA's picture");
  assert.equal((await get("wrong-key-0123456789", "aaaaaaaaaaaaaaaa")).status, 404);
  const { namePictures } = await import("../src/dashboard.js");
  assert.deepEqual(await namePictures(nenv, null), { "E1|VA": [["aaaaaaaaaaaaaaaa", "2026-11-09", "Ana"]], "luis|VB": [["bbbbbbbbbbbbbbbb", "2026-11-09", "Luis"]] },
    "a picture never sent (cccc…) is left out");
  assert.deepEqual(Object.keys(await namePictures(nenv, "VA")), ["E1|VA"]);

  // Tapping Ana: her speed chart, her error rate for the period, and her name pictures.
  store.set("pda_board", JSON.stringify({ updated: "2026-11-09 02:40", day: "2026-11-09", live: false,
    tabs: { VA: { people: [{ name: "Ana", tab: "VA", id: "E1", pdas: ["VA_05"], parcels: 2000, hours: 5, hourly: [0, 0, 0, 0, 0, 0, 0, 0, 0, 400, 400, 400, 400, 400] }], idle: [], notes: [] } } }));
  store.set("pda_history", JSON.stringify({ "2026-11-09": [["Ana", "VA", "E1", 2000, 5]] }));
  store.set("pda_errors", JSON.stringify({ updated: "2026-11-09 09:00", days: { "2026-11-09": [["Ana", "VA", "E1", 20000, 3, 0.5, "high"]] } }));
  const text = await (await handleDashboard(new Request(`https://hubops.example/pda/${keys.VA}`), nenv)).text();
  assert.ok(!text.includes("bbbbbbbbbbbbbbbb"), "VA's page holds no Vendor B picture ids");
  const els = {};
  const el = () => ({ innerHTML: "", textContent: "", style: {} });
  const doc = { documentElement: {}, body: { style: {} }, getElementById: (id) => (els[id] ||= el()), addEventListener: () => {} };
  vm.runInNewContext(text.split("<script>")[1].split("</script>")[0], { document: doc, location: { hash: "#errors-va", pathname: `/pda/${keys.VA}`, reload() {} },
    history: { replaceState() {} }, localStorage: { getItem: () => null, setItem() {} },
    sessionStorage: { getItem: (k) => (k === "pda-open" ? JSON.stringify({ key: "E1|VA", view: "day" }) : null), setItem() {} },
    navigator: { language: "en" }, window: { innerWidth: 400 }, setInterval: () => 0 });
  assert.ok(els.main.innerHTML.includes('data-key="E1|VA"'), "an error row opens the details");
  const sheet = els.detail.innerHTML;
  assert.ok(sheet.includes(`/pda/${keys.VA}/name/aaaaaaaaaaaaaaaa`), sheet);
  assert.ok(sheet.includes("Their name on the sign-out sheets"));
  assert.ok(sheet.includes("Error rate, last 7 days") && sheet.includes("1.8 /10k"), "3.5 errors in 20,000 parcels");
  assert.ok(sheet.includes("3 missed · 0.5 mis-sorted"));
}
console.log("name picture tests passed");
