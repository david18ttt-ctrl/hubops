// Truck news from the team chat, applied here. A reader on the ops host judges each chat message two ways:
// does it change something with the trucks (kind), and does the operator need to see it (attention). The
// worker then acts by itself, keeps a short list for the truck page, and sends one alert to the phone app:
//   hold / release      → KV held_routes (the truck-space check leaves a held route's bags out)
//   bulk_not_coming /
//   bulk_coming         → KV bulk_today (unless the bulk shipper's truck is already scanned in)
//   volume              → KV truckspace_calendar {night: factor}, held to 0.7–1.6 (single unusual days only:
//                         the forecast mix learns a whole season's level by itself)
//   truck / other       → nothing to change; on the page, and in the alert when it needs attention
// The reader also says when it stops seeing new messages (reader.stale), so the page and the operator know
// the chat isn't being read. KV "truck_news" = {items: [...], reader: {stale, newest, at}}.
import { addDays } from "./truckmodel.js";

export const TRUCK_NEWS_KEY = "truck_news";
const HELD_KEY = "held_routes";
const BULK_KEY = "bulk_today";
const CALENDAR_KEY = "truckspace_calendar";
const ROUTE = /^[A-Z]{3}\d{2}-\d{3}$/;
const KINDS = new Set(["hold", "release", "bulk_not_coming", "bulk_coming", "volume", "truck", "other"]);
const KEEP_MS = 3 * 86400000;
export const FACTOR_MIN = 0.7;
export const FACTOR_MAX = 1.6;

const text = (x, n) => String(x || "").replace(/\s+/g, " ").trim().slice(0, n);

// Applies the reader's actions and status. Returns the alert lines ([] when nothing is worth one).
// clock(ms) = hub clock time, "8:18 PM".
export async function applyNews(env, body, today, now, clock) {
  const news = (await env.PLAN.get(TRUCK_NEWS_KEY, "json")) || {};
  const items = (news.items || []).filter((n) => now - (n.at || 0) < KEEP_MS);
  const had = new Set(items.map((n) => `${n.key}|${n.kind}`));
  const lines = [];
  let held = null, calendar = null;
  for (const raw of Array.isArray(body.actions) ? body.actions.slice(0, 40) : []) {
    if (!raw || !KINDS.has(raw.kind) || had.has(`${raw.key}|${raw.kind}`)) continue;
    const a = { key: text(raw.key, 200), kind: raw.kind, night: /^\d{4}-\d{2}-\d{2}$/.test(raw.night) ? raw.night : today,
      routes: (raw.routes || []).map((r) => String(r).toUpperCase()).filter((r) => ROUTE.test(r)).slice(0, 10),
      factor: Number.isFinite(raw.factor) ? raw.factor : null, attention: Boolean(raw.attention), summary: text(raw.summary, 300),
      quote: text(raw.quote, 300), chat: text(raw.chat, 100), sender: text(raw.sender, 60), sent: Number(raw.sent) || now, at: now, did: null };
    const said = `${a.sender ? `${a.sender}, ` : ""}${a.chat}, ${clock(a.sent)}`;
    if (a.kind === "hold" && a.routes.length) {
      held ||= (await env.PLAN.get(HELD_KEY, "json")) || {};
      for (const route of a.routes) held[route] = { since: today, why: `${said}: ${a.quote.slice(0, 120)}` };
      a.did = `holding ${a.routes.join(", ")} (not counted for the truck)`;
    } else if (a.kind === "release" && a.routes.length) {
      held ||= (await env.PLAN.get(HELD_KEY, "json")) || {};
      const was = a.routes.filter((route) => held[route]);
      for (const route of was) delete held[route];
      if (was.length) a.did = `released ${was.join(", ")} (counted again)`;
    } else if (a.kind === "bulk_not_coming" || a.kind === "bulk_coming") {
      const bulk = await env.PLAN.get(BULK_KEY, "json");
      const coming = a.kind === "bulk_coming";
      const arrived = bulk?.day === today && bulk.at != null;
      if (a.night === today && !arrived && !(bulk?.day === today && bulk.coming === coming)) {
        await env.PLAN.put(BULK_KEY, JSON.stringify({ day: today, coming }));
        a.did = coming ? "bulk truck marked coming tonight" : "bulk truck marked not coming tonight (later stations −1–2 boxes each)";
      }
    } else if (a.kind === "volume" && a.factor > 0 && a.night >= today && a.night <= addDays(today, 7)) {
      calendar ||= (await env.PLAN.get(CALENDAR_KEY, "json")) || {};
      const factor = Math.round(100 * Math.min(FACTOR_MAX, Math.max(FACTOR_MIN, a.factor))) / 100;
      if (calendar[a.night] !== factor) {
        calendar[a.night] = factor;
        a.factor = factor;
        a.did = `peak day ${a.night.slice(5).replace("-", "/")}: boxes still to come ×${factor}`;
        a.attention = true; // a volume call is always shown to the operator
      }
    }
    items.push(a);
    had.add(`${a.key}|${a.kind}`);
    if (a.did) lines.push(`Chat → trucks: ${a.did}. ${a.summary} (${said})${a.attention ? " · check it" : ""}`);
    else if (a.attention) lines.push(`Chat, needs attention: ${a.summary} (${said})`);
  }
  if (held) await env.PLAN.put(HELD_KEY, JSON.stringify(held));
  if (calendar) {
    for (const old of Object.keys(calendar)) if (old < addDays(today, -2)) delete calendar[old];
    await env.PLAN.put(CALENDAR_KEY, JSON.stringify(calendar));
  }
  let reader = news.reader || null;
  if (body.reader && typeof body.reader.stale === "boolean") {
    const changed = Boolean(reader?.stale) !== body.reader.stale;
    reader = { stale: body.reader.stale, newest: Number(body.reader.newest) || null, at: now };
    if (changed) {
      lines.push(reader.stale
        ? `Chat reader: nothing new since ${reader.newest ? clock(reader.newest) : "?"} (reader stopped?). Holds and limit changes said in the chat aren't reaching the truck check.`
        : "Chat reader: reading messages again.");
    }
  }
  if (items.length !== (news.items || []).length || lines.length || JSON.stringify(reader) !== JSON.stringify(news.reader || null)) {
    await env.PLAN.put(TRUCK_NEWS_KEY, JSON.stringify({ items: items.slice(-60), reader }));
  }
  return { lines, items, reader };
}

// What the truck page can show: the last day's news, newest first, and whether the chat is being read.
export function newsForPage(news, now) {
  const items = (news?.items || []).filter((n) => now - (n.at || 0) < 86400000).sort((a, b) => b.sent - a.sent).slice(0, 12)
    .map((n) => ({ kind: n.kind, did: n.did || "", summary: n.summary, quote: n.quote, chat: n.chat, sender: n.sender, sent: n.sent, attention: n.attention }));
  return { items, stale: news?.reader?.stale ? { newest: news.reader.newest } : null };
}
