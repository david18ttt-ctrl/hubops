// Hub clock helpers shared by the worker modules: the hub's ids and time zone, Central Time parts of a
// moment, and the operating day (6 AM to 6 AM).
export const HUB_ID = 1; // the hub's id in the WMS
export const HUB_ID_KEY = "hub-1"; // the WMS's second form of the hub id, sent with dashboard queries
export const TIME_ZONE = "America/Chicago";

export function chicagoParts(date) {
  const parts = {};
  const format = new Intl.DateTimeFormat("en-US", {
    timeZone: TIME_ZONE, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
  for (const part of format.formatToParts(date)) parts[part.type] = part.value;
  return parts;
}

// The operating day runs 6 AM to 6 AM, Central Time.
export function operationWindow(date) {
  const now = chicagoParts(date);
  let day = new Date(Date.UTC(+now.year, +now.month - 1, +now.day));
  if (+now.hour < 6) day = new Date(day.getTime() - 86400000);
  const label = (d) => d.toISOString().slice(0, 10);
  return [`${label(day)} 06:00:01`, `${label(new Date(day.getTime() + 86400000))} 06:00:00`];
}

// The WMS (hubsim/ locally): base URL from WMS_URL, bearer token from the WMS_TOKEN secret.
export const wmsUrl = (env, path) => String(env.WMS_URL || "http://127.0.0.1:8765").replace(/[/]+$/, "") + path;
export const wmsHeaders = (env, extra = {}) => ({ "Content-Type": "application/json", Authorization: `Bearer ${env.WMS_TOKEN}`, ...extra });
