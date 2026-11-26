import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import worker from "../src/worker.js";
import { demoEnv, demoIndex } from "../src/demo.js";
import { handleLabelStation, cleanCodes, plainText, timeText, hash, agentPyVersion, agentPs1, installBat, scanPage } from "../src/labelstation.js";
import { pressChain, newestOnChute, lookup } from "../src/labelcloud.js";
import { AGENT_PS1, AGENT_PY } from "../src/agent-files.js";
import { embedded } from "../demo/embed-agent.mjs";
import CATALOG from "../src/sticker-catalog.json" with { type: "json" };

// node:sqlite is in Node 22.5+; on older Node the tests that need a database are skipped.
let DatabaseSync = null;
try { ({ DatabaseSync } = await import("node:sqlite")); } catch (e) {}
const D1 = { skip: !DatabaseSync && "needs node:sqlite (Node 22.5+)" };

// A D1 stand-in on node:sqlite: prepare().bind().run()/first()/all() and batch(), with D1's result shapes.
function d1() {
  const db = new DatabaseSync(":memory:");
  const stmt = (sql, args = []) => ({
    bind: (...a) => stmt(sql, a),
    run: async () => {
      if (/RETURNING/i.test(sql)) return { results: db.prepare(sql).all(...args), meta: {} };
      const r = db.prepare(sql).run(...args);
      return { meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } };
    },
    first: async () => db.prepare(sql).get(...args) ?? null,
    all: async () => ({ results: db.prepare(sql).all(...args) }),
  });
  return { db, prepare: (sql) => stmt(sql), batch: async (list) => Promise.all(list.map((s) => s.run())) };
}
function kv() {
  const m = new Map();
  return { get: async (k, t) => (m.has(k) ? (t === "json" ? JSON.parse(m.get(k)) : m.get(k)) : null), put: async (k, v) => m.set(k, v) };
}
const SCAN = "scan-key-1234567890", PRINT = "print-key-123456789", PUSH = "push-key-abc";
const env = (extra = {}) => ({ LABELS: d1(), PLAN: kv(), LABEL_SCAN_KEY: SCAN, LABEL_PRINT_KEY: PRINT, APP_PUSH_KEY: PUSH, ...extra });
const req = (path, { method = "GET", body, key } = {}) => new Request("https://hub.example" + path, {
  method, headers: { "content-type": "application/json", ...(key ? { "x-push-key": key } : {}) }, body: body ? JSON.stringify(body) : undefined });
const call = async (e, path, opts) => {
  const res = await handleLabelStation(req(path, opts), e);
  return { status: res.status, data: res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text() };
};
const PNG = "data:image/png;base64,iVBORw0KGgo=";

test("not the label station's path: the worker's other handlers get it", D1, async () => {
  assert.equal(await handleLabelStation(req("/a/x"), env()), null);
  assert.equal((await call(env(), "/s/wrong-key-1234567890")).status, 404);
  assert.equal((await call(env(), "/p/short")).status, 404);
  assert.equal((await call(env(), "/label-jobs")).status, 403);
});

test("scan -> host lookup -> agent prints -> phone sees Printed", D1, async () => {
  const e = env();
  const page = await call(e, `/s/${SCAN}`);
  assert.match(page.data, /Etiqueta de caja/);
  const r = await call(e, `/s/${SCAN}/req`, { method: "POST", body: { codes: [" pkg261125000000001 ", "BG012611250465-052"] } });
  assert.equal(r.status, 200);
  const id = r.data.id;
  // The same package within 15 minutes is the same request.
  assert.deepEqual((await call(e, `/s/${SCAN}/req`, { method: "POST", body: { codes: ["PKG261125000000001"] } })).data, { id, repeat: true });
  // The ops host takes it.
  const jobs = await call(e, "/label-jobs?wait=0", { key: PUSH });
  assert.deepEqual(jobs.data.map((j) => j.codes), [["PKG261125000000001", "BG012611250465-052"]]);
  let st = await call(e, `/s/${SCAN}/req/${id}`);
  assert.equal(st.data.status, "new");
  assert.equal(st.data.host, true);
  const label = { bag: "BG012611250465-052", route: "VCT01-004", chute: "87", pcs: 338, img: PNG };
  await call(e, `/label-jobs/${id}`, { method: "POST", key: PUSH, body: { status: "done", label } });
  // The agent claims it (once: a second poll gets nothing) and reports back.
  const next = await call(e, `/p/${PRINT}/next?agent=1&wait=0&v=abc&pr=Label%20printer`);
  assert.equal(next.data.queue.length, 1);
  assert.equal(next.data.queue[0].label.raw, true);
  assert.equal(next.data.agentPyVersion, agentPyVersion());
  assert.equal((await call(e, `/p/${PRINT}/next?agent=1&wait=0`)).data.queue.length, 0);
  await call(e, `/p/${PRINT}/printed`, { method: "POST", body: { id, info: "RAW sent" } });
  st = await call(e, `/s/${SCAN}/req/${id}`);
  assert.equal(st.data.status, "done");
  assert.ok(st.data.printedAt);
  assert.equal(st.data.printer, true);
  // With the agent on, a print page left open takes nothing.
  assert.equal((await call(e, `/p/${PRINT}/next?wait=0`)).data.agent, true);
});

