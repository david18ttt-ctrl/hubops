#!/usr/bin/env python3
"""Label print agent: runs on the computer next to the 4x6 thermal label printer and prints whatever the label
station queues (bag labels from the scan link, chute stickers, time labels), with no browser or window open.

It long-polls the label station's print queue (web/src/labelstation.js, GET <base>/next?agent=1), turns each label
into the printer's own language and writes it to the printer as a RAW job, past the operating system's driver
(drivers took minutes to open a job on these printers; the raw job takes well under a second):
  - TSPL (TSC-compatible printers; the default): one BITMAP command, 812 x 1218 dots at 203 dpi.
  - ZPL (Zebra printers): one ^GFA graphic field.
A label that arrives without a picture (the ops host could not draw it) is printed from its facts with the
printer's own fonts and a Code 128 barcode, so the box still gets a label.

The printer is picked by name (label / thermal / TSC / Zebra / 4BARCODE / Arkscan models), never a PDF or fax
queue; the system default only when nothing matches. The agent reports its version and printer with every poll,
and updates itself when the label station serves a newer copy: it downloads <base>/agent.py, refuses one that
does not compile, swaps it in and restarts. The base URL is always configured (--base or
HUBOPS_LABEL_PRINT_URL); there is no built-in address.

    python3 agent/label_agent.py --base http://127.0.0.1:8787/p/<print key>          the loop
    python3 agent/label_agent.py --base ... --dry-run           same, but writes the printer commands to
                                                                data/labels/ (plus a PNG of the dots) instead
    python3 agent/label_agent.py --queue data/labels/queue.json --dry-run    offline: print a saved queue
    python3 agent/label_agent.py --list-printers                what it sees and which one it would use

Standard library only (Python 3.9+). Windows writes RAW jobs through winspool; macOS / Linux through CUPS (lp -o raw).
"""
import argparse
import base64
import json
import os
import re
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zlib
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_DIR = Path(os.environ.get("HUBOPS_LABEL_OUT") or (HERE.parent / "data" / "labels"))
W, H = 812, 1218  # a 4x6 label at 203 dots per inch
ROW_BYTES = (W + 7) // 8
LABEL_PRINTER = re.compile(r"arkscan|4barcode|2054|label|thermal|zebra|tsc", re.I)
NOT_A_PRINTER = re.compile(r"pdf|xps|onenote|fax", re.I)
POLL_WAIT = 25  # seconds the label station holds a poll open when nothing is waiting
USER_AGENT = "hubops-label-agent"


def text_hash(text):
    """djb2 over the text, base 36: the same version hash the label station computes for the copy it serves."""
    h = 5381
    for ch in text:
        h = (h * 33 + ord(ch)) & 0xFFFFFFFF
    digits, out = "0123456789abcdefghijklmnopqrstuvwxyz", ""
    while True:
        h, r = divmod(h, 36)
        out = digits[r] + out
        if not h:
            return out


VERSION = text_hash(Path(__file__).read_text(encoding="utf-8"))


def log(line, path=None):
    path = path or OUT_DIR / "agent.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
            path.replace(path.with_name(path.name + ".old"))
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%m-%d %H:%M:%S} {line}\n")
    except OSError:
        pass
    print(line, flush=True)


# ---------- PNG in, dots out (no imaging library) ----------

