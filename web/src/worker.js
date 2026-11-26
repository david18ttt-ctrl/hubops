// The hub-ops worker: one Cloudflare Worker that serves the phone app (app.js), dispatch's truck page
// (trucksboard.js), the unsorted truckloads board (unsorted.js) and the PDA speed board (dashboard.js), takes
// the ops host's pushes, and runs the cron jobs that keep the truck-space forecast current (truckspace.js,
// truckcity.js, trucklocal.js).
//
// Routes (each page behind its own long view key, see wrangler.toml for the secret names):
//   /a/<APP_VIEW_KEY>/…          phone app            /app-data/*, /app-alert…  ops host pushes (X-Push-Key)
//   /trucks/<TRUCKS_VIEW_KEY>    truck page           /limit-news/<key>         chat reader's limit news
//   /u/<UNSORTED_VIEW_KEY>       unsorted board       /unsorted-data, -lists    ops host pushes
//   /pda/<PDA_VIEW_KEY>          PDA board            /pda-data, /pda-errors…   ops host pushes
//   POST /ops/command            short operator commands ("hold st07 003", "bulk not coming"), X-Push-Key
//   /s/<LABEL_SCAN_KEY>          label station scan link + sticker page; /p/<LABEL_PRINT_KEY> print queue for the
//                                print agent (agent/); /label-jobs the ops host's lookups (labelstation.js, D1)
// With DEMO = "true" (local dev), the worker fills KV with synthetic data (demo.js) and serves an index at "/".
import app from "./app.js";
import { handleDashboard } from "./dashboard.js";
import { handleTrucksBoard } from "./trucksboard.js";
import { handleUnsorted } from "./unsorted.js";
import { handleLimitNews, refreshTruckModel, runTruckSpace } from "./truckspace.js";
import { refreshCityModel } from "./truckcity.js";
import { refreshLocalModel } from "./trucklocal.js";
import { savePdaCounts } from "./pda-live.js";
import { handleCommand } from "./commands.js";
import { handleLabelStation } from "./labelstation.js";
import { demoEnv, demoIndex, seedDemo } from "./demo.js";

const HOURLY_CRON = "0 * * * *";

const HANDLERS = [handleCommand, handleLabelStation, handleDashboard, handleTrucksBoard, handleLimitNews, handleUnsorted];

export default {
  async fetch(request, env, ctx) {
    if (env.DEMO === "true") {
      env = demoEnv(env);
      await seedDemo(env);
      if (new URL(request.url).pathname === "/") return demoIndex(env);
    }
    for (const handle of HANDLERS) {
      const res = await handle(request, env);
      if (res) return res;
    }
    return app.fetch(request, env, ctx);
  },

  async scheduled(event, env) {
    await runSchedule(event, env);
  },
};

async function runSchedule(event, env) {
  if (event.cron === HOURLY_CRON) {
    // After a station's truck leaves, the truck forecast learns that night (its own run: the rebuild is the
    // heaviest thing the worker computes).
    await refreshTruckModel(env).catch((error) => console.error(error));
    await refreshCityModel(env).catch((error) => console.error(error));
    await refreshLocalModel(env).catch((error) => console.error(error));
    return;
  }
  // Every 5 minutes: the truck-space check (it runs itself every 15) and the PDA counts copy. A problem with
  // one shouldn't stop the other.
  const results = await Promise.allSettled([runTruckSpace(env), savePdaCounts(env)]);
  const failed = results.find((result) => result.status === "rejected");
  if (failed) throw failed.reason;
}