test("an error from the host shows on the phone, and errors don't block a new try", D1, async () => {
  const e = env();
  const { data } = await call(e, `/s/${SCAN}/req`, { method: "POST", body: { codes: ["PKG261125000000002"] } });
  await call(e, `/label-jobs/${data.id}`, { method: "POST", key: PUSH, body: { status: "error", msg: "Box still open" } });
  const st = await call(e, `/s/${SCAN}/req/${data.id}`);
  assert.equal(st.data.status, "error");
  assert.equal(st.data.msg, "Box still open");
  const again = await call(e, `/s/${SCAN}/req`, { method: "POST", body: { codes: ["PKG261125000000002"] } });
  assert.notEqual(again.data.id, data.id);
  assert.equal((await call(e, `/s/${SCAN}/req`, { method: "POST", body: { codes: ["no"] } })).status, 400);
});

test("stickers: catalog, plain and time stickers queue once; bad input is refused", D1, async () => {
  const e = env();
  const st = CATALOG.find((x) => x.g === "station");
  const one = await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { id: st.id, img: PNG, copies: 20 } });
  assert.equal(one.status, 400); // over 15
  const ok = await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { id: st.id, img: PNG, copies: 15 } });
  assert.equal(ok.data.ok, true);
  assert.equal(ok.data.name, String(st.chute).padStart(3, "0") + " " + st.name);
  assert.equal((await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { id: st.id, img: PNG, copies: 3 } })).data.already, true);
  assert.equal((await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { text: "phx", city: true, img: PNG, copies: 2 } })).data.name, "plain PHX");
  assert.equal((await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { time: "11/24 19:00", img: PNG, copies: 2 } })).data.name, "time 11/24 19:00");
  assert.equal((await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { time: "11/24 19:30", img: PNG, copies: 2 } })).status, 400);
  assert.equal((await call(e, `/s/${SCAN}/sticker`, { method: "POST", body: { id: st.id, img: "x", copies: 1 } })).status, 400);
  const q = (await call(e, `/p/${PRINT}/next?agent=1&wait=0`)).data.queue;
  assert.deepEqual(q.map((j) => j.label.copies), [15, 2, 2]);
  assert.ok(q.every((j) => j.label.kind === "sticker" && j.label.img === PNG));
});

test("the agents are served with the base filled in, and their embedded copies match agent/", D1, async () => {
  const e = env();
  const py = await call(e, `/p/${PRINT}/agent.py`);
  assert.equal(py.data, AGENT_PY);
  const ps = await call(e, `/p/${PRINT}/agent.ps1`);
  assert.match(ps.data, new RegExp(`\\$Base = 'https://hub.example/p/${PRINT}'`));
  assert.doesNotMatch(ps.data, /__BASE__|__VER__/);
  assert.match(installBat("https://hub.example/p/" + PRINT), /agent\.ps1/);
  assert.equal(readFileSync(new URL("../src/agent-files.js", import.meta.url), "utf8"), embedded(),
    "agent/ changed: run npm run embed-agent");
  assert.match(agentPs1("B"), /\$Version = '[0-9a-z]+'/);
});

