"""unsorted_board.py: bag tag short codes, and which building a truck was unloaded in from the shift schedule."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))

import unsorted_board as ub  # noqa: E402

SCHEDULE = {"people": [
    {"name": "Ana Torres", "days": {"2026-11-20": "Building 1 night", "2026-11-21": "Building 2"}},
    {"name": "Sam Lee", "days": {"2026-11-20": "Building 2", "2026-11-22": "B2"}},
    {"name": "Rio Park", "days": {"2026-11-18": "H1"}},
]}


class ShortCode(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(ub.short_code("BG012611250028-25"), "0028-25")
        self.assertEqual(ub.short_code("GL2611090008265"), "8265")


class Building(unittest.TestCase):
    def setUp(self):
        self.staff = ub.people(SCHEDULE)

    def test_before_8_am_the_night_shift_that_started_yesterday_counts(self):
        # 1 AM on 11/21 is the 11/20 night shift: Ana was in Building 1 then, not Building 2
        got = ub.truck_building(self.staff, {"Ana Torres": (40, "2026-11-21 01:10")})
        self.assertEqual(got, "Building 1")

    def test_most_scans_wins(self):
        got = ub.truck_building(self.staff, {"Ana Torres": (5, "2026-11-20 22:00"), "Sam Lee": (60, "2026-11-20 22:05")})
        self.assertEqual(got, "Building 2")

    def test_off_that_day_is_only_a_guess(self):
        got = ub.truck_building(self.staff, {"Rio Park": (30, "2026-11-20 21:00")})
        self.assertEqual(got, "Building 1?")

    def test_unknown_people_give_no_building(self):
        self.assertEqual(ub.truck_building(self.staff, {"Nobody Here": (30, "2026-11-20 21:00")}), "")


if __name__ == "__main__":
    unittest.main()
