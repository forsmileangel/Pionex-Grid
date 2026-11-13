"""A capture's profit is shown under the day before the capture (the day it was earned).

capture_date stays the run's key in the database; these tests cover the places a day is shown.
"""
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from v2 import store
from v2.daily_report import profit_day
from v2.legacy_excel import ledger_rows
from v2.store import profit_date
from v2.tests.test_store import FX, rec, snap


class ProfitDateHelperTests(unittest.TestCase):
    def test_one_day_back_across_month_year_and_leap_day(self):
        self.assertEqual(profit_date("2026-11-13"), "2026-11-12")
        self.assertEqual(profit_date("2026-12-01"), "2026-11-30")
        self.assertEqual(profit_date("2027-01-01"), "2026-12-31")
        self.assertEqual(profit_date("2028-03-01"), "2028-02-29")
        self.assertEqual(profit_date("2026-11-13T08:15:19+08:00"), "2026-11-12")

    def test_nothing_in_nothing_out(self):
        self.assertIsNone(profit_date(None))
        self.assertIsNone(profit_date(""))

    def test_the_excel_report_uses_the_same_rule(self):
        for day in ("2026-11-13", "2026-12-01", "2027-01-01", "2028-03-01"):
            self.assertEqual(profit_day(day), profit_date(day))


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX),
                  patch("v2.funding.usdt_twd", return_value=FX),
                  patch("v2.store.BACKUP_DIR", Path(self.tmp.name) / "backups")]:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, day, records, expected=None):
        store.ingest(snap(day, records, expected), self.db)


class MonthBoundaryTests(LedgerCase):
    def test_a_capture_on_the_first_counts_for_the_month_before(self):
        # Baseline on 10-30 (shown 10-29), then +5 (10-30), +7 (10-31, captured 11-01), +9 (11-01).
        for day, grid in (("2026-10-30", 100), ("2026-10-31", 105), ("2026-11-01", 112), ("2026-11-02", 121)):
            self.capture(day, [rec(GridProfit=grid)])
        payload = store.days_payload(self.db)
        self.assertEqual([(d["date"], d["capture_date"]) for d in payload["days"]],
                         [("2026-11-01", "2026-11-02"), ("2026-10-31", "2026-11-01"),
                          ("2026-10-30", "2026-10-31"), ("2026-10-29", "2026-10-30")])
        months = payload["averages"]["months"]
        self.assertEqual(months["2026-10"]["daily_profit"]["total"]["int"], store.to_fixed(12))
        self.assertEqual((months["2026-10"]["from"], months["2026-10"]["to"]), ("2026-10-30", "2026-10-31"))
        self.assertEqual(months["2026-11"]["daily_profit"]["total"]["int"], store.to_fixed(9))
        self.assertEqual(months["2026-11"]["days"], 1)

    def test_a_closure_recorded_on_the_first_belongs_to_the_month_before(self):
        self.capture("2026-10-31", [rec(GridProfit=100)])
        self.capture("2026-11-01", [rec(ListStatus="finished", GridProfit=104, TotalProfit=-50,
                                        Closed="2026-10-31 20:00:00")])
        closures = store.days_payload(self.db)["closures"]
        self.assertEqual(list(closures["days"]), ["2026-10-31"])
        self.assertEqual(list(closures["months"]), ["2026-10"])
        row = store.ledger_publish_payload(self.db)["settlements"][0]
        self.assertEqual((row["capture_date"], row["profit_date"]), ("2026-11-01", "2026-10-31"))

    def test_the_fx_gap_month_follows_the_latest_profit_day(self):
        self.capture("2026-10-31", [rec(GridProfit=100)])
        self.capture("2026-11-01", [rec(GridProfit=104)])
        status = store.status_payload(self.db)
        self.assertEqual(status["coin_fx_gap_month_label"], "2026-10")
        self.assertEqual(status["last_profit_date"], "2026-10-31")
        self.assertEqual(status["last_success"]["capture_date"], "2026-11-01")


class PayloadDateTests(LedgerCase):
    def setUp(self):
        super().setUp()
        self.capture("2026-09-18", [rec(GridProfit=100)])
        self.capture("2026-09-19", [rec(GridProfit=105)])
        self.capture("2026-09-21", [rec(GridProfit=111)])   # 09-20 was missed

    def test_missed_captures_are_listed_both_ways(self):
        status = store.status_payload(self.db)
        # The backfill command takes the capture date; the calendar cell left empty is the day before.
        self.assertEqual(status["missing_dates"], ["2026-09-20"])
        self.assertEqual(status["missing_profit_dates"], ["2026-09-19"])

    def test_every_per_day_payload_names_the_profit_day(self):
        self.assertEqual([(d["date"], d["capture_date"]) for d in store.daily_payload(self.db)],
                         [("2026-09-17", "2026-09-18"), ("2026-09-18", "2026-09-19"), ("2026-09-20", "2026-09-21")])
        one = store.daily_date_payload("2026-09-19", self.db)
        self.assertEqual((one["date"], one["capture_date"]), ("2026-09-18", "2026-09-19"))
        empty = store.daily_date_payload("2026-09-20", self.db)
        self.assertEqual((empty["date"], empty["capture_date"], empty["rows"]), ("2026-09-19", "2026-09-20", []))
        rows = store.day_rows_payload("2026-09-19", self.db)
        self.assertEqual((rows["date"], rows["capture_date"], len(rows["rows"])), ("2026-09-18", "2026-09-19", 1))
        history = store.grid_history_payload("g1", self.db)["profits"]
        self.assertEqual([(p["date"], p["capture_date"]) for p in history],
                         [("2026-09-17", "2026-09-18"), ("2026-09-18", "2026-09-19"), ("2026-09-20", "2026-09-21")])
        board = store.board_payload(self.db)
        self.assertEqual((board["capture_date"], board["profit_date"]), ("2026-09-21", "2026-09-20"))
        latest = store.summary_latest(self.db)
        self.assertEqual((latest["capture_date"], latest["profit_date"]), ("2026-09-21", "2026-09-20"))

    def test_the_figures_do_not_move_only_their_label_does(self):
        by_capture = {d["capture_date"]: float(d["daily_profit"]["usdt"]) for d in store.days_payload(self.db)["days"]}
        self.assertEqual(by_capture, {"2026-09-18": 0.0, "2026-09-19": 5.0, "2026-09-21": 6.0})
        ledger = {d["date"]: float(d["daily_profit_usdt"]) for d in store.ledger_publish_payload(self.db)["days"]}
        self.assertEqual(ledger, {"2026-09-17": 0.0, "2026-09-18": 5.0, "2026-09-20": 6.0})

    def test_the_sixteen_column_excel_rows(self):
        payload = ledger_rows(self.db, date(2026, 9, 21))
        self.assertEqual(payload["dates"], ["2026-09-17", "2026-09-18", "2026-09-20"])
        self.assertEqual(payload["capture_dates"], ["2026-09-18", "2026-09-19", "2026-09-21"])
        # The freshness check and the run log keep using the capture date.
        self.assertEqual(payload["latest_date"], "2026-09-21")
        self.assertEqual(sorted({r[0] for r in payload["rows"]}), payload["dates"])
        totals = [(r[0], r[10]) for r in payload["rows"] if r[3] == "__DAILY_TOTAL__"]
        self.assertEqual(totals, [("2026-09-17", 0), ("2026-09-18", 5), ("2026-09-20", 6)])
        with self.assertRaises(ValueError):
            ledger_rows(self.db, date(2026, 9, 22))   # today's capture has not run: keep the old workbook


if __name__ == "__main__":
    unittest.main()