def decode_png(data):
    """(width, height, luminance bytes 0-255, one per pixel). 8-bit grey / RGB / palette / grey+alpha / RGBA, and
    1/2/4-bit grey or palette; non-interlaced (what browsers and canvas write). Transparent counts as white."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, palette, trns = 8, [], None, None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IHDR":
            w, h, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif kind == b"PLTE":
            palette = [tuple(body[i:i + 3]) for i in range(0, len(body), 3)]
        elif kind == b"tRNS":
            trns = body
        elif kind == b"IDAT":
            idat.append(body)
        elif kind == b"IEND":
            break
    if interlace:
        raise ValueError("interlaced PNG")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bits = depth * channels
    stride = (w * bits + 7) // 8
    bpp = max(1, bits // 8)
    raw = zlib.decompress(b"".join(idat))
    out = bytearray(w * h)
    prev = bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        line = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                line[i] = (line[i] + ((line[i - bpp] if i >= bpp else 0) + prev[i]) // 2) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b, c = prev[i], (prev[i - bpp] if i >= bpp else 0)
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        o = y * w
        if depth < 8:  # grey or palette packed several to a byte
            per, mask = 8 // depth, (1 << depth) - 1
            for x in range(w):
                v = (line[x // per] >> (8 - depth * (x % per + 1))) & mask
                if color == 3:
                    r, g, b = palette[v]
                    out[o + x] = (r * 30 + g * 59 + b * 11) // 100
                else:
                    out[o + x] = v * 255 // mask
            continue
        for x in range(w):
            if color == 0:
                out[o + x] = line[x]
            elif color == 4:
                v, a = line[2 * x], line[2 * x + 1]
                out[o + x] = (v * a + 255 * (255 - a)) // 255
            elif color == 3:
                r, g, b = palette[line[x]]
                a = trns[line[x]] if trns and line[x] < len(trns) else 255
                out[o + x] = ((r * 30 + g * 59 + b * 11) // 100 * a + 255 * (255 - a)) // 255
            else:
                p = x * channels
                lum = (line[p] * 30 + line[p + 1] * 59 + line[p + 2] * 11) // 100
                a = line[p + 3] if channels == 4 else 255
                out[o + x] = (lum * a + 255 * (255 - a)) // 255
    return w, h, bytes(out)


def label_bits(w, h, lum):
    """The picture on a 4x6 label: turned upright if it is sideways, scaled to fit (nearest dot, so barcodes stay
    sharp), centred, black where darker than half. Returns ROW_BYTES * H bytes, 1 bit a dot, 1 = white (TSPL's
    BITMAP order)."""
    rotate = w > h
    sw, sh = (h, w) if rotate else (w, h)
    s = min(W / sw, H / sh)
    tw, th = int(sw * s), int(sh * s)
    ox, oy = (W - tw) // 2, (H - th) // 2
    bits = bytearray(b"\xff" * (ROW_BYTES * H))
    cols = [min(sw - 1, int((x - ox) / s)) for x in range(ox, ox + tw)]
    for ty in range(th):
        sy = min(sh - 1, int(ty / s))
        row = (oy + ty) * ROW_BYTES
        for i, sx in enumerate(cols):
            # upright (sx, sy) -> source pixel; a sideways source is turned a quarter clockwise
            v = lum[(h - 1 - sx) * w + sy] if rotate else lum[sy * w + sx]
            if v < 128:
                x = ox + i
                bits[row + (x >> 3)] &= 0xFF ^ (0x80 >> (x & 7))
    return bytes(bits)


def tspl_bitmap(bits, copies=1):
    head = f"SIZE 4,6\r\nCLS\r\nBITMAP 0,0,{ROW_BYTES},{H},0,".encode("ascii")
    return head + bits + f"\r\nPRINT 1,{max(1, int(copies))}\r\n".encode("ascii")


def zpl_bitmap(bits, copies=1):
    """ZPL marks black with 1, the opposite of TSPL."""
    data = bytes(b ^ 0xFF for b in bits).hex().upper()
    total = ROW_BYTES * H
    return (f"^XA^PW{W}^LL{H}^FO0,0^GFA,{total},{total},{ROW_BYTES},{data}^FS^PQ{max(1, int(copies))}^XZ\r\n").encode("ascii")


def _clean(text, limit=40):
    return re.sub(r"[^\x20-\x7e]", "?", str(text or "")).replace('"', "'")[:limit]


def tspl_text(label, copies=1):
    """A label without a picture, from its facts, with the printer's own fonts and barcode."""
    lines = ["SIZE 4,6", "CLS",
             f'TEXT 50,60,"4",0,2,2,"Chute: {_clean(label.get("chute"))}"',
             f'TEXT 50,300,"5",0,2,2,"{_clean(label.get("route"))}"',
             f'TEXT 50,560,"3",0,1,1,"Origin: {_clean(label.get("origin"))}"',
             f'BARCODE 60,760,"128",160,0,0,3,6,"{_clean(label.get("bag"))}"',
             f'TEXT 50,960,"4",0,1,1,"{_clean(label.get("bag"))}"',
             f'TEXT 50,1040,"3",0,1,1,"Date: {_clean(label.get("date"))}"',
             f"PRINT 1,{max(1, int(copies))}", ""]
    return "\r\n".join(lines).encode("ascii")


