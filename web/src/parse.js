// Plain-rule parsers for short operator commands (POST /ops/command in worker.js). Plain rules, so a route or
// a state only ever comes from what the message says.

// "bulk not coming today" / "bulk is coming" / "bulk?": the bulk shipper's daily truck is announced the night
// before, but the WMS only shows it once it arrives, so the operator tells the truck forecast. A question
// changes nothing.
const BULK = /\bbulk\b/i;
const BULK_NO = /\b(?:not|isn'?t|won'?t|no|never)\b[^.?!]{0,20}\b(?:coming|come|today|tonight)\b|\bno\s+bulk\b|\bcancel|\bskip/i;
const BULK_YES = /\b(?:coming|will\s+come|is\s+on|back\s+on|arriv)/i;

export function parseBulkOrder(text) {
  if (!BULK.test(text)) return null;
  if (/\?|^\s*(?:is|will|does|did)\b/i.test(text)) return { ask: true };
  if (BULK_NO.test(text)) return { coming: false };
  if (BULK_YES.test(text)) return { coming: true };
  if (/\bstatus\b/i.test(text)) return { ask: true };
  return null;
}

// "hold ggg 003", "don't send ggg01 003", "release ggg 003", "what routes are held?": a station asked the hub
// to keep one route's bags off its truck (or that it can go again).
const HOLD_VERB = /\b(?:hold|pause|stop\s+sending|don'?t\s+send|do\s+not\s+send|keep\s+off)\b/i;
const RELEASE_VERB = /\b(?:release|unhold|un-hold|resume|send\s+again|start\s+sending|can\s+send|ok\s+to\s+send)\b/i;
const HELD_LIST = /\b(?:held|holds|on\s+hold)\b/i;
const STATIONS = ["AUS", "ACT", "VCT", "BRO", "HRL", "SJT", "GGG", "TXK", "SHV", "AEX", "CXO", "SGR"];
const ROUTE = new RegExp(`\\b(${STATIONS.join("|")})(?:0?1)?[\\s-]*0*(\\d{1,3})(?!\\d)`, "gi");

export function parseHoldOrder(text) {
  const routes = [];
  for (const m of String(text).matchAll(ROUTE)) {
    const route = `${m[1].toUpperCase()}01-${m[2].padStart(3, "0")}`;
    if (!routes.includes(route)) routes.push(route);
  }
  if (RELEASE_VERB.test(text) && routes.length) return { action: "release", routes };
  if (HOLD_VERB.test(text) && routes.length) return { action: "hold", routes };
  if (HELD_LIST.test(text)) return { action: "list", routes: [] };
  return null;
}
