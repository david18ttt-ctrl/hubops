import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import hubconfig  # noqa: E402
import label_station as ls  # noqa: E402

NOW = datetime(2026, 11, 25, 21, 0)
MIN = 60000


def ms(t):
    return int(t.replace(tzinfo=hubconfig.TIME_ZONE).timestamp() * 1000)


def bag(n, chute, pcs, closed_min, opened_min=None, **extra):
    """A sorter bag closed `closed_min` minutes after 8 PM (None = still open)."""
    base = ms(NOW - timedelta(hours=1))
    opened = opened_min if opened_min is not None else (closed_min or 0) - 120
    return {"packageNo": f"BG01261125{n:04d}-{chute}", "bagType": ls.SORTER_BAG, "startCenterId": hubconfig.HUB_ID,
            "startCenterName": "HOU.H", "destinAreaCode": "VCT01-004", "destinCenterName": "VCT01", "waybillCount": pcs,
            "createTime": "2026-11-25 18:00:00", "createByName": "Sorter 1", "optCreateTime": base + opened * MIN,
            "optFinishStatusTime": None if closed_min is None else base + closed_min * MIN, "optCheckOutTime": None, **extra}


class FakeWms:
    """Answers the two calls the lookup makes, from a list of bags and {parcel: bag number}."""

    def __init__(self, bags, where):
        self.bags, self.where, self.calls = bags, where, []

    def __call__(self, box, path, body):
        self.calls.append(path)
        if path == ls.ROUTE_API:
            return {"list": [{"waybillNo": c, "packageNo": self.where[c]} for c in body["waybillNoList"] if c in self.where]}
        if body["packageNoList"]:
            return {"list": [b for b in self.bags if b["packageNo"] in body["packageNoList"]]}
        return {"list": list(self.bags)}


class PressChainTest(unittest.TestCase):
    def test_near_empty_bags_after_a_box_are_that_box(self):
        box, a, b = bag(1, "087", 400, 0), bag(2, "087", 1, 3), bag(3, "087", 10, 8)
        late, other = bag(4, "087", 2, 30), bag(5, "088", 3, 4)
        bags = [box, a, b, late, other]
        self.assertEqual([x["packageNo"] for x in ls.press_chain(b, bags)], [box["packageNo"], a["packageNo"], b["packageNo"]])
        self.assertEqual(ls.press_chain(late, bags), [late])  # more than 15 minutes after the box
        self.assertEqual(ls.press_chain(other, bags), [other])  # another chute, no box before it
        self.assertEqual(ls.press_chain(bag(6, "087", 300, 40), bags + [bag(6, "087", 300, 40)])[0]["waybillCount"], 300)

    def test_newest_on_chute(self):
        old, new = bag(1, "087", 400, 0), bag(2, "087", 5, None, opened_min=10)
        self.assertTrue(ls.newest_on_chute(new, [old]))
        self.assertFalse(ls.newest_on_chute(old, [new]))


class LookupTest(unittest.TestCase):
    def run_lookup(self, bags, where, codes):
        fake = FakeWms(bags, where)
        with mock.patch.object(ls.chute_alerts, "wms", fake):
            return ls.lookup(codes, now=NOW, box=["t"]), fake

    def test_closed_box_gets_its_label(self):
        box = bag(1, "087", 400, 0)
        (label, why), fake = self.run_lookup([box], {"PKG1": box["packageNo"]}, ["PKG9", "PKG1"])
        self.assertIsNone(why)
        self.assertEqual((label["bag"], label["route"], label["chute"], label["pcs"], label["pkg"]),
                         (box["packageNo"], "VCT01-004", "87", 400, "PKG1"))
        self.assertEqual(label["origin"], "HOU.H")
        self.assertEqual(fake.calls, [ls.ROUTE_API, ls.BAGS_API])  # two reads for a normal box

    def test_near_empty_bag_prints_the_real_box(self):
        box, tiny = bag(1, "087", 400, 0), bag(2, "087", 4, 3)
        (label, why), _ = self.run_lookup([box, tiny], {"PKG1": tiny["packageNo"]}, ["PKG1"])
        self.assertEqual(label["bag"], box["packageNo"])
        self.assertEqual(label["pcs"], 404)
        self.assertEqual(label["merge"], [tiny["packageNo"]])
        self.assertIn("near-empty", label["note"])

    def test_open_box_nobody_pressed(self):
        open_ = bag(3, "090", 120, None)
        (label, why), _ = self.run_lookup([open_], {"PKG1": open_["packageNo"]}, ["PKG1"])
        self.assertIsNone(label)
        self.assertIn("press it ONCE", why)

    def test_open_in_the_wms_but_the_button_was_pressed(self):
        open_ = bag(3, "090", 120, None, opened_min=-60)
        newer = bag(4, "090", 2, None, opened_min=30)
        (label, why), _ = self.run_lookup([open_, newer], {"PKG1": open_["packageNo"]}, ["PKG1"])
        self.assertIsNone(why)
        self.assertEqual(label["bag"], open_["packageNo"])

    def test_no_label_reasons(self):
        gone = bag(5, "087", 300, 0, optCheckOutTime=ms(NOW))
        gaylord = {"packageNo": "GL26112500042", "startCenterId": 13, "bagType": 1}
        where = {"PKG1": gone["packageNo"], "PKG2": gaylord["packageNo"], "PKG3": None}
        self.assertIn("already left", self.run_lookup([gone, gaylord], where, ["PKG1"])[0][1])
        self.assertIn("isn't a HOU.H bag", self.run_lookup([gone, gaylord], where, ["PKG2"])[0][1])
        self.assertIn("isn't in a bag", self.run_lookup([gone, gaylord], where, ["PKG3"])[0][1])
        self.assertIn("doesn't know", self.run_lookup([], {}, ["PKG4"])[0][1])


class DemoQueueTest(unittest.TestCase):
    def test_demo_queue_is_what_the_agent_reads(self):
        box = bag(1, "087", 400, 0)
        fake = FakeWms([box], {"PKG1": box["packageNo"]})
        with mock.patch.object(ls.chute_alerts, "wms", fake), mock.patch.object(ls.chute_alerts, "wms_token", lambda: "t"), \
                mock.patch.object(ls.hubconfig, "now_local", lambda: NOW), mock.patch("builtins.print"):
            jobs = ls.demo_queue(["PKG1", "PKG2"])
        self.assertEqual([(j["id"], j["label"]["bag"]) for j in jobs], [(1, box["packageNo"])])


if __name__ == "__main__":
    unittest.main()
