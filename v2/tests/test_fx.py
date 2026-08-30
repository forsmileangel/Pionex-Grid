import unittest

from v2.fx import attach_twd, compose_usdt_twd, parse_bot_usd_cash_buy

BOT_CSV = """Currency,Rate,Cash,Spot
USD,Buying,31.49500,31.84500,Selling,32.16500,31.94500,
HKD,Buying,3.91300,4.03900,Selling,4.11700,4.09900,
"""
BOT_CSV_ZH = """幣別,匯率,現金,即期
USD,本行買入,31.49000,31.84000,本行賣出,32.16000,31.94000,
"""


class FxTests(unittest.TestCase):
    def test_parse_bot_cash_buy(self):
        self.assertEqual(parse_bot_usd_cash_buy(BOT_CSV), 31.495)

    def test_parse_bot_bom(self):
        self.assertEqual(parse_bot_usd_cash_buy("\ufeff" + BOT_CSV), 31.495)

    def test_parse_bot_zh(self):
        self.assertEqual(parse_bot_usd_cash_buy(BOT_CSV_ZH), 31.49)

    def test_compose_matches_pionex_ballpark(self):
        rate = compose_usdt_twd(31.495, 0.9998)
        out = attach_twd({"usdt": "54731.28"}, rate)
        self.assertAlmostEqual(int(out["twd"]), 1723518, delta=200)

    def test_attach_twd(self):
        out = attach_twd({"usdt": "54731.28"}, 31.810571)
        self.assertAlmostEqual(int(out["twd"]), 1741033, delta=5)

    def test_missing_amount(self):
        self.assertIsNone(attach_twd(None, 31.8))


if __name__ == "__main__":
    unittest.main()
