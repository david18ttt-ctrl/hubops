#!/usr/bin/env python3
"""Team chat adapter: the few chat-platform calls the tools make, without tying them to one product.

    call(method, path, token=None, body=None)   one request in the chat platform's REST style
    get_token()                                  the app's access token
    send_text(text, to=None)                     a plain message
    send_image(path, text=None, to=None)         a picture (PNG) with a caption

Two modes:
- HUBOPS_CHAT_WEBHOOK set: messages are POSTed there as {"text": ..., "to": ...} (a Slack/Teams style incoming
  webhook, or any small relay).
- not set (demo mode): messages are printed to stdout and appended to data/chat-outbox.jsonl.

Shared tables (for example dispatch's departure log) are read from HUBOPS_TABLES_URL, which defaults to the WMS
simulator; a `/bitable/.../records/search` request is answered from its /api/dispatch/departures list.

    python3 chat_api.py --check         token + one message in the current mode
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import hubconfig

WEBHOOK = os.environ.get("HUBOPS_CHAT_WEBHOOK") or None
TABLES_URL = (os.environ.get("HUBOPS_TABLES_URL") or os.environ.get("HUBOPS_WMS_URL") or "http://127.0.0.1:8765").rstrip("/")
APP_ID = os.environ.get("HUBOPS_CHAT_APP_ID", "hubops-demo")
SECRET_FILE = hubconfig.DATA_DIR / ".chat-secret"  # optional; HUBOPS_CHAT_SECRET wins
OUTBOX = "chat-outbox.jsonl"
DEMO_TOKEN = "demo-chat-token"
TOKEN_TTL = 7200


class ChatError(Exception):
    pass




def _secret():
    secret = os.environ.get("HUBOPS_CHAT_SECRET")
    if secret:
        return secret
    try:
        return SECRET_FILE.read_text().strip()
    except OSError:
        return ""


def _post_json(url, body, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "hubops"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            text = res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        raise ChatError(f"POST {url}: HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ChatError(f"POST {url}: {e}") from None
    try:
        return json.loads(text or "{}")
    except ValueError:
        return {"text": text}


def _message_text(body):
    """The plain text of a message body ({"msg_type": "text", "content": "{\"text\": ...}"} or {"text": ...})."""
    if not isinstance(body, dict):
        return str(body)
    content = body.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return content
    if isinstance(content, dict):
        return str(content.get("text") or json.dumps(content, ensure_ascii=False))
    return str(body.get("text") or "")


def send_text(text, to=None):
    """One plain message: to the webhook, or printed and kept in data/chat-outbox.jsonl."""
    record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "to": to, "text": str(text), "id": uuid.uuid4().hex}
    if WEBHOOK:
        _post_json(WEBHOOK, {"text": record["text"], **({"to": to} if to else {})})
    else:
        with hubconfig.data_path(OUTBOX).open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"[team chat{' -> ' + to if to else ''}] {record['text']}")
    return {"message_id": record["id"]}


def send_image(path, text=None, to=None):
    """A picture (PNG) with an optional caption: to the webhook as base64, or printed with the picture's path and
    kept in data/chat-outbox.jsonl."""
    path = Path(path)
    picture = path.read_bytes()
    try:
        shown = str(path.resolve().relative_to(hubconfig.REPO))
    except ValueError:
        shown = str(path)
    record = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "to": to, "text": str(text or ""), "image": shown,
              "bytes": len(picture), "id": uuid.uuid4().hex}
    if WEBHOOK:
        _post_json(WEBHOOK, {"text": record["text"], "image_png_base64": base64.b64encode(picture).decode("ascii"),
                             "filename": path.name, **({"to": to} if to else {})}, timeout=60)
    else:
        with hubconfig.data_path(OUTBOX).open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        caption = f" {record['text']}" if record["text"] else ""
        print(f"[team chat{' -> ' + to if to else ''}] [image {shown}]{caption}")
    return {"message_id": record["id"]}


def _table_search(path, body):
    """A shared-table search, served by HUBOPS_TABLES_URL. The answer keeps the table shape:
    {"items": [{"record_id", "fields": {...}}], "total", "has_more"}."""
    data = (_post_json(TABLES_URL + "/api/dispatch/departures", {"path": path, **(body or {})}) or {}).get("data") or {}
    return {"items": data.get("items") or [], "total": data.get("total", len(data.get("items") or [])), "has_more": False}


def call(method, path, token=None, body=None):
    """One request, answered in the chat platform's shape: {"code": 0, "msg": "ok", "data": ...}.
    Supported: the app token, sending a message, searching a shared table. Anything else raises ChatError."""
    if path.startswith("/auth/"):
        return {"code": 0, "msg": "ok", "tenant_access_token": get_token(), "expire": TOKEN_TTL}
    if method == "POST" and path.startswith("/im/v1/messages"):
        to = (body or {}).get("receive_id") if isinstance(body, dict) else None
        return {"code": 0, "msg": "ok", "data": send_text(_message_text(body), to)}
    if method == "POST" and "/records/search" in path:
        return {"code": 0, "msg": "ok", "data": _table_search(path, body)}
    raise ChatError(f"{method} {path}: not supported by the team chat adapter")


def get_token():
    """The app's access token. With no secret configured, a fixed demo token (nothing checks it)."""
    secret = _secret()
    return f"t-{uuid.uuid5(uuid.NAMESPACE_URL, APP_ID + ':' + secret).hex}" if secret else DEMO_TOKEN


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        parser.print_help()
        return 0
    try:
        print(f"token ok ({'configured' if _secret() else 'demo'}); mode: {'webhook' if WEBHOOK else 'demo (stdout)'}")
        send_text("team chat adapter check")
    except ChatError as e:
        print(e, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
