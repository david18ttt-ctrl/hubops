import assert from "node:assert/strict";
import test from "node:test";
import worker, { appPage, links, printPage, serviceWorker } from "../src/app.js";

function kv() {
  const m = new Map();
  return { m, get: async (k, t) => (m.has(k) ? (t === "json" ? JSON.parse(m.get(k)) : m.get(k)) : null),
           put: async (k, v) => m.set(k, v) };
}
const VIEW = "view-key-1234567890";
const env = () => ({ PLAN: kv(), APP_PUSH_KEY: "push-key-123", APP_VIEW_KEY: VIEW, VAPID_PUBLIC: "BPub",
  APP_LINKS: JSON.stringify([{ name: "Trucks", url: "https://x/trucks/k" }, { name: "bad", url: "javascript:1" }]) });
const req = (path, init) => worker.fetch(new Request("https://x" + path, init), init?.env);
const call = (e, path, init = {}) => worker.fetch(new Request("https://x" + path, init), e);

test("data pushes need the key and show up in /data", async () => {
  const e = env();
  const chutes = { updated: "2026-11-12 19:00:00", active: true, live: [{ chute: "309", where: "SJT01-002" }], history: [] };
  assert.equal((await call(e, "/app-data/chutes", { method: "POST", headers: { "x-push-key": "no" }, body: JSON.stringify(chutes) })).status, 403);
  assert.equal((await call(e, "/app-data/chutes", { method: "POST", headers: { "x-push-key": "push-key-123" }, body: JSON.stringify(chutes) })).status, 200);
  const data = await (await call(e, "/a/" + VIEW + "/data")).json();
  assert.equal(data.chutes.live[0].where, "SJT01-002");
  assert.equal(data.feeder, null);
});

test("wrong view key is a 404 everywhere", async () => {
  const e = env();
  for (const p of ["/a/wrong-key-1234567890", "/a/wrong-key-1234567890/data", "/", "/a/" + VIEW + "/nope"]) {
    assert.equal((await call(e, p)).status, 404, p);
  }
});

test("subscriptions are saved once each and the ops host can read and remove them", async () => {
  const e = env();
  const sub = { endpoint: "https://web.push.apple.com/abc", keys: { p256dh: "p", auth: "a" } };
  for (let i = 0; i < 2; i++) {
    assert.equal((await call(e, "/a/" + VIEW + "/subscribe", { method: "POST", body: JSON.stringify(sub) })).status, 200);
  }
  assert.equal((await call(e, "/a/" + VIEW + "/subscribe", { method: "POST", body: JSON.stringify({ endpoint: "http://x" }) })).status, 400);
  assert.equal((await call(e, "/app-subs")).status, 403);
  const subs = await (await call(e, "/app-subs", { headers: { "x-push-key": "push-key-123" } })).json();
  assert.equal(subs.length, 1);
  await call(e, "/app-subs", { method: "POST", headers: { "x-push-key": "push-key-123" }, body: JSON.stringify({ remove: [sub.endpoint] }) });
  assert.equal((await (await call(e, "/app-subs", { headers: { "x-push-key": "push-key-123" } })).json()).length, 0);
});

test("page, manifest, service worker and icon are served", async () => {
  const e = env();
  const page = await (await call(e, "/a/" + VIEW)).text();
  assert.match(page, /apple-touch-icon/);
  assert.match(page, /"Trucks"/);
  assert.doesNotMatch(page, /javascript:1/);
  const man = await (await call(e, "/a/" + VIEW + "/manifest.webmanifest")).json();
  assert.equal(man.display, "standalone");
  assert.equal(man.start_url, "/a/" + VIEW + "/");
  const icon = await call(e, "/a/" + VIEW + "/icon.png");
  assert.equal(icon.headers.get("content-type"), "image/png");
  assert.equal(new Uint8Array(await icon.arrayBuffer())[1], 0x50);
});

test("the page and service worker scripts compile", () => {
  const page = appPage("/a/k", links(env()), "BPub");
  const script = page.slice(page.indexOf("<script>") + 8, page.indexOf("</script>"));
  assert.doesNotThrow(() => new Function(script));
  assert.doesNotThrow(() => new Function(serviceWorker("/a/k")));
});

test("alerts: added by the ops host, pending until pushed, an update by key stays quiet", async () => {
  const e = env();
  const post = (path, body) => call(e, path, { method: "POST", headers: { "x-push-key": "push-key-123" }, body: JSON.stringify(body) });
  const pending = async () => (await call(e, "/app-alerts-pending", { headers: { "x-push-key": "push-key-123" } })).json();
  assert.equal((await call(e, "/app-alert", { method: "POST", body: "{}" })).status, 403);
  await post("/app-alert", { title: "Trucks", text: "AUS01 add 1×26ft" });
  await post("/app-alert", { title: "PDA", text: "2 problems", key: "pda|2026-11-12" });
  const p = await pending();
  assert.equal(p.length, 2);
  await post("/app-alerts-pushed", { ids: p.map((a) => a.id) });
  assert.equal((await pending()).length, 0);
  await post("/app-alert", { title: "PDA", text: "3 problems", key: "pda|2026-11-12" });
  assert.equal((await pending()).length, 0);
  const data = await (await call(e, "/a/" + VIEW + "/data")).json();
  assert.equal(data.alerts.length, 2);
  assert.equal(data.alerts.find((a) => a.title === "PDA").text, "3 problems");
});

