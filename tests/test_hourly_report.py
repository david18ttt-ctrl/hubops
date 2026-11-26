"""hourly_report.py: counting rules, the saved table, the stale-data refusals, posting and the failure alert."""
import csv
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "hubops"))
import hourly_report as hr  # noqa: E402
import wms_client  # noqa: E402

NIGHT = datetime(2026, 11, 24, 22, 4)


def bag(no, dest, pcs, made, kind=2):
    return {"packageNo": no, "destinCenterName": dest, "waybillCount": pcs, "createTime": f"{made:%Y-%m-%d %H:%M:%S}",
            "bagType": kind}


def bag_list(now=NIGHT):
    return [
        bag("BG01-1", "AUS01", 300, now - timedelta(minutes=30)),  # 21:34, in the 22:00 report's hour (21-22)
        bag("BG01-2", "AUS01", 200, datetime(2026, 11, 24, 20, 10)),
        bag("BG01-3", "VCT01", 26000, datetime(2026, 11, 24, 19, 0)),  # at capacity: closed before its cutoff
        bag("BG01-4", "PHX.H", 500, datetime(2026, 11, 24, 21, 59)),
        bag("BG01-5", "SJT01", 900, datetime(2026, 11, 24, 10, 0), kind=3),  # a customer's pre-alert batch
        bag("BG01-6", "XYZ01", 40, datetime(2026, 11, 24, 18, 0)),  # not in the report's list
        bag("BG01-7", "", 5, datetime(2026, 11, 24, 18, 5)),
    ]


