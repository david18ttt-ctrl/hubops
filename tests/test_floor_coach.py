import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import floor_coach as fc  # noqa: E402

NOW = datetime(2026, 11, 25, 22, 8)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def api_message(text, stop="end_turn"):
    return FakeResponse(json.dumps({"type": "message", "stop_reason": stop, "content": [
        {"type": "thinking", "thinking": ""}, {"type": "text", "text": text}]}).encode())


class DataDirTest(unittest.TestCase):
    """Points the coach at an empty temporary data/ folder."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.data = Path(self.dir.name)
        for name, value in (("DATA", self.data), ("OUT_DIR", self.data / "coach")):
            p = mock.patch.object(fc, name, value)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, text):
        (self.data / name).write_text(text)


class AreaTest(unittest.TestCase):
    def test_areas(self):
        self.assertEqual(fc.area_of("012"), "Zone 2 001-048")
        self.assertEqual(fc.area_of("050"), "Zone 2 049-069")
        self.assertEqual(fc.area_of(190), "Zone 1 188-215")
        self.assertEqual(fc.area_of("240"), "Zone 1 other chutes")
        self.assertEqual(fc.area_of(None), "?")

    def test_groups(self):
        self.assertEqual(fc.group_of("VCT01-004"), "VCT01")
        self.assertEqual(fc.group_of("PHX.H"), "PHX.H (hub)")
        self.assertEqual(fc.group_of(""), "?")


class SnapshotTest(DataDirTest):
    LOG = "\n".join([
        "11/25 19:30 SEA.H (chute 052) · stuck 40 min · 500 boxes going around · since 6:50 PM",
        "11/25 21:20 STL.H (chute 050) · stuck 7 min · 96 boxes going around · since 9:13 PM",
        "11/25 21:44 SEA.H (chute 052) · STILL stuck 21 min · 93 boxes going around · since 9:22 PM | "
        "STL.H (chute 050) · STILL stuck 16 min · 301 boxes going around · since 9:28 PM",
        "11/25 21:50 ✓ SEA.H (chute 052) · fixed: no rejects since 9:35 PM · was stuck 13 min (9:22 PM–9:35 PM)",
        "11/25 21:51 AUS01-007 (chute 231) · stuck 9 min · 40 boxes going around · since 9:42 PM",
        "11/25 21:52 sent: 'STL.H (chute 050) · STILL stuck 99 min · 999 boxes going around'",
    ])

    def test_stuck_last_hour_by_area(self):
        self.write("chute-alerts.log", self.LOG)
        rows = fc.stuck_from_log(NOW)
        self.assertEqual(rows[("STL.H", "050")], [16, 301, False])  # the "sent:" echo line is ignored
        self.assertEqual(rows[("SEA.H", "052")], [21, 93, "fixed"])  # the 7:30 PM line is outside the hour
        text = fc.stuck_text(NOW)
        self.assertTrue(text.startswith("- Zone 2 049-069: 2 chutes stuck, 394 boxes circling at worst"))
        self.assertIn("chute 052 SEA.H (hub): stuck up to 21 min, 93 boxes circling (fixed since)", text)
        self.assertIn("- Zone 1 other chutes: 1 chutes stuck, 40 boxes", text)

    def test_tonight_counts_since_4pm(self):
        self.write("chute-alerts.log", self.LOG)
        text = fc.tonight_stuck(NOW)
        self.assertEqual(text.splitlines()[0], "- SEA.H (hub) [Zone 2 049-069]: 40 stuck-minutes")

    def test_no_log_means_no_stuck(self):
        self.assertEqual(fc.stuck_text(NOW), "No chute was stuck in the last hour.")

    def test_feeders_slow_fast_high(self):
        stations = [{"s": "2", "fed": 197, "normal": 531, "rej": None, "rej_normal": 1.2},
                    {"s": "11", "fed": 900, "normal": 600, "rej": 1.0, "rej_normal": 1.1},
                    {"s": "16", "fed": 600, "normal": 600, "rej": 8.5, "rej_normal": 2.0, "every_night": True},
                    {"s": "30", "fed": 50, "normal": 100, "rej": 1.0, "rej_normal": 1.0}]  # too quiet to judge
        hour = {"hour": "9–10 PM", "updated": "2026-11-25 22:02:10", "fed": 18600, "normal": 18700, "rej": 1.8,
                "stations": stations, "text": "Feed stations 9–10 PM\nSlow: 2"}
        self.write("feeder-app.json", json.dumps({"hours": [hour]}))
        text = fc.feeders(NOW)
        self.assertIn("Hour 9–10 PM (read 22:02): sorter fed 18,600 (normal 18,700)", text)
        self.assertIn("Slow (under 70% of that station's normal): 2 (197/hr vs normal 531, rejects ?%", text)
        self.assertIn("Fast (over 130%): 11 (900/hr", text)
        self.assertIn("High rejects vs that station's normal: 16 (600/hr", text)
        self.assertIn("'every night' stations: 16", text)
        self.assertIn("The hourly check said: Feed stations 9–10 PM | Slow: 2", text)

    def test_old_feeder_hour_gives_no_numbers(self):
        self.write("feeder-app.json", json.dumps({"hours": [{"hour": "6–7 PM", "updated": "2026-11-25 19:02:00"}]}))
        self.assertEqual(fc.feeders(NOW), "No feed-station numbers for the last hour (newest is 6–7 PM).")

    def test_chat_feed_window_and_filter(self):
        rows = [{"at": "2026-11-25 21:30:00", "chat": "sorting team", "from": "sorting lead", "text": "out of gaylords at 50"},
                {"at": "2026-11-25 21:40:00", "chat": "dispatch", "from": "dispatcher", "text": "truck late"},
                {"at": "2026-11-25 21:45:00", "chat": "dispatch", "from": "dispatcher", "text": "can you pull AUS01?",
                 "mentions_me": True},
                {"at": "2026-11-25 21:50:00", "chat": "sorting team", "type": "picture"},
                {"at": "2026-11-25 19:00:00", "chat": "sorting team", "text": "too old"}]
        self.write(fc.CHAT_FEED, "\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
        self.assertEqual(fc.chat_text(NOW).splitlines(), [
            "- 21:30 sorting team: sorting lead: out of gaylords at 50",
            "- 21:45 dispatch, mentions me: dispatcher: can you pull AUS01?",
            "- 21:50 sorting team: [picture]"])

    def test_dock_pulls(self):
        self.write("dock.log", "21:30 sent: 9 trucks, 2 alerts\n")
        self.write("dock_state.json", json.dumps({"ran": 0, "said": {"t1": {"pull": 5, "state": "dock"}}}))
        text = fc.dock_text(NOW)
        self.assertIn("- 21:30 sent: 9 trucks, 2 alerts", text)
        self.assertIn("truck 1 (at a dock door): 5 boxes still to pull out to it", text)

    def test_snapshot_has_every_section(self):
        self.write("chute-alerts.log", self.LOG)
        (self.data / "coach").mkdir()
        (self.data / "coach" / "history.json").write_text(json.dumps(
            [{"at": "2026-11-25 21:08", "headline": "h", "moves": [{"do": "Bring 10 empties to 49-69", "why": "x"}]},
             {"at": "2026-11-24 21:08", "headline": "old", "moves": [{"do": "yesterday", "why": "x"}]}]))
        with mock.patch.multiple(fc, speed_text=lambda now: "read 10:08 PM: 21,700/hr",
                                 inbound_text=lambda now: "Read 10:08 PM.\n- still coming: SAN 12,438 pcs",
                                 unsorted_text=lambda: "LEFT TO SORT (floor board read 22:04, 4 min ago):",
                                 broken_text=lambda now: "none"):
            snap = fc.snapshot(NOW)
        for heading in ("Broken data sources", "Stuck chutes, last hour", "Stuck-minutes tonight", "Feed stations",
                        "Sorter speed", "Planned headcount", "Unsorted freight", "Inbound linehaul trucks",
                        "Dock (outbound trucks)", "Label errors", "What people said", "Moves already suggested"):
            self.assertIn("## " + heading, snap)
        self.assertTrue(snap.startswith("# Floor snapshot Wed 11/25 10:08 PM"))
        self.assertIn("- 21:08: Bring 10 empties to 49-69", snap)
        self.assertNotIn("yesterday", snap)  # older than 14 h
        self.assertIn("21,700/hr", snap)

    def test_broken_sources_checked_here_when_live_state_is_old(self):
        import live_state as ls
        with tempfile.TemporaryDirectory() as d, mock.patch.object(ls, "DATA", Path(d)), \
                mock.patch.object(ls, "STATE", Path(d) / "live" / "state.json"):
            text = fc.broken_text(NOW)
        self.assertIn("- live state never collected", text)
        self.assertIn("- floor board: no output file (data/unsorted-latest.json)", text)

    def test_fresh_live_state_problems_are_used(self):
        import live_state as ls
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.json"
            state.write_text(json.dumps({"made": "2026-11-25 22:05:00", "watch": {"problems": {"x": "chute alerts: stale"}}}))
            with mock.patch.object(ls, "STATE", state):
                self.assertEqual(fc.broken_text(NOW), "- chute alerts: stale")


class RequestTest(unittest.TestCase):
    def test_prompt_assembly(self):
        with mock.patch.dict(os.environ, {"HUBOPS_COACH_MODEL": ""}):
            url, headers, body = fc.build_request("# Floor snapshot X", key="k-1")
        self.assertTrue(url.endswith("/v1/messages"))
        self.assertEqual(headers["x-api-key"], "k-1")
        self.assertEqual(headers["anthropic-version"], fc.API_VERSION)
        self.assertEqual(body["model"], fc.DEFAULT_MODEL)
        self.assertEqual(body["system"], fc.PROMPT.read_text())
        self.assertEqual(body["messages"], [{"role": "user", "content": fc.task_text("# Floor snapshot X")}])
        self.assertTrue(body["messages"][0]["content"].endswith("\n\n# Floor snapshot X"))
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertNotIn("maxItems", json.dumps(body["output_config"]))  # not accepted by structured outputs
        self.assertEqual(body["fallbacks"], "default")
        self.assertEqual(headers["anthropic-beta"], fc.FALLBACK_BETA)
        self.assertNotIn("temperature", body)

    def test_model_from_env_and_no_fallbacks_for_unknown_models(self):
        with mock.patch.dict(os.environ, {"HUBOPS_COACH_MODEL": "some-other-model"}):
            _, headers, body = fc.build_request("s")
        self.assertEqual(body["model"], "some-other-model")
        self.assertNotIn("fallbacks", body)
        self.assertNotIn("anthropic-beta", headers)

    def test_prompt_rules_kept(self):
        prompt = fc.PROMPT.read_text()
        for rule in ("at most 4 moves", "WHO / WHERE / HOW MANY", "Never invent", "Running clean", "every night",
                     "Still:", "Zone 2 049-069", "Vendor B", "HOU.H"):
            self.assertIn(rule, prompt)


class ParseTest(unittest.TestCase):
    def test_plain_json_trimmed_to_four(self):
        ans = {"headline": " Hub chutes stuck. ", "moves": [{"do": f"move {i}", "why": f"w{i}"} for i in range(6)]}
        res = fc.parse_answer(json.dumps(ans))
        self.assertEqual(res["headline"], "Hub chutes stuck.")
        self.assertEqual([m["do"] for m in res["moves"]], ["move 0", "move 1", "move 2", "move 3"])

    def test_fenced_json_and_bad_moves_dropped(self):
        text = 'Here:\n```json\n{"headline": "h", "moves": [{"do": "  "}, "x", {"do": "Bring empties"}]}\n```'
        self.assertEqual(fc.parse_answer(text), {"headline": "h", "moves": [{"do": "Bring empties", "why": ""}]})

    def test_json_inside_text(self):
        self.assertEqual(fc.parse_answer('ok {"headline": "Running clean.", "moves": []} done'),
                         {"headline": "Running clean.", "moves": []})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            fc.parse_answer("no moves this hour")
        with self.assertRaises(ValueError):
            fc.parse_answer("[1, 2]")

    def test_call_api_reads_text_blocks(self):
        sent = []

        def urlopen(req, timeout=None):
            sent.append(json.loads(req.data))
            return api_message('{"headline": "h", "moves": [{"do": "d", "why": "w"}]}')
        with mock.patch.object(fc.urllib.request, "urlopen", urlopen):
            text = fc.call_api(*fc.build_request("snap", key="k"))
        self.assertEqual(fc.parse_answer(text)["moves"], [{"do": "d", "why": "w"}])
        self.assertEqual(sent[0]["messages"][0]["role"], "user")

    def test_refusal_and_cut_off_raise(self):
        for stop in ("refusal", "max_tokens"):
            with mock.patch.object(fc.urllib.request, "urlopen", lambda req, timeout=None, s=stop: api_message("{}", s)):
                with self.assertRaises(RuntimeError):
                    fc.call_api(*fc.build_request("snap", key="k"))


class MainTest(DataDirTest):
    def run_main(self, *args, env=None):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["floor_coach.py", "--at", "2026-11-25 22:08", *args]), \
                mock.patch.dict(os.environ, env or {}, clear=False), \
                mock.patch.object(fc, "snapshot", lambda now: "# Floor snapshot TEST\n"), \
                contextlib.redirect_stdout(out):
            code = fc.main()
        return code, out.getvalue()

    def test_demo_without_key_prints_request_and_labelled_example(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}), \
                mock.patch.object(fc, "call_api", side_effect=AssertionError("no API call in demo")):
            code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("demo: no ANTHROPIC_API_KEY", out)
        self.assertIn("POST https://api.anthropic.com/v1/messages", out)
        self.assertIn('"x-api-key": "<ANTHROPIC_API_KEY>"', out)
        self.assertIn(fc.PROMPT.read_text().splitlines()[0], out)  # the exact system prompt
        self.assertIn("Give tonight's floor moves for this hour as the rules say.", out)
        self.assertIn("# Floor snapshot TEST", out)
        self.assertIn("EXAMPLE ANSWER (canned, not from a model", out)
        self.assertIn("1. Move 2 Vendor C pallet workers", out)
        self.assertFalse((self.data / "coach" / "history.json").exists())

    def test_demo_flag_wins_over_a_key(self):
        with mock.patch.object(fc, "call_api", side_effect=AssertionError("no API call in demo")):
            code, out = self.run_main("--demo", env={"ANTHROPIC_API_KEY": "k"})
        self.assertIn("demo: --demo", out)
        self.assertNotIn('"k"', out)  # the key is never printed

    def test_live_run_updates_tasks_alert_and_history(self):
        res = {"headline": "Hub chutes stuck.", "moves": [{"do": "Still: bring empties to 50", "why": "x"},
                                                          {"do": "Move 2 people to 52", "why": "y"}]}
        do_now, app_push = mock.MagicMock(), mock.MagicMock()
        with mock.patch.object(fc, "ask", return_value=res), \
                mock.patch.dict("sys.modules", {"do_now": do_now, "app_push": app_push}):
            code, out = self.run_main(env={"ANTHROPIC_API_KEY": "k"})
        self.assertEqual(code, 0)
        source, tasks = do_now.replace.call_args[0]
        self.assertEqual(source, "coach")
        self.assertEqual([t["push"] for t in tasks], [False, True])
        title, text = app_push.alert.call_args[0]
        self.assertEqual(title, "Floor coach · 10 PM")
        self.assertIn("2. Move 2 people to 52", text)
        hist = json.loads((self.data / "coach" / "history.json").read_text())
        self.assertEqual(hist[-1]["at"], "2026-11-25 22:08")

    def test_no_moves_no_alert(self):
        do_now, app_push = mock.MagicMock(), mock.MagicMock()
        with mock.patch.object(fc, "ask", return_value={"headline": "Running clean.", "moves": []}), \
                mock.patch.dict("sys.modules", {"do_now": do_now, "app_push": app_push}):
            self.run_main(env={"ANTHROPIC_API_KEY": "k"})
        do_now.replace.assert_called_once_with("coach", [])
        app_push.alert.assert_not_called()

    def test_outside_the_shift_does_nothing(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["floor_coach.py"]), \
                mock.patch.object(fc.hubconfig, "now_local", return_value=NOW.replace(hour=12)), \
                mock.patch.object(fc, "snapshot", side_effect=AssertionError("no snapshot by day")), \
                contextlib.redirect_stdout(out):
            self.assertEqual(fc.main(), 0)
        self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
