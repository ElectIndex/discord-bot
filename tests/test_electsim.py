import random
import unittest

from electdata import parse_lean
from electsim import fmt_margin, historical, margin, simulate_president, simulate_state, tally, tipping_point, winner

EV = {"AA": 10, "BB": 7, "CC": 5}
BASE = {"AA": (55, 45, 0), "BB": (40, 60, 0), "CC": (49, 51, 0)}


class BasicsTests(unittest.TestCase):
    def test_margin_and_winner(self):
        self.assertAlmostEqual(margin((60, 40, 0)), 20.0)
        self.assertEqual(winner((10, 20, 30)), "O")
        self.assertEqual(winner((50, 50, 0)), "D")

    def test_tally_with_adjustments(self):
        self.assertEqual(tally({"AA": "D", "BB": "R"}, EV, [("AA", -1, 0, 1)]), {"D": 9, "R": 7, "O": 1})

    def test_tipping_point(self):
        margins = {"AA": 10, "BB": -20, "CC": 2}
        winners = {"AA": "D", "BB": "R", "CC": "D"}
        # 22 EV total, 12 to win: AA (10) then CC delivers it.
        self.assertEqual(tipping_point(margins, winners, EV, "D"), "CC")

    def test_historical(self):
        o = historical(BASE, EV)
        self.assertEqual(o.ev, {"D": 10, "R": 12, "O": 0})
        self.assertEqual(o.tipping, "CC")

    def test_lean_parsing(self):
        self.assertEqual(parse_lean("D+3"), 3.0)
        self.assertEqual(parse_lean("r+2"), -2.0)
        with self.assertRaises(ValueError):
            parse_lean("lots")

    def test_fmt(self):
        self.assertEqual(fmt_margin(2.04), "D+2.0")
        self.assertEqual(fmt_margin(-0.01), "Even")


class SimulationTests(unittest.TestCase):
    def test_lean_pins_the_national_margin(self):
        o = simulate_president(BASE, EV, 7.0, "wild", random.Random(1))
        totals = {s: sum(v) for s, v in BASE.items()}
        national = sum(o.margins[s] * totals[s] for s in BASE) / sum(totals.values())
        self.assertAlmostEqual(national, 7.0, places=6)
        self.assertEqual(o.popular, 7.0)

    def test_same_seed_same_result(self):
        a = simulate_president(BASE, EV, None, "normal", random.Random(99))
        b = simulate_president(BASE, EV, None, "normal", random.Random(99))
        self.assertEqual(a.margins, b.margins)

    def test_calm_moves_less_than_wild(self):
        def spread(chaos):
            moves = []
            for seed in range(200):
                o = simulate_president(BASE, EV, 0.0, chaos, random.Random(seed))
                moves.append(abs(o.margins["AA"] - o.margins["BB"] - (margin(BASE["AA"]) - margin(BASE["BB"]))))
            return sum(moves) / len(moves)
        self.assertLess(spread("calm"), spread("wild"))

    def test_state_lean_pins_statewide(self):
        base = {"1": (100, 50, 0), "2": (20, 80, 0)}
        counties, statewide = simulate_state(base, -4.0, "normal", random.Random(3))
        totals = {f: sum(v) for f, v in base.items()}
        got = sum(counties[f] * totals[f] for f in base) / sum(totals.values())
        self.assertAlmostEqual(got, -4.0, places=6)
        self.assertEqual(statewide, -4.0)

    def test_bad_chaos(self):
        with self.assertRaises(ValueError):
            simulate_president(BASE, EV, None, "apocalyptic")


if __name__ == "__main__":
    unittest.main()
