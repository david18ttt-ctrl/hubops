"""look.py: telling a box from a bag, a truck and a destination; the WMS's three time formats; trucks in and out
worded right; the incoming gaylord never mistaken for the bag a box is in now."""
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))

import look  # noqa: E402


class KindOf(unittest.TestCase):
    def test_kinds(self):
        for thing, kind in (("PKG010852010082", "box"), ("pkg010852010082", "box"), ("1Z999AA10123456784", "box"),
                            ("GL2611140219", "bag"), ("BG012611210074-199", "bag"), ("SB0119000646", "bag"),
                            ("TT2611210034", "truck"), ("phx", "dest"), ("VCT01", "dest"),
                            ("act01-051", "dest"), ("STL.H", "dest")):
            self.assertEqual(look.kind_of(thing), kind, thing)


class Places(unittest.TestCase):
    NAMES = {"PHX.R": 1, "PHX.H": 148, "AUS01": 251, "STL.H": 97, "ACT01": 5, "SGR01": 6}

    def test_short_names(self):
        self.assertEqual(look.best_place(self.NAMES, "phx"), "PHX.H")  # the hub, not a region with the same start
        self.assertEqual(look.best_place(self.NAMES, "aus"), "AUS01")
        self.assertEqual(look.best_place(self.NAMES, "stl.h"), "STL.H")
        self.assertEqual(look.best_place(self.NAMES, "ACT01"), "ACT01")
        self.assertIsNone(look.best_place(self.NAMES, "zzz"))


class Dest(unittest.TestCase):
    def test_a_station_name_is_a_station_not_a_route(self):
        bags = [{"packageNo": "BG1-44", "destinCenterName": "ACT01", "waybillCount": 120, "bagTypeString": "Sorter",
                 "optCreateTime": 1795240000000, "optFinishStatusTime": 1795250000000}]
        fake = mock.Mock(side_effect=lambda path, body: {"list": bags, "total": 1} if body.get("pageNum") == 1 else {})
        fake.regions.return_value = {}
        with mock.patch.object(look, "board", return_value=({}, None, "floor board unreadable", [])), \
                mock.patch.object(look, "now_local", return_value=datetime(2026, 11, 21, 6, 45)), mock.patch("sys.stdout") as out:
            look.look_dest(fake, "act01")
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("DESTINATION ACT01 ", text)
        self.assertIn("BG1-44", text)


class Times(unittest.TestCase):
    def test_three_formats(self):
        want = datetime(2026, 11, 21, 4, 25, 40)
        for text in ("2026-11-21 04:25:40", "11/21/2026 04:25:40", "04:25:40 21/11/2026"):
            self.assertEqual(look.parse_time(text), want, text)
        self.assertIsNone(look.parse_time(None))

    def test_chute_from_bag_number(self):
        self.assertEqual(look.chute_of("BG012611210074-199"), "199")
        self.assertEqual(look.chute_of("BG012611210074-051"), "51")
        self.assertEqual(look.chute_of("GL2611140219"), "")


class TruckLine(unittest.TestCase):
    NOW = datetime(2026, 11, 21, 6, 45)

    def test_inbound_counts_what_was_scanned_off_here(self):
        t = {"placeOfOrigin": "STL.H", "destination": "HOU.H", "taskNo": "TT1", "actualDepartureTime": "2026-11-20 17:42:37",
             "actualArrivalTime": "2026-11-20 22:55:45", "loadBagCount": 31, "loadWaybillCount": 15812,
             "unLoadBagCount": 31, "unLoadWaybillCount": 15405}
        line = look.truck_line(t, self.NOW)
        self.assertIn("arrived 11/20 10:55 PM", line)
        self.assertIn("15,405 pcs scanned off here", line)

    def test_outbound_is_at_the_dock_or_left_never_scanned_off_here(self):
        dock = {"placeOfOrigin": "HOU.H", "destination": "MSP.H", "taskNo": "TT2", "timeOfArrivalOrigin": "2026-11-21 04:47:00",
                "plannedDepartureTime": "2026-11-21 13:11:00"}
        self.assertIn("AT THE DOCK since 4:47 AM (leave by 1:11 PM)", look.truck_line(dock, self.NOW))
        gone = {"placeOfOrigin": "HOU.H", "destination": "STL.H", "taskNo": "TT3", "actualDepartureTime": "2026-11-20 21:07:00",
                "actualArrivalTime": "2026-11-21 01:49:00", "unLoadWaybillCount": 7365}
        line = look.truck_line(gone, self.NOW)
        self.assertIn("left here 11/20 9:07 PM, arrived STL.H 1:49 AM", line)
        self.assertNotIn("scanned off here", line)


class Box(unittest.TestCase):
    def test_incoming_gaylord_is_not_the_bag_it_is_in(self):
        # the waybill list's packageNo can be the incoming gaylord; the box must not print as SORTED in it
        scans = [
            {"operateType": 200, "operateTypeDesc": "Transit Center Checkout", "operatorDeptName": "STL.H", "bagNo": "GL1",
             "checkoutNo": "CO1", "operateTimestamp": 1795200000000},
            {"operateType": 411, "operateTypeDesc": "Vehicle Departure", "operatorDeptName": "STL.H", "bagNo": "GL1",
             "operateTimestamp": 1795201000000},
            {"operateType": 412, "operateTypeDesc": "Vehicle Arrival", "operatorDeptName": "HOU.H", "operateTimestamp": 1795245257000},
            {"operateType": 211, "operateTypeDesc": "Unload the goods", "operatorDeptName": "HOU.H", "bagNo": "GL1",
             "operateTimestamp": 1795256740829}]

        def wms(path, body):
            if path == look.SCANS_API:
                return {"list": scans, "total": len(scans)}
            if path == look.ROUTE_API:
                return {"list": [{"waybillNo": "PKG1", "packageNo": "GL1", "destinationCenter": "AUS01", "courierWorkArea": "AUS01-142"}]}
            if path == look.BAGS_API:  # the gaylord comes back even with startCenterId (the WMS ignores it for a number)
                return {"list": [{"packageNo": "GL1", "startCenterName": "STL.H", "destinCenterName": "HOU.H"}]}
            return {}
        fake = mock.Mock(side_effect=wms)
        fake.regions.return_value = {}
        truck = {"task": "TT9", "from": "STL.H", "arrived": "2026-11-20 22:55:45"}
        with mock.patch.object(look, "board", return_value=({"GL1": (truck, {"left": 793, "of": 793})}, datetime(2026, 11, 21, 6, 39), "", [])), \
                mock.patch("sys.stdout") as out:
            look.look_box(fake, "PKG1")
        text = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("NOT SORTED: in gaylord GL1", text)
        self.assertIn("STL.H truck TT9", text)
        self.assertNotIn("SORTED, in bag", text)


if __name__ == "__main__":
    unittest.main()
