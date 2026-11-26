// Label station: anyone with the scan link scans (or photographs, or types) a package from a box, the ops host
// finds the box (bag) it is in, and the print agent by the label printer prints that bag's 4x6 label by itself.
// Also the phone sticker page (stickerpage.js): chute stickers, plain codes and time labels to the same printer.
//
//   GET  /s/<LABEL_SCAN_KEY>              the scan page (camera, photo, or typed number)
//   GET  /s/<LABEL_SCAN_KEY>/stickers     the sticker page; POST …/sticker {id|text|time, img, copies}
//   POST /s/<LABEL_SCAN_KEY>/req          {codes: [...]} -> {id}
//   GET  /s/<LABEL_SCAN_KEY>/req/<id>     what happened to it (+ whether the ops host and the printer are on)
//   POST /s/<LABEL_SCAN_KEY>/img/<id>     the phone's drawing of a label the worker looked up itself
//   POST /s/<LABEL_SCAN_KEY>/again/<id>   print that label once more
//   GET  /p/<LABEL_PRINT_KEY>             print station page (fallback: left open in a kiosk-printing browser)
//   GET  /p/<LABEL_PRINT_KEY>/next        labels waiting to print (?agent=1 from the print agent; long poll)
//   POST /p/<LABEL_PRINT_KEY>/printed     {id, info}
//   GET  /p/<LABEL_PRINT_KEY>/agent.py    the print agent (agent/label_agent.py), for installs and self-updates
//   GET  /p/<LABEL_PRINT_KEY>/agent.ps1   the Windows background agent; /install.bat installs it
//   GET  /label-jobs                      (ops host, X-Push-Key) requests waiting for a WMS lookup
//   POST /label-jobs/<id>                 (ops host) {status: "done"|"error", label, msg}
//   POST /label-jobs                      (ops host) {stickers: [{name, img, copies}], labels: [...]} straight to the printer
// Storage is D1 (LABELS binding), not KV: KV allows only 1,000 writes a day and every scan is several writes.

import CATALOG from "./sticker-catalog.json" with { type: "json" };
import { stickersPage } from "./stickerpage.js";
import { lookup as cloudLookup, LoginExpired } from "./labelcloud.js";
import { AGENT_PS1, AGENT_PY } from "./agent-files.js";

const HUB = "HOU.H";
const NO_STORE = { "cache-control": "no-store", "x-robots-tag": "noindex" };
const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { ...NO_STORE, "content-type": "application/json" } });
const html = (text) => new Response(text, { headers: { ...NO_STORE, "content-type": "text/html; charset=utf-8" } });

const CODE = /^[A-Z0-9-]{8,40}$/;
const PER_IP_MINUTE = 20;
const PER_HOUR = 600;
const SAME_CODE = 15 * 60 * 1000; // the same package again within 15 min = the same request (no second label)
const LOOKUP_WAIT = 10 * 60 * 1000; // the ops host only takes requests this fresh
const PRINT_WAIT = 30 * 60 * 1000; // the printer only prints labels this fresh
const MAX_STICKERS = 15; // one press of Print on the sticker page
const PLAIN_MAX = 12; // longest code on a plain sticker (still readable across a gaylord)
const IMG_MAX = 600000; // a phone-drawn 4x6 picture (data URL)
const isPng = (s) => /^data:image[/]png;base64,/.test(String(s || "")) && String(s).length < IMG_MAX;
// A typed code for a plain sticker, cleaned: upper case, single spaces; null if it has anything else.
export function plainText(s) {
  const t = String(s ?? "").toUpperCase().replace(/\s+/g, " ").trim();
  return t && t.length <= PLAIN_MAX && /^[A-Z0-9 .\-/#]+$/.test(t) ? t : null;
}
// Time label "11/24 19:00": drawn on the phone like a plain sticker; null if it isn't that shape.
export function timeText(s) {
  const t = String(s ?? "").trim();
  return /^(0[1-9]|1[0-2])[/](0[1-9]|[12]\d|3[01]) ([01]\d|2[0-3]):00$/.test(t) ? t : null;
}
const BEAT = 60 * 1000; // "seen" is written at most once a minute per side
const CLOUD_AFTER = 25 * 1000; // a request the ops host hasn't answered this long: the worker looks it up itself
const DRAW_WAIT = 40 * 1000; // a worker label the phone never drew: printed from its facts in plain letters
// The ops host and the print agent long-poll: one request waits up to LONG_POLL, checking D1 every STEP, so the
// pollers cost ~3,500 requests a day each instead of ~29,000.
const LONG_POLL = 25000;
const STEP = 1200;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function waitFor(get, wait) {
  const until = Date.now() + Math.min(wait, LONG_POLL);
  for (;;) {
    const rows = await get(Date.now());
    if (rows.length || Date.now() + STEP > until) return rows;
    await sleep(STEP);
  }
}

export const SCHEMA = `CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, codes TEXT NOT NULL, code TEXT NOT NULL,
  at INTEGER NOT NULL, ip TEXT, status TEXT NOT NULL DEFAULT 'new', label TEXT, msg TEXT, done_at INTEGER, printed_at INTEGER);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status, at);
CREATE INDEX IF NOT EXISTS jobs_code ON jobs(code, at);
CREATE TABLE IF NOT EXISTS seen (name TEXT PRIMARY KEY, at INTEGER NOT NULL);`;

// The tables, once per database per isolate (a fresh D1, e.g. local dev, needs no separate migration step).
const READY = new WeakSet();
async function ensureSchema(db) {
  if (READY.has(db)) return;
  await db.batch(SCHEMA.split(";").map((s) => s.trim()).filter(Boolean).map((s) => db.prepare(s)));
  READY.add(db);
}

export function hash(text) { let h = 5381; for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) | 0; return (h >>> 0).toString(36); }
let PAGE_VERSION = "";
const pageVersion = () => PAGE_VERSION || (PAGE_VERSION = hash(pageText("", "")));
const keyOk = (got, want) => !!want && want.length >= 16 && got === want;
const pushAllowed = (request, env) => !!env.APP_PUSH_KEY && request.headers.get("x-push-key") === env.APP_PUSH_KEY;

export function cleanCodes(list) {
  const out = [];
  for (const c of Array.isArray(list) ? list : []) {
    const code = String(c || "").trim().toUpperCase().replace(/\s+/g, "");
    if (CODE.test(code) && !out.includes(code)) out.push(code);
  }
  return out.slice(0, 8);
}

