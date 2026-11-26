// Shared settings for the push scripts, all from the environment (the same names hubops/app_push.py reads):
//   HUBOPS_APP_URL        the app worker's base URL, e.g. https://hubops-app.<you>.workers.dev
//   HUBOPS_APP_KEY        the push key the worker expects in X-Push-Key (wrangler secret APP_PUSH_KEY)
//   HUBOPS_VAPID_PUBLIC / HUBOPS_VAPID_PRIVATE   the Web Push key pair (npx web-push generate-vapid-keys);
//                         the private key stays on the ops host, the public one is the worker's VAPID_PUBLIC
//   HUBOPS_VAPID_SUBJECT  contact for the push services (default mailto:ops@example.com)
function config() {
  const app = (process.env.HUBOPS_APP_URL || "").replace(/[/]+$/, "");
  const key = process.env.HUBOPS_APP_KEY || "";
  if (!app || !key) throw new Error("set HUBOPS_APP_URL and HUBOPS_APP_KEY");
  return { app, key, vapid: { publicKey: process.env.HUBOPS_VAPID_PUBLIC || "", privateKey: process.env.HUBOPS_VAPID_PRIVATE || "" },
    subject: process.env.HUBOPS_VAPID_SUBJECT || "mailto:ops@example.com" };
}
module.exports = { config };
