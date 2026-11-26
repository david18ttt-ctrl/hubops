#!/usr/bin/env python3
"""A small client for the warehouse management system (WMS) JSON API.

Every request is a POST of a JSON body to HUBOPS_WMS_URL + path with a bearer token. Answers look like
{"code": 0, "msg": "ok", "data": ...}; `call` returns that whole payload (callers read ["data"]).

    HUBOPS_WMS_URL    base URL (default http://127.0.0.1:8765, the simulator in hubsim/)
    HUBOPS_WMS_TOKEN  bearer token (the simulator accepts any)

    python3 wms_client.py --check     one read-only request, prints what came back
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE_URL = os.environ.get("HUBOPS_WMS_URL", "http://127.0.0.1:8765").rstrip("/")
DEMO_TOKEN = "demo-token"
TIME_ZONE = "America/Chicago"
TIMEOUT = 60
RETRIES = (2, 5)  # seconds to wait before each retry of a read that hit a hiccup

# This hub, as the WMS dashboard calls name it.
CENTER_ID = 1
CENTER_ID_ENCRYPTION = "HOU.H"  # an opaque copy of the center id some dashboard calls also want


class LoginExpired(Exception):
    """The WMS answered 401: the token is missing or no longer accepted."""


class WmsError(RuntimeError):
    """The WMS refused the request (code other than 0) or answered with an HTTP error."""


def operation_window(now=None, day=None):
    """WMS operating day: 06:00:01 to 06:00:00 the next morning, hub time. Give `day` to pick one."""
    if day is None:
        now = now or datetime.now(ZoneInfo(TIME_ZONE))
        day = now.date() if now.hour >= 6 else now.date() - timedelta(days=1)
    return f"{day} 06:00:01", f"{day + timedelta(days=1)} 06:00:00"


def default_token():
    """The token to send: HUBOPS_WMS_TOKEN, else the demo token the simulator takes."""
    return os.environ.get("HUBOPS_WMS_TOKEN") or DEMO_TOKEN


def busy(payload):
    """Worth asking again? Not when the WMS refused the request itself (code -1 with a reason such as
    "time range over 3 days"): the same body gets the same answer, so fix the body instead."""
    code, msg = payload.get("code"), str(payload.get("msg") or "").lower()
    if code in (0, 200, 401):
        return False
    return not (code == -1 and msg and not any(w in msg for w in ("busy", "timeout", "later")))


def post_http(token_, body, path):
    """One POST, retried on network hiccups, 5xx and busy answers (WMS reads change nothing, so a retry is
    safe). Returns (http status, payload)."""
    data = json.dumps(body).encode("utf-8")
    for wait in (*RETRIES, None):
        req = urllib.request.Request(BASE_URL + path, data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + (token_ or "")})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
                payload = json.loads(res.read().decode("utf-8") or "{}")
                status = res.status
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return 401, {}
            if e.code < 500 or wait is None:
                raise WmsError(f"WMS returned HTTP {e.code} for {path}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if wait is None:
                raise WmsError(f"WMS not reachable at {BASE_URL}: {e}") from None
        else:
            if wait is None or not busy(payload):
                return status, payload
        time.sleep(wait)
    raise WmsError("unreachable")  # pragma: no cover - the loop always returns or raises


def call(path, body, page=None, token=None, allow_empty=False):
    """One WMS request. Returns the payload ({"code", "msg", "data"}). `page` is accepted for the older
    in-browser call style and ignored. Raises LoginExpired on 401, WmsError when code isn't 0."""
    status, payload = post_http(token if token is not None else default_token(), body, path)
    if status == 401 or payload.get("code") == 401:
        raise LoginExpired("WMS login missing or expired: set HUBOPS_WMS_TOKEN")
    if payload.get("code") not in (0, 200) or (not payload.get("data") and not allow_empty):
        raise WmsError(f"WMS returned code={payload.get('code')} msg={payload.get('msg')} for {path}")
    return payload


def call_data(path, body, token=None):
    """The "data" part of one request (empty answers allowed)."""
    return call(path, body, token=token, allow_empty=True).get("data")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="one read-only request")
    args = parser.parse_args()
    if not args.check:
        parser.print_help()
        return 0
    try:
        places = call_data("/api/places", {})
    except (LoginExpired, WmsError) as e:
        print(e, file=sys.stderr)
        return 2
    print(f"{BASE_URL}: {len(places or [])} places")
    return 0


if __name__ == "__main__":
    sys.exit(main())
