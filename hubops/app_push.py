"""The tools' side of the phone app (web/src/app.js).

data(part, body)  -> the app shows it (POST /app-data/<part>, part = "chutes", "feeder", "donow", ...)
alert(title, text, key, image) -> the Alerts tab + a phone notification (scripts/webpush/flush.js, every minute)
notify(title, body, tag) -> a notification on every phone that turned them on, right now (scripts/webpush/send.js)

    HUBOPS_APP_URL   the app's base URL (the deployed worker). Not set = demo mode: everything is written
                     under data/app/ and printed instead of sent.
    HUBOPS_APP_KEY   the push key the app expects in X-Push-Key.
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request

import hubconfig

HERE = hubconfig.HERE
APP = (os.environ.get("HUBOPS_APP_URL") or "").rstrip("/") or None  # None = demo mode
SENDER = hubconfig.REPO / "scripts" / "webpush" / "send.js"
NODE = os.environ.get("HUBOPS_NODE") or shutil.which("node") or "node"
USER_AGENT = "hubops-app"


def demo():
    return APP is None


def push_key():
    return os.environ.get("HUBOPS_APP_KEY") or ""


def _demo_write(name, record):
    """Demo mode: keep what would have been sent, one JSON line per send, under data/app/."""
    with hubconfig.data_path("app", name).open("a") as f:
        f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), **record}, ensure_ascii=False) + "\n")


def _post(path, data, content_type="application/json", timeout=30):
    req = urllib.request.Request(f"{APP}{path}", data=data, method="POST",
                                 headers={"Content-Type": content_type, "User-Agent": USER_AGENT, "X-Push-Key": push_key()})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def request(path, body=None):
    """GET (body None) or POST JSON to the app; the answer parsed as JSON when it is JSON. In demo mode a GET
    answers [] (nothing pressed in the app) and a POST to /app-data/<part> goes to data()."""
    if demo():
        if body is None:
            return []
        if path.startswith("/app-data/"):
            data(path.rsplit("/", 1)[-1], body)
        else:
            _demo_write("requests.jsonl", {"path": path, "body": body})
        return "OK"
    req = urllib.request.Request(APP + path, method="POST" if body is not None else "GET",
                                 data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT,
                                          "X-Push-Key": push_key()})
    with urllib.request.urlopen(req, timeout=30) as res:
        text = res.read().decode()
    try:
        return json.loads(text or "null")
    except ValueError:  # POST /app-data answers a plain "OK"
        return text


def data(part, body):
    if demo():
        path = hubconfig.data_path("app", f"{part}.json")
        path.write_text(json.dumps(body, ensure_ascii=False, indent=1))
        print(f"[app] {part} updated -> {path.relative_to(hubconfig.REPO)}")
        return
    _post(f"/app-data/{part}", json.dumps(body, ensure_ascii=False).encode())


def image(name, jpeg):
    """Stores a picture (JPEG bytes, under 3 MB) for alert(..., image=name); kept 3 days. name: a-z, 0-9, -."""
    if demo():
        hubconfig.data_path("app", "img", f"{name}.jpg").write_bytes(jpeg)
        return
    _post(f"/app-img/{name}", jpeg, content_type="image/jpeg", timeout=60)


def alert(title, text, key=None, image=None, reply=None, link=None):
    """A private alert: it shows in the app's Alerts tab and the phone gets a notification within a minute
    (scripts/webpush/flush.js). Same key = update that alert in place, quietly. image: a name stored with
    image() first, shown under the text. reply: a suggested text to send, shown as "Suggested reply"; with
    link (the chat's deep link) the app adds a "Copy reply & open chat" button."""
    body = {"title": title, "text": text, **({"key": key} if key else {}), **({"image": image} if image else {}),
            **({"reply": reply} if reply else {}), **({"link": link} if link else {})}
    if demo():
        _demo_write("alerts.jsonl", body)
        print(f"[app alert] {title}: {text}")
        return
    _post("/app-alert", json.dumps(body, ensure_ascii=False).encode())


def notify(title, body, tag=None):
    """Returns the sender's "sent N of M" line; raises if no phone got it."""
    payload = {"title": title, "body": body, **({"tag": tag} if tag else {})}
    if demo() or not SENDER.exists():
        _demo_write("notifications.jsonl", payload)
        print(f"[phone] {title}: {body}")
        return "sent 0 of 0 (demo mode)"
    run = subprocess.run([NODE, str(SENDER), json.dumps(payload, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    if run.returncode:
        raise RuntimeError(f"phone notification not sent: {(run.stderr or run.stdout).strip()[:200]}")
    return run.stdout.strip()
