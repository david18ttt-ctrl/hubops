import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hubops"))
import lost_labels as ll  # noqa: E402

T0 = int(datetime(2026, 11, 15, 20, 0).timestamp() * 1000)
MIN = 60000


def at(minute):
    return T0 + int(minute * MIN)


def row(chute, minute, n, ok=True):
    bag = f"BG01261116{n:05d}-{chute}" if ok else ""
    return {"opType": ll.PRINTED, "opResult": ll.OK if ok else 0, "chuteNo": str(chute), "bagLabelCode": bag,
            "opTime": at(minute)}


def run(labels, checks, where="HRL01", missing=()):
    """labels: [(chute, minute, pcs, n)]; checks: minutes. Returns {minute: [[pcs, pcs, ...] per alert]}."""
    rows = [row(c, m, n) for c, m, _, n in labels]
    bags = {f"BG01261116{n:05d}-{c}": {"pcs": p, "where": where} for c, m, p, n in labels
            if f"BG01261116{n:05d}-{c}" not in missing}
    state, sent = ll.new_state(), {}
    for m in checks:
        prints = ll.prints_from([r for r in rows if r["opTime"] <= at(m)])
        found = ll.step(state, prints, bags, at(m))
        if found:
            sent[m] = [[a["pairs"][0][0]["pcs"]] + [b["pcs"] for _, b in a["pairs"]] for a in found]
    return sent


class LostLabels(unittest.TestCase):
    def test_two_labels_with_packages_within_15_min_alert(self):
        self.assertEqual(run([(133, 0, 467, 1), (133, 2, 3, 2)], [1, 2.5]), {2.5: [[467, 3]]})

    def test_more_than_15_min_apart_is_fine(self):
        self.assertEqual(run([(133, 0, 467, 1), (133, 16, 3, 2)], [1, 17]), {})

    def test_zero_package_labels_dont_matter_and_dont_break_a_pair(self):
        self.assertEqual(run([(133, 0, 0, 1), (133, 5, 0, 2)], [6]), {})
        self.assertEqual(run([(133, 0, 272, 1), (133, 1, 0, 2), (133, 2, 3, 3)], [0.5, 1.5, 2.5]), {2.5: [[272, 3]]})

    def test_the_next_box_after_an_alert_is_not_another_alert(self):
        self.assertEqual(run([(51, 0, 272, 1), (51, 2, 3, 2), (51, 14, 162, 3)], [1, 3, 15]), {3: [[272, 3]]})

    def test_every_route_needs_one_small_bag(self):
        self.assertEqual(run([(59, 0, 405, 1), (59, 13, 490, 2)], [1, 14], where="SAN.H"), {})
        self.assertEqual(run([(325, 0, 555, 1), (325, 11, 101, 2)], [1, 12]), {})
        self.assertEqual(run([(17, 0, 395, 1), (17, 1, 2, 2)], [2]), {2: [[395, 2]]})

    def test_prebuilt_container_route_needs_one_small_bag(self):
        full = [(51, 0, 363, 1), (51, 6, 228, 2)]
        self.assertEqual(run(full, [1, 7], where="PHX.H"), {})
        self.assertEqual(run([(51, 0, 272, 1), (51, 2, 3, 2)], [1, 3], where="PHX.H"), {3: [[272, 3]]})

    def test_chutes_are_separate(self):
        self.assertEqual(run([(133, 0, 400, 1), (134, 2, 3, 2)], [3]), {})

    def test_sent_once(self):
        self.assertEqual(run([(133, 0, 467, 1), (133, 2, 3, 2)], [3, 3.5, 4, 10]), {3: [[467, 3]]})

    def test_old_labels_are_not_news(self):
        self.assertEqual(run([(133, 0, 467, 1), (133, 2, 3, 2)], [14]), {})

    def test_failed_prints_are_skipped(self):
        self.assertEqual(ll.prints_from([row(190, 0, 1, ok=False)]), [])

    def test_a_bag_wms_cant_find_yet_waits_then_counts(self):
        labels = [(133, 0, 467, 1), (133, 2, 3, 2)]
        rows = [row(c, m, n) for c, m, _, n in labels]
        state = ll.new_state()
        bags = {"BG0126111600001-133": {"pcs": 467, "where": "HRL01"}}
        self.assertEqual(ll.step(state, ll.prints_from(rows), bags, at(2.5)), [])
        bags["BG0126111600002-133"] = {"pcs": 3, "where": "HRL01"}
        found = ll.step(state, [], bags, at(3))
        self.assertEqual([b["pcs"] for b in [found[0]["pairs"][0][0], found[0]["pairs"][0][1]]], [467, 3])

    def test_text(self):
        a = {"chute": "51", "where": "PHX.H", "pairs": [(
            {"bag": "BG0126111600193-51", "pcs": 272, "at": at(0)}, {"bag": "BG0126111600266-51", "pcs": 3, "at": at(2)})]}
        lines = ll.text(a).splitlines()
        self.assertTrue(lines[0].startswith("Chute 51 · PHX.H · 2 labels 2 min apart · "))
        self.assertIn("BG0126111600193-51 272 pcs", lines[1])
        self.assertEqual(lines[2], "  scan the gaylord at chute 51")


if __name__ == "__main__":
    unittest.main()
