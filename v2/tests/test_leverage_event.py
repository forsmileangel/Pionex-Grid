"""調槓桿 event: Pionex keeps investment x leverage constant when the leverage setting changes."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2 import store
from v2.store import (SCALE, _classify_event, _leverage_x, apply_event_decision, connect, ingest,
                      summary_values, to_fixed)
from v2.tests.test_store import FX, rec, snap


class LeverageParseTests(unittest.TestCase):
    def test_reads_the_multiple_and_never_guesses(self):
        self.assertEqual(_leverage_x("6x long"), 6.0)
        self.assertEqual(_leverage_x("2.5x short"), 2.5)
        self.assertEqual(_leverage_x(10), 10.0)
        # The collector writes the bare trend when the API gave no leverage.
        self.assertIsNone(_leverage_x("long"))
        self.assertIsNone(_leverage_x(None))
        self.assertIsNone(_leverage_x(""))
        self.assertIsNone(_leverage_x("0x long"))


class LeverageEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "test.sqlite"
        fx = patch("v2.store.usdt_twd", return_value=FX)
        fx.start()
        self.addCleanup(fx.stop)

    def day2(self, first, second):
        ingest(snap("2026-08-25", [rec(**first)]), self.db)
        ingest(snap("2026-08-26", [rec(**second)]), self.db)
        con = connect(self.db)
        try:
            return dict(con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone())
        finally:
            con.close()

    def test_lower_leverage_with_more_investment_is_not_an_add(self):
        # The AMD case of 2026-11-12: 10x -> 6x, investment 3000 -> 5000 (30000 both times).
        row = self.day2(dict(GridProfit=2247.28, Investment=3000, Leverage="10x long"),
                        dict(GridProfit=2306.53, Investment=5000, Leverage="6x long"))
        self.assertEqual(row["event"], "leverage")
        self.assertEqual(row["daily_profit_i"], to_fixed("59.25"))
        self.assertEqual(row["leverage_x"], 6.0)

    def test_higher_leverage_with_less_investment_is_labelled_too(self):
        row = self.day2(dict(GridProfit=100, Investment=6000, Leverage="4x long"),
                        dict(GridProfit=107.23, Investment=4000, Leverage="6x long"))
        self.assertEqual(row["event"], "leverage")
        self.assertEqual(row["daily_profit_i"], to_fixed("7.23"))

    def test_leverage_change_with_real_new_money_stays_an_add(self):
        # 30000 -> 36000 of position: the leverage moved, but capital was added as well.
        row = self.day2(dict(GridProfit=100, Investment=3000, Leverage="10x long"),
                        dict(GridProfit=105, Investment=6000, Leverage="6x long"))
        self.assertEqual(row["event"], "add")

    def test_unknown_previous_leverage_is_not_a_change(self):
        row = self.day2(dict(GridProfit=100, Investment=3000, Leverage="long"),
                        dict(GridProfit=105, Investment=5000, Leverage="6x long"))
        self.assertEqual(row["event"], "add")

    def test_same_leverage_is_untouched(self):
        row = self.day2(dict(GridProfit=100, Investment=3000, Leverage="6x long"),
                        dict(GridProfit=105, Investment=5000, Leverage="6x long"))
        self.assertEqual(row["event"], "add")

    def test_big_grid_drop_is_not_explained_by_leverage(self):
        row = self.day2(dict(GridProfit=400, Investment=3000, Leverage="10x long"),
                        dict(GridProfit=100, Investment=5000, Leverage="6x long"))
        self.assertNotIn("leverage", row["event"])

    def test_withdrawal_on_the_same_day_is_named_as_well(self):
        row = self.day2(dict(GridProfit=100, Investment=3000, Leverage="10x long", ProfitWithdrawn=1000),
                        dict(GridProfit=105, Investment=5000, Leverage="6x long", ProfitWithdrawn=1300))
        self.assertEqual(row["event"], "leverage+withdraw")
        self.assertEqual(row["daily_profit_i"], to_fixed(5))

    def test_manual_decision_wins(self):
        row = self.day2(dict(GridProfit=100, Investment=3000, Leverage="10x long"),
                        dict(GridProfit=105, Investment=5000, Leverage="6x long"))
        self.assertEqual(row["event"], "leverage")
        result = apply_event_decision("2026-08-26", "g1", "add", self.db)
        self.assertEqual(result["event"], "add")

    def test_label_only_never_changes_the_days_profit(self):
        """With and without the leverage inputs the classifier must return the same daily profit."""
        u = SCALE
        for inv_before, inv_after, lev_before, lev_after in ((3000, 5000, 10, 6), (6000, 4000, 4, 6), (5250, 4200, 4, 5)):
            for grid_d in (-15, -5, 0, 7):
                for re_d in (0, 5, 15):
                    prev = {"grid_profit_i": 500 * u, "investment_i": inv_before * u, "withdrawn_i": 0,
                            "reinvest_i": 100 * u, "reduce_i": 0, "lifetime_i": 600 * u}
                    args = ("continue", prev, (500 + grid_d) * u, inv_after * u, 0, (100 + re_d) * u, 0)
                    plain = _classify_event(*args)
                    labelled = _classify_event(*args, lev_x=float(lev_after), prev_lev_x=float(lev_before))
                    self.assertEqual(labelled[0], "leverage", (inv_before, grid_d, re_d))
                    self.assertEqual(labelled[1:], plain[1:], (inv_before, grid_d, re_d, plain[0]))

    def test_v13_migration_relabels_history_without_changing_figures(self):
        ingest(snap("2026-08-25", [rec(GridProfit=2247.28, Investment=3000, Leverage="10x long")]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=2306.53, Investment=5000, Leverage="6x long")]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=2310, Investment=5000, Leverage="6x long")]), self.db)
        con = connect(self.db)
        try:
            # Put the ledger back the way v12 recorded it: no leverage column values, the day called an add.
            con.execute("UPDATE daily_grid_profit SET leverage_x=NULL")
            con.execute("UPDATE daily_grid_profit SET event='add' WHERE capture_date='2026-08-26'")
            con.execute("DELETE FROM schema_meta")
            con.execute("INSERT INTO schema_meta(version, applied_at) VALUES (12, '2026-08-27T00:00:00+08:00')")
            con.commit()
            before = summary_values(con)
        finally:
            con.close()
        con = connect(self.db)
        try:
            rows = {r["capture_date"]: r for r in con.execute("SELECT * FROM daily_grid_profit ORDER BY capture_date")}
            self.assertEqual([rows[d]["event"] for d in sorted(rows)], ["baseline", "leverage", "continue"])
            self.assertEqual([rows[d]["leverage_x"] for d in sorted(rows)], [10.0, 6.0, 6.0])
            self.assertEqual(summary_values(con), before)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM ledger_corrections").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT MAX(version) FROM schema_meta").fetchone()[0], store.SCHEMA_VERSION)
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