async function beat(db, name, now) {
  await db.prepare("INSERT INTO seen (name, at) VALUES (?1, ?2) ON CONFLICT(name) DO UPDATE SET at = ?2 WHERE at < ?3")
    .bind(name, now, now - BEAT).run();
}

async function seenTimes(db) {
  const rows = (await db.prepare("SELECT name, at FROM seen").all()).results || [];
  return Object.fromEntries(rows.map((r) => [r.name, r.at]));
}
const on = (seen, name, now) => !!seen[name] && now - seen[name] < 3 * BEAT;

function view(job, seen, now) {
  if (!job) return null;
  let label = null;
  try { label = job.label ? JSON.parse(job.label) : null; } catch (e) {}
  return { id: job.id, code: job.code, at: job.at, status: job.status, msg: job.msg || "", label, printedAt: job.printed_at || null,
    host: on(seen, "host", now), printer: on(seen, "printer", now) };
}

// worker.js handler: a Response, or null when the path isn't the label station's.
export async function handleLabelStation(request, env) {
  const path = new URL(request.url).pathname;
  if (!/^[/](s|p|label-jobs)([/]|$)/.test(path)) return null;
  if (!env.LABELS) return new Response("Label station not set up (no LABELS database)", { status: 404 });
  await ensureSchema(env.LABELS);
  return labelStation(request, env);
}

