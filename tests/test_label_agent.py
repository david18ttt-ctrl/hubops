import base64
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent"))
import label_agent as la  # noqa: E402


def png(w, h, pixel, color=2):
    """A small 8-bit PNG (RGB or RGBA) with pixel(x, y) -> tuple, rows with mixed filters to test decoding."""
    ch = {2: 3, 6: 4, 0: 1}[color]
    raw = b""
    for y in range(h):
        row = bytes(v for x in range(w) for v in pixel(x, y))
        if y % 2:  # Sub filter on odd rows
            enc = bytearray(row)
            for i in range(len(row) - 1, ch - 1, -1):
                enc[i] = (row[i] - row[i - ch]) & 255
            raw += b"\x01" + bytes(enc)
        else:
            raw += b"\x00" + row
    chunk = lambda k, b: struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b))  # noqa: E731
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def dot(bits, x, y):
    """True if the dot at (x, y) is black (TSPL: 0 = black)."""
    return not bits[y * la.ROW_BYTES + (x >> 3)] & (0x80 >> (x & 7))


class PictureTest(unittest.TestCase):
    def test_decode_png_rgb_rgba_grey(self):
        w, h, lum = la.decode_png(png(4, 3, lambda x, y: (255, 255, 255) if x < 2 else (0, 0, 0)))
        self.assertEqual((w, h), (4, 3))
        self.assertEqual(list(lum[:4]), [255, 255, 0, 0])
        self.assertEqual(lum[4:8], lum[:4])  # the Sub-filtered row decodes to the same
        _, _, lum = la.decode_png(png(2, 1, lambda x, y: (0, 0, 0, 0 if x else 255), color=6))
        self.assertEqual(list(lum), [0, 255])  # transparent = white paper
        _, _, lum = la.decode_png(png(2, 2, lambda x, y: (x * 255,), color=0))
        self.assertEqual(list(lum), [0, 255, 0, 255])

    def test_full_label_maps_dot_for_dot(self):
        # 812 x 1218 portrait picture: black top-left quarter-inch square, the rest white.
        pic = png(la.W, la.H, lambda x, y: (0, 0, 0) if x < 50 and y < 50 else (255, 255, 255))
        bits = la.label_bits(*la.decode_png(pic))
        self.assertEqual(len(bits), la.ROW_BYTES * la.H)
        self.assertTrue(dot(bits, 0, 0) and dot(bits, 49, 49))
        self.assertFalse(dot(bits, 50, 0) or dot(bits, 0, 50) or dot(bits, 811, 1217))

    def test_sideways_picture_is_turned_upright(self):
        # 1218 x 812 landscape with the black mark top-left: after a quarter turn clockwise it sits top-right.
        pic = png(la.H, la.W, lambda x, y: (0, 0, 0) if x < 40 and y < 40 else (255, 255, 255))
        bits = la.label_bits(*la.decode_png(pic))
        self.assertTrue(dot(bits, la.W - 1, 0) and dot(bits, la.W - 30, 30))
        self.assertFalse(dot(bits, 0, 0))

    def test_tspl_and_zpl_commands(self):
        bits = bytes([0xFF] * (la.ROW_BYTES * la.H))
        tspl = la.tspl_bitmap(bits, copies=3)
        self.assertTrue(tspl.startswith(b"SIZE 4,6\r\nCLS\r\nBITMAP 0,0,102,1218,0,"))
        self.assertTrue(tspl.endswith(b"\r\nPRINT 1,3\r\n"))
        self.assertEqual(len(tspl), len(b"SIZE 4,6\r\nCLS\r\nBITMAP 0,0,102,1218,0,") + len(bits) + len(b"\r\nPRINT 1,3\r\n"))
        zpl = la.zpl_bitmap(bits, copies=2).decode()
        self.assertTrue(zpl.startswith(f"^XA^PW812^LL1218^FO0,0^GFA,{102 * 1218},{102 * 1218},102,000000"))
        self.assertIn("^PQ2^XZ", zpl)
        text = la.tspl_text({"chute": "87", "route": "VCT01-004", "bag": "BG012611250465-087", "date": "x"}, 2).decode()
        self.assertIn('BARCODE 60,760,"128",160,0,0,3,6,"BG012611250465-087"', text)
        self.assertIn("PRINT 1,2", text)
        self.assertIn("^BCN", la.zpl_text({"bag": "BG1"}).decode())

    def test_preview_png_has_the_same_dots(self):
        bits = la.label_bits(*la.decode_png(png(la.W, la.H, lambda x, y: (0, 0, 0) if y < 10 else (255, 255, 255))))
        w, h, lum = la.decode_png(la.png_of_bits(bits))
        self.assertEqual((w, h), (la.W, la.H))
        self.assertEqual(lum[0], 0)
        self.assertEqual(lum[20 * la.W], 255)


