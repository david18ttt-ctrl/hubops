// Sends one notification to every phone that turned on notifications in the app (web/src/app.js).
// Usage: node send.js '{"title": "...", "body": "...", "tag": "..."}'
// Reads the phones' subscriptions from the app worker (X-Push-Key), signs with the VAPID private key that only
// lives on the ops host, and tells the worker to forget phones the push service says are gone.
// Prints "sent N of M"; exits 1 when there was no phone to send to or every send failed.
const webpush = require("web-push");
const { config } = require("./config");

async function main() {
  const payload = process.argv[2];
  if (!payload) throw new Error("no message");
  const { app, key, vapid, subject } = config();
  if (!vapid.publicKey || !vapid.privateKey) throw new Error("set HUBOPS_VAPID_PUBLIC and HUBOPS_VAPID_PRIVATE");
  webpush.setVapidDetails(subject, vapid.publicKey, vapid.privateKey);
  const res = await fetch(app + "/app-subs", { headers: { "x-push-key": key } });
  if (!res.ok) throw new Error("app worker said " + res.status);
  const subs = await res.json();
  const gone = [];
  let sent = 0;
  for (const sub of subs) {
    try {
      await webpush.sendNotification(sub, payload, { TTL: 600, urgency: "high" });
      sent++;
    } catch (e) {
      if (e.statusCode === 404 || e.statusCode === 410) gone.push(sub.endpoint);
      else console.error("push failed: " + (e.statusCode || "") + " " + (e.body || e.message));
    }
  }
  if (gone.length) {
    await fetch(app + "/app-subs", { method: "POST", headers: { "x-push-key": key, "content-type": "application/json" },
      body: JSON.stringify({ remove: gone }) });
  }
  console.log(`sent ${sent} of ${subs.length}`);
  if (!sent) process.exit(1);
}
main().catch((e) => { console.error(e.message); process.exit(1); });