export async function labelStation(request, env) {
  const url = new URL(request.url);
  const path = url.pathname;
  const db = env.LABELS;
  const now = Date.now();

  if (path === "/label-jobs" || path.startsWith("/label-jobs/")) {
    if (!pushAllowed(request, env)) return new Response("Forbidden", { status: 403 });
    if (request.method === "GET") {
      await beat(db, "host", now);
      const rows = await waitFor(async (t) => (await db.prepare("SELECT id, codes, at FROM jobs WHERE status = 'new' AND at > ?1 ORDER BY id LIMIT 20")
        .bind(t - LOOKUP_WAIT).all()).results || [], Number(url.searchParams.get("wait") || 0) * 1000);
      return json(rows.map((r) => ({ id: r.id, at: r.at, codes: JSON.parse(r.codes) })));
    }
    // Finished pictures from the ops host (chute stickers in bulk, or bag labels it drew for another tool),
    // queued as already looked up so the printer takes them next.
    if (path === "/label-jobs" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      const list = (Array.isArray(b.stickers) ? b.stickers : []).slice(0, 60);
      let i = 0;
      for (const x of list) {
        if (!/^data:image[/]png;base64,/.test(String(x.img || ""))) continue;
        const label = { kind: "sticker", route: String(x.name || "Sticker").slice(0, 40), img: x.img,
          copies: Math.max(1, Math.min(15, Number(x.copies) || 1)) };
        if (x.raw) label.raw = true;
        await db.prepare("INSERT INTO jobs (codes, code, at, ip, status, label, done_at) VALUES ('[]', ?1, ?2, 'host', 'done', ?3, ?4)")
          .bind("sticker " + label.route, now, JSON.stringify(label), now + i++).run();
      }
      for (const x of (Array.isArray(b.labels) ? b.labels : []).slice(0, 60)) {
        if (!/^data:image[/]png;base64,/.test(String(x.img || ""))) continue;
        const label = {};
        for (const k of ["bag", "route", "dest", "chute", "pcs", "date", "origin", "operator", "pkg", "note"]) label[k] = x[k] == null ? "" : x[k];
        label.img = x.img;
        label.note = label.note || "Sent by the ops host";
        await db.prepare("INSERT INTO jobs (codes, code, at, ip, status, label, done_at) VALUES (?1, ?2, ?3, 'host', 'done', ?4, ?5)")
          .bind(JSON.stringify([String(label.pkg || label.bag)]), String(label.pkg || label.bag).slice(0, 40), now, JSON.stringify(label), now + i++).run();
      }
      return json({ ok: true, queued: i });
    }
    const m = /^[/]label-jobs[/](\d+)$/.exec(path);
    if (m && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      const status = b.status === "done" ? "done" : "error";
      await db.prepare("UPDATE jobs SET status = ?1, label = ?2, msg = ?3, done_at = ?4 WHERE id = ?5")
        .bind(status, b.label ? JSON.stringify(b.label) : null, String(b.msg || "").slice(0, 300), now, Number(m[1])).run();
      return json({ ok: true });
    }
    return new Response("Not found", { status: 404 });
  }

  const s = /^[/]s[/]([^/]+)([/].*)?$/.exec(path);
  if (s) {
    if (!keyOk(s[1], env.LABEL_SCAN_KEY)) return new Response("Not found", { status: 404 });
    const base = "/s/" + s[1], rest = s[2] || "/";
    if (rest === "/" && request.method === "GET") return html(scanPage(base));
    // Sticker page: anyone with the scan link picks a chute sticker, a plain code or the time label, how many,
    // and the print agent prints it.
    if (rest === "/stickers" && request.method === "GET") return html(stickersPage(base));
    if (rest === "/stickers/status" && request.method === "GET") {
      const seen = await seenTimes(db);
      return json({ printer: on(seen, "printer", now) });
    }
    if (rest === "/sticker" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      // Plain stickers (a hub code or any typed code) and time labels, drawn on the phone.
      const plain = b.text != null ? plainText(b.text) : b.time != null ? timeText(b.time) : null;
      const st = plain ? null : CATALOG.find((x) => x.id === String(b.id || ""));
      const copies = Math.floor(Number(b.copies) || 0);
      if (b.time != null && !plain) return json({ error: "Hora no válida." }, 400);
      if (b.text != null && !plain) return json({ error: "Código: letras, números, - . / (máx. " + PLAIN_MAX + ")." }, 400);
      if ((!st && !plain) || copies < 1 || copies > MAX_STICKERS) return json({ error: "Elige una etiqueta y cuántas (1–" + MAX_STICKERS + ")." }, 400);
      if (!isPng(b.img)) return json({ error: "No se pudo dibujar la etiqueta. Intenta otra vez." }, 400);
      const ip = request.headers.get("cf-connecting-ip") || "";
      // At most MAX_STICKERS a print, no total for the night. Only a burst guard per phone.
      const c = await db.prepare("SELECT COUNT(*) AS minute FROM jobs WHERE code LIKE 'sticker%' AND ip = ?1 AND at > ?2")
        .bind(ip, now - 60000).first();
      if ((c?.minute || 0) >= 12) return json({ error: "Demasiadas seguidas. Espera un minuto." }, 429);
      const img = String(b.img);
      const name = plain ? (b.time != null ? "time " : b.city ? "plain " : "custom ") + plain
        : st.g === "rst" ? st.name : String(st.chute).padStart(3, "0") + " " + st.name;
      // Pressing again while the printer catches up must not print the same stickers twice.
      const waiting = await db.prepare("SELECT COUNT(*) AS n FROM jobs WHERE code = ?1 AND printed_at IS NULL AND at > ?2")
        .bind("sticker " + name, now - PRINT_WAIT).first();
      if (waiting?.n) {
        const seen = await seenTimes(db);
        return json({ ok: true, name, copies, already: true, printer: on(seen, "printer", now) });
      }
      for (let left = copies, i = 0; left > 0; left -= 15, i++) {
        const label = { kind: "sticker", route: name.slice(0, 40), img, copies: Math.min(15, left) };
        await db.prepare("INSERT INTO jobs (codes, code, at, ip, status, label, done_at) VALUES ('[]', ?1, ?2, ?3, 'done', ?4, ?5)")
          .bind("sticker " + name, now, ip, JSON.stringify(label), now + i).run();
      }
      const seen = await seenTimes(db);
      return json({ ok: true, name, copies, printer: on(seen, "printer", now) });
    }
    if (rest === "/req" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      const codes = cleanCodes(b.codes);
      if (!codes.length) return json({ error: "No package number found. Try again closer, or type it." }, 400);
      const ip = request.headers.get("cf-connecting-ip") || "";
      const same = await db.prepare("SELECT * FROM jobs WHERE code = ?1 AND at > ?2 AND status != 'error' ORDER BY id DESC LIMIT 1")
        .bind(codes[0], now - SAME_CODE).first();
      if (same) return json({ id: same.id, repeat: true });
      const counts = await db.prepare("SELECT SUM(CASE WHEN ip = ?1 AND at > ?2 THEN 1 ELSE 0 END) AS mine, COUNT(*) AS hour FROM jobs WHERE at > ?3")
        .bind(ip, now - 60000, now - 3600000).first();
      if ((counts?.mine || 0) >= PER_IP_MINUTE || (counts?.hour || 0) >= PER_HOUR) return json({ error: "Too many requests. Wait a minute." }, 429);
      const r = await db.prepare("INSERT INTO jobs (codes, code, at, ip) VALUES (?1, ?2, ?3, ?4)")
        .bind(JSON.stringify(codes), codes[0], now, ip).run();
      return json({ id: r.meta.last_row_id });
    }
    const q = /^[/]req[/](\d+)$/.exec(rest);
    if (q && request.method === "GET") {
      let job = await db.prepare("SELECT * FROM jobs WHERE id = ?1").bind(Number(q[1])).first();
      if (!job) return json({ error: "not found" }, 404);
      const seen = await seenTimes(db);
      // The ops host is off (or hasn't taken it in CLOUD_AFTER): the worker looks it up, the phone draws the label.
      if (job.status === "new" && env.WMS_TOKEN && (!on(seen, "host", now) || now - job.at > CLOUD_AFTER)) {
        const claim = await db.prepare("UPDATE jobs SET status = 'cloud' WHERE id = ?1 AND status = 'new'").bind(job.id).run();
        if (claim.meta.changes) {
          let label = null, why = "";
          try {
            ({ label, why } = await cloudLookup(env, JSON.parse(job.codes)));
          } catch (e) {
            why = e instanceof LoginExpired ? "WMS login expired: the lookup can't run until the token is renewed."
              : "WMS lookup failed (" + String(e.message || e).slice(0, 80) + "). Try again in a few minutes.";
          }
          await db.prepare("UPDATE jobs SET status = ?1, label = ?2, msg = ?3, done_at = ?4 WHERE id = ?5")
            .bind(label ? "draw" : "error", label ? JSON.stringify(label) : null, why || "", Date.now(), job.id).run();
          job = await db.prepare("SELECT * FROM jobs WHERE id = ?1").bind(job.id).first();
        }
      }
      return json(view(job, seen, now));
    }
    // The phone's picture of a label the worker looked up: now it can print.
    const im = /^[/]img[/](\d+)$/.exec(rest);
    if (im && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      const job = await db.prepare("SELECT * FROM jobs WHERE id = ?1 AND status = 'draw'").bind(Number(im[1])).first();
      if (!job) return json({ ok: false });
      const label = JSON.parse(job.label);
      if (isPng(b.img)) label.img = b.img;
      await db.prepare("UPDATE jobs SET status = 'done', label = ?1, done_at = ?2 WHERE id = ?3 AND status = 'draw'")
        .bind(JSON.stringify(label), now, job.id).run();
      return json({ ok: true });
    }
    const a = /^[/]again[/](\d+)$/.exec(rest);
    if (a && request.method === "POST") {
      const r = await db.prepare("UPDATE jobs SET printed_at = NULL, done_at = ?1 WHERE id = ?2 AND status = 'done' AND printed_at IS NOT NULL AND printed_at < ?3")
        .bind(now, Number(a[1]), now - 30000).run();
      return json({ ok: r.meta.changes > 0 });
    }
    return new Response("Not found", { status: 404 });
  }

  const p = /^[/]p[/]([^/]+)([/].*)?$/.exec(path);
  if (p) {
    if (!keyOk(p[1], env.LABEL_PRINT_KEY)) return new Response("Not found", { status: 404 });
    const base = "/p/" + p[1], rest = p[2] || "/";
    if (rest === "/" && request.method === "GET") return html(printStationPage(base, env.LABEL_SCAN_KEY ? url.origin + "/s/" + env.LABEL_SCAN_KEY : ""));
    // Background printing for a Windows laptop: a browser holds print() while its page is hidden, so a small
    // program prints the pictures instead, with no window open.
    if (rest === "/install.bat" && request.method === "GET")
      return new Response(installBat(url.origin + base), { headers: { ...NO_STORE, "content-type": "application/octet-stream",
        "content-disposition": 'attachment; filename="install-label-printer.bat"' } });
    if (rest === "/agent.ps1" && request.method === "GET")
      return new Response(agentPs1(url.origin + base), { headers: { ...NO_STORE, "content-type": "text/plain; charset=utf-8" } });
    if (rest === "/agent.py" && request.method === "GET")
      return new Response(AGENT_PY, { headers: { ...NO_STORE, "content-type": "text/x-python; charset=utf-8" } });
    if (rest === "/next" && request.method === "GET") {
      const isAgent = url.searchParams.get("agent") === "1";
      await beat(db, isAgent ? "agent" : "printer", now);
      if (isAgent) await beat(db, "printer", now);
      if (isAgent && url.searchParams.get("v")) // the agent's version + printer, as a "seen" row the ops host can read
        await db.prepare("INSERT OR REPLACE INTO seen (name, at) VALUES (?1, ?2)")
          .bind(("agentinfo v" + url.searchParams.get("v") + " | " + (url.searchParams.get("pr") || "")).slice(0, 400), now).run();
      // The agent is running: the page must not take labels (a hidden page would sit on them).
      if (!isAgent) {
        const seen = await seenTimes(db);
        if (on(seen, "agent", now)) return json({ version: pageVersion(), queue: [], recent: [], agent: true });
      }
      await db.prepare("UPDATE jobs SET status = 'done', done_at = ?1 WHERE status = 'draw' AND done_at < ?2").bind(now, now - DRAW_WAIT).run();
      // Claimed in one UPDATE: with two printers polling, each label still goes to only one of them.
      const rows = await waitFor(async (t) => (await db.prepare("UPDATE jobs SET printed_at = ?1 WHERE id IN (SELECT id FROM jobs WHERE status = 'done' AND printed_at IS NULL AND done_at > ?2 ORDER BY done_at LIMIT 10) AND printed_at IS NULL RETURNING id, label")
        .bind(Date.now(), t - PRINT_WAIT).all()).results || [], Number(url.searchParams.get("wait") || 0) * 1000);
      const recent = (await db.prepare("SELECT id, label, printed_at FROM jobs WHERE printed_at IS NOT NULL ORDER BY printed_at DESC LIMIT 15").all()).results || [];
      const lab = (r) => { try { return JSON.parse(r.label); } catch (e) { return null; } };
      const queued = (r) => { const l = lab(r); if (l && RAW_ALL && isAgent) l.raw = true; return { id: r.id, label: l }; };
      return json({ version: pageVersion(), agentVersion: agentVersion(), agentPyVersion: agentPyVersion(), queue: rows.map(queued).filter((r) => r.label),
        recent: recent.map((r) => { const l = lab(r); if (l) delete l.img; return { id: r.id, label: l, at: r.printed_at }; }) });
    }
    if (rest === "/printed" && request.method === "POST") {
      const b = await request.json().catch(() => ({}));
      await db.prepare("UPDATE jobs SET printed_at = ?1 WHERE id = ?2").bind(now, Number(b.id)).run();
      // What the printer side says happened (printer used, queue state, or the error), for the ops host's log.
      if (b.info) await db.prepare("UPDATE jobs SET msg = ?1 WHERE id = ?2").bind(String(b.info).slice(0, 600), Number(b.id)).run();
      return json({ ok: true });
    }
    return new Response("Not found", { status: 404 });
  }
  return null;
}

