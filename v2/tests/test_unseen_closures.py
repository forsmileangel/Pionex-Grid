"""Bots opened and closed between two captures, and liquidations that lose the whole margin."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2 import funding, settlements, store
from v2.tests.test_store import FX, rec, snap

TABLES = ("daily_grid_profit", "daily_summary", "grid_positions")


def dump(db):
    con = sqlite3.connect(db)
    try:
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall() for t in TABLES}
    finally:
        con.close()


def quick(oid="gx", created="2026-09-18 10:00:00", closed="2026-09-18 20:00:00", data=None, **kwargs):
    """A finished bot that was never running at a capture."""
    order = {"base": "SOL.PERP", "quote": "USDT", "buOrderData": data or {}}
    fields = dict(ApiOrderId=oid, Key=f"SOL/USDT|{created}", Symbol="SOL/USDT", Created=created, Closed=closed,
                  ListStatus="finished", Investment=500, GridProfit=6, FundingFee=-1.5, RawJson={"order": order})
    fields.update(kwargs)
    return rec(**fields)


LIQUIDATED = {"investmentFrom": "USER", "quoteInvestment": "500", "extraMargin": "0", "unlockQuoteAmount": "0",
              "profitReinvest": "0", "profitWithdrawn": "0", "totalRealizedProfit": "-490", "totalFee": "-15",
              "totalFundingFee": "-1.5", "reasonBy": "force_liquidation"}


class Ledger(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "ledger.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX), patch("v2.store.BACKUP_DIR", self.dir / "backups")]:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, date, rows, db=None, **kwargs):
        return store.ingest(snap(date, rows), db or self.db, **kwargs)

    def day(self, date, db=None):
        con = store.connect(db or self.db)
        try:
            return dict(con.execute("SELECT * FROM daily_summary WHERE capture_date=?", (date,)).fetchone())
        finally:
            con.close()

    def rows_for(self, oid):
        con = store.connect(self.db)
        try:
            return [dict(r) for r in con.execute(
                "SELECT capture_date, status, daily_profit_i FROM daily_grid_profit WHERE bu_order_id=? ORDER BY capture_date", (oid,))]
        finally:
            con.close()

    def settlement(self, oid):
        return next((r for r in settlements.get_settlements(self.db)["rows"] if r["id"] == oid), None)

    def full_rebuild_matches(self):
        copy = self.dir / "copy.sqlite"
        for p in self.dir.glob("copy.sqlite*"):
            p.unlink()
        shutil.copy2(self.db, copy)
        con = store.connect(copy)
        try:
            store.rebuild_daily_profits(con)
            con.commit()
        finally:
            con.close()
        self.assertEqual(dump(copy), dump(self.db))


class UnseenClosureTests(Ledger):
    def test_a_bot_opened_and_closed_between_captures_counts_on_that_day(self):
        self.capture("2026-09-18", [rec(GridProfit=100)])
        self.capture("2026-09-19", [rec(GridProfit=104), quick(data=LIQUIDATED)])
        day = self.day("2026-09-19")
        self.assertEqual(day["daily_profit_i"], store.to_fixed(4 + 6))
        self.assertEqual((day["closed_count"], day["unresolved_count"], day["active_count"]), (1, 0, 1))
        self.assertEqual(self.rows_for("gx"), [{"capture_date": "2026-09-19", "status": "closed",
                                                "daily_profit_i": store.to_fixed(6)}])
        row = self.settlement("gx")
        self.assertEqual((row["status"], row["daily_reconciled"], row["net_profit"]), ("settled", True, "-500"))
        self.assertEqual(self.day("2026-09-19")["true_profit_i"], store.to_fixed(104 + 6))
        con = store.connect(self.db)
        try:
            rows, _ = funding.funding_by_day(con)
        finally:
            con.close()
        self.assertEqual(rows[("2026-09-19", "gx")]["delta_i"], store.to_fixed(-1.5))
        self.full_rebuild_matches()

    def test_found_a_day_late_it_lands_on_the_original_day_and_is_logged(self):
        self.capture("2026-09-18", [rec(GridProfit=100)])
        self.capture("2026-09-19", [rec(GridProfit=104)])
        self.assertEqual(self.day("2026-09-19")["daily_profit_i"], store.to_fixed(4))
        self.capture("2026-09-20", [rec(GridProfit=107), quick()])
        self.assertEqual(self.day("2026-09-19")["daily_profit_i"], store.to_fixed(10))
        self.assertEqual(self.day("2026-09-20")["daily_profit_i"], store.to_fixed(3))
        fixed = {(c["capture_date"], c["field"]): c for c in store.ledger_publish_payload(self.db)["corrections"]}
        self.assertEqual((fixed[("2026-09-19", "daily_profit_i")]["old_value"],
                          fixed[("2026-09-19", "daily_profit_i")]["new_value"]), ("4", "10"))
        self.full_rebuild_matches()

    def test_the_partial_live_write_agrees_with_the_full_daily_pass(self):
        full = self.dir / "full.sqlite"
        for db in (self.db, full):
            self.capture("2026-09-18", [rec(GridProfit=100)], db=db)
            self.capture("2026-09-19", [rec(GridProfit=104)], db=db)
        self.capture("2026-09-20", [rec(GridProfit=107), quick()], db=full)
        self.capture("2026-09-20", [rec(GridProfit=107), quick()], full_rebuild=False)
        self.assertEqual(dump(self.db), dump(full))

    def test_replacing_the_day_keeps_the_closure_once(self):
        self.capture("2026-09-18", [rec(GridProfit=100)])
        self.capture("2026-09-19", [rec(GridProfit=104), quick()])
        self.capture("2026-09-19", [rec(GridProfit=105), quick()], replace_date=True)
        self.assertEqual(self.day("2026-09-19")["daily_profit_i"], store.to_fixed(5 + 6))
        self.assertEqual(len(self.rows_for("gx")), 1)
        settlements.refresh(snap("2026-09-19", [rec(GridProfit=105), quick()]), self.db)
        self.assertEqual(self.day("2026-09-19")["daily_profit_i"], store.to_fixed(5 + 6))
        self.assertEqual(sum(r["id"] == "gx" for r in settlements.get_settlements(self.db)["rows"]), 1)

    def test_bots_from_before_the_ledger_or_across_a_capture_are_left_alone(self):
        before = quick("old", created="2026-08-01 10:00:00", closed="2026-08-02 10:00:00")
        straddle = quick("gy", created="2026-09-17 22:00:00", closed="2026-09-18 12:00:00")
        across = quick("gz", created="2026-09-18 10:00:00", closed="2026-09-19 10:00:00")
        self.capture("2026-09-18", [rec(GridProfit=100), before])
        self.capture("2026-09-19", [rec(GridProfit=104), before, straddle])
        self.capture("2026-09-20", [rec(GridProfit=107), before, straddle, across])
        for oid in ("old", "gy", "gz"):
            self.assertEqual(self.rows_for(oid), [])
            self.assertIsNone(self.settlement(oid))
        self.assertEqual([self.day(d)["daily_profit_i"] for d in ("2026-09-19", "2026-09-20")],
                         [store.to_fixed(4), store.to_fixed(3)])

    def test_a_coin_margined_bot_counts_at_its_close_price(self):
        coin = dict(Product="coin_margined_contract_grid", Symbol="ETH", RawGridProfit="0.002", ConversionPrice=2600,
                    GridProfit=5.2, RawJson={"order": {"base": "USDT.PERP", "quote": "ETH", "buOrderData": {
                        "investmentFrom": "USER", "quoteInvestment": "0.2", "unlockQuoteAmount": "0.202",
                        "closedPrice": "0.0004"}}})
        self.capture("2026-09-18", [rec(GridProfit=100)])
        self.capture("2026-09-19", [rec(GridProfit=104), quick("gc", **coin)])
        self.assertEqual(self.day("2026-09-19")["daily_profit_i"], store.to_fixed(4 + 5))  # 0.002 ETH x 2500
        self.full_rebuild_matches()


class LiquidationTests(Ledger):
    def closed(self, **data):
        order = {"base": "BTC.PERP", "quote": "USDT",
                 "buOrderData": {"investmentFrom": "USER", "quoteInvestment": "2000", "extraMargin": "1000",
                                 "unlockQuoteAmount": "0", "totalRealizedProfit": "-2950", "totalFee": "-60",
                                 "totalFundingFee": "-20", "reasonBy": "force_liquidation", **data}}
        self.capture("2026-09-18", [rec(GridProfit=100)])
        self.capture("2026-09-19", [rec(ListStatus="finished", Closed="2026-09-18 22:00:00", GridProfit=104,
                                        RawJson={"order": order})])
        return self.settlement("g1")

    def test_a_liquidation_loses_the_margin_and_the_platform_bears_the_rest(self):
        row = self.closed()
        self.assertEqual(row["net_profit"], "-3000")
        self.assertEqual(row["net_check"], "-3030")
        self.assertEqual(row["liquidation_absorbed"], "30")
        self.assertTrue(row["net_check_ok"])
        self.assertFalse(row["needs_review"])
        self.assertEqual(self.day("2026-09-19")["daily_profit_i"], store.to_fixed(4))
        self.assertEqual(store.status_payload(self.db)["settlement_check_flags"], 0)

    def test_the_same_gap_without_a_liquidation_is_still_flagged(self):
        row = self.closed(reasonBy="user_cancel")
        self.assertIsNone(row["liquidation_absorbed"])
        self.assertTrue(row["needs_review"])

    def test_a_liquidation_that_returned_money_or_lost_less_is_still_checked(self):
        self.assertTrue(self.closed(unlockQuoteAmount="12")["needs_review"])

    def test_a_liquidation_reporting_less_loss_than_the_wallet_is_flagged(self):
        self.assertTrue(self.closed(totalRealizedProfit="-2900")["needs_review"])


if __name__ == "__main__":
    unittest.main()
