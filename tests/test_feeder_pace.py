import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import feeder_pace as fp  # noqa: E402


def hour(total, seats):
    """One by_hour() entry: sorter total plus {seat: (fed, rejects)}."""
    return {"": total, **seats}


def night(hours=8, seat_fed=1000, seat_rej=25, seats=("2", "3", "5"), extra=None):
    """A baseline night: busy hours with every seat at the same pace."""
    out = {}
    for h in range(hours):
        per = {s: (seat_fed, seat_rej) for s in seats}
        per.update(extra or {})
        fed = sum(v[0] for v in per.values())
        rej = sum(v[1] for v in per.values())
        out[f"2026-11-1{h % 10} {h:02d}"] = {"": (max(fed, fp.BUSY_HOUR), rej), **per}
    return out


class Baseline(unittest.TestCase):
    def test_normal_is_each_stations_own_median(self):
        base = fp.baseline([night(seat_fed=1000), night(seat_fed=800), night(seat_fed=1200)])
        self.assertEqual(base["normal"]["2"], 1000)
        self.assertIn("2", base["swing"])
        self.assertIn("", base["normal"])

    def test_quiet_hours_do_not_count(self):
        quiet = {"2026-11-10 22": {"": (5000, 50), "2": (100, 1)}}
        base = fp.baseline([{**night(), **quiet}])
        self.assertEqual(base["normal"]["2"], 1000)

    def test_every_night_needs_most_nights_high(self):
        bad = {"9": (2000, 200)}  # 10% rejects
        nights = [night(extra=bad), night(extra=bad), night(extra=bad), night()]
        self.assertEqual(fp.baseline(nights)["every_night"], ["9"])
        nights = [night(extra=bad), night(), night(), night()]
        self.assertEqual(fp.baseline(nights)["every_night"], [])


class Report(unittest.TestCase):
    base = {"normal": {"": 20000, "2": 1000, "3": 1000, "4": 100, "16": 1000}, "every_night": ["16"]}

    def seats(self, **over):
        seats = {"": (20000, 500), "2": (1000, 25), "3": (1000, 25), "4": (100, 0), "16": (1000, 25)}
        seats.update({k.lstrip("s"): v for k, v in over.items()})
        return seats

    def test_quiet_hour_sends_nothing(self):
        self.assertIsNone(fp.report("2026-11-12 01", self.seats(), self.base))

    def test_slow_station_against_its_own_normal(self):
        text = fp.report("2026-11-12 01", self.seats(s3=(500, 10)), self.base)
        self.assertIn("Feed stations 1–2 AM · 20,000 fed (normal 20,000)", text)
        self.assertIn("Slow: 3: 500 (expected 1,000)", text)

    def test_starved_station_is_expected_to_drop_when_the_sorter_is_quieter(self):
        base = {**self.base, "swing": {"3": 2.0}}
        seats = self.seats(s3=(500, 10))
        seats[""] = (17000, 400)  # 85% of a normal hour: still judged, but station 3 expects 70% of its normal
        self.assertIsNone(fp.report("2026-11-12 01", seats, base))
        seats = self.seats(s3=(300, 10))
        seats[""] = (17000, 400)
        self.assertIn("Slow: 3: 300 (expected 700)", fp.report("2026-11-12 01", seats, base))

    def test_swing(self):
        flat = [(t, 1000) for t in range(20000, 30000, 500)]
        self.assertEqual(fp.swing_of(flat), 0.0)
        in_step = [(t, t / 20) for t in range(20000, 30000, 500)]
        self.assertAlmostEqual(fp.swing_of(in_step), 1.0)
        self.assertEqual(fp.swing_of(in_step[:5]), 0.0)  # too few hours to tell

    def test_low_volume_station_is_never_slow(self):
        self.assertIsNone(fp.report("2026-11-12 01", self.seats(s4=(10, 0)), self.base))

    def test_whole_sorter_slow_sends_nothing(self):
        seats = self.seats(s3=(100, 0))
        seats[""] = (12000, 300)  # break, start or no freight
        self.assertIsNone(fp.report("2026-11-12 22", seats, self.base))

    def test_high_rejects_and_every_night_tag(self):
        text = fp.report("2026-11-12 02", self.seats(s16=(1000, 80), s2=(1000, 50)), self.base)
        self.assertIn("Rejects: 16: 8.0% (every night) · 2: 5.0%", text)

    def test_station_that_fed_nothing(self):
        seats = self.seats()
        del seats["3"]
        self.assertIn("Fed nothing: 3", fp.report("2026-11-12 01", seats, self.base))

    def test_labels(self):
        self.assertEqual(fp.label("2026-11-12 01"), "1–2 AM")
        self.assertEqual(fp.label("2026-11-11 23"), "11 PM–12 AM")
        self.assertEqual(fp.label("2026-11-11 19"), "7–8 PM")

    def test_operating_day(self):
        self.assertEqual(str(fp.operating_day(datetime(2026, 11, 12, 3))), "2026-11-11")
        self.assertEqual(str(fp.operating_day(datetime(2026, 11, 12, 19))), "2026-11-12")

    def test_by_hour_reads_wms_rows(self):
        rows = [{"hourRange": "2026-11-12 01", "seatNo": "", "pickCnt": "28318", "refuseCnt": "700"},
                {"hourRange": "2026-11-12 01", "seatNo": "16", "pickCnt": "1091", "refuseCnt": "70"},
                {"hourRange": "2026-11-12 05", "seatNo": "16", "pickCnt": None, "refuseCnt": None}]
        self.assertEqual(fp.by_hour(rows), {"2026-11-12 01": {"": (28318, 700), "20": (1091, 70)}})

    def test_report_seats_become_floor_stations(self):
        row = lambda seat, n: {"hourRange": "2026-11-12 01", "seatNo": seat, "pickCnt": n, "refuseCnt": 0}
        old = fp.by_hour([row("19", 5), row("26", 6), row("1", 7)])["2026-11-12 01"]  # older numbering
        self.assertEqual(old, {"23": (5, 0), "30": (6, 0), "1-5": (7, 0)})
        new = fp.by_hour([row("28", 5), row("3", 6), row("30", 7)])["2026-11-12 01"]  # floor numbers
        self.assertEqual(new, {"28": (5, 0), "3": (6, 0), "30": (7, 0)})
        self.assertEqual(sorted(["30", "1-5", "6"], key=fp.station_order), ["1-5", "6", "30"])

if __name__ == "__main__":
    unittest.main()