const STYLE = `
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #f2f2f0; color: #111; font: 17px "Segoe UI", -apple-system, Roboto, Arial, sans-serif; }
main { max-width: 560px; margin: 0 auto; padding: 16px; }
h1 { font-size: 26px; font-weight: 900; letter-spacing: -0.5px; margin: 6px 0 4px; text-transform: uppercase; }
button, .btn { font: inherit; font-weight: 800; font-size: 18px; padding: 18px; border: 2px solid #111; border-radius: 4px;
  background: #111; color: #fff; width: 100%; display: block; text-align: center; cursor: pointer; text-transform: uppercase; letter-spacing: .3px; }
button.alt, .btn.alt { background: #fff; color: #111; }
button:active, .btn:active { transform: translateY(1px); }
button:disabled { opacity: .4; }
.row { display: flex; gap: 10px; margin-bottom: 10px; }
.row > * { flex: 1; }
input[type=text] { font: inherit; font-size: 18px; width: 100%; padding: 16px; border-radius: 4px; border: 2px solid #111; background: #fff; color: #111; }
.card { background: #fff; border: 2px solid #111; border-radius: 4px; padding: 16px; margin: 12px 0; }
.big { font-size: 30px; font-weight: 900; letter-spacing: -0.5px; }
.muted { color: #555; font-size: 15px; }
.ok { color: #0a7d32; } .bad { color: #c41a1a; } .wait { color: #9a6200; }
video { width: 100%; border: 2px solid #111; border-radius: 4px; background: #000; display: block; }
[hidden] { display: none !important; }
@media (prefers-color-scheme: dark) {
  :root { color-scheme: dark; }
  body { background: #000; color: #f5f5f5; }
  button, .btn { background: #f5f5f5; color: #000; border-color: #f5f5f5; }
  button.alt, .btn.alt { background: #000; color: #f5f5f5; }
  input[type=text], .card { background: #111; color: #f5f5f5; border-color: #f5f5f5; }
  video { border-color: #f5f5f5; }
  .muted { color: #aaa; } .ok { color: #3ddc76; } .bad { color: #ff5c5c; } .wait { color: #ffc23d; }
}
`;

export const ZXING_READER = "https://cdn.jsdelivr.net/npm/zxing-wasm@3.1.6/dist/iife/reader/index.js";
export const ZXING_WRITER = "https://cdn.jsdelivr.net/npm/zxing-wasm@3.1.6/dist/iife/writer/index.js";