test("the agent's version hash is the one the Python agent computes for itself", () => {
  // label_agent.text_hash("hello") (djb2, base 36), checked in tests/test_label_agent.py too.
  assert.equal(hash("hello"), "4bj995");
  assert.equal(hash(""), "45h");
});

test("code cleaning, plain text and time text", () => {
  assert.deepEqual(cleanCodes([" pkg 1234 5678", "PKG12345678", "x", 5]), ["PKG12345678"]);
  assert.equal(plainText(" phx  2 "), "PHX 2");
  assert.equal(plainText("ABCDEFGHIJKLM"), null);
  assert.equal(plainText("VCT01-004"), "VCT01-004");
  assert.equal(timeText("11/24 19:00"), "11/24 19:00");
  assert.equal(timeText("13/24 19:00"), null);
  assert.match(scanPage("/s/x"), /startsWith\("PKG"\)/);
});

// ---- the worker's own lookup (ops host off) ----
const T = Date.UTC(2026, 10, 25, 3, 0); // 9 PM hub time
const bag = (n, chute, pcs, closedMin, extra = {}) => ({ packageNo: `BG01261125${String(n).padStart(4, "0")}-${chute}`, bagType: 2,
  startCenterId: 1, startCenterName: "HOU.H", destinAreaCode: "VCT01-004", destinCenterName: "VCT01", waybillCount: pcs,
  optCreateTime: T + (closedMin - 120) * 60000, optFinishStatusTime: closedMin == null ? null : T + closedMin * 60000, ...extra });

test("press chain: near-empty bags right after a real box are that box", () => {
  const box = bag(1, "087", 400, 0), a = bag(2, "087", 1, 3), b = bag(3, "087", 10, 8), late = bag(4, "087", 2, 30), other = bag(5, "088", 3, 4);
  const all = [box, a, b, late, other];
  assert.deepEqual(pressChain(b, all).map((x) => x.packageNo), [box, a, b].map((x) => x.packageNo));
  assert.deepEqual(pressChain(late, all), [late]);
  assert.deepEqual(pressChain(other, all), [other]);
  const open = bag(6, "087", 5, null, { optCreateTime: T + 40 * 60000 });
  assert.equal(newestOnChute(open, all), true);
  assert.equal(newestOnChute(box, all.concat([open])), false);
});

test("worker lookup against a WMS: near-empty bag -> the real box's label; open bag -> press the button", async () => {
  const box = bag(1, "087", 400, 0), tiny = bag(2, "087", 4, 3), open = bag(7, "090", 120, null);
  const where = { PKG1: tiny.packageNo, PKG2: open.packageNo };
  const realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const body = JSON.parse(init.body);
    const path = new URL(url).pathname;
    let data;
    if (path === "/api/parcels/route") data = { list: body.waybillNoList.filter((c) => where[c]).map((c) => ({ waybillNo: c, packageNo: where[c] })) };
    else if (body.packageNoList.length) data = { list: [box, tiny, open].filter((b) => body.packageNoList.includes(b.packageNo)) };
    else data = { list: [box, tiny, open] };
    return new Response(JSON.stringify({ code: 0, msg: "ok", data }));
  };
  try {
    const e = { WMS_URL: "http://wms.test", WMS_TOKEN: "t" };
    const a = await lookup(e, ["PKG1"], T + 60 * 60000);
    assert.equal(a.label.bag, box.packageNo);
    assert.equal(a.label.pcs, 404);
    assert.deepEqual(a.label.merge, [tiny.packageNo]);
    assert.equal(a.label.chute, "87");
    const b = await lookup(e, ["PKG2"], T + 60 * 60000);
    assert.match(b.why, /Box still open/);
    assert.match((await lookup(e, ["PKG9"])).why, /doesn't know/);
  } finally {
    globalThis.fetch = realFetch;
  }
});

test("worker.js routes the label station, and the demo index links it", D1, async () => {
  const e = { ...env(), DEMO: "false" };
  const res = await worker.fetch(req(`/s/${SCAN}/stickers`), e, {});
  assert.equal(res.status, 200);
  assert.match(await res.text(), /Etiquetas/);
  const index = await demoIndex(demoEnv({})).text();
  assert.match(index, /href="[/]s[/]demo-scan-0+"/);
  assert.match(index, /href="[/]p[/]demo-print-0+"/);
});
