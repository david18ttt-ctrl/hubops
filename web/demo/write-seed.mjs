// Writes demo/seed.json: one snapshot of the demo KV data (src/demo.js), for reading or for loading into a
// local KV namespace with `wrangler kv bulk put --local`. Usage: node demo/write-seed.mjs
import { writeFileSync } from "node:fs";
import { demoSeed } from "../src/demo.js";

const seed = await demoSeed();
const bulk = Object.entries(seed).filter(([, v]) => typeof v === "string").map(([key, value]) => ({ key, value }));
writeFileSync(new URL("./seed.json", import.meta.url), JSON.stringify(bulk, null, 1) + "\n");
console.log(`demo/seed.json: ${bulk.length} keys`);
