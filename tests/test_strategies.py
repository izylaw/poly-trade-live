import unittest
from unittest.mock import MagicMock
from src.config.settings import Settings
from src.strategies.high_probability import HighProbabilityStrategy
from src.strategies.arbitrage import ArbitrageStrategy


class MockClobClient:
    def __init__(self, prices: dict[str, dict]):
        self._prices = prices

    def get_price(self, token_id: str) -> dict:
        return self._prices.get(token_id, {"bid": 0.5, "ask": 0.5, "mid": 0.5})


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
            "volume": 5000,
            "liquidity": 2000,
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.93, "ask": 0.94, "mid": 0.935},
            "no_tok": {"bid": 0.05, "ask": 0.06, "mid": 0.055},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].outcome, "Yes")
        self.assertGreaterEqual(signals[0].price, 0.92)

    def test_skips_low_price_market(self):
        market = {
            "conditionId": "cond2",
            "question": "Will Y happen?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
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
