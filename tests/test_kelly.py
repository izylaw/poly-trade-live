import unittest
from src.risk.kelly import half_kelly, calc_payout_ratio


class TestKelly(unittest.TestCase):
    def test_positive_edge(self):
        # 90% win rate, buying at 0.85 -> payout ratio = 0.15/0.85 = 0.176
        payout = calc_payout_ratio(0.85)
        size = half_kelly(win_prob=0.90, payout_ratio=payout, available_balance=100)
        self.assertGreater(size, 0)
        self.assertLessEqual(size, 10)  # max 10% of balance

    def test_negative_edge_returns_zero(self):
        # 50% win, bad payout -> negative kelly
        payout = calc_payout_ratio(0.95)
        size = half_kelly(win_prob=0.50, payout_ratio=payout, available_balance=100)
        self.assertEqual(size, 0)

    def test_min_trade_enforcement(self):
        # Very small size should return 0 if below min
        payout = calc_payout_ratio(0.93)
        size = half_kelly(win_prob=0.94, payout_ratio=payout, available_balance=5, min_trade=0.50)
        # With $5 balance, half-kelly on a small edge might be below $0.50
        self.assertTrue(size == 0 or size >= 0.50)

    def test_max_trade_cap(self):
        payout = calc_payout_ratio(0.50)
        size = half_kelly(win_prob=0.99, payout_ratio=payout, available_balance=100, max_trade_pct=0.10)
        self.assertLessEqual(size, 10.0)

    def test_payout_ratio_boundaries(self):
        self.assertEqual(calc_payout_ratio(0.0), 0.0)
        self.assertEqual(calc_payout_ratio(1.0), 0.0)
        self.assertGreater(calc_payout_ratio(0.50), 0)

    def test_zero_balance(self):
        size = half_kelly(win_prob=0.90, payout_ratio=0.5, available_balance=0)
        self.assertEqual(size, 0)


class TestPayoutRatio(unittest.TestCase):
    def test_normal_price(self):
        self.assertAlmostEqual(calc_payout_ratio(0.50), 1.0, places=2)
        self.assertAlmostEqual(calc_payout_ratio(0.80), 0.25, places=2)

    def test_high_price(self):
        ratio = calc_payout_ratio(0.95)
        self.assertAlmostEqual(ratio, 0.0526, places=3)


if __name__ == "__main__":
    unittest.main()
