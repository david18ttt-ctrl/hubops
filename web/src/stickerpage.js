// Phone sticker page: pick a chute sticker, an R / S / T gaylord sticker, a plain code or the time label, how many,
// Print. The job goes into the label station's print queue (labelstation.js) and the print agent by the label
// printer prints it (agent/). The list is sticker-catalog.json (scripts/make_sticker_catalog.py, from the sort
// plan); the phone draws each sticker as a 4x6 picture at 203 dpi, so the printer gets exactly what the preview shows.
import CATALOG from "./sticker-catalog.json" with { type: "json" };

const HUB = "HOU.H";
const HUB_SHORT = "HOU";
const ORDER = ["AUS01", "ACT01", "VCT01", "BRO01", "HRL01", "SJT01", "GGG01", "TXK01", "SHV01", "AEX01", "CXO01", "SGR01"];
const TYPE_ES = { Exception: "Excepción", Special: "Especial", Free: "Libre", Reserved: "Reservado", Pending: "Pendiente" };
const SIDE_SPLIT = 150; // hub chutes below this are on side 2, the mirror chutes (188 and up) on side 1

export function stickersPage(base) {
  const list = CATALOG.map((x) => ({ ...x, type: x.type ? TYPE_ES[x.type] || "" : "" }));
  return `<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex"><meta name="theme-color" content="#111"><title>Etiquetas</title>
<style>
:root { color-scheme: light; --ink: #111; --bg: #f2f2f0; --line: #d6d6d2; --ok: #0a7d32; --bad: #c41a1a; }
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body { margin: 0; background: var(--bg); color: var(--ink); font: 17px -apple-system, "Segoe UI", Roboto, Arial, sans-serif; }
header { position: sticky; top: 0; z-index: 5; background: var(--ink); color: #fff; padding: calc(10px + env(safe-area-inset-top)) 16px 12px; }
.bar { display: flex; align-items: center; gap: 10px; min-height: 40px; }
.bar h1 { flex: 1; margin: 0; font-size: 22px; font-weight: 900; text-transform: uppercase; letter-spacing: -.3px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
#back { border: 0; background: #fff; color: var(--ink); font: inherit; font-weight: 900; font-size: 22px; width: 44px; height: 44px; border-radius: 8px; }
#back[hidden], #home[hidden] { display: none; }
#home { display: inline-flex; align-items: center; text-decoration: none; border: 0; background: #fff; color: var(--ink); font: inherit; font-weight: 900; font-size: 15px; height: 44px; padding: 0 12px; border-radius: 8px; text-transform: uppercase; }
.dot { width: 12px; height: 12px; border-radius: 50%; background: #777; flex: 0 0 12px; }
.dot.on { background: #3ad06a; } .dot.off { background: #ff4b4b; }
#q { margin-top: 10px; width: 100%; font: inherit; font-size: 18px; padding: 13px 14px; border-radius: 8px; border: 0; background: #fff; color: var(--ink); }
main { padding: 14px 16px calc(120px + env(safe-area-inset-bottom)); max-width: 640px; margin: 0 auto; }
.note { color: #555; font-size: 14px; margin: 2px 0 12px; }
.sec { font-size: 13px; font-weight: 800; text-transform: uppercase; letter-spacing: .6px; color: #555; margin: 18px 0 8px; }
.tiles { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
.tile { display: flex; flex-direction: column; justify-content: space-between; min-height: 104px; padding: 14px; border-radius: 10px;
  border: 2px solid var(--ink); background: #fff; color: var(--ink); text-decoration: none; font: inherit; text-align: left; width: 100%; }
.tile b { font-size: 24px; font-weight: 900; line-height: 1.05; }
.tile span { font-size: 14px; color: #555; }
.tile.dark { background: var(--ink); color: #fff; } .tile.dark span { color: #ccc; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(98px, 1fr)); gap: 8px; }
.card { border: 2px solid var(--ink); background: #fff; border-radius: 10px; padding: 10px 6px 8px; text-align: center; font: inherit; color: var(--ink); min-height: 84px; width: 100%; }
.card .big { display: block; font-size: 30px; font-weight: 900; letter-spacing: -.5px; line-height: 1.05; word-break: break-word; }
.card .big.long { font-size: 19px; }
.card .sm { display: block; font-size: 12.5px; color: #555; margin-top: 4px; }
.card .tag { display: inline-block; font-size: 11px; font-weight: 800; background: #eee; border-radius: 4px; padding: 1px 5px; margin-top: 4px; }
.seg { display: flex; gap: 8px; margin-bottom: 12px; }
.seg a { flex: 1; text-align: center; padding: 12px 0; border: 2px solid var(--ink); border-radius: 10px; font-size: 26px; font-weight: 900; color: var(--ink); text-decoration: none; background: #fff; }
.seg a.on { background: var(--ink); color: #fff; }
.empty { color: #555; padding: 24px 0; text-align: center; }
/* Bottom sheet */
#shade { position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 20; }
#sheet { position: fixed; left: 0; right: 0; bottom: 0; z-index: 21; background: #fff; border-radius: 16px 16px 0 0;
  padding: 14px 16px calc(16px + env(safe-area-inset-bottom)); max-width: 640px; margin: 0 auto; max-height: 92vh; overflow: auto; }
#shade[hidden], #sheet[hidden] { display: none; }
.prev { display: flex; justify-content: center; align-items: center; height: 34vh; max-height: 300px; margin-bottom: 10px; overflow: hidden; }
.prev img { max-height: 100%; max-width: 100%; border: 1px solid var(--line); background: #fff; }
.prev img.side { transform: rotate(-90deg); max-height: none; max-width: none; height: auto; width: min(30vh, calc((100vw - 40px) / 1.5)); flex: 0 0 auto; }
.what { text-align: center; font-size: 24px; font-weight: 900; }
.what small { display: block; font-size: 14px; font-weight: 600; color: #555; }
.qty { display: flex; align-items: center; justify-content: center; gap: 14px; margin: 14px 0 10px; }
.qty button { width: 64px; height: 64px; border-radius: 50%; border: 2px solid var(--ink); background: #fff; font-size: 34px; font-weight: 900; color: var(--ink); }
.qty output { min-width: 90px; text-align: center; font-size: 46px; font-weight: 900; }
.chips { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; margin-bottom: 14px; }
.chips button { border: 2px solid var(--ink); background: #fff; border-radius: 999px; padding: 9px 16px; font: inherit; font-weight: 800; color: var(--ink); }
.chips button.on { background: var(--ink); color: #fff; }
#print { width: 100%; border: 0; border-radius: 12px; padding: 20px; font: inherit; font-size: 22px; font-weight: 900; background: var(--ink); color: #fff; text-transform: uppercase; }
#print:disabled { opacity: .45; }
#cancel { width: 100%; margin-top: 8px; border: 0; background: none; font: inherit; font-weight: 700; color: #555; padding: 10px; }
#toast { position: fixed; left: 16px; right: 16px; bottom: calc(16px + env(safe-area-inset-bottom)); z-index: 30; background: var(--ink); color: #fff;
  border-radius: 12px; padding: 14px 16px; font-weight: 700; max-width: 608px; margin: 0 auto; box-shadow: 0 6px 24px rgba(0,0,0,.25); }
#toast[hidden] { display: none; } #toast.bad { background: var(--bad); }
</style></head><body>
<header>
  <div class="bar"><button id="back" hidden aria-label="Atrás">‹</button><h1 id="title">Etiquetas</h1><a id="home" href="${base}" aria-label="Inicio">⌂ Inicio</a><span class="dot" id="dot" title="Impresora"></span></div>
  <input id="q" type="search" inputmode="search" autocomplete="off" placeholder="Buscar: 087, VCT 5, R 004, PHX…">
</header>
<main id="main"></main>
<div id="shade" hidden></div>
<div id="sheet" hidden>
  <div class="prev"><img id="pimg" alt=""></div>
  <div class="what" id="pname"></div>
  <div class="qty"><button id="minus" aria-label="Menos">−</button><output id="n">10</output><button id="plus" aria-label="Más">+</button></div>
  <div class="chips" id="chips"></div>
  <button id="print">Imprimir</button>
  <button id="cancel">Cancelar</button>
</div>
<div id="toast" hidden></div>
<script>
const BASE = ${JSON.stringify(base)};
const ALL = ${JSON.stringify(list)};
const ORDER = ${JSON.stringify(ORDER)};
const HUB = ${JSON.stringify(HUB)}, HUB_SHORT = ${JSON.stringify(HUB_SHORT)};
const SIDE_SPLIT = ${SIDE_SPLIT};
const MAX = 15;
// Plain stickers: just the hub code in huge letters, or any code typed in. Nothing else on it.
const CITIES = [...new Set(ALL.filter((x) => x.g === "city").map((x) => x.sub.replace(/[.]H$/, "")))].sort();
const PLAIN = CITIES.map((c) => ({ id: "p:" + c, g: "plain", name: c, city: true }));
const PMAX = 12;
const cleanCode = (s) => { const t = String(s || "").toUpperCase().replace(/\\s+/g, " ").trim(); return t && t.length <= PMAX && /^[A-Z0-9 .\\-\\/#]+$/.test(t) ? t : null; };
// Time label: the same label the chute printers' feed button prints ("HOU 11/24" / "19:00" / "HOU.H"), hub time,
// hour rounded down. Goes on the gaylord when it is pulled, 2 copies (front + back).
function timeNow() {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/Chicago", month: "2-digit", day: "2-digit", hour: "2-digit", hourCycle: "h23" })
    .formatToParts(new Date()).map((x) => [x.type, x.value]));
  return { md: p.month + "/" + p.day, hh: p.hour + ":00" };
}
function timeItem() { const t = timeNow(); return { id: "t:now", g: "time", name: t.md + " " + t.hh, md: t.md, hh: t.hh }; }
// A 4x6 portrait canvas turned so the drawing is a 1218 x 812 landscape (read sideways, like the chute stickers).
function sideways() {
  const c = document.createElement("canvas"); c.width = 812; c.height = 1218;
  const g = c.getContext("2d");
  g.fillStyle = "#fff"; g.fillRect(0, 0, 812, 1218);
  g.translate(812, 0); g.rotate(Math.PI / 2);
  g.fillStyle = "#000"; g.textAlign = "center"; g.textBaseline = "alphabetic";
  return [c, g];
}
const F = (px, w) => (w || 900) + " " + px + 'px "Arial Black", "Helvetica Neue", Arial, sans-serif';
function fitText(g, t, px, w, h) {
  for (; px > 20; px -= 4) { g.font = F(px); const m = g.measureText(t); if (m.width <= w && (!h || m.actualBoundingBoxAscent + m.actualBoundingBoxDescent <= h)) break; }
  return px;
}
function drawTime(x) {
  const [c, g] = sideways();
  fitText(g, HUB_SHORT + " " + x.md, 130, 1100); g.fillText(HUB_SHORT + " " + x.md, 609, 150);
  fitText(g, x.hh, 440, 1150); g.fillText(x.hh, 609, 600);
  fitText(g, HUB, 70, 600); g.fillText(HUB, 609, 760);
  return c.toDataURL("image/png");
}
function byId(id) {
  if (id === "t:now") return timeItem();
  if (String(id).startsWith("u:")) { const t = cleanCode(id.slice(2)); return t ? { id: "u:" + t, g: "plain", name: t, city: false } : null; }
  return PLAIN.find((x) => x.id === id) || ALL.find((x) => x.id === id) || null;
}
// Plain sticker: the code alone, as big as fits.
function drawPlain(x) {
  const [c, g] = sideways();
  const top = 40, W = 1218 - 80, H = 812 - top - 40;
  fitText(g, x.name, 700, W, H);
  const m = g.measureText(x.name), h = m.actualBoundingBoxAscent + m.actualBoundingBoxDescent;
  g.fillText(x.name, 609, top + (H - h) / 2 + m.actualBoundingBoxAscent);
  return c.toDataURL("image/png");
}
// Chute sticker: the part people read off the chute huge, the rest and the chute number small.
function drawChute(x) {
  if (x.g === "rst") {
    const c = document.createElement("canvas"); c.width = 812; c.height = 1218;
    const g = c.getContext("2d");
    g.fillStyle = "#fff"; g.fillRect(0, 0, 812, 1218); g.fillStyle = "#000"; g.textAlign = "center";
    const [letter, num] = x.name.split(" ");
    fitText(g, letter, 520, 700); g.fillText(letter, 406, 560);
    fitText(g, num, 300, 740); g.fillText(num, 406, 900);
    g.font = F(56, 700); g.fillText(x.route || "", 406, 1080);
    return c.toDataURL("image/png");
  }
  const f = face(x), [c, g] = sideways();
  g.font = F(64, 800); g.fillText((f.pre || x.sub || "") + "", 609, 110);
  fitText(g, f.big, 460, 1120, 520); g.fillText(f.big, 609, 640);
  g.font = F(60, 800); g.fillText("CHUTE " + String(x.chute).padStart(3, "0"), 609, 770);
  return c.toDataURL("image/png");
}
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const store = { get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} } };

// What a card shows: big = the part people read off the chute / gaylord, small = the rest.
function face(x) {
  if (x.g === "time") return { big: x.hh, sm: HUB_SHORT + " " + x.md };
  if (x.g === "plain") return { big: x.name, sm: x.city ? "simple" : "propio · custom" };
  if (x.g === "rst") return { big: x.name.split(" ")[1], sm: x.sub + " · " + (x.route || "").split("-")[0] };
  const i = x.name.lastIndexOf("-");
  if (x.g === "station" && i > 0) return { big: x.name.slice(i + 1), sm: "rampa " + x.chute, pre: x.name.slice(0, i) };
  return { big: x.name, sm: "rampa " + x.chute };
}
function card(x) {
  const f = face(x);
  return '<button class="card" data-id="' + esc(x.id) + '"><span class="big' + (f.big.length > 5 ? " long" : "") + '">' + esc(f.big) + '</span><span class="sm">' + esc((SHOWPRE && f.pre ? f.pre + " · " : "") + f.sm) + "</span>" +
    (["Pendiente", "Reservado", "Libre"].includes(x.type) ? '<span class="tag">' + esc(x.type) + "</span>" : "") + "</button>";
}
const grid = (xs) => xs.length ? '<div class="grid">' + xs.map(card).join("") + "</div>" : '<div class="empty">Nada · Nothing</div>';
const tile = (href, b, span, dark) => '<a class="tile' + (dark ? " dark" : "") + '" href="' + href + '"><b>' + b + "</b><span>" + span + "</span></a>";
const count = (f) => ALL.filter(f).length;
// Hub (.H) chutes come in two rows: side 2 has the low numbers (049 and up), side 1 the mirror ones (188 and up).
const side2 = (x) => x.g === "city" && x.chute < SIDE_SPLIT, side1 = (x) => x.g === "city" && x.chute >= SIDE_SPLIT;
const RST_STATION = (ALL.find((x) => x.g === "rst") || {}).route?.split("-")[0] || "";

function home() {
  const recent = store.get("recent", []).map(byId).filter(Boolean);
  return '<div class="note">Elige la etiqueta, cuántas, e IMPRIMIR. Salen en la impresora de etiquetas. · Pick a sticker, how many, PRINT.</div>' +
    '<button class="tile dark" data-id="t:now" style="width:100%;min-height:0;margin-bottom:6px"><b>⏱ Etiqueta de hora</b><span>Time label · ' + esc(timeNow().md + " " + timeNow().hh) + ' · 2 (frente + atrás)</span></button>' +
    (recent.length ? '<div class="sec">Recientes</div>' + grid(recent) : "") +
    '<div class="sec">Grupos</div><div class="tiles">' +
    (RST_STATION ? tile("#rst/R", "R · S · T", RST_STATION + " gaylords · " + count((x) => x.g === "rst"), true) : "") +
    tile("#station", "Estaciones", ORDER.slice(0, 3).join(", ") + "… · " + count((x) => x.g === "station")) +
    tile("#city/2", "Hubs lado 2", "side 2 · " + count(side2)) +
    tile("#city/1", "Hubs lado 1", "side 1 · " + count(side1)) +
    tile("#plain", "Hubs simples", "solo PHX, STL… grande · " + PLAIN.length) +
    tile("#custom", "Código propio", "escribe cualquier código · custom", true) +
    tile("#other", "Otros", "NOREAD, libres · " + count((x) => x.g === "other")) + "</div>";
}
function view(route) {
  const [g, sub] = route.split("/");
  if (g === "rst") {
    const L = ["R", "S", "T"].includes(sub) ? sub : "R";
    return ["R · S · T  " + RST_STATION, '<div class="seg">' + ["R", "S", "T"].map((l) => '<a href="#rst/' + l + '" class="' + (l === L ? "on" : "") + '">' + l + "</a>").join("") + "</div>" +
      grid(ALL.filter((x) => x.g === "rst" && x.sub === L))];
  }
  if (g === "station" && sub) {
    const xs = ALL.filter((x) => x.g === "station" && x.sub === sub);
    const main = xs.filter((x) => !["Pendiente", "Reservado"].includes(x.type)), rest = xs.filter((x) => !main.includes(x));
    return [sub, grid(main) + (rest.length ? '<div class="sec">Pendientes · sin regla</div>' + grid(rest) : "")];
  }
  if (g === "station") {
    const subs = ORDER.filter((s) => ALL.some((x) => x.g === "station" && x.sub === s));
    return ["Estaciones", '<div class="tiles">' + subs.map((s) => tile("#station/" + s, s, count((x) => x.g === "station" && x.sub === s) + " rampas")).join("") + "</div>"];
  }
  if (g === "city") {
    const f = sub === "2" ? side2 : sub === "1" ? side1 : (x) => x.g === "city";
    return [sub ? "Hubs lado " + sub : "Hubs", grid(ALL.filter(f).sort((a, b) => a.chute - b.chute))];
  }
  if (g === "plain") return ["Hubs simples", '<div class="note">Solo el código del hub, letras grandes. · Plain hub stickers.</div>' + grid(PLAIN)];
  if (g === "custom") return ["Código propio", '<div class="note">Escribe el código (máx. ' + PMAX + ' letras/números) y toca IMPRIMIR. · Type any code.</div>' +
    '<input id="cc" autocomplete="off" autocapitalize="characters" maxlength="' + PMAX + '" placeholder="Ej: VCT01, PHX, PALLET 3" style="width:100%;font:inherit;font-size:28px;font-weight:900;padding:14px;border:2px solid #111;border-radius:10px;text-transform:uppercase">' +
    '<button id="ccgo" style="margin-top:12px;width:100%;border:0;border-radius:12px;padding:18px;font:inherit;font-size:20px;font-weight:900;background:#111;color:#fff;text-transform:uppercase">Ver e imprimir</button>'];
  if (g === "other") {
    const xs = ALL.filter((x) => x.g === "other");
    const by = {};
    xs.forEach((x) => (by[x.type || "Otros"] = by[x.type || "Otros"] || []).push(x));
    return ["Otros", Object.keys(by).map((k) => '<div class="sec">' + esc(k) + "</div>" + grid(by[k])).join("")];
  }
  return ["Etiquetas", home()];
}
function search(q) {
  const t = q.toUpperCase().replace(/\\s+/g, " ").trim();
  const num = /^\\d{1,3}$/.test(t) ? Number(t) : null;
  const words = t.split(" ").filter(Boolean);
  const pad = (w) => (/^\\d{1,2}$/.test(w) ? w.padStart(3, "0") : w);
  const hits = ALL.filter((x) => {
    if (num !== null && x.chute === num) return true;
    const hay = (x.name + " " + (x.route || "") + " " + (x.sub || "") + " " + (x.type || "")).toUpperCase();
    return words.every((w) => hay.includes(w) || hay.includes(pad(w)));
  });
  hits.sort((a, b) => (b.chute === num) - (a.chute === num));
  const ph = PLAIN.filter((x) => words.every((w) => x.name.includes(w.replace(/[.]H$/, ""))));
  return (ph.length ? '<div class="sec">Simples</div>' + grid(ph) : "") + (ph.length && hits.length ? '<div class="sec">Rampas</div>' : "") + (hits.length || !ph.length ? grid(hits.slice(0, 120)) : "");
}
let SHOWPRE = false; // station prefix on cards: only where the page title doesn't already say it
function render() {
  const q = $("q").value.trim();
  const route = location.hash.slice(1);
  SHOWPRE = !!q || !route;
  let title, body;
  if (q) [title, body] = ["Buscar", search(q)];
  else [title, body] = view(route);
  $("title").textContent = title;
  $("back").hidden = !route && !q;
  $("main").innerHTML = body;
}

// ----- the bottom sheet -----
let cur = null, n = Math.min(15, store.get("lastN", 10));
const CHIPS = [1, 2, 5, 10, 15];
function setN(v) {
  n = Math.max(1, Math.min(MAX, v));
  $("n").textContent = n;
  $("print").textContent = "Imprimir " + n;
  $("chips").querySelectorAll("button").forEach((b) => b.classList.toggle("on", Number(b.dataset.n) === n));
}
$("chips").innerHTML = CHIPS.map((c) => '<button data-n="' + c + '">' + c + "</button>").join("");
function open(id) {
  cur = byId(id);
  if (!cur) return;
  cur = { ...cur };
  const f = face(cur);
  cur.img = cur.g === "plain" ? drawPlain(cur) : cur.g === "time" ? drawTime(cur) : drawChute(cur);
  $("pimg").src = cur.img;
  $("pimg").className = cur.g === "rst" ? "" : "side";
  if (cur.g === "time") $("pname").innerHTML = "Hora " + esc(cur.hh) + "<small>" + esc(HUB_SHORT + " " + cur.md) + " · una al frente, una atrás · front + back</small>";
  else $("pname").innerHTML = esc(cur.g === "rst" ? cur.name + " · " + RST_STATION : cur.name) + "<small>" + (cur.g === "rst" ? "Gaylord " + esc(cur.route) : cur.g === "plain" ? esc(f.sm) : "Rampa " + cur.chute + (cur.type ? " · " + esc(cur.type) : "")) + "</small>";
  setN(cur.g === "time" ? 2 : n);
  $("print").disabled = false;
  $("shade").hidden = $("sheet").hidden = false;
}
function close() {
  $("shade").hidden = $("sheet").hidden = true;
  if (cur && cur.g === "time") n = Math.min(15, store.get("lastN", 10));
  cur = null;
  if (location.hash === "#time") history.replaceState(null, "", location.pathname + location.search);
}
function toast(text, bad) {
  const t = $("toast");
  t.textContent = text; t.className = bad ? "bad" : ""; t.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => (t.hidden = true), bad ? 7000 : 4500);
}
async function print() {
  if (!cur) return;
  const x = cur, k = n;
  $("print").disabled = true; $("print").textContent = "Enviando…";
  try {
    const body = x.g === "time" ? { time: x.name, img: x.img, copies: k } : x.g === "plain" ? { text: x.name, city: x.city, img: x.img, copies: k } : { id: x.id, img: x.img, copies: k };
    const r = await fetch(BASE + "/sticker", { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    const d = await r.json();
    if (!r.ok || !d.ok) throw new Error(d.error || "Error");
    if (x.g !== "time") {
      store.set("lastN", k);
      store.set("recent", [x.id, ...store.get("recent", []).filter((i) => i !== x.id)].slice(0, 6));
    }
    close(); render();
    if (d.already) { toast("Ya está en la fila — espera, va a salir. · Already in line, it's coming."); dot(d.printer); return; }
    toast(d.printer ? "✓ " + k + " × " + d.name + " — imprimiendo" : "Enviado, pero la impresora está apagada. Saldrán al encenderla (hasta 30 min).", !d.printer);
    dot(d.printer);
  } catch (e) {
    $("print").disabled = false; setN(n);
    toast(e.message === "Failed to fetch" ? "Sin internet. Intenta otra vez." : e.message, true);
  }
}
function dot(on) { $("dot").className = "dot " + (on ? "on" : "off"); $("dot").title = on ? "Impresora encendida" : "Impresora apagada"; }
async function status() { try { dot((await (await fetch(BASE + "/stickers/status", { cache: "no-store" })).json()).printer); } catch (e) {} }

document.addEventListener("click", (e) => {
  const c = e.target.closest(".card, .tile[data-id]");
  if (c) return open(c.dataset.id);
  if (e.target.id === "ccgo") {
    const t = cleanCode($("cc").value);
    if (!t) return toast("Solo letras, números, - . / # (máx. " + PMAX + ").", true);
    return open("u:" + t);
  }
  const chip = e.target.closest("#chips button");
  if (chip) setN(Number(chip.dataset.n));
});
document.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target.id === "cc") { e.preventDefault(); $("ccgo").click(); } });
$("minus").onclick = () => setN(n - 1);
$("plus").onclick = () => setN(n + 1);
$("print").onclick = print;
$("cancel").onclick = close;
$("shade").onclick = close;
$("back").onclick = () => { if ($("q").value) { $("q").value = ""; render(); } else { const r = location.hash.slice(1); location.hash = r.startsWith("rst") || r.startsWith("city") || !r.includes("/") ? "" : r.split("/")[0]; } };
$("q").oninput = render;
window.addEventListener("hashchange", () => { $("q").value = ""; render(); window.scrollTo(0, 0); });
render();
if (location.hash === "#time") open("t:now"); // straight from the "Time label" button on the scan page
status();
setInterval(status, 30000);
</script></body></html>`;
}
