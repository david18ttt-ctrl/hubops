// Renders the label station's pages into stand-alone HTML files under docs/ (no worker or network needed):
// docs/label-station-preview.html (the scan link; its requests answered by a small in-page stand-in that plays
// one lookup through to "Printed"), docs/label-stickers-preview.html and docs/label-printer-preview.html.
// Usage: node demo/render-label-preview.mjs
import { writeFileSync } from "node:fs";
import { scanPage, printStationPage } from "../src/labelstation.js";
import { stickersPage } from "../src/stickerpage.js";

const out = (name, html) => writeFileSync(new URL(`../../docs/${name}`, import.meta.url), html);

// A box the simulator's night has (hubops/label_station.py --lookup PKG261125689488444 gives the same facts).
const LABEL = { bag: "BG012611251011-043", route: "VCT01-017", dest: "VCT01", chute: "43", pcs: 77, date: "2026-11-25 21:36:03",
  origin: "HOU.H", operator: "Sorter 1", merge: [], pkg: "PKG261125689488444", note: "" };
const shim = (base) => `<script>
(() => {
  const LABEL = ${JSON.stringify(LABEL)};
  let asked = 0, at = Date.now();
  const ok = (data) => Promise.resolve(new Response(JSON.stringify(data), { headers: { "content-type": "application/json" } }));
  const real = window.fetch.bind(window);
  window.fetch = (url, init) => {
    const u = String(url);
    if (u === ${JSON.stringify(base)} + "/req") { at = Date.now(); asked = 0; return ok({ id: 1 }); }
    if (u === ${JSON.stringify(base)} + "/req/1") {
      asked++; // first answer: still looking; then looked up and printed
      return ok({ id: 1, code: LABEL.pkg, at, status: asked < 2 ? "new" : "done", msg: "", label: LABEL,
        printedAt: asked < 2 ? null : Date.now(), host: true, printer: true });
    }
    if (u.endsWith("/stickers/status")) return ok({ printer: true });
    if (u.endsWith("/sticker")) return ok({ ok: true, name: "sticker", copies: 1, printer: true });
    if (u.startsWith(${JSON.stringify(base)})) return ok({ ok: true });
    return real(url, init);
  };
})();
</script>`;
const base = "/s/demo-scan-0000000000";
out("label-station-preview.html", scanPage(base).replace("<head>", "<head>\n" + shim(base))
  .replace(`href="${base}/stickers#time"`, 'href="label-stickers-preview.html#time"').replace(`href="${base}/stickers"`, 'href="label-stickers-preview.html"'));
out("label-stickers-preview.html", stickersPage(base).replace("<head>", "<head>\n" + shim(base)).replace(`href="${base}"`, 'href="label-station-preview.html"'));
out("label-printer-preview.html", printStationPage("/p/demo-print-000000000", "https://hub.example/s/demo-scan-0000000000")
  .replace("<head>", "<head>\n" + `<script>window.fetch = () => Promise.resolve(new Response(JSON.stringify({ version: "", queue: [], recent: [] })));</script>`));
console.log("docs/label-station-preview.html, label-stickers-preview.html, label-printer-preview.html");
