import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import do_now  # noqa: E402

T0 = datetime(2026, 11, 21, 20, 0)


class DoNowTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = Path(self.dir.name)
        for name, path in (("FILE", d / "do-now.json"), ("LOCK", d / "do-now.lock"), ("LOG", d / "do-now.log")):
            p = mock.patch.object(do_now, name, path)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.dir.cleanup()

    def data(self):
        with do_now._store() as d:
            return d

    def test_new_task_buzzes_once_update_stays_quiet(self):
        do_now.put("chute", "299|a", "Chute 299: swap", "Stuck 5 min", rank=10, now=T0)
        with do_now._store() as d:
            self.assertEqual(do_now.to_push(d)[0], "Chute 299: swap")
        do_now.put("chute", "299|a", "Chute 299: swap", "Stuck 10 min", rank=10, now=T0 + timedelta(minutes=5))
        with do_now._store() as d:
            self.assertIsNone(do_now.to_push(d))
            t = d["tasks"]["chute|299|a"]
            self.assertEqual(t["since"], "2026-11-21 20:00:00")
            self.assertEqual(t["why"], "Stuck 10 min")

    def test_replace_drops_what_source_no_longer_has(self):
        do_now.replace("dock", [{"tid": "1", "do": "Pull 5"}, {"tid": "2", "do": "Pull 3"}], now=T0)
        do_now.replace("dock", [{"tid": "2", "do": "Pull 1"}], now=T0)
        d = self.data()
        self.assertEqual(list(d["tasks"]), ["dock|2"])
        self.assertEqual(d["cleared"][-1]["id"], "dock|1")

    def test_replace_leaves_other_sources(self):
        do_now.put("coach", "x", "Move 2 people", now=T0)
        do_now.replace("dock", [], now=T0)
        self.assertIn("coach|x", self.data()["tasks"])

    def test_several_new_tasks_are_one_buzz(self):
        do_now.put("dock", "1", "Pull 5 BRO01 boxes to dock 14", rank=20, now=T0)
        do_now.put("chute", "51|a", "Chute 51 PHX.H: swap", rank=10, now=T0)
        with do_now._store() as d:
            title, body = do_now.to_push(d)
        self.assertEqual(title, "2 things to do")
        self.assertTrue(body.startswith("• Chute 51"))  # most important first

    def test_quiet_task_never_buzzes(self):
        do_now.put("coach", "x", "Still: pull boxes", push=False, now=T0)
        with do_now._store() as d:
            self.assertIsNone(do_now.to_push(d))

    def test_done_press_hides_task_even_if_job_puts_it_back(self):
        do_now.put("big", "BG1", "Check bag", now=T0)
        with do_now._store() as d:
            do_now.tidy(d, [{"id": "big|BG1", "at": "x"}], T0)
        do_now.put("big", "BG1", "Check bag", now=T0 + timedelta(minutes=10))
        with do_now._store() as d:
            do_now.tidy(d, [], T0 + timedelta(minutes=10))
            self.assertEqual(d["tasks"], {})

    def test_expired_task_goes(self):
        do_now.put("coach", "x", "Move", ttl_min=65, now=T0)
        with do_now._store() as d:
            do_now.tidy(d, [], T0 + timedelta(minutes=66))
            self.assertEqual(d["tasks"], {})
            self.assertEqual(d["cleared"][-1]["how"], "expired")

    def test_sync_sends_only_on_change_and_buzzes_new(self):
        sent, buzzed = [], []
        app_push = mock.MagicMock()
        app_push.notify.side_effect = lambda t, b, tag=None: buzzed.append((t, tag))

        def app(path, body=None):
            if path == "/app-done":
                return []
            sent.append(body)

        do_now.put("dock", "1", "Pull 5", now=T0)
        with mock.patch.object(do_now, "_app", app), mock.patch.dict("sys.modules", {"app_push": app_push}):
            do_now.sync(T0)
            do_now.sync(T0 + timedelta(minutes=1))
        self.assertEqual(len(sent), 1)
        self.assertEqual(buzzed, [("Pull 5", "donow")])


class ProducerTest(unittest.TestCase):
    def test_chute_tasks_only_alerted_and_rejecting_now(self):
        import chute_alerts as ca
        now = datetime(2026, 11, 21, 20, 30)
        state = {"stuck": {
            "299": {"start": "2026-11-21 20:15:00", "last": "2026-11-21 20:29:00", "sent": [5, 10], "dest": "AEX01-002",
                    "boxes": ["a", "b"], "rejects": 30},
            "51": {"start": "2026-11-21 20:00:00", "last": "2026-11-21 20:20:00", "sent": [5], "dest": "PHX.H",
                   "boxes": ["c"], "rejects": 10},  # quiet 10 min: someone swapped it
            "85": {"start": "2026-11-21 20:27:00", "last": "2026-11-21 20:29:00", "sent": [], "dest": "AUS01",
                   "boxes": ["d"], "rejects": 6}}}  # not alerted yet
        tasks = ca.chute_tasks(state, now)
        self.assertEqual([t["tid"] for t in tasks], ["299|2026-11-21 20:15:00"])
        self.assertIn("Chute 299 AEX01-002", tasks[0]["do"])
        self.assertEqual(tasks[0]["extra"]["fixed"], {"chute": "299", "start": "2026-11-21 20:15:00"})

    def test_coach_still_moves_dont_buzz(self):
        import floor_coach
        res = {"moves": [{"do": "Still: pull 7 boxes", "why": "x"}, {"do": "Bring 10 empties to 49-69", "why": "y"}]}
        tasks = floor_coach.coach_tasks(res, datetime(2026, 11, 21, 20, 8))
        self.assertEqual([t["push"] for t in tasks], [False, True])
        self.assertEqual(tasks[1]["tid"], "112120|1")


if __name__ == "__main__":
    unittest.main()
