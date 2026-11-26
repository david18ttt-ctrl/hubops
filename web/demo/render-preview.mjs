// Renders the pages with demo data into stand-alone HTML files under docs/ (no worker or network needed):
// docs/app-preview.html (the phone app, its /data answered from the embedded snapshot), docs/trucks-preview.html,
// docs/unsorted-preview.html and docs/pda-preview.html. Usage: node demo/render-preview.mjs
import { writeFileSync } from "node:fs";
import { appPage } from "../src/app.js";
import { page as pdaPage } from "../src/dashboard.js";
import { trucksPage, publicPage } from "../src/trucksboard.js";
import { unsortedPage } from "../src/unsorted.js";
import { demoSeed } from "../src/demo.js";

const seed = await demoSeed();
const kv = (k) => (seed[k] ? JSON.parse(seed[k]) : null);
const out = (name, html) => writeFileSync(new URL(`../../docs/${name}`, import.meta.url), html);

// The app fetches <base>/data; the preview answers it from the snapshot and keeps every other request local.
const alerts = kv("app_alerts") || [];
const data = { chutes: kv("app_chutes"), feeder: kv("app_feeder"), floor: kv("app_floor"), donow: kv("app_donow"), done: [], fixed: [],
  alerts: alerts.map(({ id, at, updated, title, text, reply, link, image, ref, chat, asking }) => ({ id, at, updated, title, text, reply, link, image, ref, chat, asking })) };
const shim = `<script>
(() => {
  const DATA = ${JSON.stringify(data).replace(/</g, "\\u003c")};
  const real = window.fetch.bind(window);
  window.fetch = (url, init) => String(url).endsWith("/data") ? Promise.resolve(new Response(JSON.stringify(DATA), { headers: { "content-type": "application/json" } }))
    : String(url).startsWith("/a/") ? Promise.resolve(new Response("{}", { status: 200 })) : real(url, init);
  try { localStorage.removeItem("app_data"); } catch (e) {}
})();
</script>`;
const links = [{ name: "Unsorted", icon: "▤", url: "unsorted-preview.html" }, { name: "Trucks", icon: "⛟", url: "trucks-preview.html" },
  { name: "PDA", icon: "▦", url: "pda-preview.html" }];
// Tab links are relative files here; the worker itself only takes https links or its own paths.
out("app-preview.html", appPage("/a/demo", [], "").replace("<head>", "<head>\n" + shim)
  .replace('const CFG = {"base":"/a/demo","links":[]', 'const CFG = {"base":"/a/demo","links":' + JSON.stringify(links)));
const state = kv("truckspace");
out("trucks-preview.html", publicPage(trucksPage(state.board, state.city?.board || null, null)));
out("unsorted-preview.html", unsortedPage(kv("unsorted_board")));
out("pda-preview.html", pdaPage(kv("pda_board"), kv("pda_history"), kv("pda_errors"), null, {}));
console.log("docs/app-preview.html, trucks-preview.html, unsorted-preview.html, pda-preview.html");
