import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from v2 import store
from v2.averages import average_i, profit_averages
from v2.funding import funding_payload
from v2.tests.test_store import FX, rec, snap


def entry(day, value, covered=1, baseline=False, pending=False):
    return {"date": day, "covered": covered, "baseline": baseline, "pending": pending, "p": value}


class ProfitAverageTests(unittest.TestCase):
    def test_month_so_far_leaves_out_the_baseline_day(self):
        result = profit_averages([entry("2026-08-30", 0, baseline=True), entry("2026-08-31", 30),
                                  entry("2026-09-01", 10), entry("2026-09-02", 20)], ("p",))
        aug, sep = result["months"]["2026-08"], result["months"]["2026-09"]
        self.assertEqual((aug["days"], aug["sums"]["p"], aug["from"]), (1, 30, "2026-08-31"))
        self.assertEqual((sep["days"], sep["sums"]["p"], sep["from"], sep["to"]), (2, 30, "2026-09-01", "2026-09-02"))
        self.assertEqual(average_i(sep["sums"]["p"], sep["days"]), 15)

    def test_a_missed_capture_counts_every_day_it_covers(self):
        # 09-05 follows a two-day gap, so its 30 is three days of profit: 40 over 4 days, not 2.
        result = profit_averages([entry("2026-09-01", 0, baseline=True), entry("2026-09-02", 10),
                                  entry("2026-09-05", 30, covered=3)], ("p",))
        month = result["months"]["2026-09"]
        self.assertEqual((month["days"], average_i(month["sums"]["p"], month["days"])), (4, 10))

    def test_pending_days_are_left_out_and_counted(self):
        result = profit_averages([entry("2026-09-01", 0, baseline=True), entry("2026-09-02", 10),
                                  entry("2026-09-03", 99, pending=True), entry("2026-09-04", 20)], ("p",))
        month = result["months"]["2026-09"]
        self.assertEqual((month["days"], month["skipped"], month["sums"]["p"]), (2, 1, 30))
        self.assertEqual(result["recent"]["skipped"], 1)

    def test_recent_window_is_the_last_30_days(self):
        start = date(2026, 9, 1)
        entries = [entry((start + timedelta(days=i)).isoformat(), i, baseline=i == 0) for i in range(40)]
        recent = profit_averages(entries, ("p",))["recent"]
        self.assertEqual((recent["from"], recent["to"], recent["days"]), ("2026-09-11", "2026-10-10", 30))
        self.assertEqual(recent["sums"]["p"], sum(range(10, 40)))

    def test_short_history_divides_by_the_days_there_are(self):
        recent = profit_averages([entry("2026-09-01", 0, baseline=True), entry("2026-09-02", 6),
                                  entry("2026-09-03", 10)], ("p",))["recent"]
        self.assertEqual((recent["days"], average_i(recent["sums"]["p"], recent["days"])), (2, 8))

    def test_a_missing_amount_adds_nothing(self):
        month = profit_averages([entry("2026-09-01", 0, baseline=True), entry("2026-09-02", None),
                                 entry("2026-09-03", 8)], ("p",))["months"]["2026-09"]
        self.assertEqual((month["days"], month["sums"]["p"]), (2, 8))

    def test_nothing_counted_has_no_average(self):
        self.assertIsNone(average_i(0, 0))
        result = profit_averages([entry("2026-09-01", 0, baseline=True)], ("p",))
        self.assertEqual((result["recent"]["days"], result["months"]["2026-09"]["days"]), (0, 0))


class PayloadAverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX), patch("v2.funding.usdt_twd", return_value=FX),
                  patch("v2.store.BACKUP_DIR", Path(self.tmp.name) / "backups")]:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, day, rows):
        store.ingest(snap(day, rows), self.db)

    def test_calendar_and_funding_pages_share_the_rules(self):
        # Daily grid profit: baseline, +6, +4, +10. Funding: unknown, -1, not reported, -3.
        self.capture("2026-08-30", [rec(GridProfit=10, FundingFee=-1)])
        self.capture("2026-08-31", [rec(GridProfit=16, FundingFee=-2)])
        self.capture("2026-09-01", [rec(GridProfit=20, FundingFee=None)])
        self.capture("2026-09-02", [rec(GridProfit=30, FundingFee=-5)])

        days = store.days_payload(self.db)["averages"]
        aug, sep, recent = days["months"]["2026-08"], days["months"]["2026-09"], days["recent"]
        self.assertEqual((aug["days"], aug["daily_profit"]["avg"]["int"]), (1, store.to_fixed(6)))
        self.assertEqual((sep["days"], sep["from"], sep["to"]), (2, "2026-09-01", "2026-09-02"))
        self.assertEqual(sep["daily_profit"]["total"]["int"], store.to_fixed(14))
        self.assertEqual(sep["daily_profit"]["avg"]["int"], store.to_fixed(7))
        self.assertEqual(recent["days"], 3)
        self.assertEqual(recent["daily_profit"]["avg"]["int"], round(store.to_fixed(20) / 3))
        self.assertIn("twd", sep["daily_profit"]["avg"])

        fund = funding_payload(self.db)["averages"]
        sep = fund["months"]["2026-09"]
        # 09-01 had no funding figure; 09-02's -3 covers both days, so September still sums to -3.
        self.assertEqual(sep["days"], 2)
        self.assertEqual(sep["grid_profit"]["total"]["int"], store.to_fixed(14))
        self.assertEqual(sep["funding"]["total"]["int"], store.to_fixed(-3))
        self.assertEqual(sep["net"]["avg"]["int"], store.to_fixed(5.5))
        self.assertEqual(sep["pct_of_grid"], -21.43)
        self.assertEqual(fund["recent"]["net"]["total"]["int"], store.to_fixed(16))
        self.assertEqual(fund["recent"]["days"], 3)

    def test_a_pending_closure_is_left_out_of_both_pages(self):
        self.capture("2026-09-18", [rec(GridProfit=1500, ProfitWithdrawn=0)])
        self.capture("2026-09-19", [rec(GridProfit=1505, ProfitWithdrawn=1000)])
        self.capture("2026-09-20", [])
        sep = store.days_payload(self.db)["averages"]["months"]["2026-09"]
        self.assertEqual((sep["days"], sep["skipped"]), (1, 1))
        self.assertEqual(sep["daily_profit"]["total"]["int"], store.to_fixed(5))
        fund = funding_payload(self.db)["averages"]["months"]["2026-09"]
        self.assertEqual((fund["days"], fund["skipped"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
