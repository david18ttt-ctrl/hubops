import assert from "node:assert/strict";
import test from "node:test";
import { gzipSync } from "node:zlib";
import worker, { truckCsv, unsortedPage } from "../src/unsorted.js";

function kv() {
  const m = new Map();
  return { m, get: async (k, t) => (m.has(k) ? (t === "json" ? JSON.parse(m.get(k)) : m.get(k)) : null),
           put: async (k, v) => m.set(k, v), delete: async (k) => m.delete(k) };
}
const env = () => ({ PLAN: kv(), UNSORTED_PUSH_KEY: "push-key-123", UNSORTED_VIEW_KEY: "view-key-1234567890" });
const board = { updated: "2026-11-11 05:00:00", day: "2026-11-10", wms: { waiting: 100, older: 5 }, almost: [],
  trucks: [{ task: "TT2611110333", from: "PHX.H", arrived: "2026-11-10 22:11:00", kind: "", total: 10, left: 8,
    gaylords: 2, open: 1, in_full: 8, unloaded: ["2026-11-10 22:19", "2026-11-10 23:07"], by: ["A"],
    bags: [{ bag: "GL26111000028", code: "0028", left: 8, of: 9, full: true, to: ["SGR01"] }] }] };
const lists = "#TT2611110333\ngaylord,parcel,station\nGL1,PKG000000000000001,SGR01\n#TT2611110337\ngaylord,parcel,station\nGL2,PKG000000000000002,CXO01";
const post = (e, path, key, body) => worker.fetch(new Request("https://x" + path, { method: "POST", headers: { "x-push-key": key }, body }), e);

test("pushes need the key, then the page and each truck's CSV show", async () => {
  const e = env();
  assert.equal((await post(e, "/unsorted-data", "wrong", JSON.stringify(board))).status, 403);
  assert.equal((await post(e, "/unsorted-data", "push-key-123", JSON.stringify(board))).status, 200);
  assert.equal((await post(e, "/unsorted-lists", "push-key-123", gzipSync(lists))).status, 200);
  const page = await (await worker.fetch(new Request("https://x/u/view-key-1234567890"), e)).text();
  assert.match(page, /TT2611110333/);
  assert.match(page, /0028/);
  const csv = await worker.fetch(new Request("https://x/u/view-key-1234567890/csv/TT2611110333"), e);
  assert.equal(csv.status, 200);
  const text = await csv.text();
  assert.match(text, /PKG000000000000001/);
  assert.doesNotMatch(text, /PKG000000000000002/);
  assert.equal((await worker.fetch(new Request("https://x/u/view-key-1234567890/csv/TT0000000000"), e)).status, 404);
  assert.equal((await worker.fetch(new Request("https://x/u/wrong-key-1234567890"), e)).status, 404);
});

test("truckCsv finds the last truck too", () => {
  assert.match(truckCsv(lists, "TT2611110337"), /PKG000000000000002,CXO01$/);
  assert.equal(truckCsv(lists, "TT9"), null);
});

test("the page's script compiles", () => {
  for (const b of [null, board, { ...board, almost: [{ task: "TT1", from: "PHX.H", arrived: "2026-11-10 03:35:00", left: 4, total: 9, gaylords: 2 }] }]) {
    const page = unsortedPage(b);
    const js = page.slice(page.lastIndexOf("<script>") + 8, page.lastIndexOf("</script>"));
    assert.doesNotThrow(() => new Function(js));
  }
});

test("page escapes data and handles no data", () => {
  assert.match(unsortedPage(null), /No data yet/);
  const page = unsortedPage({ ...board, trucks: [{ ...board.trucks[0], from: "</script><b>" }] });
  assert.doesNotMatch(page, /<\/script><b>/);
});

// Runs the page's script against a tiny fake page: returns what it puts in <main>.
function renderPage(data, saved) {
  const html = unsortedPage(data);
  const js = html.slice(html.indexOf("<script>") + 8, html.lastIndexOf("</script>"));
  const els = { main: { innerHTML: "", querySelectorAll: () => [] }, sub: { textContent: "" } };
  const fake = { getElementById: (id) => els[id], querySelectorAll: () => [] };
  const store = { getItem: () => saved, setItem: () => {} };
  new Function("document", "localStorage", "location", "setInterval", js)(fake, store, { pathname: "/u/k" }, () => {});
  return els.main.innerHTML;
}

test("trucks are tagged Building 1 or Building 2 and the picked building filters them", () => {
  const two = { ...board, trucks: [{ ...board.trucks[0], where: "Building 2" },
    { ...board.trucks[0], task: "TT2611110444", from: "PIT.H", where: "Building 1?" }],
    almost: [{ task: "TT2611110555", from: "MSP.H", arrived: "2026-11-10 20:00:00", left: 3, total: 9, gaylords: 1, where: "Building 1" }] };
  const all = renderPage(two, null);
  assert.match(all, /bldg b2">Building 2</);
  assert.match(all, /bldg b1 guess">Building 1\?</);
  assert.match(all, /Building 2 1<\/button>/);
  assert.match(all, /Building 1 1<\/button>/);
  const b2 = renderPage(two, "Building 2");
  assert.match(b2, /TT2611110333/);
  assert.doesNotMatch(b2, /TT2611110444|TT2611110555/);
  const b1 = renderPage(two, "Building 1");
  assert.match(b1, /TT2611110444/);
  assert.match(b1, /TT2611110555/);
  assert.doesNotMatch(b1, /TT2611110333/);
  assert.match(renderPage({ ...board, trucks: [] }, "Building 2"), /Every truck is sorted/);
});