def zpl_text(label, copies=1):
    return ("^XA^PW812^LL1218"
            f"^FO50,60^A0N,70,70^FDChute: {_clean(label.get('chute'))}^FS"
            f"^FO50,300^A0N,140,120^FD{_clean(label.get('route'))}^FS"
            f"^FO50,560^A0N,40,40^FDOrigin: {_clean(label.get('origin'))}^FS"
            f"^FO60,760^BY3^BCN,160,N,N,N^FD{_clean(label.get('bag'))}^FS"
            f"^FO50,960^A0N,50,50^FD{_clean(label.get('bag'))}^FS"
            f"^FO50,1040^A0N,40,40^FDDate: {_clean(label.get('date'))}^FS"
            f"^PQ{max(1, int(copies))}^XZ\r\n").encode("ascii")


def png_of_bits(bits):
    """The dots as a 1-bit PNG (in a 1-bit grey PNG 1 = white too, so the TSPL rows go in unchanged)."""
    raw = b"".join(b"\x00" + bits[y * ROW_BYTES:(y + 1) * ROW_BYTES] for y in range(H))
    chunk = lambda kind, body: struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 1, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def commands(label, lang):
    """(printer commands, the dots or None) for one queued label."""
    copies = label.get("copies") or 1
    img = str(label.get("img") or "")
    if img.startswith("data:image/png;base64,"):
        bits = label_bits(*decode_png(base64.b64decode(img.split(",", 1)[1])))
        return (zpl_bitmap if lang == "zpl" else tspl_bitmap)(bits, copies), bits
    return (zpl_text if lang == "zpl" else tspl_text)(label, copies), None


# ---------- printers ----------

