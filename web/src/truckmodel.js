// The self-judging truck forecast: how many boxes each station will have when its truck must leave.
//
// Evaluated by walk-forward replay (each forecast sees only what had happened by then). Against a plain
// pace forecast it was several times closer in the morning and about half the miss 5–7 hours before the
// truck, and on a replay of dispatch's bookings it warned on every night that needed more room than was
// booked.
//
// How it works: six simple methods, each a guess of tonight's boxes from what's bagged so far, and a
// mix that every night gives more weight to the methods that were closest lately:
//   rem10 / rem5 / rem14: boxes so far + the usual boxes still to come after this time (last 10 nights,
//     last 5, or the last 14 with newer nights counting more)
//   stationFix: rem10 shifted by half its typical miss at this station lately
//   hubFix: rem10 scaled by how far off it was across all stations over the last 3 nights (a hub-wide
//     surge, like peak season, shows here within a night or two)
//   usual: the station's usual final boxes
//   pace: rem10's still-to-come scaled by tonight's boxes so far vs the usual by this time (0.5–2x)
//   hubFix1: like hubFix, from last night alone (catches a jump a night sooner)
// pace and hubFix1, and hubFix reaching 0.6–1.8x instead of 0.8–1.35x, came from stress tests (replayed
// nights with a +50% peak, a ramp to 1.6x, a 1.7x one-night spike and a −40% slow spell): a little better in
// every shock, the same on normal nights.
// A forecast is for one station-night at a "slot": half hours before the truck's latest departure
// (slot 0 = departure, 47 = 23.5 hours before). curves = {station: {night: {b: [48 boxes or null], f}}}.

export const SLOTS = 48;
export const MODEL_KIND = "8-methods-sqrt-range"; // v2; a model built another way is rebuilt
export const MEMBERS = ["rem10", "rem5", "rem14", "stationFix", "hubFix", "usual", "pace", "hubFix1"];
// v3 (the stations only; cities and SGR01 keep v2 until tested there): 4 more methods for a rising season,
// and the mix's still-to-come scaled by half of how far the whole hub ran over or under lately. On a
// walk-forward replay (each night from earlier nights only) it ran less low in a rising season and made the
// right truck call slightly more often, with the 5-hours-out miss unchanged. Stress replays: better on a
// ramp to 1.6× and a +50% step, a little worse after a one-night 1.7× spike and a −40% slow spell.
export const V3 = { kind: "v3-12-methods-hub-debias", extraMinSlot: 12, debias: { nights: 5, k: 0.5, minSlot: 12, lo: 0.8, hi: 1.4 } };
export const MEMBERS_V3 = [...MEMBERS, "usualSt", "usualHub", "trendSt", "fullFix"];
const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
const HISTORY = 14;
const LOOKBACK = 10; // nights the mix and the warning range look back over
const DAY = 86400000;

