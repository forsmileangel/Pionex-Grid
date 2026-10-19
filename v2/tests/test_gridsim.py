import unittest

from v2.gridsim import levels, liq_price, new_bot_liq, per_volume, simulate


def candle(t, o, h, l, c):
    return {"t": t, "o": o, "h": h, "l": l, "c": c}


class GridSimTests(unittest.TestCase):
    def test_round_trip_pays_one_grid_step(self):
        lv = levels(90, 110, 20, "arithmetic")  # 1.0 per grid
        pv = 2.0
        day = 1_791_400_000_000
        # bought at 100 on the way down; the pair closes only when price reaches the next level, 101
        half = simulate([candle(day, 100.2, 100.2, 99.2, 99.2), candle(day + 900_000, 99.2, 100.4, 99.2, 100.4)],
                        lv, pv, start_price=100.2, fee_rate=0)
        self.assertEqual(half["pairs"], 0)
        sim = simulate([candle(day, 100.2, 100.2, 99.2, 99.2), candle(day + 900_000, 99.2, 101.2, 99.2, 101.2)],
                       lv, pv, start_price=100.2, fee_rate=0)
        self.assertEqual(sim["pairs"], 1)
        self.assertAlmostEqual(sum(sim["by_day"].values()), pv * 1.0)

    def test_no_profit_without_a_completed_pair(self):
        lv = levels(90, 110, 20, "arithmetic")
        sim = simulate([candle(1_791_400_000_000, 100.5, 100.5, 95.5, 95.5)], lv, 1.0, start_price=100.5, fee_rate=0)
        self.assertEqual(sim["pairs"], 0)
        self.assertGreater(sim["position"], 0)

    def test_higher_leverage_moves_liquidation_closer(self):
        liq3, _ = new_bot_liq(100, 50, 125, 300, 3)
        liq6, _ = new_bot_liq(100, 50, 125, 300, 6)
        self.assertLess(liq3, liq6)
        self.assertLess(liq6, 100)

    def test_more_margin_lowers_liquidation(self):
        lv = levels(50, 125, 300)
        pv = per_volume(1000, 4, lv)
        position = pv * sum(1 for l in lv[:-1] if l >= 100)
        self.assertGreater(liq_price(1000, 100, position, lv, pv), liq_price(1500, 100, position, lv, pv))