def installed_printers():
    """(every printer name, the system default or "")."""
    if sys.platform == "win32":
        ps = ("Get-CimInstance Win32_Printer | ForEach-Object { ($(if ($_.Default) {'*'} else {' '})) + $_.Name }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=30).stdout
        rows = [r for r in out.splitlines() if r.strip()]
        names = [r[1:] for r in rows]
        return names, next((r[1:] for r in rows if r.startswith("*")), "")
    try:
        names = subprocess.run(["lpstat", "-e"], capture_output=True, text=True, timeout=15).stdout.split()
        default = subprocess.run(["lpstat", "-d"], capture_output=True, text=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError):
        return [], ""
    m = re.search(r":\s*(\S+)\s*$", default.strip())
    return names, (m.group(1) if m else "")


def pick_printer(names, default=""):
    """The label printer by name; the system default only if none matches."""
    hit = next((n for n in names if LABEL_PRINTER.search(n) and not NOT_A_PRINTER.search(n)), None)
    return hit or default or (names[0] if names else "")


def language(printer, choice="auto"):
    if choice in ("tspl", "zpl"):
        return choice
    return "zpl" if re.search(r"zebra|zpl|zdesigner", printer or "", re.I) else "tspl"


def send_raw(printer, data):
    """One RAW job straight to the printer, past the driver."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class DocInfo(ctypes.Structure):
            _fields_ = [("name", wintypes.LPWSTR), ("output", wintypes.LPWSTR), ("datatype", wintypes.LPWSTR)]

        spool = ctypes.WinDLL("winspool.drv", use_last_error=True)
        handle = wintypes.HANDLE()
        if not spool.OpenPrinterW(printer, ctypes.byref(handle), None):
            raise OSError(f"OpenPrinter failed, error {ctypes.get_last_error()}")
        try:
            if not spool.StartDocPrinterW(handle, 1, ctypes.byref(DocInfo("Hub label", None, "RAW"))):
                raise OSError(f"StartDocPrinter failed, error {ctypes.get_last_error()}")
            try:
                spool.StartPagePrinter(handle)
                written = wintypes.DWORD()
                ok = spool.WritePrinter(handle, data, len(data), ctypes.byref(written))
                spool.EndPagePrinter(handle)
                if not ok or written.value != len(data):
                    raise OSError(f"WritePrinter wrote {written.value} of {len(data)}, error {ctypes.get_last_error()}")
            finally:
                spool.EndDocPrinter(handle)
        finally:
            spool.ClosePrinter(handle)
        return
    res = subprocess.run(["lp", "-d", printer, "-o", "raw"], input=data, capture_output=True, timeout=60)
    if res.returncode:
        raise OSError((res.stderr or res.stdout).decode(errors="replace").strip() or f"lp exit {res.returncode}")


def keep_awake():
    """Keep the computer from sleeping while the agent runs (the screen may still turn off)."""
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED
    elif sys.platform == "darwin" and not getattr(keep_awake, "proc", None):
        try:
            keep_awake.proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError:
            keep_awake.proc = True


def one_copy(lock_path):
    """True if no other agent is running (a lock file held for the life of the process)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    one_copy.handle = f
    return True


# ---------- one job ----------

def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text or "label"))[:60].strip("_") or "label"


