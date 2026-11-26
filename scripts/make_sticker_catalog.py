#!/usr/bin/env python3
"""Builds web/src/sticker-catalog.json, the list of chute stickers the phone sticker page offers (web/src/stickerpage.js),
from the sort plan: one sticker per chute of the 300 (station route, hub, exception chute or free chute), plus the
R / S / T gaylord stickers one station uses for its routes.

    python3 scripts/make_sticker_catalog.py      rewrite the catalog after the sort plan changes

Each entry: {id, g (group: station / city / other / rst), sub (station or hub), name (what the sticker says), chute,
type (Main, Exception, Free, Special)}. The phone draws the sticker picture itself from these facts.
"""
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "hubsim"))
import model  # noqa: E402

OUT = REPO / "web" / "src" / "sticker-catalog.json"
RST_STATION = "AUS01"  # the station whose gaylords carry R / S / T route stickers
EXCEPTION_NAMES = {**{c: "NOREAD" for c in model.NOREAD_CHUTES}, model.REJECT_CHUTE: "OVER LOOP",
                   model.INTERCEPT_CHUTE: "INTERCEPT", model.NO_MATCH_CHUTE: "NO MATCH"}


def catalog():
    out = []
    for n in range(1, model.CHUTES + 1):
        chute = f"{n:03d}"
        route = model.CHUTE_ROUTE.get(chute)
        item = {"id": f"c{chute}", "chute": n}
        if chute in EXCEPTION_NAMES:
            item.update(g="other", sub="Exception", name=EXCEPTION_NAMES[chute], type="Exception")
        elif route in model.HUBS:
            item.update(g="city", sub=route, name=route, type="Main")
        elif route:
            item.update(g="station", sub=route.split("-")[0], name=route, type="Main")
        else:
            item.update(g="other", sub="Free", name="FREE", type="Free")
        out.append(item)
    routes = [r for r in model.STATION_ROUTES[RST_STATION]]
    for letter in "RST":
        for r in routes:
            num = r.split("-")[1]
            out.append({"id": f"r{letter}-{num}", "g": "rst", "sub": letter, "name": f"{letter} {num}", "route": r,
                        "chute": int(model.ROUTES[r][0]), "type": "Main"})
    return out


if __name__ == "__main__":
    items = catalog()
    OUT.write_text(json.dumps(items, separators=(",", ":")) + "\n")
    print(f"{OUT.relative_to(REPO)}: {len(items)} stickers")
