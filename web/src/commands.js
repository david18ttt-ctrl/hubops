// POST /ops/command: short operator commands for the truck-space check, sent from the ops host or a phone
// shortcut with X-Push-Key. (Kept out of worker.js: a Workers entry module may only export handlers.)
import { bulkOrder, holdRoutes } from "./truckspace.js";
import { parseBulkOrder, parseHoldOrder } from "./parse.js";

export const NOT_UNDERSTOOD = 'Not understood. Try "hold st07 003", "release st07 003", "what routes are held?", ' +
  '"bulk not coming today" or "bulk?".';

// POST /ops/command {text}: the operator commands the truck-space check understands. Answers {reply}.
export async function handleCommand(request, env) {
  const url = new URL(request.url);
  if (url.pathname !== "/ops/command") return null;
  if (request.method !== "POST") return new Response("Not found", { status: 404 });
  if (!env.APP_PUSH_KEY || request.headers.get("x-push-key") !== env.APP_PUSH_KEY) return new Response("Forbidden", { status: 403 });
  const { text = "" } = await request.json().catch(() => ({}));
  const bulk = parseBulkOrder(String(text));
  const hold = bulk ? null : parseHoldOrder(String(text));
  const reply = bulk ? await bulkOrder(env, bulk) : hold ? await holdRoutes(env, hold) : NOT_UNDERSTOOD;
  return Response.json({ reply });
}