export function scanPage(base) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Box label</title><style>${STYLE}</style></head><body><main>
<a href="${base}/stickers#time" style="display:block;margin:0 0 8px;padding:14px;border:2px solid #111;border-radius:4px;background:#111;color:#fff;font-weight:900;font-size:20px;text-decoration:none">⏱ Etiqueta de hora → <span style="font-weight:600;color:#ccc">Time label</span></a>
<a href="${base}/stickers" style="display:block;margin:0 0 12px;padding:12px 14px;border:2px solid #111;border-radius:4px;background:#fff;color:#111;font-weight:800;text-decoration:none">Etiquetas de rampa y R·S·T → <span style="font-weight:600;color:#555">Stickers</span></a>
<h1>Etiqueta de caja</h1>
<div class="muted" style="margin-bottom:12px">Escanea UN paquete de adentro de la caja, presiona IMPRIMIR y recoge la etiqueta en la mesa de clasificación.<br>Scan ONE package from inside the box, press PRINT, pick up the label at the sorting table.</div>
<div class="row"><button id="cam">Escanear · Scan</button></div>
<div class="row"><label class="btn alt" for="photo">Foto · Photo</label><input id="photo" type="file" accept="image/*" capture="environment" hidden></div>
<video id="video" playsinline muted hidden></video>
<div class="row"><input id="typed" type="text" placeholder="o escribe el número · or type the number" autocapitalize="characters" autocomplete="off"><button id="go" class="alt" style="flex:0 0 120px">Buscar</button></div>
<div id="out"></div>
</main>
<script>
const BASE = ${JSON.stringify(base)};
const HUB = ${JSON.stringify(HUB)};
const READ = { formats: ["Code128", "QRCode", "DataMatrix", "Code39"], maxNumberOfSymbols: 12, tryHarder: true };
// Bag, gaylord and truck labels in the same frame are not packages.
const BAG = /^(BG\\d{11,15}-\\d{1,3}|GL\\d{6,}|SB\\d{6,}|TT\\d{6,})$/;
const CODE = /^[A-Z0-9-]{8,40}$/;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
let lib = null, stream = null, timer = 0, busy = false, poll = 0;
function loadLib() {
  if (!lib) lib = new Promise((ok, bad) => { const s = document.createElement("script"); s.src = ${JSON.stringify(ZXING_READER)};
    s.onload = () => ok(window.ZXingWASM); s.onerror = () => { lib = null; bad(new Error("Scanner didn't load. Check internet.")); };
    document.head.appendChild(s); });
  return lib;
}
function say(h) { $("out").innerHTML = '<div class="card">' + h + "</div>"; }
// Package numbers first (the hub's own PKG numbers, then the longest: tracking numbers are long); bag labels are left out.
function pick(texts) {
  const c = [...new Set(texts.map((t) => String(t || "").trim().toUpperCase().replace(/\\s+/g, "")))].filter((t) => CODE.test(t) && !BAG.test(t));
  return c.sort((a, b) => (b.startsWith("PKG") - a.startsWith("PKG")) || b.length - a.length);
}
// Labels the worker looked up itself (ops host off): this phone draws the 4x6 label at 203 dpi, same layout as
// the print page's, and sends it.
let writer = null;
function loadWriter() {
  if (!writer) writer = new Promise((ok, bad) => { const keep = window.ZXingWASM; const s = document.createElement("script"); s.src = ${JSON.stringify(ZXING_WRITER)};
    s.onload = () => { const w = window.ZXingWASM; if (keep) window.ZXingWASM = keep; ok(w); }; s.onerror = () => { writer = null; bad(new Error("writer didn't load")); };
    document.head.appendChild(s); });
  return writer;
}
function svgImage(svg) {
  return new Promise((ok, bad) => { const im = new Image(); im.onload = () => ok(im); im.onerror = bad;
    im.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg); });
}
async function drawLabel(x) {
  const W = await loadWriter();
  const D = 203, IN = (v) => Math.round(v * D), PT = (v) => Math.round(v / 72 * D);
  const c = document.createElement("canvas"); c.width = IN(4); c.height = IN(6);
  const g = c.getContext("2d");
  g.fillStyle = "#fff"; g.fillRect(0, 0, c.width, c.height); g.fillStyle = "#000"; g.imageSmoothingEnabled = false;
  const font = (pt, bold) => (bold ? "bold " : "") + PT(pt) + 'px "Arial Narrow", Arial, sans-serif';
  const qr = await svgImage((await W.writeBarcode(x.bag, { format: "QRCode", scale: 4 })).svg);
  const bar = await svgImage((await W.writeBarcode(x.bag, { format: "Code128", scale: 3 })).svg);
  // Positions measured off the print page's own picture (812x1218), so both print the same.
  const L = 52, R = c.width - 52;
  g.drawImage(qr, 52, 46, 254, 254);
  g.textBaseline = "alphabetic"; g.textAlign = "left";
  g.font = font(10.5, true); g.fillText("Destination:", L, 333);
  g.font = font(15); g.fillText("Chute: " + (x.chute ?? ""), 342, 95); g.fillText("Origin: " + (x.origin || HUB), 342, 150);
  const route = String(x.route || ""), cut = route.lastIndexOf("-");
  const dest = cut > 0 ? route.slice(cut + 1) : route, remark = cut > 0 ? route.slice(0, cut + 1) : "";
  let size = 58; g.font = font(size, true);
  while (g.measureText(dest).width > R - L && size > 8) { size--; g.font = font(size, true); }
  g.textAlign = "center"; g.fillText(dest, c.width / 2, 540);
  g.textAlign = "left"; g.font = font(10.5, true); g.fillText("Remark:", L, 670);
  g.font = font(22, true); g.fillText(remark, L, 735);
  const m = /^(\\d{4})-(\\d{2})-(\\d{2})[ T](\\d{2}:\\d{2}:\\d{2})/.exec(String(x.date || ""));
  const date = m ? m[4] + " " + m[2] + "/" + m[3] + "/" + m[1] : String(x.date || "");
  g.drawImage(bar, 130, 905, 553, 152);
  g.textAlign = "center"; g.font = font(15, true);
  g.fillText(x.bag, c.width / 2, 1105); g.fillText("Date: " + date, c.width / 2, 1150);
  return c.toDataURL("image/png");
}
async function sendPicture(id, label) {
  let img = "";
  try { img = await drawLabel(label); } catch (e) {} // no picture: the agent prints the facts with the printer's fonts
  try { await fetch(BASE + "/img/" + id, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ img }) }); } catch (e) {}
}

