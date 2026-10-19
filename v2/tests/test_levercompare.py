import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2.levercompare import compare_payload
from v2.store import ingest
from v2.tests.test_store import FX, rec, snap


class LeverCompareTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite"
        self.fx = patch("v2.store.usdt_twd", return_value=FX)
        self.fx.start()

    def tearDown(self):
        self.fx.stop()
        self.tmp.cleanup()

    def row(self, payload, oid="g1"):
        return next(r for r in payload["rows"] if r["bu_order_id"] == oid)

    def test_theirs_is_ours_times_equivalent_leverage(self):
        # Raised from 4x to 10x with the margin kept in the bot: 3000 investment + 4500 extra.
        ingest(snap("2026-08-25", [rec(Investment=3000, ExtraMargin=4500, Leverage="10x long")]), self.db)
        r = self.row(compare_payload(self.db))
        self.assertEqual(r["ours"], 7500)
        self.assertEqual(r["theirs"], 30000)
        self.assertEqual(r["our_leverage"], 4.0)
        self.assertEqual(r["ratio"], 4.0)

    def test_extra_margin_only_moves_ours(self):
        ingest(snap("2026-08-25", [
            rec(Investment=1000, Leverage="5x long"),
            rec(ApiOrderId="g2", Key="ETH/USDT|2026-08-01 11:00:00", Symbol="ETH/USDT",
                Created="2026-08-01 11:00:00", Investment=1000, ExtraMargin=1000, Leverage="5x long"),
        ]), self.db)
        p = compare_payload(self.db)
        plain, padded = self.row(p), self.row(p, "g2")
        self.assertEqual(plain["theirs"], padded["theirs"])
        self.assertEqual(padded["ours"], 2 * plain["ours"])
        self.assertEqual(padded["our_leverage"], 2.5)
        self.assertEqual(p["totals"]["ours"], 3000)
        self.assertEqual(p["totals"]["theirs"], 10000)
        self.assertEqual(p["totals"]["our_leverage"], 3.33)
        self.assertEqual(plain["our_share"], 33.33)
        self.assertEqual(plain["their_share"], 50.0)

    def test_efficiency_differs_only_by_the_base(self):
        ingest(snap("2026-08-25", [rec(Investment=1000, ExtraMargin=1000, Leverage="4x long",
                                       Created="2026-08-15 00:00:00", GridProfit=20)]), self.db)
        r = self.row(compare_payload(self.db))
        self.assertEqual(r["days_open"], 10)
        self.assertEqual(r["our_eff"], 36.5)       # 20 / 2000 / 10 x 365
        self.assertEqual(r["their_eff"], 18.25)    # 20 / 4000 / 10 x 365

    def test_coin_margined_theirs_uses_the_current_price(self):
        raw = {"order": {"quote": "ETH", "buOrderData": {
            "quoteInvestment": 2, "openQuotePrice": 2000, "gridProfit": 0.1,
            "totalFundingFee": 0.01, "fundingFeePayment": -0.02}}}
        ingest(snap("2026-08-25", [rec(Product="coin_margined_contract_grid", Symbol="ETH",
                                       Investment=4000, Leverage="4x short", Trend="short",
                                       GridProfit=250, RawGridProfit=0.1, ConversionPrice=2500,
                                       ConversionSymbol="ETH_USDT", RawJson=raw)]), self.db)
        r = self.row(compare_payload(self.db))
        self.assertEqual(r["investment"], 4000)       # ours: 2 ETH at the 2000 open price
        self.assertEqual(r["their_position"], 5000)   # theirs: 2 ETH at today's 2500
        self.assertEqual(r["theirs"], 20000)
        self.assertEqual(r["funding_net"], 25)
        self.assertEqual(r["funding_paid"], -50)

    def test_empty_ledger(self):
        p = compare_payload(self.db)
        self.assertEqual(p["rows"], [])
        self.assertIsNone(p["totals"])


if __name__ == "__main__":
    unittest.main()
