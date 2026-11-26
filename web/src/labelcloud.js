// The label station's WMS lookup, in the worker: the same steps and rules as hubops/label_station.py lookup() on
// the ops host, used only when the host isn't answering (off, or a request waiting longer than CLOUD_AFTER).
// Read-only in the WMS.
import { TIME_ZONE, wmsHeaders, wmsUrl } from "./hubtime.js";

const ROUTE_API = "/api/parcels/route"; // parcel -> the bag it is in now
const BAGS_API = "/api/bags/search"; // bag search
const HUB_ID = 1;
const HUB = "HOU.H";
const SORTER_BAG = 2;
const TINY_PCS = 20; // a bag under this many packages is "near-empty"
const CHAIN = 15 * 60 * 1000; // near-empty bags closed this soon after a real box on the same chute belong to it
const LONG_LOOK_BACK = 14 * 3600 * 1000; // a full box sits open 2-7 hours, sometimes more

export class LoginExpired extends Error {}

async function wms(env, path, body) {
  const res = await fetch(wmsUrl(env, path), { method: "POST", headers: wmsHeaders(env), body: JSON.stringify(body) });
  if (res.status === 401) throw new LoginExpired("WMS login expired");
  const p = await res.json().catch(() => ({}));
  if (p.code === 401) throw new LoginExpired("WMS login expired");
  if (p.code !== 0 && p.code !== 200) throw new Error("WMS returned code " + (p.code ?? res.status) + " " + (p.msg || ""));
  return p.data || {};
}

// "YYYY-MM-DD HH:MM:SS" in hub time (how the WMS takes and shows times).
export function local(ms) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: TIME_ZONE, hourCycle: "h23",
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
    .formatToParts(new Date(ms)).map((p) => [p.type, p.value]));
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

export const chuteOf = (bag) => String(bag.packageNo || "").split("-").pop().replace(/^0+/, "");
const made = (b) => b.optCreateTime || b.optFinishStatusTime || 0;
const small = (b) => (b.waybillCount || 0) < TINY_PCS;

async function readBags(env, nowMs, back) {
  const body = { packageNoList: [], createBeginTime: local(nowMs - back), createEndTime: local(nowMs), startCenterId: HUB_ID, pageSize: 1000 };
  let bags = [];
  for (let page = 1; page < 10; page++) {
    const got = (await wms(env, BAGS_API, { ...body, pageNum: page })).list || [];
    bags = bags.concat(got);
    if (got.length < 1000) break;
  }
  return bags;
}

// Whether `bag` is the last bag opened on its chute: its label is the chute's label for the box filling now.
export function newestOnChute(bag, bags) {
  const chute = chuteOf(bag);
  const same = bags.concat([bag]).filter((b) => b.bagType === SORTER_BAG && chuteOf(b) === chute);
  if (!same.length) return false;
  return same.reduce((a, b) => (made(b) > made(a) ? b : a)).packageNo === bag.packageNo;
}

// A real box + the near-empty bags closed right after it on the same chute (extra presses of the print button):
// one box. Same rule as label_station.press_chain.
export function pressChain(bag, bags) {
  if (!bag.optFinishStatusTime) return [bag];
  const chute = chuteOf(bag);
  const byNo = new Map();
  for (const b of bags) if (b.bagType === SORTER_BAG && b.optFinishStatusTime && chuteOf(b) === chute) byNo.set(b.packageNo, b);
  const same = [...byNo.values()].sort((a, b) => a.optFinishStatusTime - b.optFinishStatusTime);
  const i = same.findIndex((b) => b.packageNo === bag.packageNo);
  if (i < 0) return [bag];
  let j = i;
  while (j >= 0 && small(same[j])) j--;
  if (j < 0) return [bag];
  const box = same[j], chain = [box];
  for (const b of same.slice(j + 1)) {
    if (!small(b) || b.optFinishStatusTime - box.optFinishStatusTime > CHAIN) break;
    chain.push(b);
  }
  return chain.some((b) => b.packageNo === bag.packageNo) ? chain : [bag];
}

// label_station.lookup: {label} or {why}.
export async function lookup(env, codes, nowMs = Date.now()) {
  const res = await wms(env, ROUTE_API, { waybillNoList: codes, pageNum: 1, pageSize: 20 });
  const rows = new Map((res.list || []).map((r) => [r.waybillNo, r]));
  let code = codes.find((c) => rows.has(c));
  if (!code && rows.size) code = rows.keys().next().value;
  if (!code) return { why: `The WMS doesn't know package ${codes[0]}` };
  const bagNo = rows.get(code).packageNo;
  if (!bagNo) return { why: `Package ${code} isn't in a bag in the WMS yet (not sorted?)` };
  const got = (await wms(env, BAGS_API, { packageNoList: [bagNo], startCenterId: HUB_ID, pageNum: 1, pageSize: 10 })).list || [];
  const bag = got.find((b) => b.packageNo === bagNo);
  if (!bag || bag.startCenterId !== HUB_ID) return { why: `Package ${code} is in ${bagNo}, which isn't a ${HUB} bag` };
  if (bag.optCheckOutTime) return { why: `Bag ${bagNo} already left on a truck (${local(bag.optCheckOutTime)})` };
  let keep = bag, chain = [bag], note = "";
  if (!bag.optFinishStatusTime) {
    // Still open in the WMS: only a label if a newer bag started on that chute (the button WAS pressed).
    let pressed = false;
    if (bag.bagType === SORTER_BAG && bag.optCreateTime) {
      const back = Math.min(nowMs - bag.optCreateTime + 5 * 60000, LONG_LOOK_BACK);
      pressed = !newestOnChute(bag, await readBags(env, nowMs, back));
    }
    if (!pressed) {
      return { why: `La caja sigue abierta (rampa ${chuteOf(bag)} · ${bag.waybillCount || 0} pcs). ` +
        "Si no has presionado el botón, presiónalo UNA vez. Si ya lo presionaste, espera 5 minutos y vuelve a escanear. · " +
        "Box still open: if you didn't press the button, press it ONCE. If you already did, wait 5 minutes and scan again." };
    }
  }
  if (small(bag) && bag.bagType === SORTER_BAG && bag.optFinishStatusTime) {
    const recent = await readBags(env, bag.optFinishStatusTime + 60000, LONG_LOOK_BACK);
    chain = pressChain(bag, recent.concat([bag]));
    keep = chain.reduce((a, b) => ((b.waybillCount || 0) > (a.waybillCount || 0) ? b : a));
    if (keep !== bag) note = `Package was in near-empty bag ${bagNo}; label is for the real box`;
  }
  const route = keep.exchangeBureauCode || keep.destinAreaCode || keep.destinCenterName || "";
  return { label: { bag: keep.packageNo, route, dest: keep.destinCenterName || "", chute: chuteOf(keep),
    pcs: chain.reduce((s, b) => s + (b.waybillCount || 0), 0),
    date: keep.optCreateTime ? local(keep.optCreateTime) : keep.createTime || "", origin: keep.startCenterName || HUB,
    operator: keep.createByName || "", merge: chain.filter((b) => b.packageNo !== keep.packageNo).map((b) => b.packageNo),
    pkg: code, note: note || "Looked up by the worker (ops host off)" } };
}