async function send(codes) {
  if (busy || !codes.length) return;
  busy = true; stopCam();
  say("Sending " + esc(codes[0]) + "…");
  try {
    const r = await fetch(BASE + "/req", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ codes }) });
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || "error " + r.status);
    follow(d.id, d.repeat);
  } catch (e) { say('<span class="bad">Not sent: ' + esc(e.message) + "</span>"); busy = false; }
}
async function follow(id, repeat) {
  clearTimeout(poll);
  let j;
  try { j = await (await fetch(BASE + "/req/" + id, { cache: "no-store" })).json(); } catch (e) { poll = setTimeout(() => follow(id, repeat), 2000); return; }
  const L = j.label, age = (Date.now() - j.at) / 1000;
  let h = '<div class="muted">Package ' + esc(j.code) + "</div>";
  let again = false;
  if (j.status === "draw") {
    say(h + '<div class="wait big">Imprimiendo… · Printing…</div>');
    await sendPicture(id, L);
    poll = setTimeout(() => follow(id, repeat), 300);
    return;
  }
  if (j.status === "new" || j.status === "cloud") {
    h += '<div class="wait big">Buscando la caja… · Finding the box…</div>' + (age > 90 ? '<div class="bad">Lookup not answering. Try later.</div>' : "");
  } else if (j.status === "error") {
    h += '<div class="bad big">Sin etiqueta · No label</div><div>' + esc(j.msg) + "</div>";
  } else {
    h += '<div class="big">' + esc(L.route) + "</div><div>" + esc(L.bag) + " · " + esc(L.pcs) + " pcs · chute " + esc(L.chute) + "</div>" +
      (L.note ? '<div class="wait">' + esc(L.note) + "</div>" : "");
    if (j.printedAt) { h += '<div class="ok big">Impresa ✓ Printed</div><div>Recoge la etiqueta en la mesa de clasificación · Pick it up at the sorting table</div>'; again = true; }
    else h += '<div class="wait big">Imprimiendo… · Printing…</div>' + (!j.printer ? '<div class="bad">The label printer is offline: the label prints when it\\'s back on.</div>' : "");
  }
  if (repeat) h += '<div class="muted">Already sent in the last 15 minutes.</div>';
  if (again) h += '<div class="row" style="margin-top:10px"><button class="alt" id="again">Imprimir otra vez · Print again</button></div>';
  say(h);
  if (again) $("again").onclick = async () => { $("again").disabled = true; const r = await (await fetch(BASE + "/again/" + id, { method: "POST" })).json(); repeat = false; if (r.ok) setTimeout(() => follow(id), 500); };
  busy = j.status === "new" || j.status === "cloud" || (j.status === "done" && !j.printedAt);
  if (busy && age < 1800) poll = setTimeout(() => follow(id, repeat), 2000);
  else busy = false;
}
async function startCam() {
  if (stream) { stopCam(); return; }
  try {
    const z = await loadLib();
    stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } } });
    const v = $("video"); v.hidden = false; v.srcObject = stream; await v.play();
    $("cam").textContent = "Parar · Stop"; say("Apunta a la etiqueta del paquete · Point at the package label.");
    const cv = document.createElement("canvas"), cx = cv.getContext("2d", { willReadFrequently: true });
    const loop = async () => {
      if (!stream) return;
      if (v.videoWidth) {
        const k = Math.min(1, 1600 / Math.max(v.videoWidth, v.videoHeight));
        cv.width = Math.round(v.videoWidth * k); cv.height = Math.round(v.videoHeight * k);
        cx.drawImage(v, 0, 0, cv.width, cv.height);
        try { const c = pick((await z.readBarcodes(cx.getImageData(0, 0, cv.width, cv.height), READ)).filter((r) => r.isValid !== false).map((r) => r.text));
          if (c.length) { if (navigator.vibrate) navigator.vibrate(60); ask(c); return; } } catch (e) {}
      }
      timer = setTimeout(loop, 60);
    };
    loop();
  } catch (e) { stopCam(); say('<span class="bad">' + esc(e && e.name === "NotAllowedError" ? "Camera blocked: allow the camera for this page, or use Take photo." : e.message || e) + "</span>"); }
}
// After a scan: show the package and wait for IMPRIMIR (so a wrong code doesn't print by itself).
function ask(codes) {
  stopCam();
  say('<div class="muted">Paquete · Package</div><div class="big" style="word-break:break-all">' + esc(codes[0]) + '</div>' +
    '<div class="row" style="margin-top:12px"><button id="yes">Imprimir · Print</button></div>' +
    '<div class="row"><button class="alt" id="no">Escanear otro · Scan again</button></div>');
  $("yes").onclick = () => send(codes);
  $("no").onclick = () => { say("…"); startCam(); };
}
function stopCam() {
  clearTimeout(timer);
  if (stream) stream.getTracks().forEach((t) => t.stop());
  stream = null; $("video").hidden = true; $("video").srcObject = null; $("cam").textContent = "Escanear · Scan";
}
$("cam").onclick = startCam;
$("photo").onchange = async (e) => {
  const f = e.target.files && e.target.files[0]; e.target.value = "";
  if (!f) return;
  say("Reading the photo…");
  try {
    const z = await loadLib();
    const bmp = await createImageBitmap(f);
    const k = Math.min(1, 2400 / Math.max(bmp.width, bmp.height));
    const cv = document.createElement("canvas"); cv.width = Math.round(bmp.width * k); cv.height = Math.round(bmp.height * k);
    const cx = cv.getContext("2d"); cx.drawImage(bmp, 0, 0, cv.width, cv.height);
    const c = pick((await z.readBarcodes(cx.getImageData(0, 0, cv.width, cv.height), READ)).filter((r) => r.isValid !== false).map((r) => r.text));
    if (!c.length) { say('<span class="bad">No barcode found in the photo. Get closer to the label, or type the number.</span>'); return; }
    ask(c);
  } catch (err) { say('<span class="bad">' + esc(err.message || err) + "</span>"); }
};
const typed = () => { const c = pick([$("typed").value]); if (!c.length) { say('<span class="bad">That doesn\\'t look like a package number.</span>'); return; } $("typed").value = ""; ask(c); };
$("go").onclick = typed;
$("typed").addEventListener("keydown", (e) => { if (e.key === "Enter") typed(); });
</script></body></html>`;
}

// The fallback when no agent runs: left open in a browser started with --kiosk-printing, print() goes straight to
// the default printer with no dialog, so each new label prints by itself. Also where the ops host draws labels
// (?render=1) and where the sign with the scan link's QR code is printed.
export function printStationPage(base, scanUrl) {
  return pageText(base, scanUrl).replace("__PAGE_VERSION__", pageVersion());
}
function pageText(base, scanUrl) {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex"><title>Label printer</title>
<style>${STYLE}
@page { size: 4in 6in; margin: 0; }
#sheet { display: none; }
/* Label + sign: used off-screen for measuring and on paper. */
/* Same fields and layout as the chute printer's own bag label; nothing on it says "reprint". No piece count:
   it changes after merges, one more chance to be wrong. */
.label { width: 4in; height: 6in; padding: 0.22in 0.25in 0.3in; display: flex; flex-direction: column; overflow: hidden;
  font-family: "Arial Narrow", Arial, sans-serif; color: #000; background: #fff; position: relative; }
.top { display: flex; gap: 0.18in; align-items: flex-start; }
.qr svg { width: 1.25in; height: 1.25in; display: block; }
.facts { font-size: 15pt; line-height: 1.35; padding-top: 0.02in; }
.lbl { font-size: 10.5pt; font-weight: 700; margin-top: 0.04in; }
.dest { font-size: 58pt; font-weight: 800; text-align: center; line-height: 1; margin: 0.3in 0 0.35in; white-space: nowrap; overflow: hidden; }
.remark { font-size: 22pt; font-weight: 700; min-height: 0.4in; }
.bar { text-align: center; margin-top: auto; }
.bar svg { width: 3in; height: 0.75in; display: block; margin: 0 auto; }
.no { font-size: 15pt; font-weight: 700; margin-top: 0.04in; }
/* The sign is laid out sideways (6x4) and turned to fit the 4x6 label roll. */
.label, .label *, .signpage, .signpage * { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.signpage { width: 4in; height: 6in; overflow: hidden; position: relative; }
.sign { width: 6in; height: 4in; padding: 0.18in 0.22in; display: flex; flex-direction: column; overflow: hidden;
  font-family: Arial, sans-serif; color: #000; position: absolute; top: 0; left: 0; transform-origin: top left;
  transform: translateX(4in) rotate(90deg); }
.sign .q { white-space: nowrap; overflow: hidden; background: #000; color: #fff; font-size: 20pt; font-weight: 900; text-align: center; padding: 0.06in; line-height: 1.1; }
.sign .stop { font-size: 21pt; white-space: nowrap; overflow: hidden; font-weight: 900; text-align: center; line-height: 1.1; margin: 0.06in 0 0.08in; }
.sign .cols { display: flex; gap: 0.2in; align-items: center; flex: 1; border-top: 0.03in solid #000; padding-top: 0.08in; }
.sign .code svg { width: 2.1in; height: 2.1in; display: block; }
.sign .inst { font-size: 13pt; font-weight: 900; letter-spacing: 1px; margin-bottom: 0.08in; }
.sign ol { margin: 0; padding: 0; list-style: none; font-size: 13pt; font-weight: 700; line-height: 1.2; }
.sign li { display: flex; gap: 0.08in; align-items: flex-start; margin-bottom: 0.08in; }
.sign li b { flex: 0 0 0.3in; height: 0.3in; border-radius: 50%; border: 0.02in solid #000; background: #000; color: #fff; font-size: 14pt;
  display: flex; align-items: center; justify-content: center; }

.date { font-size: 13pt; font-weight: 700; }
/* Chute sticker: a finished 4x6 picture, one page per copy. */
.stk { width: 4in; height: 6in; overflow: hidden; }
.stk + .stk { break-before: page; }
.stk img { width: 4in; height: 6in; display: block; }
@media print {
  body { background: #fff; color: #000; }
  main { display: none; }
  #sheet { display: block; }
}
</style></head><body><main>
<h1>Label printer</h1>
<div class="card"><div class="big" id="state">Starting…</div><div class="muted" id="sub"></div></div>
<div class="row"><button class="alt" id="test">Print a test label</button></div>
<div class="row"><button class="alt" id="sign">Imprimir letrero · Print sign</button></div>
<div class="row"><a class="btn alt" href="${base}/install.bat">Install background printer (Windows)</a></div>
<div class="muted">With the background printer installed (or <code>agent/label_agent.py</code> running on the computer by the printer), nothing needs to stay open: labels print by themselves, even with this page closed.</div>
<h1 style="margin-top:24px;font-size:18px">Printed</h1><div id="recent"></div>
</main><div id="sheet"></div>
<script src="${ZXING_WRITER}"></script>
<script>
const BASE = ${JSON.stringify(base)};
const SCAN_URL = ${JSON.stringify(scanUrl || "")};
const HUB = ${JSON.stringify(HUB)};
const VERSION = "__PAGE_VERSION__";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
function fit(svg, stretch) {
  const w = /width="(\\d+(?:\\.\\d+)?)"/.exec(svg), h = /height="(\\d+(?:\\.\\d+)?)"/.exec(svg);
  if (!w || !h) return svg;
  return svg.replace("<svg", '<svg viewBox="0 0 ' + w[1] + " " + h[1] + '" preserveAspectRatio="' + (stretch ? "none" : "xMidYMid meet") + '"')
    .replace(/ width="[^"]*"/, "").replace(/ height="[^"]*"/, "");
}
// "VCT01-004" -> destination "004", remark "VCT01-" (how the chute printer splits it); no dash -> all destination.
function split(route) {
  const i = String(route || "").lastIndexOf("-");
  return i > 0 ? [route.slice(i + 1), route.slice(0, i + 1)] : [route || "", ""];
}
// "2026-11-18 11:09:13" -> "11:09:13 11/18/2026", like the chute label
function wmsDate(d) {
  const m = /^(\\d{4})-(\\d{2})-(\\d{2})[ T](\\d{2}:\\d{2}:\\d{2})/.exec(String(d || ""));
  return m ? m[4] + " " + m[2] + "/" + m[3] + "/" + m[1] : String(d || "");
}
async function draw(x) {
  const qr = await ZXingWASM.writeBarcode(x.bag, { format: "QRCode", scale: 4 });
  const bar = await ZXingWASM.writeBarcode(x.bag, { format: "Code128", scale: 3 });
  const [dest, remark] = split(x.route);
  return '<div class="label"><div class="top"><div><div class="qr">' + fit(qr.svg) + '</div><div class="lbl">Destination:</div></div>' +
    '<div class="facts"><div>Chute: ' + esc(x.chute) + "</div><div>Origin: " + esc(x.origin || HUB) + "</div></div></div>" +
    '<div class="dest">' + esc(dest) + '</div><div class="lbl">Remark:</div><div class="remark">' + esc(remark) + "</div>" +
    '<div class="bar">' + fit(bar.svg, true) + '<div class="no">' + esc(x.bag) + '</div><div class="date">Date: ' + esc(wmsDate(x.date)) + "</div></div></div>";
}
async function signHtml() {
  const qr = await ZXingWASM.writeBarcode(SCAN_URL, { format: "QRCode", scale: 6, ecLevel: "M" });
  return '<div class="signpage"><div class="sign"><div class="q">¿LA ETIQUETA NO SALIÓ BIEN?</div>' +
    '<div class="stop">¡NO PRESIONES EL BOTÓN OTRA VEZ!</div><div class="cols"><div class="code">' + fit(qr.svg) + '</div>' +
    '<div><div class="inst">EN LUGAR DE ESO:</div><ol>' +
    '<li><b>1</b><span>Escanea este código QR con tu teléfono</span></li>' +
    '<li><b>2</b><span>Con la cámara, escanea UN paquete de adentro de la caja</span></li>' +
    '<li><b>3</b><span>Presiona IMPRIMIR</span></li>' +
    '<li><b>4</b><span>Ven a la mesa de clasificación y recoge la etiqueta</span></li></ol></div></div></div></div>';
}
// Lay the sheet out off-screen, shrink any line that's wider than its box (Windows fonts run wider), then print.
async function printSheet(markup) {
  const sh = $("sheet");
  sh.innerHTML = markup;
  sh.style.cssText = "display:block;position:fixed;left:-20000px;top:0";
  await new Promise((r) => setTimeout(r, 100));
  await Promise.all([...sh.querySelectorAll("img")].map((i) => i.decode().catch(() => {})));
  sh.querySelectorAll(".q, .stop, .dest").forEach((el) => {
    let size = parseFloat(getComputedStyle(el).fontSize);
    while (el.scrollWidth > el.clientWidth + 1 && size > 8) { size -= 1; el.style.fontSize = size + "px"; }
  });
  sh.style.cssText = "";
  await new Promise((r) => setTimeout(r, 50));
  window.print();
  sh.innerHTML = "";
}
async function printOne(x) {
  if (x.kind === "sticker") return printSheet(Array(x.copies || 1).fill('<div class="stk"><img src="' + x.img + '"></div>').join(""));
  await printSheet(await draw(x)); // with --kiosk-printing print() returns after the job is sent, no dialog
}
let working = false, fails = 0, first = true;
async function tick() {
  if (working) return;
  working = true;
  try {
    const d = await (await fetch(BASE + "/next?wait=" + (first ? 0 : 25), { cache: "no-store" })).json();
    first = false;
    if (d.agent) {
      $("state").innerHTML = '<span class="ok">Background printer is on ✓</span>';
      $("sub").textContent = "This page isn't needed. You can close it.";
      working = false; setTimeout(tick, 20000); return;
    }
    if (d.version && d.version !== VERSION && !d.queue.length) { location.reload(); return; } // a newer page: load it
    fails = 0;
    for (const j of d.queue) {
      $("state").textContent = "Printing " + j.label.route + "…";
      await printOne(j.label);
      await fetch(BASE + "/printed", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: j.id }) });
    }
    $("state").innerHTML = '<span class="ok">Ready ✓</span>';
    $("sub").textContent = "Checked " + new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" });
    const t = (ms) => new Date(ms).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    $("recent").innerHTML = (d.recent || []).filter((r) => r.label).map((r) => '<div class="card"><b>' + esc(r.label.route) + "</b>" + (r.label.bag ? " · " + esc(r.label.bag) : "") +
      (r.label.kind === "sticker" ? " ×" + esc(r.label.copies) : " · " + esc(r.label.pcs) + " pcs") + '<div class="muted">' + t(r.at) + "</div></div>").join("") || '<div class="muted">Nothing yet.</div>';
  } catch (e) {
    fails++;
    $("state").innerHTML = '<span class="bad">No internet</span>'; $("sub").textContent = "Retrying…";
    await new Promise((r) => setTimeout(r, 3000));
  }
  working = false;
  setTimeout(tick, 200); // the next long poll
}
$("sign").onclick = async () => printSheet(await signHtml());
$("test").onclick = () => printOne({ route: "TEST-000", bag: "TEST-LABEL-0001", chute: "0", date: "", origin: HUB, pcs: 0 });
// Keep the computer awake while this page is showing.
async function awake() { try { if (navigator.wakeLock && !document.hidden) await navigator.wakeLock.request("screen"); } catch (e) {} }
// ?render=1: the ops host draws a label here and takes its picture (the agent prints pictures).
if (new URLSearchParams(location.search).has("render")) {
  window.renderLabel = async (x) => {
    const sh = $("sheet");
    sh.innerHTML = await draw(x);
    sh.style.cssText = "display:block;position:fixed;left:0;top:0;background:#fff;z-index:9";
    await new Promise((r) => setTimeout(r, 50));
    sh.querySelectorAll(".dest").forEach((el) => {
      let size = parseFloat(getComputedStyle(el).fontSize);
      while (el.scrollWidth > el.clientWidth + 1 && size > 8) { size -= 1; el.style.fontSize = size + "px"; }
    });
    return true;
  };
  $("state").textContent = "Render mode";
} else {
  document.addEventListener("visibilitychange", awake);
  awake();
  tick();
}
</script></body></html>`;
}

// ---------- background printer for a Windows laptop (no browser, no window) ----------
// install.bat puts agent.ps1 in %LOCALAPPDATA%\Hub-Label-Printer, starts it hidden, and adds it to Startup.
// The agent (agent/windows/label-agent.ps1) long-polls /next?agent=1, prints each picture on the label printer
// at 4x6, keeps the laptop from sleeping, and logs to agent.log next to itself.
export function installBat(base) {
  return [
    "@echo off",
    "title Hub label printer - install",
    'set "D=%LOCALAPPDATA%\\Hub-Label-Printer"',
    'if not exist "%D%" mkdir "%D%"',
    "echo Downloading the background printer...",
    `powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing '${base}/agent.ps1' -OutFile \\"$env:LOCALAPPDATA\\Hub-Label-Printer\\agent.ps1\\""`,
    'if not exist "%D%\\agent.ps1" (echo Download failed. Check the internet and run this again. & pause & exit /b 1)',
    '> "%D%\\run.vbs" echo CreateObject("Wscript.Shell").Run "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""%D%\\agent.ps1""", 0, False',
    'copy /y "%D%\\run.vbs" "%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup\\Hub-Label-Printer.vbs" >nul',
    "echo Stopping an older copy if one is running...",
    `powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \\"Name='powershell.exe'\\" | Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*-File*Hub-Label-Printer*agent.ps1*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"`,
    'wscript.exe "%D%\\run.vbs"',
    "echo.",
    "echo Done. Labels and stickers now print by themselves - no page needs to be open.",
    "echo It starts again by itself when the laptop turns on. Log: %D%\\agent.log",
    "echo.",
    "pause",
    "",
  ].join("\r\n");
}

// A "raw" job skips the Windows driver: the agent turns the picture into the printer's own commands (TSPL
// BITMAP) and writes them to the spooler as RAW (the driver took minutes to open each job; the raw job well under
// a second); if that fails it prints through the driver. false = only jobs marked raw; true = every agent job.
const RAW_ALL = true;

// Each agent updates itself when the copy served here changes (its version is a hash of the text).
let AGENT_VERSION = "", AGENT_PY_VERSION = "";
export const agentVersion = () => AGENT_VERSION || (AGENT_VERSION = hash(AGENT_PS1));
export const agentPyVersion = () => AGENT_PY_VERSION || (AGENT_PY_VERSION = hash(AGENT_PY));
export function agentPs1(base) {
  return AGENT_PS1.replace("__BASE__", base).replace("__VER__", agentVersion());
}
