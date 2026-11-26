// Turns new app alerts into phone notifications (run every minute on the ops host, e.g. from cron or launchd).
// The alerts are added by the worker and the ops host (web/src/appalert.js); this reads the ones not pushed
// yet, sends each to every phone that turned notifications on, and marks them pushed.
// Quiet when there's nothing new. Prints one line per alert.
const path = require("path");
const { execFileSync } = require("child_process");
const { config } = require("./config");

// Too many notifications get ignored, so only alerts that need someone's hands or a reply buzz. Things to
// physically do go through the Tasks list (hubops/do_now.py), which buzzes once per new task; everything else
// (feed stations, truck space and truck news, system warnings, the shift card) lands in the Alerts tab quietly.
// "Chat Q": someone asked a question that has a suggested answer to send. Direct chat messages are not
// repeated here: the team chat's own app already notifies for those.
const BUZZ = [/^Chat Q\b/, /^Closing report\b/, /^Headcount sheet\b/, /^Before you leave\b/, /^WMS login expired\b/];
function buzzes(a) {
  return BUZZ.some((re) => re.test(a.title || "")) || !!a.link; // a.link: the chat reader judged a message needs a reply
}
module.exports = { buzzes };

async function main() {
  const { app, key } = config();
  const res = await fetch(app + "/app-alerts-pending", { headers: { "x-push-key": key } });
  if (!res.ok) throw new Error("app worker said " + res.status);
  const pending = await res.json();
  if (!pending.length) return;
  const done = [];
  for (const a of pending) {
    if (!buzzes(a)) {
      console.log(new Date().toISOString(), "quiet:", a.title);
      done.push(a.id);
      continue;
    }
    const body = a.text.length > 300 ? a.text.slice(0, 297) + "…" : a.text;
    try {
      execFileSync(process.execPath, [path.join(__dirname, "send.js"), JSON.stringify({ title: a.title, body })], { stdio: "pipe" });
      console.log(new Date().toISOString(), "pushed:", a.title);
    } catch (e) {
      console.error(new Date().toISOString(), "not pushed:", a.title, String(e.stderr || e.message).trim().slice(0, 200));
    }
    done.push(a.id); // Marked either way: the alert stays in the app's list
  }
  await fetch(app + "/app-alerts-pushed", { method: "POST", headers: { "x-push-key": key, "content-type": "application/json" },
    body: JSON.stringify({ ids: done }) });
}
if (require.main === module) main().catch((e) => { console.error(new Date().toISOString(), e.message); process.exit(1); });
