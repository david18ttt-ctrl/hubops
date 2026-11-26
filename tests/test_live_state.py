"""Live state rules: an old floor count must never print as a number, a truck nobody unloaded must show, what is
left is split at midnight, and the hourly report's own numbers are read right."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))

import floor_now  # noqa: E402
import live_state as ls  # noqa: E402
import unsorted_board as ub  # noqa: E402

NOW = datetime(2026, 11, 21, 1, 30)


def truck(frm, arrived, left, full=10, state="", noscan_full=0, noscan_in_full=0, total=None):
    return {"from": frm, "arrived": arrived, "left": left, "total": total or left, "in_full": left, "full_gaylords": full,
            "gaylords": full, "task": "TT" + frm, "state": state, "sorted": 0, "noscan_full": noscan_full, "noscan_in_full": noscan_in_full}


class FloorLines(unittest.TestCase):
    def test_old_board_prints_unknown_and_no_number(self):
        fl = {"read": "2026-11-20 06:43", "trucks": [truck("ABQ", "2026-11-19 21:01", 85800)], "strays": 0, "stray_trucks": 0}
        text = "\n".join(ls.floor_lines(fl, {"per_hour": 27000}, NOW))
        self.assertIn("UNKNOWN", text)
        self.assertNotIn("85", text)  # a day-old count must not appear as tonight's
        self.assertNotIn("ABQ", text)

    def test_split_at_midnight_and_names_the_truck_nobody_unloaded(self):
        fl = {"read": "2026-11-21 01:25", "took_s": 52, "strays": 900, "stray_trucks": 3, "trucks": [
            truck("STL", "2026-11-20 22:55", 15800, 31, "not unloaded", 31, 15800),
            truck("STL", "2026-11-20 23:57", 11500, 22),
            truck("PHX", "2026-11-21 00:01", 6200, 13)]}
        lines = ls.floor_lines(fl, {"per_hour": 27300}, NOW)
        tonight = next(line for line in lines if "tonight's" in line)
        later = next(line for line in lines if "after midnight" in line)
        self.assertIn("27.3k", tonight)  # 15.8k + 11.5k
        self.assertIn("NOT UNLOADED", tonight)
        self.assertIn("1.0 h of belt", tonight)
        self.assertIn("6.2k", later)
        self.assertNotIn("PHX", tonight)
        self.assertIn("read 01:25, 5 min ago", lines[0])

    def test_before_six_am_the_cutoff_is_last_midnight(self):
        self.assertEqual(ls.tonight_cutoff(datetime(2026, 11, 21, 1, 30)), datetime(2026, 11, 21))
        self.assertEqual(ls.tonight_cutoff(datetime(2026, 11, 20, 20, 0)), datetime(2026, 11, 21))
        self.assertEqual(ls.tonight_cutoff(datetime(2026, 11, 21, 7, 0)), datetime(2026, 11, 22))

    def test_after_six_am_last_nights_leftovers_are_carried_over_not_tonights(self):
        # after 6 AM, trucks in before the previous midnight are an earlier night's freight, not tonight's
        fl = {"read": "2026-11-21 06:39", "strays": 0, "stray_trucks": 0, "trucks": [
            truck("STL", "2026-11-20 22:55", 15400, 31), truck("PHX", "2026-11-21 01:29", 11200, 30),
            truck("BOS", "2026-11-21 06:01", 13500, 30)]}
        lines = ls.floor_lines(fl, {"per_hour": 27000}, datetime(2026, 11, 21, 6, 42))
        carried = next(line for line in lines if "carried over" in line)
        tonight = next(line for line in lines if "tonight's" in line)
        self.assertIn("15.4k", carried)
        self.assertIn("STL", carried)
        self.assertIn("24.7k", tonight)  # PHX 1:29 AM + BOS 6:01 AM are the new night's
        self.assertNotIn("STL", tonight)

    def test_before_six_am_nothing_is_carried_from_tonight(self):
        lines = ls.floor_lines({"read": "2026-11-21 01:25", "strays": 0, "stray_trucks": 0,
                                "trucks": [truck("STL", "2026-11-20 22:55", 15800)]}, {"per_hour": 27000}, NOW)
        self.assertFalse(any("carried over" in line for line in lines))
        self.assertIn("15.8k", next(line for line in lines if "tonight's" in line))

    def test_stopped_belt_uses_the_normal_pace_and_says_so(self):
        fl = {"read": "2026-11-21 01:28", "strays": 0, "stray_trucks": 0, "trucks": [truck("STL", "2026-11-20 23:57", 25734)]}
        text = "\n".join(ls.floor_lines(fl, {"per_hour": 0, "stopped": True, "normal": 25734}, NOW))
        self.assertIn("1.0 h of belt at the normal 25.7k/hr", text)


class Watch(unittest.TestCase):
    def test_hours(self):
        self.assertTrue(ls.in_hours((16, 8), datetime(2026, 11, 21, 1, 0)))
        self.assertFalse(ls.in_hours((16, 8), datetime(2026, 11, 21, 12, 0)))
        self.assertTrue(ls.in_hours(None, datetime(2026, 11, 21, 12, 0)))

    def test_dead_board_is_a_problem_and_alerts_once(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ls, "DATA", Path(tmp)), \
                mock.patch.object(ls, "WATCH", (("floor board", "floor numbers", "unsorted-latest.json", 20, None),)), \
                mock.patch.object(ls.subprocess, "run", side_effect=OSError), mock.patch("app_push.alert") as alert:
            (Path(tmp) / "unsorted-latest.json").write_text(json.dumps({"made": "2026-11-20 06:43"}))
            first = ls.watch({}, {}, NOW)
            self.assertIn("floor board", first["problems"])
            self.assertIn("18 h 47 min", first["problems"]["floor board"])
            alert.assert_not_called()  # one run is not an alert yet
            second = ls.watch({}, first, NOW + timedelta(minutes=5))
            self.assertEqual(alert.call_count, 1)
            ls.watch({}, second, NOW + timedelta(minutes=10))
            self.assertEqual(alert.call_count, 1)  # not again for 6 h
            (Path(tmp) / "unsorted-latest.json").write_text(json.dumps({"made": "2026-11-21 01:38"}))
            self.assertNotIn("floor board", ls.watch({}, second, NOW + timedelta(minutes=10))["problems"])


class Freight(unittest.TestCase):
    def board(self, made, trucks):
        tmp = Path(tempfile.mkdtemp()) / "unsorted-latest.json"
        tmp.write_text(json.dumps({"made": made, "trucks": trucks}))
        return mock.patch.object(floor_now, "UNSORTED", tmp)

    def test_old_board_is_an_error_not_a_count(self):
        with self.board("2026-11-20 06:43", []):
            got = floor_now.freight(NOW)
        self.assertIn("error", got)
        self.assertNotIn("in_full", got)

    def test_floor_and_not_unloaded_are_apart(self):
        trucks = [{"from": "STL.H", "arrived": "2026-11-20 22:55:45", "left": 15800, "in_full": 15800, "open": 31, "state": "not unloaded",
                   "noscan_full": 31, "noscan_in_full": 15800, "bags": [{"code": "8930", "full": True, "noscan": True}]},
                  {"from": "STL.H", "arrived": "2026-11-20 23:57:00", "left": 11500, "in_full": 11300, "open": 22, "bags": [{"code": "0671", "full": True}]},
                  {"from": "PHX.H", "arrived": "2026-11-21 00:01:00", "left": 6200, "in_full": 4900, "open": 13, "bags": []}]
        with self.board("2026-11-21 01:25", trucks):
            got = floor_now.freight(NOW)
        self.assertEqual(got["in_full"], 11300 + 4900)  # on the floor: the unscanned trailer is not in it
        self.assertEqual((got["not_unloaded_pcs"], got["not_unloaded"][0]["gaylords"]), (15800, 31))
        self.assertEqual((got["tonight"], got["later"]), (15800 + 11500, 6200))


class Post(unittest.TestCase):
    TEXT = """HOU.H sort report
