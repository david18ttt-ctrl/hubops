// Alerts for the phone app (app.js). Every worker module and the ops host write here (same KV namespace); the
// app's Alerts tab shows them, and the ops host (scripts/webpush/flush.js, every minute) turns each new one into
// a phone notification and marks it pushed.
//
// An alert: { id, key, at: "YYYY-MM-DD HH:MM:SS" hub time, title, text, pushed }.
// `key` lets a sender update one alert in place (e.g. one problem list per shift): same key = same alert,
// and an update to an alert already pushed stays quiet, like an edited chat message.
export const ALERTS_KEY = "app_alerts";
const KEEP = 80;
const KEEP_MS = 3 * 86400000;
// Deep links into the team chat are the only links an alert may carry.
export const CHAT_LINK = /^https:[/][/]chat[.]example[.]com[/]/;

export function houstonNow(ms = Date.now()) {
  const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: "America/Chicago", hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(new Date(ms)).map((x) => [x.type, x.value]));
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

// Optional: reply (a suggested chat reply, shown with a "Copy reply & open chat" button), link (the team chat's
// deep link to the conversation; https only), image (the name of a picture the ops host stored with
// POST /app-img/<name>, shown under the text), ref (the chat reader's id for the source message).
// research: true = the assistant runner on the ops host looks into it first; its answer lands in the alert's chat.
export function addAlertTo(list, { title, text, key, reply, link, image, ref, research }, now = Date.now()) {
  const at = houstonNow(now);
  const cut = houstonNow(now - KEEP_MS);
  const old = key ? list.find((a) => a.key === key) : null;
  const kept = list.filter((a) => a.at >= cut && (!key || a.key !== key));
  const id = old ? old.id : `${now.toString(36)}${Math.random().toString(36).slice(2, 6)}`;
  kept.push({ id, key: key || null, at: old ? old.at : at, updated: at, title: String(title || "Hub ops").slice(0, 80),
    // research: the ops host pushes it itself once the runner's summary is in.
    text: String(text || "").slice(0, 4000), pushed: old ? old.pushed : research === true,
    ...(reply ? { reply: String(reply).slice(0, 2000) } : {}),
    ...(typeof link === "string" && CHAT_LINK.test(link) ? { link } : {}),
    ...(typeof image === "string" && /^[a-z0-9-]{1,60}$/.test(image) ? { image } : {}),
    ...(Number.isSafeInteger(ref) && ref > 0 ? { ref } : old && old.ref ? { ref: old.ref } : {}),
    chat: old && old.chat ? old.chat : [], asking: old ? old.asking || false : research === true ? "research" : false });
  return kept.slice(-KEEP);
}

export async function addAlert(env, alert, now = Date.now()) {
  const list = (await env.PLAN.get(ALERTS_KEY, "json")) || [];
  await env.PLAN.put(ALERTS_KEY, JSON.stringify(addAlertTo(list, alert, now)));
}

export function markPushed(list, ids) {
  return list.map((a) => (ids.includes(a.id) ? { ...a, pushed: true } : a));
}

// The conversation under an alert: from "you" (the app) waits for an answer; from "assistant" (the ops host's
// runner) answers.
const CHAT_KEEP = 40;
// The runner's fast pass rewrites an alert in place (cleaner title and text). Quiet: no new notification.
export function editAlert(list, id, { title, text }, now = Date.now()) {
  const a = list.find((x) => x.id === id);
  if (!a) return false;
  if (title) a.title = String(title).slice(0, 80);
  if (text) a.text = String(text).slice(0, 4000);
  a.updated = houstonNow(now);
  return true;
}

// answered: the time of the question this answer is for; a newer question still waits for its own answer.
export function chatAdd(list, id, from, text, now = Date.now(), answered = "", still = false) {
  const a = list.find((x) => x.id === id);
  if (!a || !text || !["you", "assistant"].includes(from)) return false;
  const newer = from === "assistant" && (a.chat || []).some((c) => c.from === "you" && c.at > answered);
  a.chat = [...(a.chat || []), { from, text: String(text).slice(0, from === "you" ? 2000 : 6000), at: houstonNow(now) }].slice(-CHAT_KEEP);
  a.asking = from === "you" || newer ? "ask" : still ? a.asking : false; // still: a first quick answer, more coming
  a.updated = houstonNow(now);
  return true;
}