test("an alert can carry a suggested reply and a team-chat link; other links are dropped", async () => {
  const e = env();
  const post = (body) => call(e, "/app-alert", { method: "POST", headers: { "x-push-key": "push-key-123" }, body: JSON.stringify(body) });
  await post({ title: "Shift lead asks", text: "How many left?", reply: "About 12k left, done by 4.", link: "https://chat.example.com/open?conversation=c1" });
  await post({ title: "Bad link", text: "x", link: "https://evil.example/x" });
  const data = await (await call(e, "/a/" + VIEW + "/data")).json();
  assert.equal(data.alerts[0].reply, "About 12k left, done by 4.");
  assert.match(data.alerts[0].link, /^https:[/][/]chat[.]example[.]com/);
  assert.equal(data.alerts[1].link, undefined);
  const pending = await (await call(e, "/app-alerts-pending", { headers: { "x-push-key": "push-key-123" } })).json();
  assert.deepEqual(pending.map((a) => a.link ? "link" : "none"), ["link", "none"]);
});

test("floor scans: the phone sends a batch, sending again replaces it, the ops host reads them with the key", async () => {
  const e = env();
  const send = (body) => call(e, "/a/" + VIEW + "/scans", { method: "POST", body: JSON.stringify(body) });
  assert.equal((await send({ id: "b1", codes: [] })).status, 400);
  assert.equal((await send({ id: "b1", codes: [{ code: "BG012611130236-284", at: "2026-11-13T20:00:00Z" }] })).status, 200);
  await send({ id: "b1", codes: [{ code: "BG012611130236-284" }, { code: "BG012611140011-284" }] });
  await send({ id: "b2", codes: [{ code: "SB2611120018107" }] });
  assert.equal((await call(e, "/app-scans")).status, 403);
  const batches = await (await call(e, "/app-scans", { headers: { "x-push-key": "push-key-123" } })).json();
  assert.deepEqual(batches.map((b) => [b.id, b.codes.length]), [["b1", 2], ["b2", 1]]);
  assert.equal((await call(e, "/a/wrong-key-1234567890/scans", { method: "POST", body: "{}" })).status, 404);
});

test("the page's bag-label pattern takes bag numbers and skips package labels", () => {
  const page = appPage("/a/k", [], "");
  const src = /const BAG = (\/.*\/);/.exec(page)[1];
  const BAG = new Function("return " + src)();
  for (const ok of ["BG012611130236-284", "BG012611140011-51", "SB2611120018107", "SB2611120000019"]) assert.ok(BAG.test(ok), ok);
  for (const no of ["PKG010268115732", "TBA312345678901", "BG01-284", "SJT01-006", "SB26111"]) assert.ok(!BAG.test(no), no);
});

test("labels: the ops host pushes them with the key, the phone reads them, the print page has only the new ones", async () => {
  const e = env();
  const labels = { made: "2026-11-13 21:00:00", scanned: 3, items: [
    { print: true, bag: "BG012611130236-284", route: "BRO01-002", pcs: 445, date: "15:20:35 13/11/2026", origin: "HOU.H", operator: "HOU.H Sorter 1", goesOn: "box with package X", merge: ["BG012611140011-284"] },
    { print: false, bag: "BG012611130045-313", route: "SJT01-006", pcs: 412, goesOn: "label already on the box" }] };
  assert.equal((await call(e, "/app-data/labels", { method: "POST", body: JSON.stringify(labels) })).status, 403);
  assert.equal((await call(e, "/app-data/labels", { method: "POST", headers: { "x-push-key": "push-key-123" }, body: JSON.stringify(labels) })).status, 200);
  assert.equal((await (await call(e, "/a/" + VIEW + "/labels")).json()).items.length, 2);
  const page = await (await call(e, "/a/" + VIEW + "/print")).text();
  assert.match(page, /1 label/);
  assert.match(page, /BG012611130236-284/);
  assert.doesNotMatch(page, /BG012611130045-313/);
  const scripts = [...page.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.doesNotThrow(() => new Function(scripts[0]));
  assert.match(printPage({ items: [{ print: true, bag: "</script><b>" }] }), /\\u003c\/script>/);
});

test("app tabs take https links and same-worker paths, nothing else", () => {
  const e = { APP_LINKS: JSON.stringify([{ name: "A", url: "https://x/y" }, { name: "B", url: "/trucks/k" }, { name: "C", url: "//evil" }, { name: "D", url: "javascript:1" }]) };
  assert.deepEqual(links(e).map((l) => l.name), ["A", "B"]);
});

test("the assistant answers under an alert; a newer question keeps it asking", async () => {
  const e = env();
  const post = (path, body) => call(e, path, { method: "POST", headers: { "x-push-key": "push-key-123" }, body: JSON.stringify(body) });
  await post("/app-alert", { title: "Truck news", text: "x" });
  const id = (await (await call(e, "/a/" + VIEW + "/data")).json()).alerts[0].id;
  assert.equal((await call(e, "/a/" + VIEW + "/ask", { method: "POST", body: JSON.stringify({ id, text: "why?" }) })).status, 200);
  let asks = await (await call(e, "/app-asks-pending", { headers: { "x-push-key": "push-key-123" } })).json();
  assert.equal(asks[0].asking, "ask");
  await post("/app-ask-answer", { id, text: "Because.", answered: "9999" });
  asks = await (await call(e, "/app-asks-pending", { headers: { "x-push-key": "push-key-123" } })).json();
  assert.equal(asks.length, 0);
  const alert = (await (await call(e, "/a/" + VIEW + "/data")).json()).alerts[0];
  assert.deepEqual(alert.chat.map((c) => c.from), ["you", "assistant"]);
});