def print_job(job, printer, lang, dry_run, out_dir=None):
    """Print (or in a dry run, write out) one queued label. Returns the info line reported back to the station."""
    label = job.get("label") or {}
    t0 = time.monotonic()
    data, bits = commands(label, lang)
    made = int((time.monotonic() - t0) * 1000)
    kind = "bitmap" if bits else "text"
    if dry_run:
        out_dir = Path(out_dir or OUT_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"{job.get('id', 0)}-{safe_name(label.get('bag') or label.get('route'))}"
        path = out_dir / f"{stem}.{lang}"
        path.write_bytes(data)
        extra = ""
        if bits:
            (out_dir / f"{stem}.png").write_bytes(png_of_bits(bits))
            extra = f" + {stem}.png"
        return f"dry run: {lang.upper()} {kind} ({len(data)} bytes, made in {made} ms) written to {path}{extra}"
    t1 = time.monotonic()
    send_raw(printer, data)
    return (f"RAW sent to {printer} ({lang.upper()} {kind}, {len(data)} bytes, made in {made} ms, "
            f"sent in {int((time.monotonic() - t1) * 1000)} ms)")


# ---------- the label station ----------

def http(url, body=None, timeout=60):
    req = urllib.request.Request(url, method="POST" if body is not None else "GET",
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read()


def self_update(base, new_version):
    """Download the station's newer copy, keep it only if it compiles, swap it in and restart."""
    me = Path(__file__).resolve()
    new = me.with_name(me.name + ".new")
    try:
        text = http(base + "/agent.py", timeout=60).decode("utf-8")
        compile(text, str(new), "exec")  # never swap in a copy that doesn't parse: printing would stop
        if len(text) < 1000:
            raise ValueError(f"only {len(text)} characters")
        new.write_text(text, encoding="utf-8")
        new.replace(me)
    except Exception as e:  # noqa: BLE001 - keep printing with this copy
        log(f"update v{new_version} not used, kept v{VERSION}: {type(e).__name__}: {e}")
        return False
    log(f"updating to v{new_version}")
    os.execv(sys.executable, [sys.executable, str(me), *sys.argv[1:]])
    return True  # pragma: no cover - execv does not return


def poll(base, printer, names, wait):
    pr = printer + " ; all: " + ", ".join(names)
    url = f"{base}/next?agent=1&wait={wait}&v={VERSION}&pr={urllib.parse.quote(pr)}"
    return json.loads(http(url, timeout=wait + 35).decode() or "{}")


def run(args):
    if not one_copy(OUT_DIR / ".agent.lock"):
        print("another agent is already running", file=sys.stderr)
        return 1
    names, default = installed_printers()
    printer = args.printer or pick_printer(names, default)
    log(f"started v{VERSION}, printer: {printer or '(none)'}{' (dry run)' if args.dry_run else ''} (all: {', '.join(names)})")
    first = True
    while True:
        keep_awake()
        try:
            d = poll(args.base, printer, names, 0 if first else POLL_WAIT)
            first = False
            queue = [j for j in d.get("queue") or [] if j]
            newer = d.get("agentPyVersion")
            if newer and newer != VERSION and not queue and not args.no_update:
                self_update(args.base, newer)
            if queue and not args.printer:  # printers come and go (USB unplugged, a new one installed)
                names, default = installed_printers()
                printer = pick_printer(names, default)
            for job in queue:
                lang = language(printer, args.lang)
                try:
                    info = print_job(job, printer, lang, args.dry_run)
                    log(f"printed {job['label'].get('route')} {job['label'].get('bag') or ''} x{job['label'].get('copies') or 1} {info}")
                except Exception as e:  # noqa: BLE001 - report it and go on with the next label
                    info = f"PRINT FAILED on {printer}: {type(e).__name__}: {e}"
                    log(info)
                try:
                    http(args.base + "/printed", {"id": job["id"], "info": info}, timeout=30)
                except Exception:  # noqa: BLE001 - the station re-offers nothing; the label is out either way
                    pass
            if args.once:
                return 0
        except Exception as e:  # noqa: BLE001 - no connection: wait and try again
            log(f"no connection: {type(e).__name__}: {e}")
            if args.once:
                return 2
            time.sleep(5)


def run_queue(args):
    """Offline: every label in a saved queue file ([{id, label}], what <base>/next hands out)."""
    jobs = json.loads(Path(args.queue).read_text(encoding="utf-8"))
    names, default = installed_printers() if not args.dry_run else ([], "")
    printer = args.printer or pick_printer(names, default) or "(dry run)"
    for job in jobs:
        info = print_job(job, printer, language(printer, args.lang), args.dry_run)
        print(f"#{job.get('id')} {job['label'].get('route')} {job['label'].get('bag') or ''}: {info}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", default=os.environ.get("HUBOPS_LABEL_PRINT_URL", "").rstrip("/"),
                   help="the label station's print path, e.g. http://127.0.0.1:8787/p/<key> (or HUBOPS_LABEL_PRINT_URL)")
    p.add_argument("--queue", help="print the labels in this queue file instead of polling the station")
    p.add_argument("--dry-run", action="store_true", help=f"write printer commands to {OUT_DIR} instead of printing")
    p.add_argument("--printer", help="printer name (default: picked by name)")
    p.add_argument("--lang", choices=["auto", "tspl", "zpl"], default="auto")
    p.add_argument("--once", action="store_true", help="one poll, then stop")
    p.add_argument("--no-update", action="store_true", help="never replace this copy with the station's")
    p.add_argument("--list-printers", action="store_true")
    args = p.parse_args(argv)
    if args.list_printers:
        names, default = installed_printers()
        print("\n".join(f"{'*' if n == default else ' '} {n}" for n in names) or "(no printers)")
        print(f"would use: {pick_printer(names, default) or '(none)'}")
        return 0
    if args.queue:
        return run_queue(args)
    if not args.base:
        p.error("--base (or HUBOPS_LABEL_PRINT_URL) is required, e.g. http://127.0.0.1:8787/p/<print key>")
    args.base = args.base.rstrip("/")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
