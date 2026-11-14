"""Collector: which limit % the band shows, and where the window's learning starts.
    python -m unittest discover -s tests -p "test_*.py"
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "collector"))
import umeter  # noqa: E402
import view  # noqa: E402

MIN = 60_000
NOW = 1_794_643_000_000
RESETS = NOW + 3 * view.DAY + view.HOUR


def requests(minutes: range) -> list:
    """One Opus request a minute, as a day file the view reads: (minutes before NOW)."""
    reqs = [[NOW - m * MIN, 0, -1, -1, 0, 100, 2_000, 50_000, 1_000, 0, 0, 0, f"msg{m}", -1, -1, -1] for m in minutes]
    reqs.sort(key=lambda r: r[0])
    doc = {"v": 4, "host": "test", "day": view.dt.datetime.fromtimestamp(NOW / 1000, view.dt.timezone.utc).strftime("%Y-%m-%d"),
           "s": ["s"], "p": [], "a": [], "m": ["claude-opus-5-5"], "e": [], "k": [], "x": [],
           "sessions": {"s": {"title": "", "cwd": ""}}, "prompts": {}, "agents": {}, "reqs": reqs}
    return [view.Entry("day.json", 0.0, doc, umeter.Prices())]


def week(sessions: dict, entries: list = ()) -> dict:
    """Build the view from per-session readings [(minutes before NOW, pct)] and return the 7 day window."""
    with tempfile.TemporaryDirectory() as root:
        inbox = os.path.join(root, "local", "limits")
        os.makedirs(inbox)
        for sid, readings in sessions.items():
            samples = [[NOW - m * MIN, "seven_day", pct, RESETS] for m, pct in readings]
            with open(os.path.join(inbox, f"{sid}.json"), "w") as f:
                json.dump({"sessionId": sid, "samples": samples}, f)
        b = view.ViewBuilder(root, "test", umeter.Prices(), root)
        v, _ = b.build(list(entries), b.limits(), NOW)
        return next(w for w in v["windows"] if w["kind"] == "seven_day")


class LimitReadingTest(unittest.TestCase):
    def test_a_slower_sessions_stale_reading_does_not_pull_the_percent_down(self):
        w = week({
            "active": [(30, 40), (2, 43)],
            "slower": [(20, 40), (1, 40)],  # saved after the active one, still on 40
        })
        self.assertEqual(w["pct"], 43)

    def test_an_early_reset_that_holds_starts_again_from_its_own_level(self):
        # a plan upgrade: 93% drops to 0 with the same reset time, then climbs again
        w = week({"s": [(400, 92), (300, 93), (290, 0), (200, 4), (100, 8), (5, 10)]})
        self.assertEqual(w["pct"], 10)

    def test_a_drop_that_does_not_hold_is_a_lagging_reading(self):
        w = week({"s": [(400, 50), (300, 55), (290, 30), (280, 56), (5, 57)]})
        self.assertEqual(w["pct"], 57)

    def test_points_after_an_early_reset_are_learned_from(self):
        # readings ten minutes apart, so every point crossed was watched
        before = [(600 - 10 * i, 80 + i) for i in range(10)]  # 80..89
        after = [(480 - 10 * i, i) for i in range(40)]  # 0..39, held for hours
        w = week({"s": before + after}, requests(range(1, 600)))
        self.assertEqual(w["pct"], 39)
        self.assertEqual(w["rateFrom"], "learned")
        self.assertTrue(w["stretches"])
        upgrade = NOW - 480 * MIN
        self.assertTrue(all(st["from"] >= upgrade for st in w["stretches"]))
        # spent since the reset (from the last reading before the drop, 510 min ago), not since the window began
        one = umeter.Prices().usd("claude-opus-5-5", 100, 2_000, 50_000, 1_000, 0, 0, 0, NOW)
        self.assertAlmostEqual(w["usdInWindow"], one * 509, delta=0.01)


if __name__ == "__main__":
    unittest.main()
