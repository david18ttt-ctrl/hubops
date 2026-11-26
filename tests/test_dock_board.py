import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import dock_board as db  # noqa: E402

H = 3600000
NIGHT = int(datetime(2026, 11, 12, 6, 0, tzinfo=db.TZ).timestamp() * 1000)  # 6 AM
DAY = NIGHT - 6 * H  # departure date: midnight of the operating day
NOW = int(datetime(2026, 11, 13, 0, 35, tzinfo=db.TZ).timestamp() * 1000)


def truck(rid, key="SGR01", dock=None, task=None, arrived=None, left=None, kind="53FT"):
    return {"id": rid, "day": DAY, "key": key, "type": kind, "cancelled": False, "task": task, "planned": None,
            "driver": None, "arrived": arrived, "left": left, "dock": dock, "leave_by": "4:00"}


def bag(dest, closed_min_ago, checked_out=False, waybills=200):
    closed = NOW - closed_min_ago * 60000
    return {"destinCenterName": dest, "waybillCount": waybills, "optFinishStatusTime": closed,
            "optCreateTime": closed - 30 * 60000, "optCheckOutTime": closed + 60000 if checked_out else None}


class Loaded(unittest.TestCase):
    def test_a_truck_leaving_doesnt_wipe_the_boxes_on_the_others(self):
        # 12:35 AM: an SGR01 truck left at 12:33; TT…421 (dock 22) already had 29 boxes checked out,
        # TT…890 (dock 20) 25, and 24 closed boxes were still on the floor.
        rows = [truck("gone", dock="19", task="TT1", arrived=NOW - 5 * H, left=NOW - 2 * 60000),
                truck("d20", dock="20", task="TT890"), truck("d22", dock="22", task="TT421")]
        bags = [bag("SGR01", 120) for _ in range(24)] + [bag("SGR01", 200, checked_out=True) for _ in range(54)]
        board = db.build(rows, bags, set(), NOW, NIGHT, {"TT890": 25, "TT421": 29})
        by = {t["id"]: t for t in board["trucks"]}
        self.assertEqual((by["d20"]["loaded"], by["d20"]["out"], by["d20"]["pull"]), (25, 24, 0))
        self.assertEqual((by["d22"]["loaded"], by["d22"]["out"], by["d22"]["pull"]), (29, 20, 0))
        self.assertEqual(by["d22"]["extra"], 20)
        self.assertEqual(board["pools"]["SGR01"]["out"], 24)

    def test_nothing_loaded_yet_works_like_before(self):
        rows = [truck("c", key="TXK01", dock="14", task="TT5", arrived=NOW - H)]
        bags = [bag("TXK01", 30) for _ in range(23)]
        t = db.build(rows, bags, set(), NOW, NIGHT, {})["trucks"][0]
        self.assertEqual((t["loaded"], t["out"], t["pull"]), (0, 23, 4))

    def test_partly_loaded_truck_only_needs_the_rest(self):
        rows = [truck("c", key="TXK01", dock="14", task="TT5", arrived=NOW - H)]
        bags = [bag("TXK01", 30) for _ in range(3)]
        t = db.build(rows, bags, set(), NOW, NIGHT, {"TT5": 20})["trucks"][0]
        self.assertEqual((t["loaded"], t["out"], t["pull"]), (20, 3, 4))
        self.assertIn("Loaded: 20", db.block(t))

    def test_st12_counts_from_5_pm_only(self):
        rows = [truck("d", dock="20", task="TT9")]
        bags = [bag("SGR01", 60 * 10) for _ in range(5)] + [bag("SGR01", 60) for _ in range(2)]  # 2:35 PM and 11:35 PM
        t = db.build(rows, bags, set(), NOW, NIGHT, {})["trucks"][0]
        self.assertEqual(t["out"], 2)

    def test_small_bags_and_open_bags_are_not_boxes(self):
        rows = [truck("d", dock="20", task="TT9")]
        open_bag = {**bag("SGR01", 60), "optFinishStatusTime": None}
        bags = [bag("SGR01", 60, waybills=10), open_bag, bag("SGR01", 60)]
        board = db.build(rows, bags, set(), NOW, NIGHT, {})
        self.assertEqual(board["trucks"][0]["out"], 1)
        self.assertEqual(board["pools"]["SGR01"]["filling"], 1)


class TripFromWms(unittest.TestCase):
    def test_fills_missing_sheet_times(self):
        """No arrival/departure in the sheet; the WMS had it signed out at 1:37 AM and unloaded at the station at 7:22 AM."""
        h = 3600000
        now = 1794470000000
        base = {"task": "TT1", "left": None, "arrived": None}
        rows = [dict(base, task="TT1"), dict(base, task="TT2"), dict(base, task="TT3"),
                dict(base, task="TT4", arrived=now - 2 * h), dict(base, task="TT5", arrived=now - 2 * h)]
        trips = {"TT1": ("loaded", now - 2 * h),      # signed out long ago, no sheet arrival: gone
                 "TT2": ("loaded", now - 5 * 60000),  # checking out right now: here
                 "TT4": ("loaded", now - h),          # at the dock per the sheet, paused: still here
                 "TT5": ("unloaded", now - h)}        # unloaded at the station: gone
        out = {r["task"]: r for r in db.with_trip(rows, trips, now)}
        self.assertEqual(out["TT1"]["left"], now - 2 * h)
        self.assertEqual(out["TT2"]["arrived"], now - 5 * 60000)
        self.assertIsNone(out["TT2"]["left"])
        self.assertEqual(out["TT3"], rows[2])
        self.assertIsNone(out["TT4"]["left"])
        self.assertEqual(out["TT5"]["left"], now - h)


class Sheet(unittest.TestCase):
    def test_rows_from_sheet_items(self):
        items = [{"record_id": "rec1", "fields": {"Departure date": DAY, "Destination": "TXK01", "Truck type": "26ft",
                                                  "Task no.": "TT2611120001", "Dock": "14", "Driver": "Driver 1"}},
                 {"record_id": "rec2", "fields": {"Departure date": DAY, "Destination": "CMH.H", "Task no.": "23:30"}},
                 {"record_id": "rec3", "fields": {"Departure date": DAY, "Destination": "BRO01", "Task no.": "cancelled"}},
                 {"record_id": "rec4", "fields": {"Destination": "BRO01"}}]  # no date: skipped
        rows = {r["id"]: r for r in db.rows_from(items)}
        self.assertEqual(sorted(rows), ["rec1", "rec2", "rec3"])
        self.assertEqual((rows["rec1"]["task"], rows["rec1"]["type"], rows["rec1"]["dock"]), ("TT2611120001", "26FT", "14"))
        self.assertEqual((rows["rec2"]["key"], rows["rec2"]["planned"], rows["rec2"]["task"]), ("STL", "23:30", None))
        self.assertTrue(rows["rec3"]["cancelled"])


if __name__ == "__main__":
    unittest.main()
