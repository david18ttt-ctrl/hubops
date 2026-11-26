import sys
import unittest
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import chute_alerts as ca  # noqa: E402

T0 = datetime(2026, 11, 12, 0, 0, 0)
_ids = iter(range(10 ** 9))


def row(minute, chute="141", reason="CHUTE_FULL", dest="TXK01-006", box=None, second=0):
    t = T0 + timedelta(minutes=minute, seconds=second)
    return {"id": f"r{next(_ids)}", "deviceSortTime": t.strftime("%Y-%m-%d %H:%M:%S"),
            "matchedChutes": [chute], "chuteRemark": dest, "refluxReason": reason,
            "orderCode": box or f"PKG{minute:06d}{second:03d}{int(chute):06d}"}


def stuck_rows(minutes, per_minute=4, **kw):
    return [row(m, second=s * (60 // per_minute), **kw) for m in range(minutes + 1) for s in range(per_minute)]


def at(minute):
    return T0 + timedelta(minutes=minute)


class Stuck(unittest.TestCase):
    def test_alert_at_five_minutes_not_before(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(4))
        self.assertEqual(ca.stuck_alerts(state, at(4)), [])
        ca.update(state, [row(5, second=s) for s in (0, 20, 40)])
        lines = ca.stuck_alerts(state, at(5))
        self.assertEqual(len(lines), 1)
        self.assertIn("TXK01-006 (chute 141) · stuck 5 min", lines[0])
        self.assertIn("since 12:00 AM", lines[0])

    def test_again_every_five_minutes_while_stuck(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(6))
        self.assertEqual(len(ca.stuck_alerts(state, at(6))), 1)
        self.assertEqual(ca.stuck_alerts(state, at(6)), [])  # Same 5-minute step: once only
        sent_at = []
        for minute in range(7, 41):
            ca.update(state, [row(minute, second=s) for s in (0, 15, 30, 45)])
            lines = ca.stuck_alerts(state, at(minute))
            if lines:
                self.assertIn(f"STILL stuck {minute} min", lines[0])
                sent_at.append(minute)
        self.assertEqual(sent_at, [10, 15, 20, 25, 30, 35, 40])

    def test_no_route_from_wms_uses_the_chute_map(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(6, chute="370", dest=""))
        self.assertIn("AUS01 (chute 370) · stuck 6 min", ca.stuck_alerts(state, at(6))[0])

    def test_too_few_rejects_is_not_stuck(self):
        state = ca.new_state()
        ca.update(state, [row(m) for m in range(0, 12)])  # one reject a minute: 12 in 11 minutes
        self.assertEqual(ca.stuck_alerts(state, at(11)), [])

    def test_a_long_gap_starts_a_new_spell(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(6))
        ca.update(state, [row(m + 23, second=s) for m in range(5) for s in (0, 15, 30, 45)])
        self.assertEqual(state["stuck"]["141"]["start"], "2026-11-12 00:23:00")
        self.assertEqual(ca.stuck_alerts(state, at(27)), [])

    def test_quiet_for_a_few_minutes_is_the_same_problem_coming_back(self):
        # Stuck, 12 quiet minutes, stuck again: one problem, and the alert says it came back.
        state = ca.new_state()
        ca.update(state, stuck_rows(11))
        self.assertEqual(len(ca.stuck_alerts(state, at(11))), 1)  # 10 min
        self.assertEqual(ca.fixed_alerts(state, at(20)), [])  # 9 quiet minutes: not called fixed
        ca.update(state, [row(m, second=s) for m in range(23, 27) for s in (0, 15, 30, 45)])
        lines = ca.stuck_alerts(state, at(26))
        self.assertIn("STILL stuck 26 min · BACK after 11 quiet min, not fixed", lines[0])
        self.assertIn("since 12:00 AM", lines[0])
        ca.update(state, [row(27, second=s) for s in (0, 30)])
        self.assertNotIn("BACK", "".join(ca.stuck_alerts(state, at(30))))

    def test_fixed_is_said_once_after_15_quiet_minutes(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(11))
        ca.stuck_alerts(state, at(11))
        self.assertEqual(ca.fixed_alerts(state, at(26)), [])  # last reject 12:11:45
        lines = ca.fixed_alerts(state, at(27))
        self.assertEqual(lines, ["✓ TXK01-006 (chute 141) · fixed: no rejects since 12:11 AM · was stuck 11 min (12:00 AM–12:11 AM)"])
        self.assertEqual(ca.fixed_alerts(state, at(28)), [])
        quiet = ca.new_state()
        ca.update(quiet, stuck_rows(3))  # never alerted: nothing to call fixed
        self.assertEqual(ca.fixed_alerts(quiet, at(30)), [])

    def test_fixed_button_ends_the_spell_and_a_new_jam_is_new(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(6))
        self.assertEqual(len(ca.stuck_alerts(state, at(6))), 1)
        start = state["stuck"]["141"]["start"]
        state["history"] = {f"141|{start}": {"cleared": False}}
        pressed = at(7).astimezone().isoformat()  # the phone sends UTC; same moment
        ca.mark_fixed(state, [{"chute": "141", "start": start, "at": pressed}])
        self.assertNotIn("141", state["stuck"])
        self.assertTrue(state["history"][f"141|{start}"]["cleared"])
        ca.update(state, [row(6, second=50)])  # posted late by the WMS, from before the press: ignored
        self.assertNotIn("141", state["stuck"])
        self.assertEqual(ca.fixed_alerts(state, at(30)), [], "no 'fixed' message after a press")
        ca.mark_fixed(state, [{"chute": "141", "start": start, "at": pressed}])  # read again: used once
        ca.update(state, [row(m, second=s * 15) for m in range(9, 15) for s in range(4)])
        lines = ca.stuck_alerts(state, at(14))
        self.assertEqual(len(lines), 1, "jammed again after the press: a new alert")
        self.assertIn("since 12:09 AM", lines[0])

    def test_finished_spell_is_not_reported_late(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(12))
        self.assertEqual(ca.stuck_alerts(state, at(20)), [])  # last reject 8 min ago: fixed already

    def test_exception_chutes_and_other_reasons_are_ignored(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(12, chute="147"))
        ca.update(state, stuck_rows(12, chute="200", reason="WAYBILL_NOT_EXIST"))
        self.assertEqual(state["stuck"], {})

    def test_blank_reason_counts_as_a_locked_chute(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(11, chute="138", reason="", dest="TXK01-003"))
        self.assertIn("TXK01-003 (chute 138)", ca.stuck_alerts(state, at(11))[0])

    def test_repeated_rows_are_counted_once(self):
        state = ca.new_state()
        rows = stuck_rows(11)
        ca.update(state, rows)
        ca.update(state, rows)
        self.assertEqual(state["stuck"]["141"]["rejects"], len(rows))

    def test_boxes_counts_packages_not_rejects(self):
        state = ca.new_state()
        ca.update(state, [row(m, second=s, box=f"BOX{s}") for m in range(11) for s in range(0, 60, 15)])
        self.assertIn("· 4 boxes going around", ca.stuck_alerts(state, at(10))[0])


class NoRule(unittest.TestCase):
    def rows(self, start, end, per_minute):
        return [row(m, chute="147", reason="NO_MATCHED_RULE", dest="", second=s * (60 // per_minute),
                    box=f"B{m}-{s}") for m in range(start, end) for s in range(per_minute)]

    def test_two_busy_blocks_in_a_row_alert(self):
        state = ca.new_state()
        ca.update(state, self.rows(0, 20, 7))
        boxes = ca.no_rule_spike(state, at(20))
        self.assertEqual(len(boxes), 139)  # the reject exactly 20 minutes ago falls outside the window

    def test_one_busy_block_is_not_enough(self):
        state = ca.new_state()
        ca.update(state, self.rows(10, 20, 7))
        self.assertIsNone(ca.no_rule_spike(state, at(20)))

    def test_normal_night_level_stays_quiet(self):
        state = ca.new_state()
        ca.update(state, self.rows(0, 20, 2))  # 20 per 10 minutes, a normal night's level
        self.assertIsNone(ca.no_rule_spike(state, at(20)))

    def test_no_repeat_for_an_hour(self):
        state = ca.new_state()
        ca.update(state, self.rows(0, 81, 7))
        self.assertIsNotNone(ca.no_rule_spike(state, at(20)))
        self.assertIsNone(ca.no_rule_spike(state, at(50)))
        self.assertIsNotNone(ca.no_rule_spike(state, at(80)))

    def test_line_names_the_stations(self):
        boxes = ["A", "B", "C", "A"]
        line = ca.no_rule_line(boxes, {"A": "BRO01", "B": "BRO01", "C": "SHV01"})
        self.assertIn("4 rejects / 3 boxes in 20 min", line)
        self.assertIn("For: BRO01 2 · SHV01 1 (of 3 looked up)", line)


class Check(unittest.TestCase):
    def test_one_message_for_everything_found(self):
        state = ca.new_state()
        rows = stuck_rows(10) + stuck_rows(10, chute="144", dest="TXK01-009")
        text = ca.check(state, rows, at(10), lookup=lambda boxes: {})
        self.assertEqual(text.count("\n"), 1)
        self.assertIn("(chute 141)", text)
        self.assertIn("(chute 144)", text)

    def test_nothing_happening_sends_nothing(self):
        self.assertIsNone(ca.check(ca.new_state(), [], at(10), lookup=lambda boxes: {}))

    def test_prune_drops_old_rows(self):
        state = ca.new_state()
        ca.update(state, stuck_rows(5))
        ca.prune(state, at(60))
        self.assertEqual(state, {**ca.new_state(), "no_rule_sent": None})

    def test_quiet_hours(self):
        self.assertTrue(ca.active(datetime(2026, 11, 12, 23, 0)))
        self.assertTrue(ca.active(datetime(2026, 11, 12, 3, 0)))
        self.assertFalse(ca.active(datetime(2026, 11, 12, 12, 0)))


if __name__ == "__main__":
    unittest.main()
