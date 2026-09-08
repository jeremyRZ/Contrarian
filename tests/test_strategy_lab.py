import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import numpy as np
import pandas as pd

from scripts import research_strategy_lab as lab


class ResearchLabTest(unittest.TestCase):
    def sample(self):
        dates = pd.bdate_range("2024-01-01", periods=4)
        rows = {"HK.00001": {date: {"open": 10., "close": 10., "volume": 100.,
                                   "ma20": 5., "rsi2": 20.} for date in dates}}
        ranks = {"momentum60": {d: [(1., "HK.00001")] for d in dates}}
        return dates, rows, ranks

    def test_next_open_and_cash_include_fees(self):
        dates, rows, ranks = self.sample()
        result = lab.simulate(rows, ranks, dates, {"HK.00001": 100}, "momentum60",
                              1050., 1, dates[0], dates[-1])
        self.assertEqual(result["fills"][0]["date"], str(dates[1].date()))
        self.assertEqual(result["fills"][0]["qty"], 100)
        self.assertTrue(all(p["cash"] >= 0 for p in result["curve"]))
        self.assertLess(result["net_return_pct"], 0)
        self.assertEqual(lab.quantity(10., 1000., 100, .0008), 0)

    def test_same_day_close_cannot_fund_open(self):
        dates, rows, ranks = self.sample()
        rows["HK.00001"][dates[1]]["close"] = 1000.
        result = lab.simulate(rows, ranks, dates, {"HK.00001": 100}, "momentum60",
                              1050., 1, dates[0], dates[-1])
        self.assertEqual(result["fills"][0]["qty"], 100)

    def test_missing_position_bar_retains_value(self):
        dates, rows, ranks = self.sample()
        del rows["HK.00001"][dates[-1]]
        result = lab.simulate(rows, ranks, dates, {"HK.00001": 100}, "momentum60",
                              1050., 1, dates[0], dates[-1])
        self.assertEqual(result["unresolved_positions"], ["HK.00001"])
        self.assertEqual(result["stale_position_days"], 1)
        self.assertGreater(result["ending_equity"], 1000)

    def test_future_prices_do_not_change_past_features(self):
        dates = pd.bdate_range("2020-01-01", periods=260)
        c = np.linspace(10, 30, len(dates))
        frame = pd.DataFrame({"open": c, "close": c, "high": c+1, "low": c-1,
                              "volume": 10000000, "turnover": 50000000}, index=dates)
        original = lab.features({"a": frame})["a"].iloc[:240]
        frame.iloc[240:, frame.columns.get_loc("close")] *= 10
        changed = lab.features({"a": frame})["a"].iloc[:240]
        pd.testing.assert_frame_equal(original, changed)

    def test_absent_snapshot_cannot_create_new_position(self):
        dates, rows, ranks = self.sample()
        result = lab.simulate(rows, ranks, dates, {"HK.00001": 100}, "momentum60",
                              1050., 1, dates[0], dates[-1], memberships={})
        self.assertEqual(result["fills"], [])

    def test_split_and_dividend_are_not_a_price_crash_or_spendable_cash(self):
        dates, rows, ranks = self.sample()
        for d in dates[2:]:
            rows["HK.00001"][d].update(open=4.5, close=4.5)
        rows["HK.00001"][dates[2]].update(share_factor=2., cash_entitlement=1.)
        result = lab.simulate(rows, ranks, dates, {"HK.00001": 100}, "momentum60",
                              1050., 1, dates[0], dates[-1])
        self.assertEqual(result["fills"][-1]["qty"], 200)
        self.assertEqual(result["dividend_receivable_hkd"], 100.)
        self.assertGreater(result["net_return_pct"], -5)
        self.assertAlmostEqual(result["ending_equity"] - result["curve"][-1]["cash"], 100.)

    def test_candidate_and_outcome_records_are_idempotent(self):
        dates, rows, _ = self.sample()
        with tempfile.TemporaryDirectory() as folder:
            with closing(lab.connect(Path(folder)/"research.sqlite3")) as db, db:
                db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           ("v", str(dates[0].date()), "momentum60", "HK.00001", 1, 1., 1, "ELIGIBLE", 10., 100, 1, "2024-01-01T17:00:00+08:00", "FORWARD_AFTER_CLOSE"))
                lab.settle_outcomes(db, rows, dates, "v")
                lab.settle_outcomes(db, rows, dates, "v")
                self.assertEqual(db.execute("SELECT count(*) FROM outcomes").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT horizon FROM outcomes").fetchone()[0], 1)
                self.assertLess(db.execute("SELECT net_return_pct FROM outcomes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