11-20 06:00~11-21 06:00 CT  Updated: 2026-11-21 00:16
Waiting to bag
Linehaul
26,932  Branch
19,395
Due before cutoff today
0 trucks  0 pcs
HOU.H sort report
11-20 06:00~11-21 06:00 CT  Updated: 2026-11-21 01:16
Waiting to bag
Linehaul
21,484  Branch
34,401
Due before cutoff today
2 trucks  20,671 pcs
Bagged this hour
⏳ Waiting to bag
🚛 Linehaul:
STL.H
:
6,948
MSP.H
:
6,205
🚚 Branch:
AUS01
:
10,326
🚛 Due to arrive (23 trucks)
TT2611200062  9,294  SEA.H-HOU.H(12:47)  ETA: 2026-11-21 01:15:32 (CT)
"""

    def test_newest_posts_numbers(self):
        got = floor_now.parse_post(self.TEXT)
        self.assertEqual(got["posted"], "2026-11-21 01:16")
        self.assertEqual(got["waiting"], {"trunk": 21484, "branch": 34401})
        self.assertEqual(got["before_cutoff"], {"trucks": 2, "pcs": 20671})
        self.assertEqual(got["by_dest"], {"STL.H": 6948, "MSP.H": 6205, "AUS01": 10326})

    def test_truck_list_from_the_newest_post(self):
        got = floor_now.parse_trucks(self.TEXT, datetime(2026, 11, 21, 1, 20))
        self.assertEqual(got, [{"task": "TT2611200062", "pkgs": 9294, "route": "SEA.H", "eta": "2026-11-21 01:15"}])

    def test_read_soon_after_each_hourly_post(self):
        cached = {"read": "2026-11-21T01:01", "posted": "2026-11-21 00:16", "waiting": {}}
        self.assertFalse(floor_now.read_due(cached, datetime(2026, 11, 21, 1, 10)))  # the 1:16 post isn't up yet
        self.assertTrue(floor_now.read_due(cached, datetime(2026, 11, 21, 1, 18)))   # it is now
        fresh = {"read": "2026-11-21T01:18", "posted": "2026-11-21 01:16", "waiting": {}}
        self.assertFalse(floor_now.read_due(fresh, datetime(2026, 11, 21, 1, 40)))
        self.assertTrue(floor_now.read_due(fresh, datetime(2026, 11, 21, 2, 18)))


class Speed(unittest.TestCase):
    def test_nothing_fed_this_hour_is_stopped_not_last_hours_pace(self):
        hours = {"2026-11-21 01": {"": (22576, 700), "3": (900, 20)}}
        with mock.patch("chute_alerts.wms_token", return_value="t"), mock.patch("feeder_pace.fetch_day", return_value=[]), \
                mock.patch("feeder_pace.by_hour", return_value=hours):
            got = floor_now.speed(datetime(2026, 11, 21, 2, 25))
        self.assertTrue(got["stopped"])
        self.assertEqual((got["per_hour"], got["last_hour"]["fed"]), (0, 22576))


class SortedCheck(unittest.TestCase):
    def test_untouched_gaylords_are_sampled_and_a_started_one_is_asked_in_full(self):
        db = sqlite3.connect(":memory:")
        db.executescript("CREATE TABLE sorted (waybill TEXT PRIMARY KEY, seen TEXT); CREATE TABLE cache (key TEXT PRIMARY KEY, fetched TEXT, data TEXT);")
        now = datetime(2026, 11, 21, 1, 30)
        db.execute("INSERT INTO cache VALUES ('strays_asked', ?, '')", (now.isoformat(),))  # leftovers not due this run
        quiet = {f"Q{i:04}": "AUS01" for i in range(400)}    # untouched, stays untouched
        started = {f"S{i:04}": "ACT01" for i in range(400)}  # untouched so far, dumped just now
        trucks = {"TT1": {"actualArrivalTime": "2026-11-20 23:57:00"}}
        loads = {"TT1": {"bags": {"quiet": {"waybills": quiet}, "started": {"waybills": started}}}}
        asked = []

        def fake(token, numbers, since):
            asked.append(list(numbers))
            return {w for w in numbers if w.startswith("S") and int(w[1:]) < 300}  # 300 of the started gaylord are sorted

        with mock.patch.object(ub.hn, "sorted_waybills", side_effect=fake):
            done = ub.refresh_sorted(db, "t", trucks, loads, now - timedelta(days=2), now)
        self.assertEqual(len(asked[0]), 2 * ub.SAMPLE)          # 20 from each gaylord, not 800
        self.assertEqual(len(asked[1]), 400 - ub.SAMPLE)        # then the rest of the one that started
        self.assertFalse(any(w.startswith("Q") for w in asked[1]))
        self.assertEqual(len(done), 300)


if __name__ == "__main__":
    unittest.main()