class PrinterTest(unittest.TestCase):
    def test_label_printer_by_name(self):
        names = ["Office Laser", "Microsoft Print to PDF", "Label Printer PDF", "4BARCODE 4B-2054K", "Zebra ZD421"]
        self.assertEqual(la.pick_printer(names, "Office Laser"), "4BARCODE 4B-2054K")
        self.assertEqual(la.pick_printer(["Office Laser", "Fax"], "Office Laser"), "Office Laser")
        self.assertEqual(la.pick_printer([], ""), "")
        self.assertEqual(la.language("Zebra ZD421"), "zpl")
        self.assertEqual(la.language("TSC TE200"), "tspl")
        self.assertEqual(la.language("Zebra ZD421", "tspl"), "tspl")


class DryRunTest(unittest.TestCase):
    def test_dry_run_writes_commands_and_a_picture_of_the_dots(self):
        img = "data:image/png;base64," + base64.b64encode(png(8, 12, lambda x, y: (0, 0, 0) if y < 6 else (255,) * 3)).decode()
        jobs = [{"id": 7, "label": {"route": "VCT01-004", "bag": "BG012611250465-087", "img": img, "copies": 2}},
                {"id": 8, "label": {"route": "PHX.H", "bag": "BG012611250470-049", "chute": "49"}}]
        with tempfile.TemporaryDirectory() as d:
            q = Path(d) / "queue.json"
            q.write_text(json.dumps(jobs))
            with mock.patch.object(la, "OUT_DIR", Path(d)), mock.patch("builtins.print"):
                self.assertEqual(la.main(["--queue", str(q), "--dry-run"]), 0)
            files = sorted(p.name for p in Path(d).iterdir())
            self.assertEqual(files, ["7-BG012611250465-087.png", "7-BG012611250465-087.tspl", "8-BG012611250470-049.tspl", "queue.json"])
            self.assertTrue((Path(d) / "7-BG012611250465-087.tspl").read_bytes().endswith(b"PRINT 1,2\r\n"))
            self.assertIn(b'TEXT 50,300,"5",0,2,2,"PHX.H"', (Path(d) / "8-BG012611250470-049.tspl").read_bytes())

    def test_station_poll_dry_run_reports_back(self):
        sent = []

        def fake_http(url, body=None, timeout=60):
            sent.append((url, body))
            if "/next?" in url:
                return json.dumps({"queue": [{"id": 3, "label": {"route": "R 004", "kind": "sticker"}}],
                                   "agentPyVersion": la.VERSION}).encode()
            return b"{}"

        with tempfile.TemporaryDirectory() as d, mock.patch.object(la, "http", fake_http), \
                mock.patch.object(la, "OUT_DIR", Path(d)), mock.patch.object(la, "installed_printers", lambda: (["TSC TE200"], "")), \
                mock.patch.object(la, "keep_awake", lambda: None), mock.patch("builtins.print"):
            self.assertEqual(la.main(["--base", "http://127.0.0.1:8787/p/demo-print-000000000", "--dry-run", "--once"]), 0)
        self.assertIn("v=" + la.VERSION, sent[0][0])
        self.assertIn("pr=TSC%20TE200", sent[0][0])
        self.assertEqual(sent[1][0], "http://127.0.0.1:8787/p/demo-print-000000000/printed")
        self.assertEqual(sent[1][1]["id"], 3)
        self.assertIn("dry run: TSPL", sent[1][1]["info"])

    def test_base_url_is_required(self):
        with mock.patch.dict("os.environ", {"HUBOPS_LABEL_PRINT_URL": ""}), mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                la.main([])


class SelfUpdateTest(unittest.TestCase):
    def test_version_is_the_stations_hash(self):
        self.assertEqual(la.text_hash("hello"), "4bj995")  # same as labelstation.js hash("hello")
        self.assertEqual(la.VERSION, la.text_hash((REPO / "agent" / "label_agent.py").read_text()))
        self.assertTrue((REPO / "agent" / "label_agent.py").read_text().isascii())  # JS and Python hash the same characters

    def test_broken_or_tiny_update_is_not_used(self):
        for text in ("def broken(:\n" * 200, "x = 1\n"):
            with mock.patch.object(la, "http", lambda *a, **k: text.encode()), mock.patch.object(la.os, "execv") as ex, \
                    mock.patch.object(la, "log"), mock.patch.object(Path, "replace") as rep:
                self.assertFalse(la.self_update("http://127.0.0.1:1/p/k", "zzz"))
                ex.assert_not_called()
                rep.assert_not_called()

    def test_good_update_is_swapped_in_and_restarted(self):
        good = "# agent\n" + "x = 1\n" * 300
        with mock.patch.object(la, "http", lambda *a, **k: good.encode()), mock.patch.object(la.os, "execv") as ex, \
                mock.patch.object(la, "log"), mock.patch.object(Path, "write_text") as wt, mock.patch.object(Path, "replace") as rep:
            la.self_update("http://127.0.0.1:1/p/k", "zzz")
        wt.assert_called_once()
        rep.assert_called_once()
        ex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
