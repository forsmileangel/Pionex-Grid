import unittest

from v2.liq import normalize_liq


class LiqTests(unittest.TestCase):
    def test_usdt_long_stays_positive(self):
        liq, dist, mode = normalize_liq("long", 456.73, 286.69, coin_margined=False)
        self.assertEqual(mode, "usdt")
        self.assertAlmostEqual(liq, 286.69)
        self.assertGreater(dist, 0)
        self.assertLess(dist, 100)

    def test_usdt_short_stays_positive(self):
        liq, dist, mode = normalize_liq("short", 100, 130, coin_margined=False)
        self.assertEqual(mode, "usdt")
        self.assertAlmostEqual(dist, 30)

    def test_usdt_negative_is_clamped_not_inverted(self):
        liq, dist, mode = normalize_liq("long", 456, 500, coin_margined=False)
        self.assertEqual(mode, "usdt-clamped")
        self.assertEqual(liq, 500)
        self.assertEqual(dist, 0)

    def test_coin_inverse_raw_price(self):
        liq, dist, mode = normalize_liq("short", 2519.86, 0.0007690328458623, coin_margined=True)
        self.assertAlmostEqual(liq, 1300.34, delta=0.02)
        self.assertGreaterEqual(dist, 0)
        self.assertLessEqual(dist, 100)
        self.assertAlmostEqual(dist, 48.4, delta=0.3)

    def test_coin_already_converted_usdt_price(self):
        liq, dist, _mode = normalize_liq("short", 2519.86, 1300.34, coin_margined=True)
        self.assertAlmostEqual(liq, 1300.34, delta=0.02)
        self.assertGreaterEqual(dist, 0)
        self.assertLessEqual(dist, 100)

    def test_negative_without_flag_but_magnitude_mismatch_is_coin(self):
        liq, dist, _mode = normalize_liq("short", 2519.86, 0.000769, coin_margined=False)
        self.assertAlmostEqual(liq, 1 / 0.000769, delta=1)
        self.assertGreaterEqual(dist, 0)
        self.assertLessEqual(dist, 100)


if __name__ == "__main__":
    unittest.main()
