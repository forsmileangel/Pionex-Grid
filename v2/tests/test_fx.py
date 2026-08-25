import unittest

from v2.fx import attach_twd


class FxTests(unittest.TestCase):
    def test_attach_twd(self):
        out = attach_twd({"usdt": "54731.28"}, 31.810571)
        self.assertAlmostEqual(int(out["twd"]), 1741033, delta=5)

    def test_missing_amount(self):
        self.assertIsNone(attach_twd(None, 31.8))


if __name__ == "__main__":
    unittest.main()
