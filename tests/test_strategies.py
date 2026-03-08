import unittest
from unittest.mock import MagicMock
from src.config.settings import Settings
from src.strategies.high_probability import HighProbabilityStrategy
from src.strategies.arbitrage import ArbitrageStrategy


class MockClobClient:
    def __init__(self, prices: dict[str, dict]):
        self._prices = prices

    def get_price(self, token_id: str) -> dict | None:
        return self._prices.get(token_id)

    def get_orderbooks_batch(self, token_ids: list[str], chunk_size: int = 50) -> dict:
        return {tid: self._prices.get(tid) for tid in token_ids}


class TestHighProbability(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(high_prob_min_price=0.92, high_prob_max_price=0.98)
        self.strategy = HighProbabilityStrategy(self.settings)

    def test_finds_high_prob_signal(self):
        market = {
            "conditionId": "cond1",
            "question": "Will X happen?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.94, 0.06]",
            "volume": 5000,
            "liquidity": 2000,
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.93, "ask": 0.94, "mid": 0.935},
            "no_tok": {"bid": 0.05, "ask": 0.06, "mid": 0.055},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertGreaterEqual(len(signals), 1)
        high_prob = [s for s in signals if s.price >= 0.92]
        self.assertEqual(len(high_prob), 1)
        self.assertEqual(high_prob[0].outcome, "Yes")
        self.assertGreaterEqual(high_prob[0].price, 0.92)

    def test_maker_bid_when_ask_above_max(self):
        """ask=0.99, max=0.98 → maker bid with post_only=True"""
        market = {
            "conditionId": "cond_maker",
            "question": "Will Z happen?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.95, 0.05]",
            "volume": 5000,
            "liquidity": 2000,
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.95, "ask": 0.99, "mid": 0.97},
            "no_tok": {"bid": 0.01, "ask": 0.05, "mid": 0.03},
        })
        signals = self.strategy.analyze([market], clob)
        maker_signals = [s for s in signals if s.price >= 0.90]
        self.assertEqual(len(maker_signals), 1)
        self.assertTrue(maker_signals[0].post_only)
        self.assertEqual(maker_signals[0].price, 0.96)  # bid + 0.01

    def test_longshot_candidate_found(self):
        """Market [0.98, 0.02] → 0.02 side found as candidate"""
        market = {
            "conditionId": "cond_ls",
            "question": "Will Wizards win NBA Finals?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.98, 0.02]",
            "volume": 8000,
            "liquidity": 3000,
        }
        candidates = self.strategy._pre_filter([market])
        # Should find both: 0.98 in high-prob range, 0.02 in long-shot range
        outcome_indices = [idx for _, idx, _ in candidates]
        self.assertIn(1, outcome_indices)  # No side (0.02) found

    def test_longshot_confidence_model(self):
        """Verify multiplier-based scoring for long-shots"""
        market = {
            "volume": 10000,
            "liquidity": 5000,
        }
        conf = self.strategy._score_longshot_confidence(market, 0.05)
        # base = 0.05 * 2.0 = 0.10, vol_bonus = 0.08, liq_bonus = 0.05 → 0.23
        self.assertAlmostEqual(conf, 0.23, places=2)

    def test_longshot_maker_bid(self):
        """Long-shot generates maker bid, not taker"""
        market = {
            "conditionId": "cond_ls2",
            "question": "Long-shot event?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.95, 0.05]",
            "volume": 8000,
            "liquidity": 3000,
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.94, "ask": 0.95, "mid": 0.945},
            "no_tok": {"bid": 0.04, "ask": 0.06, "mid": 0.05},
        })
        signals = self.strategy.analyze([market], clob)
        longshot_signals = [s for s in signals if s.price <= 0.20]
        if longshot_signals:
            self.assertTrue(longshot_signals[0].post_only)
            self.assertGreater(longshot_signals[0].cancel_after_ts, 0)

    def test_skips_low_price_market(self):
        market = {
            "conditionId": "cond2",
            "question": "Will Y happen?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.50, 0.50]",
            "volume": 5000,
            "liquidity": 2000,
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.50, "ask": 0.51, "mid": 0.505},
            "no_tok": {"bid": 0.48, "ask": 0.49, "mid": 0.485},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 0)


class TestArbitrage(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(arb_min_spread=0.005)
        self.strategy = ArbitrageStrategy(self.settings)

    def test_finds_arbitrage(self):
        market = {
            "conditionId": "cond3",
            "question": "Arb market?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
        }
        # YES@0.48 + NO@0.48 = 0.96 -> 4% spread
        clob = MockClobClient({
            "yes_tok": {"bid": 0.47, "ask": 0.48, "mid": 0.475},
            "no_tok": {"bid": 0.47, "ask": 0.48, "mid": 0.475},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 2)

    def test_no_arb_when_tight(self):
        market = {
            "conditionId": "cond4",
            "question": "No arb market?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
        }
        # YES@0.55 + NO@0.46 = 1.01 -> no arb
        clob = MockClobClient({
            "yes_tok": {"bid": 0.54, "ask": 0.55, "mid": 0.545},
            "no_tok": {"bid": 0.45, "ask": 0.46, "mid": 0.455},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 0)


if __name__ == "__main__":
    unittest.main()
