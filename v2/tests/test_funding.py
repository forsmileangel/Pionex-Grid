import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2.funding import funding_payload
from v2.store import ingest
from v2.tests.test_store import FX, rec, snap


def usdt(value):
    return None if value["usdt"] is None else round(float(value["usdt"]), 6)


class FundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite"
        self.fx = patch("v2.store.usdt_twd", return_value=FX)
        self.fx.start()
        self.fx2 = patch("v2.funding.usdt_twd", return_value=FX)
        self.fx2.start()

    def tearDown(self):
        self.fx.stop()
        self.fx2.stop()
        self.tmp.cleanup()

    def day(self, payload, date):
        return next(d for d in payload["days"] if d["date"] == date)

    def row(self, payload, date, oid="g1"):
        return next(r for r in payload["rows_by_date"][date] if r["bu_order_id"] == oid)

    def test_daily_funding_is_the_change_in_the_running_total(self):
        ingest(snap("2026-08-25", [rec(GridProfit=10, FundingFee=-10)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=16, FundingFee=-13)]), self.db)
        p = funding_payload(self.db)
        self.assertIsNone(usdt(self.row(p, "2026-08-25")["funding_day"]))
        self.assertIsNone(usdt(self.day(p, "2026-08-25")["funding"]))
        self.assertEqual(usdt(self.row(p, "2026-08-26")["funding_day"]), -3)
        day2 = self.day(p, "2026-08-26")
        self.assertEqual(usdt(day2["funding"]), -3)
        self.assertEqual(day2["pct_of_grid"], -50.0)
        self.assertEqual(usdt(day2["net"]), 3)
        self.assertEqual(usdt(self.row(p, "2026-08-26")["net"]), 3)
        self.assertIsNone(usdt(self.day(p, "2026-08-25")["net"]))
        self.assertEqual(usdt(day2["total"]), -13)

    def test_new_grid_counts_its_funding_since_creation(self):
        ingest(snap("2026-08-25", [rec(GridProfit=10, FundingFee=-10)]), self.db)
        ingest(snap("2026-08-26", [
            rec(GridProfit=16, FundingFee=-13),
            rec(ApiOrderId="g2", Key="ETH/USDT|2026-08-25 20:00:00", Symbol="ETH/USDT",
                Created="2026-08-25 20:00:00", GridProfit=1, FundingFee=-1),
        ]), self.db)
        p = funding_payload(self.db)
        self.assertEqual(usdt(self.row(p, "2026-08-26", "g2")["funding_day"]), -1)
        self.assertEqual(usdt(self.day(p, "2026-08-26")["funding"]), -4)

    def test_day_without_funding_rolls_into_the_next_day(self):
        ingest(snap("2026-08-25", [rec(GridProfit=10, FundingFee=-10)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=12, FundingFee=None)]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=14, FundingFee=-16)]), self.db)
        p = funding_payload(self.db)
        self.assertIsNone(usdt(self.row(p, "2026-08-26")["funding_day"]))
        self.assertEqual(usdt(self.day(p, "2026-08-26")["total"]), -10)
        day3 = self.day(p, "2026-08-27")
        self.assertEqual(usdt(day3["funding"]), -6)
        self.assertEqual(day3["span_days"], 2)

    def test_coin_margined_funding_is_priced_at_the_day_rate(self):
        def coin(date, price, coin_funding):
            raw = {"order": {"quote": "ETH", "buOrderData": {"gridProfit": 0.1}}}
            ingest(snap(date, [rec(Product="coin_margined_contract_grid", Symbol="ETH",
                                   GridProfit=0.1 * price, RawGridProfit=0.1, ConversionPrice=price,
                                   ConversionSymbol="ETH_USDT", FundingFee=coin_funding * price,
                                   RawJson=raw)]), self.db)
        coin("2026-08-25", 2000, -0.01)
        coin("2026-08-26", 2500, -0.015)
        p = funding_payload(self.db)
        # 0.005 ETH more paid, priced at 2500 — not the -17.5 a plain USDT difference would show.
        self.assertEqual(usdt(self.row(p, "2026-08-26")["funding_day"]), -12.5)

    def test_closed_grid_keeps_its_total(self):
        ingest(snap("2026-08-25", [
            rec(GridProfit=10, FundingFee=-10),
            rec(ApiOrderId="g2", Key="ETH/USDT|2026-08-01 11:00:00", Symbol="ETH/USDT",
                Created="2026-08-01 11:00:00", GridProfit=5, FundingFee=-4),
        ]), self.db)
        ingest(snap("2026-08-26", [
            rec(GridProfit=12, FundingFee=-11),
            rec(ApiOrderId="g2", Key="ETH/USDT|2026-08-01 11:00:00", Symbol="ETH/USDT",
                Created="2026-08-01 11:00:00", GridProfit=6, FundingFee=-5,
                ListStatus="finished", Closed="2026-08-25 23:00:00"),
        ]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=13, FundingFee=-12)]), self.db)
        p = funding_payload(self.db)
        self.assertEqual(usdt(self.day(p, "2026-08-26")["funding"]), -2)
        self.assertEqual(usdt(self.day(p, "2026-08-27")["total"]), -17)
        closed = next(g for g in p["grids"] if g["bu_order_id"] == "g2")
        self.assertEqual(usdt(closed["funding_cum"]), -5)
        self.assertEqual(closed["lifecycle"], "closed")


if __name__ == "__main__":
    unittest.main()