class TempOut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patcher = mock.patch.object(hr, "OUT_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)


class Clock(unittest.TestCase):
    def test_end_hour_and_this_hour(self):
        self.assertEqual(hr.hour_label(NIGHT), "22:00")
        self.assertEqual(hr.slot_name(hr.this_hour_slot(NIGHT)), "21:00")
        late = datetime(2026, 11, 25, 1, 5)
        self.assertEqual(hr.hour_label(late), "1:00")
        self.assertEqual(hr.slot_name(hr.this_hour_slot(late)), "00:00")

    def test_morning_keeps_last_night_until_11(self):
        morning = datetime(2026, 11, 25, 9, 4)
        self.assertEqual(hr.operating_day(morning).isoformat(), "2026-11-24")
        self.assertEqual(hr.hour_label(morning), "6:00")
        self.assertEqual(hr.slot_name(hr.this_hour_slot(morning)), "05:00")
        self.assertEqual(hr.operating_day(datetime(2026, 11, 25, 11, 4)).isoformat(), "2026-11-25")

    def test_closed_past_cutoff_or_at_capacity(self):
        self.assertTrue(hr.row_is_closed(NIGHT, "20:30", 0.4))
        self.assertFalse(hr.row_is_closed(NIGHT, "23:30", 0.4))
        self.assertTrue(hr.row_is_closed(NIGHT, "23:30", 1.0))
        # 1 AM counts as after 23:30, not before it
        self.assertTrue(hr.row_is_closed(datetime(2026, 11, 25, 1, 0), "23:30", 0.1))
        self.assertFalse(hr.row_is_closed(datetime(2026, 11, 25, 1, 0), "3:00", 0.1))
        self.assertTrue(hr.row_is_closed(datetime(2026, 11, 25, 9, 0), "3:00", None))
        self.assertFalse(hr.row_is_closed(NIGHT, "not a time", None))


class Counting(unittest.TestCase):
    def test_tally_by_destination_and_hour(self):
        counts = hr.tally(bag_list(), NIGHT.date())
        self.assertNotIn("SJT01", counts)  # forecast batches don't count
        self.assertEqual(sum(counts["AUS01"]["pcs"]), 500)
        self.assertEqual(counts["AUS01"]["pcs"][15], 300)  # 21:00-22:00 is slot 15
        self.assertEqual(counts["AUS01"]["bags"][14], 1)
        self.assertEqual(sum(counts[hr.NO_DESTINATION]["pcs"]), 5)

    def test_table_totals_and_unlisted(self):
        counts = hr.tally(bag_list(), NIGHT.date())
        table, unlisted = hr.build_table(counts, NIGHT, NIGHT, 6)
        self.assertEqual(unlisted, {"XYZ01": 40, hr.NO_DESTINATION: 5})
        rows = {r["destination"]: r for r in table["stations"] + table["hubs"]}
        self.assertEqual(rows["AUS01"]["hour_pcs"], 300)
        self.assertEqual(rows["AUS01"]["pcs"], 500)
        self.assertTrue(rows["AUS01"]["closed"])  # 20:30 cutoff
        self.assertTrue(rows["VCT01"]["closed"])  # past its cutoff and at 100%
        self.assertFalse(rows["SJT01"]["closed"])
        self.assertIsNone(rows["TXK01"]["progress"])  # no limit
        self.assertAlmostEqual(rows["VCT01"]["progress"], 1.0)
        self.assertEqual(table["hub_total"]["pcs"], 500)
        self.assertEqual(table["grand_total"]["pcs"], 500 + 26000 + 500 + 40 + 5)
        self.assertEqual(table["station_total"]["capacity"], sum(c for _, _, c in hr.STATIONS if c))
        self.assertEqual(table["this_hour"], "21:00-22:00")
        self.assertEqual(table["slots"][-1], "22:00")  # through the hour now running


class SavedTable(TempOut):
    def test_csv_round_trip_matches_the_wms(self):
        counts = hr.tally(bag_list(), NIGHT.date())
        table, _ = hr.build_table(counts, NIGHT, NIGHT, 6)
        path = hr.save_table(table)
        hr.check_saved(path, 27045, 6)
        with path.open(newline="") as f:
            rows = list(csv.DictReader(f))
        aus = next(r for r in rows if r["destination"] == "AUS01")
        self.assertEqual((aus["pcs 21:00"], aus["pcs 20:00"], aus["closed"]), ("300", "200", "yes"))
        self.assertEqual(hr.load_table()["read_at"], "2026-11-24 22:04:00")

    def test_a_table_that_does_not_add_up_is_refused(self):
        table, _ = hr.build_table(hr.tally(bag_list(), NIGHT.date()), NIGHT, NIGHT, 6)
        path = hr.save_table(table)
        with self.assertRaises(hr.ReportProblem):
            hr.check_saved(path, 27046, 6)


class Stale(TempOut):
    def test_short_read_is_refused(self):
        short = {"rows": [bag("BG01-1", "AUS01", 3, NIGHT)], "total": 2}
        with mock.patch.object(hr, "read_bags", return_value=short) as read:
            with self.assertRaises(hr.ReportProblem):
                hr.fetch_bags(NIGHT.date(), NIGHT)
        self.assertEqual(read.call_count, hr.READ_TRIES)

    def test_bag_list_that_stopped_while_sorting(self):
        old = [bag("BG01-1", "AUS01", 3, NIGHT - timedelta(hours=2))]
        self.assertIn("stale", hr.stale_reason(old, NIGHT))
        self.assertIsNone(hr.stale_reason([bag("BG01-1", "AUS01", 3, NIGHT - timedelta(minutes=5))], NIGHT))
        self.assertIsNone(hr.stale_reason(old, datetime(2026, 11, 24, 13, 4)))  # sorter not running: no check

    def test_old_read_or_old_picture_is_not_posted(self):
        table = {"read_at": "2026-11-24 22:04:00"}
        picture = Path(self.tmp.name) / "p.png"
        picture.write_bytes(b"png")
        hr.check_fresh(table, picture, "playwright", datetime(2026, 11, 24, 22, 10))
        with self.assertRaises(hr.ReportProblem):
            hr.check_fresh(table, picture, "playwright", datetime(2026, 11, 24, 22, 40))
        future = {"read_at": (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")}
        with self.assertRaises(hr.ReportProblem):  # drawn before this read
            hr.check_fresh(future, picture, "playwright", datetime.now() + timedelta(hours=1))


class Picture(TempOut):
    def test_html_has_every_destination_and_the_read_time(self):
        table, _ = hr.build_table(hr.tally(bag_list(), NIGHT.date()), NIGHT, NIGHT, 6)
        page = hr.report_html(table)
        for name, _, _ in hr.STATIONS:
            self.assertIn(name, page)
        for name in hr.HUBS + ["XYZ01"]:
            self.assertIn(name, page)
        self.assertIn("WMS bag list read 11/24 22:04", page)
        self.assertIn('class="b green"', page)

    def test_html_only_when_no_renderer(self):
        table, _ = hr.build_table(hr.tally(bag_list(), NIGHT.date()), NIGHT, NIGHT, 6)
        with mock.patch.dict(os.environ, {"HUBOPS_REPORT_RENDERER": "html"}):
            path, how = hr.render_image(table)
        self.assertEqual((how, path.name), ("html", hr.HTML_NAME))

    def test_a_missing_renderer_is_a_failure_not_a_stale_picture(self):
        table, _ = hr.build_table(hr.tally(bag_list(), NIGHT.date()), NIGHT, NIGHT, 6)
        with mock.patch.dict(os.environ, {"HUBOPS_REPORT_RENDERER": "matplotlib"}), \
                mock.patch.object(hr, "render_matplotlib", side_effect=ImportError("No module named 'matplotlib'")):
            with self.assertRaises(hr.ReportProblem):
                hr.render_image(table)


class Run(TempOut):
    def run_main(self, argv, rows=None, now=NIGHT, renderer="html"):
        rows = bag_list(now) if rows is None else rows
        with mock.patch.object(hr, "read_bags", return_value={"rows": rows, "total": len(rows)}), \
                mock.patch.dict(os.environ, {"HUBOPS_REPORT_RENDERER": renderer}), \
                mock.patch.object(hr.chat_api, "send_text") as text, \
                mock.patch.object(hr.chat_api, "send_image") as image, \
                mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            code = hr.main(argv, now=now)
        return code, text, image

    def test_post_then_skip_the_same_hour(self):
        code, text, _ = self.run_main(["--chat", "report", "--alert-chat", "ops", "--skip-if-done"])
        self.assertEqual(code, 0)
        posted = [c for c in text.call_args_list if c.kwargs.get("to") == "report"]
        self.assertEqual(len(posted), 1)
        self.assertTrue(posted[0].args[0].startswith("11/24   22:00"))
        unlisted = [c for c in text.call_args_list if c.kwargs.get("to") == "ops"]
        self.assertIn("XYZ01", unlisted[0].args[0])
        code, text, image = self.run_main(["--chat", "report", "--skip-if-done"])
        self.assertEqual(code, 0)
        text.assert_not_called()
        image.assert_not_called()

    def test_picture_is_posted_as_an_image(self):
        def fake_render(t):
            path = hr.out(hr.PNG_NAME)
            path.write_bytes(b"png")
            return path, "playwright"
        with mock.patch.object(hr, "render_image", side_effect=fake_render):
            code, text, image = self.run_main(["--chat", "report"], now=datetime.now().replace(microsecond=0))
        self.assertEqual(code, 0)
        image.assert_called_once()
        self.assertEqual(image.call_args.kwargs["to"], "report")

    def test_no_posts_in_the_morning_lull(self):
        code, text, image = self.run_main(["--chat", "report", "--skip-if-done"], now=datetime(2026, 11, 25, 9, 4))
        self.assertEqual(code, 0)
        text.assert_not_called()

    def test_stale_data_alerts_and_posts_nothing(self):
        old = [bag("BG01-1", "AUS01", 3, NIGHT - timedelta(hours=3))]
        code, text, image = self.run_main(["--chat", "report", "--alert-chat", "ops"], rows=old)
        self.assertEqual(code, 1)
        image.assert_not_called()
        self.assertEqual([c.kwargs.get("to") for c in text.call_args_list], ["ops"])
        self.assertIn("failed, nothing posted", text.call_args.args[0])
        self.assertFalse(hr.out(hr.POSTED_NAME).exists())

    def test_wms_down_alerts(self):
        with mock.patch.object(hr, "read_bags", side_effect=wms_client.WmsError("WMS not reachable")), \
                mock.patch.object(hr.chat_api, "send_text") as text, mock.patch("sys.stderr"):
            code = hr.main(["--chat", "report", "--alert-chat", "ops"], now=NIGHT)
        self.assertEqual(code, 1)
        self.assertIn("WMS not reachable", text.call_args.args[0])

    def test_dry_run_posts_nothing(self):
        code, text, image = self.run_main(["--dry-run"])
        self.assertEqual(code, 0)
        text.assert_not_called()
        image.assert_not_called()
        self.assertTrue(hr.out(hr.CSV_NAME).exists())


class AgainstTheSimulator(TempOut):
    """One read of the simulated WMS: the report's total is the simulator's own count."""

    def test_grand_total_matches_the_simulator(self):
        sys.path.insert(0, str(ROOT / "hubsim"))
        import model as m
        import server
        httpd, sim = server.make_server(port=0, clock="live", quiet=True, fixed_now=NIGHT)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with mock.patch.object(wms_client, "BASE_URL", f"http://127.0.0.1:{httpd.server_address[1]}"):
            t0 = time.time()
            table, unlisted, path = hr.build(NIGHT, "test")
        start = datetime(2026, 11, 24, 6)
        want = sum(m.bag_count(b, NIGHT) for n in sim.nights() for b in n.bags
                   if b["kind"] != "forecast" and start <= b["created"] <= NIGHT)
        self.assertEqual(table["grand_total"]["pcs"], want)
        self.assertEqual(unlisted, {})
        self.assertGreater(table["station_total"]["pcs"], table["hub_total"]["pcs"])
        self.assertLess(time.time() - t0, 30)


if __name__ == "__main__":
    unittest.main()
