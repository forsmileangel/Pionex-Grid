import unittest

from v2.levercheck import compare


def state(**kw):
    base = {"id": "g1", "symbol": "ADA/USDT", "inverse": False, "leverage": 3.0, "investment": 4500.0,
            "quote_investment": 4500.0, "extra_margin": 0.0, "capital": 4500.0, "margin_balance": 4700.0,
            "per_volume": 176.0, "position": 29000.0, "mark": 0.25, "grid_profit": 240.0, "reinvest": 0.0,
            "withdrawn": 0.0, "reduce": 0.0, "top": 0.36, "bottom": 0.14, "rows": 450, "grid_type": "geometric",
            "liq_pionex": 0.155}
    base.update(kw)
    base["notional_basis"] = base["investment"] * base["leverage"]
    return base


class LeverCheckTests(unittest.TestCase):
    def verdict(self, after, before=None):
        return compare({"g1": before or state()}, {"g1": after})["rows"][0]

    def test_margin_kept_in_bot_is_equivalent(self):
        r = self.verdict(state(leverage=6.0, investment=2250.0, quote_investment=2250.0, extra_margin=2250.0,
                               grid_profit=241.0))
        self.assertEqual(r["verdict"], "adjusted")
        self.assertTrue(any("實際曝險不變" in n for n in r["notes"]))
        self.assertTrue(any("帳本「投入」" in n for n in r["notes"]))
        self.assertFalse(any("不能直接比" in n for n in r["notes"]))

    def test_bigger_grid_quantity_is_flagged(self):
        r = self.verdict(state(leverage=6.0, per_volume=352.0))
        self.assertEqual(r["verdict"], "exposure_changed")
        self.assertTrue(any("不能直接比" in n for n in r["notes"]))

    def test_add_keeps_per_u_comparable(self):
        r = self.verdict(state(investment=5000.0, quote_investment=5000.0, capital=5000.0, per_volume=195.0))
        self.assertEqual(r["verdict"], "exposure_changed")
        self.assertFalse(any("不能直接比" in n for n in r["notes"]))

    def test_recreated_bot_and_reset_profit(self):
        res = compare({"g1": state()}, {"g2": state(id="g2")})["rows"]
        self.assertEqual({r["verdict"] for r in res}, {"missing", "new"})
        self.assertEqual(self.verdict(state(grid_profit=3.0))["verdict"], "grid_reset")