export const addDays = (day, n) => new Date(Date.parse(`${day}T12:00:00Z`) + n * DAY).toISOString().slice(0, 10);
const median = (xs) => {
  const v = [...xs].sort((a, b) => a - b);
  if (!v.length) return null;
  return v.length % 2 ? v[(v.length - 1) / 2] : (v[v.length / 2 - 1] + v[v.length / 2]) / 2;
};
// numpy's default (linear) quantile
const quantile = (xs, q) => {
  const v = [...xs].sort((a, b) => a - b);
  const pos = (v.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return v[lo] + (v[hi] - v[lo]) * (pos - lo);
};

// Tonight's pace: rem10's still-to-come scaled by boxes so far vs the usual boxes by now (0.5–2x).
const pace = (p10, boxes, at) => (at == null || at < 3 || boxes < 3 ? p10 : boxes + (p10 - boxes) * Math.min(2, Math.max(0.5, boxes / at)));

export function forecaster(curves, opts = null) {
  const v3 = opts === V3;
  const memo = new Map();
  const remember = (key, make) => {
    if (!memo.has(key)) memo.set(key, make());
    return memo.get(key);
  };
  const stations = Object.keys(curves).sort();
  const night = (st, day) => {
    const r = curves[st]?.[day];
    return r && r.f != null ? r : null;
  };
  // Finished nights before `day`, newest first: up to 14, within 40 days.
  const hist = (st, day) => remember(`h|${st}|${day}`, () => {
    const out = [];
    for (let back = 1; back < 40 && out.length < HISTORY; back++) {
      const d = addDays(day, -back);
      const r = night(st, d);
      if (r) out.push(r);
    }
    return out;
  });
  // The usual boxes still to come after this slot: mean of (final − boxes then) over the first n nights.
  const rem = (h, slot, n, recency = 1) => {
    const rows = h.filter((r) => r.b[slot] != null).slice(0, n);
    if (!rows.length) return null;
    let sum = 0, weights = 0;
    rows.forEach((r, i) => { const w = recency ** i; sum += w * (r.f - r.b[slot]); weights += w; });
    return sum / weights;
  };
  // The station's own stats for night `day` (no tonight's boxes needed).
  const stats = (st, day, slot) => remember(`s|${st}|${day}|${slot}`, () => {
    const h = hist(st, day);
    if (h.length < 4) return null;
    return {
      n: h.length,
      r10: rem(h, slot, 10), r5: rem(h, slot, 5), r14: rem(h, slot, 14, 0.8),
      usual: median(h.slice(0, 10).map((r) => r.f)),
      at: usualAt(h, slot),
      fix: stationFix(st, day, slot),
      hub: hubFix(day, slot),
      hub1: hubFix1(day, slot),
      g: v3 ? growth(h) : 1, hg: v3 ? hubGrowth(day) : 1,
    };
  });
  // v3: a station's growth, its last 3 nights' finals over the 7 before them (0.7–1.6×), and the same over
  // the whole hub.
  const growth = (h) => {
    if (h.length < 8) return 1;
    const a = h.slice(0, 3).reduce((x, r) => x + r.f, 0) / 3;
    const b = h.slice(3, 10).reduce((x, r) => x + r.f, 0) / Math.min(7, h.length - 3);
    return b > 0 ? clamp(a / b, 0.7, 1.6) : 1;
  };
  const hubGrowth = (day) => remember(`g|${day}`, () => {
    let a = 0, b = 0;
    for (let i = 1; i <= 10; i++) {
      const d = addDays(day, -i);
      for (const st of stations) { const r = night(st, d); if (r) (i <= 3 ? (a += r.f / 3) : (b += r.f / 7)); }
    }
    return b > 0 ? clamp(a / b, 0.7, 1.6) : 1;
  });
  // The usual boxes by this slot: mean over the last 10 nights.
  const usualAt = (h, slot) => {
    const rows = h.slice(0, 10).filter((r) => r.b[slot] != null);
    return rows.length ? rows.reduce((a, r) => a + r.b[slot], 0) / rows.length : null;
  };
  // rem10's forecast for a finished night, from that night's own boxes at the slot.
  const pastRem10 = (st, day, slot) => remember(`p|${st}|${day}|${slot}`, () => {
    const r = night(st, day);
    const h = hist(st, day);
    if (!r || r.b[slot] == null || h.length < 4) return null;
    return r.b[slot] + Math.max(0, rem(h, slot, 10) ?? 0);
  });
  // Half of rem10's typical miss at this station over the last 7 nights (0 with fewer than 3).
  const stationFix = (st, day, slot) => remember(`f|${st}|${day}|${slot}`, () => {
    const misses = [];
    for (let i = 0; i < 7; i++) {
      const d = addDays(day, -1 - i);
      const q = pastRem10(st, d, slot);
      if (q != null) misses.push(night(st, d).f - q);
    }
    return misses.length >= 3 ? 0.5 * median(misses) : 0;
  });
  // How far off rem10 was across all stations over the last `nights` nights, as a ratio (1 with too few).
  const hubRatio = (day, slot, nights) => remember(`u${nights}|${day}|${slot}`, () => {
    let actual = 0, said = 0, pairs = 0;
    for (let i = 0; i < nights; i++) {
      const d = addDays(day, -1 - i);
      for (const st of stations) {
        const q = pastRem10(st, d, slot);
        if (q != null) { actual += night(st, d).f; said += q; pairs++; }
      }
    }
    if (pairs < 3) return 1;
    return Math.min(1.8, Math.max(0.6, actual / Math.max(1, said)));
  });
  const hubFix = (day, slot) => hubRatio(day, slot, 3);
  const hubFix1 = (day, slot) => hubRatio(day, slot, 1);
  // Each method's forecast for a station-night given its boxes at the slot.
  const members = (st, day, slot, boxes) => {
    const s = stats(st, day, slot);
    if (!s) return null;
    const up = (r) => boxes + Math.max(0, r ?? 0);
    const p10 = up(s.r10);
    return membersFrom(boxes, s.r10, s.r5, s.r14, s.fix, s.usual, s.at, s.hub, s.hub1, v3 ? s : null);
  };
  const names = v3 ? MEMBERS_V3 : MEMBERS;
  // How much the mix trusts each method for night `day`: closeness over the last 10 nights, newer counting more.
  const weights = (day, slot) => remember(`w|${day}|${slot}`, () => {
    const loss = names.map(() => 0);
    let total = 0;
    for (let back = 0; back < LOOKBACK; back++) {
      const d = addDays(day, -1 - back);
      for (const st of stations) {
        const r = night(st, d);
        if (!r || r.b[slot] == null) continue;
        const preds = members(st, d, slot, r.b[slot]);
        if (!preds) continue;
        const w = 0.9 ** back;
        total += w;
        preds.forEach((p, i) => { loss[i] += (w * Math.abs(p - r.f)) / Math.max(r.f, 5); });
      }
    }
    const on = (i) => i < MEMBERS.length || slot >= V3.extraMinSlot;
    if (!total) return names.map((_, i) => (on(i) ? 1 : 0));
    const best = Math.min(...loss.filter((_, i) => on(i))) / total;
    return loss.map((l, i) => (on(i) ? Math.exp(-10 * (l / total - best)) : 0));
  });
  const rawMix = (st, day, slot, boxes) => {
    const preds = members(st, day, slot, boxes);
    if (!preds || hist(st, day).length < 5) return null;
    const w = weights(day, slot);
    return preds.reduce((a, p, i) => a + w[i] * p, 0) / w.reduce((a, b) => a + b, 0);
  };
  // v3: how far the mix's still-to-come ran over (or under) across the hub over the last 5 nights, at this
  // slot; half of it is applied, 0.8–1.4×, from 6 hours before the truck back.
  const mixRatio = (day, slot) => remember(`m|${day}|${slot}`, () => {
    const d3 = V3.debias;
    if (!v3 || slot < d3.minSlot) return 1;
    let a = 0, b = 0;
    for (let i = 0; i < d3.nights; i++) {
      const d = addDays(day, -1 - i);
      for (const st of stations) {
        const r = night(st, d);
        if (!r || r.b[slot] == null) continue;
        const q = rawMix(st, d, slot, r.b[slot]);
        if (q != null) { a += r.f - r.b[slot]; b += q - r.b[slot]; }
      }
    }
    return clamp(1 + d3.k * ((b > 2 ? a / b : 1) - 1), d3.lo, d3.hi);
  });
  const mix = (st, day, slot, boxes) => {
    const m = rawMix(st, day, slot, boxes);
    return m == null ? m : boxes + (m - boxes) * mixRatio(day, slot);
  };
  // The range: how far the mix was off over the last 10 nights (every station, same time before the truck),
  // as misses in boxes over the square root of the forecast, so a small station's 5-box swing doesn't widen
  // a big station's range (counts vary about like the square root of their size). On replayed nights this
  // kept the warnings' catches and cut false alarms 5 hours out by about half against a plain ratio range.
  // hi/lo: 85th/15th percentile. plan: the 70th,
  // what the 9 AM truck advice is sized for (with 4 boxes allowed to wait: right 85%, short 6%, one too many 9%).
  const range = (day, slot) => remember(`r|${day}|${slot}`, () => {
    const z = [];
    for (let back = 0; back < LOOKBACK; back++) {
      const d = addDays(day, -1 - back);
      for (const st of stations) {
        const r = night(st, d);
        if (!r || r.b[slot] == null) continue;
        const p = mix(st, d, slot, r.b[slot]);
        if (p != null) z.push((r.f - p) / Math.sqrt(Math.max(p, 4)));
      }
    }
    if (z.length < 15) return { hi: 1.5, lo: -1.5, plan: 0.8 };
    return { hi: Math.max(0, quantile(z, 0.85)), lo: Math.min(0, quantile(z, 0.15)), plan: Math.max(0, quantile(z, 0.7)) };
  });
  return { hist, stats, weights, mix, range, hubFix, hubFix1, mixRatio, hubGrowth, stations };
}

// Each method's forecast. v3 (the night's stats with g/hg) adds usualSt, usualHub, trendSt and fullFix.
function membersFrom(boxes, r10, r5, r14, fix, usual, at, hub, hub1, v3) {
  const up = (r) => boxes + Math.max(0, r ?? 0);
  const p10 = up(r10);
  const base = [p10, up(r5), up(r14), p10 + fix, p10 * hub, Math.max(usual ?? boxes, boxes), pace(p10, boxes, at), p10 * hub1];
  if (!v3) return base;
  const u = usual ?? boxes;
  return [...base, Math.max(u * v3.g, boxes), Math.max(u * v3.hg, boxes), boxes + Math.max(0, r10 ?? 0) * v3.g, p10 + 2 * fix];
}

// What the worker keeps in KV for the night: per station and slot, the numbers that don't depend on tonight's
// boxes, so each 15-minute check only adds tonight's boxes. `day` is the night they're for.
export function buildModel(curves, day, opts = null) {
  const v3 = opts === V3;
  const f = forecaster(curves, opts);
  const st = {};
  for (const name of f.stations) {
    const rows = [];
    for (let slot = 0; slot < SLOTS; slot++) {
      const s = f.stats(name, day, slot);
      rows.push(s && s.n >= 5 ? [s.r10, s.r5, s.r14, s.fix, s.usual, s.at, ...(v3 ? [s.g] : [])].map((x) => (x == null ? null : Math.round(x * 1000) / 1000)) : null);
    }
    st[name] = rows;
  }
  const slots = [];
  for (let slot = 0; slot < SLOTS; slot++) {
    const { hi, lo, plan } = f.range(day, slot);
    slots.push({ w: f.weights(day, slot).map((x) => Math.round(x * 1e6) / 1e6), hub: Math.round(f.hubFix(day, slot) * 1e4) / 1e4,
      hub1: Math.round(f.hubFix1(day, slot) * 1e4) / 1e4,
      hi: Math.round(hi * 1e4) / 1e4, lo: Math.round(lo * 1e4) / 1e4, plan: Math.round(plan * 1e4) / 1e4,
      ...(v3 ? { mr: Math.round(f.mixRatio(day, slot) * 1e4) / 1e4, hg: Math.round(f.hubGrowth(day) * 1e4) / 1e4 } : {}) });
  }
  return { day, kind: v3 ? V3.kind : MODEL_KIND, st, slots };
}

// Tonight's forecast from the kept numbers: {point, low, high, plan} boxes, or null without enough history.
export function forecastFromModel(model, station, slot, boxes) {
  const row = model?.st?.[station]?.[slot];
  const at = model?.slots?.[slot];
  const v3 = model?.kind === V3.kind;
  if (!row || !at || (model.kind !== MODEL_KIND && !v3)) return null;
  const [r10, r5, r14, fix, usual, usualAt, g] = row;
  const preds = membersFrom(boxes, r10, r5, r14, fix, usual, usualAt, at.hub, at.hub1, v3 ? { g: g ?? 1, hg: at.hg ?? 1 } : null);
  const raw = preds.reduce((a, p, i) => a + at.w[i] * p, 0) / at.w.reduce((a, b) => a + b, 0);
  const point = v3 ? boxes + (raw - boxes) * (at.mr ?? 1) : raw;
  const spread = Math.sqrt(Math.max(point, 4));
  return { point, high: point + at.hi * spread + 0.5, low: Math.max(boxes, point + at.lo * spread - 0.5), plan: point + at.plan * spread + 0.5 };
}
