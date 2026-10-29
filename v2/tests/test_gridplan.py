import unittest

from v2.gridsim import levels, liq_price
from v2.gridplan import best_config, existing_actions, group_of, margin_for_liq, shock_for, support_table


def _config(lev, rate, liq_dist, outside=0.1):
    return {"lev": lev, "rate": rate, "liq_dist": liq_dist, "outside30": outside, "outside60": outside}


class GridPlanRuleTests(unittest.TestCase):
    def test_best_config_is_the_highest_rate_inside_the_drawdown_limit(self):
        plan = {"liq_min": 40, "dd_mult": 1.0, "lev_max": 5}
        maxdd = 50
        need = max(plan["liq_min"], plan["dd_mult"] * maxdd)
        configs = [
            _config(5, 0.90, need - 1),
            _config(6, 0.80, need + 30),
            _config(4, 0.70, need + 10, outside=0.5),
            _config(5, 0.40, need + 5),
            _config(3, 0.25, need + 20),
        ]
        best = best_config(configs, plan, maxdd)
        allowed = [
            item for item in configs
            if item["lev"] <= plan["lev_max"] and item["liq_dist"] >= need
            and (item["outside30"] + item["outside60"]) / 2 < 0.4
        ]
        self.assertEqual(best["rate"], max(item["rate"] for item in allowed))
        self.assertLess(best["rate"], max(item["rate"] for item in configs))
        self.assertGreaterEqual(best["liq_dist"], need)
        self.assertIsNone(best_config([_config(5, 0.9, need - 1)], plan, maxdd))

    def test_margin_for_liq_moves_liquidation_to_the_target_distance(self):
        safe = {
            "InverseCoinMargined": False, "Bottom": 80, "Top": 120, "Row": 10,
            "GridType": "geometric", "MarkPrice": 100, "MarginBalance": 200,
            "Position": 2, "PerVolume": 0.2,
        }
        tight = dict(safe, MarginBalance=20, Position=5, PerVolume=0.5)
        self.assertIsNone(margin_for_liq(None, 10))
        self.assertIsNone(margin_for_liq({"InverseCoinMargined": True}, 10))
        self.assertEqual(margin_for_liq(safe, 15), 0.0)
        self.assertIsNone(margin_for_liq(tight, 20))

        extra = margin_for_liq(tight, 5)
        target = tight["MarkPrice"] * (1 - 5 / 100)
        ladder = levels(tight["Bottom"], tight["Top"], tight["Row"], tight["GridType"])
        before = liq_price(tight["MarginBalance"], tight["MarkPrice"], tight["Position"], ladder, tight["PerVolume"])
        after = liq_price(tight["MarginBalance"] + extra, tight["MarkPrice"], tight["Position"], ladder, tight["PerVolume"])
        self.assertGreater(extra, 0)
        self.assertGreater(before, target)
        self.assertLessEqual(after, target)

    def test_support_table_picks_the_lowest_leverage_that_reaches_each_target(self):
        def cfg(lev, capacity, liq, down=0.5, up=0.25):
            return {"down": down, "up": up, "lev": lev, "capacity": capacity, "liq_dist": liq}

        high = {
            "base": "HIGH", "class": "加密貨幣", "sector": "主流", "held": True,
            "maxdd180_pct": 40, "atr14_pct": 3, "corr_book": 0.2,
            "configs": [
                cfg(3, 0.10, 60), cfg(4, 0.16, 45), cfg(5, 0.22, 30), cfg(6, 0.30, 18),
                cfg(3, 9, 1, down=0.4),
            ],
        }
        low = {
            "base": "LOW", "class": "美股", "sector": "能源", "held": False,
            "maxdd180_pct": 20, "atr14_pct": 1, "corr_book": 0.1,
            "configs": [cfg(3, 0.60, 50), cfg(4, 0.70, 40)],
        }
        missing = dict(low, base="NO3", configs=[cfg(4, 0.90, 30)])
        rows = support_table([high, low, missing], targets=(0.15, 0.40))
        self.assertEqual([row["base"] for row in rows], ["LOW", "HIGH"])
        chosen = rows[1]["need"]["0.15"]
        in_range = [item for item in high["configs"] if item["down"] == 0.5 and item["up"] == 0.25]
        lowest = min(item["lev"] for item in in_range if item["capacity"] >= 0.15)
        self.assertEqual(chosen["lev"], lowest)
        self.assertEqual(chosen["liq_dist"], rows[1]["liq_by_lev"][str(lowest)])
        self.assertIsNone(rows[1]["need"]["0.40"]["lev"])

    def test_group_of_and_shock_for_follow_the_sector_rules(self):
        self.assertEqual(group_of({"sector": "半導體", "class": "美股", "base": "NVDA"}), "晶片／AI 硬體")
        self.assertEqual(group_of({"sector": "其他", "class": "加密貨幣", "base": "BTC"}), "加密主流")
        self.assertEqual(group_of({"sector": "其他", "class": "加密貨幣", "base": "DOGE"}), "加密山寨")
        self.assertEqual(group_of({"sector": "能源", "class": "商品", "base": "XAU"}), "能源")
        shock = {"drop": {"加密山寨": 45, "加密貨幣": 20, "*": 10}}
        self.assertEqual(shock_for({"group": "加密山寨", "class": "加密貨幣"}, shock), 45)
        self.assertEqual(shock_for({"group": "其他", "class": "加密貨幣"}, shock), 20)
        self.assertEqual(shock_for({"group": "其他", "class": "股票"}, shock), 10)

    def test_existing_actions_keep_top_up_or_close_by_the_plan_rules(self):
        plan = {"liq_min": 50, "dd_mult": 1.0}
        tight = {
            "InverseCoinMargined": False, "Bottom": 80, "Top": 120, "Row": 10,
            "GridType": "geometric", "MarkPrice": 100, "MarginBalance": 20,
            "Position": 5, "PerVolume": 0.5,
        }
        rows = existing_actions("保守", plan, [
            {"label": "coin", "coin": True, "rate": 1.0, "liq_distance_pct": 90, "maxdd180_pct": 10, "capital": 1000, "rec": {}},
            {"label": "weak", "coin": False, "rate": 0.01, "liq_distance_pct": 90, "maxdd180_pct": 10, "capital": 1000, "rec": {}},
            {"label": "none", "coin": False, "rate": None, "liq_distance_pct": 90, "maxdd180_pct": 10, "capital": 1000, "rec": {}},
            {"label": "safe", "coin": False, "rate": 0.2, "liq_distance_pct": 90, "maxdd180_pct": 10, "capital": 1000, "rec": {}},
            {"label": "far", "coin": False, "rate": 0.2, "liq_distance_pct": 1, "maxdd180_pct": 10, "capital": 1000, "rec": tight},
        ])
        actions = {row["label"]: row["action"] for row in rows}
        self.assertEqual(actions, {"coin": "close_later", "weak": "close", "none": "close", "safe": "keep", "far": "close"})

        top_up = {
            "label": "add", "coin": False, "rate": 0.5, "liq_distance_pct": 1,
            "maxdd180_pct": 0, "capital": 1000, "rec": tight,
        }
        added = existing_actions("積極", {"liq_min": 5, "dd_mult": 0}, [top_up])[0]
        extra = margin_for_liq(tight, 5)
        self.assertEqual(added["action"], "top_up")
        self.assertGreater(extra, 0)
        self.assertGreaterEqual(added["top_up"], extra)
        self.assertEqual(added["top_up"] % 50, 0)
        self.assertLessEqual(added["top_up"], top_up["capital"] * 0.8)

        dear = dict(top_up, label="dear", capital=10)
        self.assertEqual(existing_actions("積極", {"liq_min": 5, "dd_mult": 0}, [dear])[0]["action"], "close")
