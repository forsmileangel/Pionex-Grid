import unittest

from v2.healthcheck import asset_class, kline_metrics, needed_deposit, project, suggest_params
from v2.healthcheck_report import range_bar


def bars(closes):
    return [{"time": i, "open": c, "close": c, "high": c * 1.02, "low": c * 0.98} for i, c in enumerate(closes)]


class HealthcheckTests(unittest.TestCase):
    def test_choppy_series_scores_higher_than_trend(self):
        choppy = kline_metrics(bars([100 + (5 if i % 2 else -5) for i in range(200)]))
        trend = kline_metrics(bars([100 + i for i in range(200)]))
        self.assertLess(choppy["er90"], 0.05)
        self.assertGreater(trend["er90"], 0.95)
        self.assertGreater(choppy["grid_score"], trend["grid_score"])
        self.assertIsNone(kline_metrics(bars([100] * 30)))

    def test_leverage_is_capped_by_drawdown(self):
        calm = {"atr14_pct": 1.5, "maxdd180_pct": 12, "last": 100, "lo180": 80, "hi180": 120}
        rough = {**calm, "maxdd180_pct": 45}
        self.assertEqual(suggest_params(calm)["leverage"], 6)
        self.assertEqual(suggest_params(rough)["leverage"], 3)
        self.assertEqual(suggest_params(rough)["liq_target_pct"], 68)
        s = suggest_params(calm)
        self.assertLess(s["bottom"], 80)
        self.assertGreater(s["top"], 120)

    def test_goal_deposit_reaches_target(self):
        need = needed_deposit(100_000, 0.7, 0.001, 6, 200_000)
        self.assertGreaterEqual(project(100_000, 0.7, 0.001, 6, need)[-1], 200_000 - 1)
        self.assertEqual(needed_deposit(100_000, 0.7, 0.001, 6, 50_000), 0.0)

    def test_classification_and_range_bar(self):
        self.assertEqual(asset_class("AMDX"), "美股代幣")
        self.assertEqual(asset_class("AVAX"), "加密貨幣")
        self.assertEqual(asset_class("XAU"), "商品")
        svg = range_bar({"bottom": 190, "top": 690, "mark": 647, "liq": 281})
        self.assertIn("<svg", svg)
        self.assertIn("預估強平 281", svg)
        self.assertEqual(range_bar({"bottom": None, "top": 690, "mark": 647}), "—")
