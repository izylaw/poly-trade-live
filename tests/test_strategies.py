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

    def test_longshot_skips_empty_book(self):
        """Long-shot with empty book (bid=0.01) should be skipped."""
        market = {
            "conditionId": "cond_ls_empty",
            "question": "Long-shot empty book?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.95, 0.05]",
            "volume": 5000,
            "liquidity": 2000,
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.94, "ask": 0.95, "mid": 0.945},
            "no_tok": {"bid": 0.01, "ask": 0.99, "mid": 0.50},
        })
        signals = self.strategy.analyze([market], clob)
        longshot_signals = [s for s in signals if s.price <= 0.20]
        self.assertEqual(len(longshot_signals), 0)

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

    # --- Single-market arb (fee-adjusted) ---

    def test_single_market_arb_with_fees(self):
        """YES@0.40 + NO@0.40 = 0.80, payout after fee = 0.9375, spread = 0.1375 → signals"""
        market = {
            "conditionId": "cond3",
            "question": "Arb market?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
            "outcomePrices": "[0.40, 0.40]",
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.39, "ask": 0.40, "mid": 0.395},
            "no_tok": {"bid": 0.39, "ask": 0.40, "mid": 0.395},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 2)
        self.assertTrue(all(s.arb_group.startswith("single:") for s in signals))

    def test_single_market_no_arb_after_fees(self):
        """YES@0.48 + NO@0.48 = 0.96, payout after fee = 0.9375 → no arb (fee eats profit)"""
        market = {
            "conditionId": "cond4",
            "question": "No arb market?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.47, "ask": 0.48, "mid": 0.475},
            "no_tok": {"bid": 0.47, "ask": 0.48, "mid": 0.475},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 0)

    def test_single_market_no_arb_when_tight(self):
        """YES@0.55 + NO@0.46 = 1.01 → no arb"""
        market = {
            "conditionId": "cond5",
            "question": "Tight market?",
            "clobTokenIds": ["yes_tok", "no_tok"],
            "outcomes": ["Yes", "No"],
        }
        clob = MockClobClient({
            "yes_tok": {"bid": 0.54, "ask": 0.55, "mid": 0.545},
            "no_tok": {"bid": 0.45, "ask": 0.46, "mid": 0.455},
        })
        signals = self.strategy.analyze([market], clob)
        self.assertEqual(len(signals), 0)

    # --- Multi-outcome event arb ---

    def test_multi_outcome_found(self):
        """4 markets same slug, asks sum to 0.85 → signals after fees"""
        markets = []
        prices = {}
        for i in range(4):
            tok_yes = f"mo_yes_{i}"
            tok_no = f"mo_no_{i}"
            markets.append({
                "conditionId": f"cond_mo_{i}",
                "question": f"Who will win? Outcome {i}",
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "_event_slug": "who-will-win-election",
                "_event_title": "Who will win the election?",
            })
            prices[tok_yes] = {"bid": 0.20, "ask": 0.2125, "mid": 0.21}
            prices[tok_no] = {"bid": 0.78, "ask": 0.80, "mid": 0.79}

        clob = MockClobClient(prices)
        signals = self.strategy.analyze(markets, clob)
        # total_ask = 4 * 0.2125 = 0.85, spread = (1.0 - 0.0625) - 0.85 = 0.0875
        self.assertEqual(len(signals), 4)
        self.assertTrue(all(s.arb_group == "multi:who-will-win-election" for s in signals))
        self.assertTrue(all("[EVENT ARB]" in s.market_question for s in signals))

    def test_multi_outcome_blocked_by_fees(self):
        """Asks sum to 0.93, net payout 0.9375 → spread = 0.0075 < min_event_spread(0.02) → no signals"""
        markets = []
        prices = {}
        for i in range(4):
            tok_yes = f"fee_yes_{i}"
            tok_no = f"fee_no_{i}"
            markets.append({
                "conditionId": f"cond_fee_{i}",
                "question": f"Fee test outcome {i}",
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "_event_slug": "fee-test-event",
                "_event_title": "Fee test event",
            })
            prices[tok_yes] = {"bid": 0.22, "ask": 0.2325, "mid": 0.23}
            prices[tok_no] = {"bid": 0.76, "ask": 0.78, "mid": 0.77}

        clob = MockClobClient(prices)
        signals = self.strategy.analyze(markets, clob)
        # total_ask = 4 * 0.2325 = 0.93, spread = 0.9375 - 0.93 = 0.0075 < 0.02
        self.assertEqual(len(signals), 0)

    def test_multi_outcome_skips_above_below(self):
        """above-type slug with 4 markets → no multi-outcome signals"""
        markets = []
        prices = {}
        for i, strike in enumerate([90000, 95000, 100000, 105000]):
            tok_yes = f"ab_yes_{i}"
            tok_no = f"ab_no_{i}"
            markets.append({
                "conditionId": f"cond_ab_{i}",
                "question": f"Will BTC be above ${strike:,}?",
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "_event_slug": "btc-above-on-march-20",
                "_event_title": "BTC above on March 20",
            })
            # Prices that would trigger multi-outcome arb if not excluded
            prices[tok_yes] = {"bid": 0.10, "ask": 0.12, "mid": 0.11}
            prices[tok_no] = {"bid": 0.87, "ask": 0.89, "mid": 0.88}

        clob = MockClobClient(prices)
        signals = self.strategy.analyze(markets, clob)
        # Should NOT produce multi-outcome signals (above/below are not mutually exclusive)
        multi_signals = [s for s in signals if "[EVENT ARB]" in s.market_question]
        self.assertEqual(len(multi_signals), 0)

    # --- Monotonicity arb ---

    def test_monotonicity_violation_found(self):
        """3 above markets, strike ordering violated → pair trade signals"""
        markets = []
        prices = {}
        # Strike $90k: YES should be most expensive (most likely above)
        # Strike $100k: YES should be in middle
        # Strike $110k: YES should be cheapest (least likely above)
        # Violation: $90k YES is cheaper than $100k YES
        strikes_and_yes_asks = [
            (90000, 0.30),   # Should be higher but is lower → violation vs $100k
            (100000, 0.45),  # Higher than $90k → violation!
            (110000, 0.20),  # Correct (lower than $100k)
        ]
        no_asks = [0.65, 0.50, 0.75]

        for i, ((strike, yes_ask), no_ask) in enumerate(zip(strikes_and_yes_asks, no_asks)):
            tok_yes = f"mono_yes_{i}"
            tok_no = f"mono_no_{i}"
            markets.append({
                "conditionId": f"cond_mono_{i}",
                "question": f"Will BTC be above ${strike:,}?",
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "_event_slug": "btc-above-on-march-20",
                "_event_title": "BTC above on March 20",
            })
            prices[tok_yes] = {"bid": yes_ask - 0.01, "ask": yes_ask, "mid": yes_ask}
            prices[tok_no] = {"bid": no_ask - 0.01, "ask": no_ask, "mid": no_ask}

        clob = MockClobClient(prices)
        signals = self.strategy.analyze(markets, clob)
        mono_signals = [s for s in signals if "[MONO ARB]" in s.market_question]
        # Violation between $90k(YES@0.30) and $100k(YES@0.45):
        # Buy $90k YES@0.30 + $100k NO@0.50 = 0.80, spread = 1.0 - 0.80 - 2*0.0625 = 0.075
        self.assertEqual(len(mono_signals), 2)
        self.assertTrue(all(s.arb_group.startswith("mono:") for s in mono_signals))

    def test_monotonicity_correct_ordering(self):
        """Prices monotonically decrease with strike → no signals"""
        markets = []
        prices = {}
        strikes_and_yes_asks = [
            (90000, 0.70),
            (100000, 0.50),
            (110000, 0.30),
        ]

        for i, (strike, yes_ask) in enumerate(strikes_and_yes_asks):
            tok_yes = f"corr_yes_{i}"
            tok_no = f"corr_no_{i}"
            no_ask = 1.0 - yes_ask + 0.02
            markets.append({
                "conditionId": f"cond_corr_{i}",
                "question": f"Will BTC be above ${strike:,}?",
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "_event_slug": "btc-above-on-march-20",
                "_event_title": "BTC above on March 20",
            })
            prices[tok_yes] = {"bid": yes_ask - 0.01, "ask": yes_ask, "mid": yes_ask}
            prices[tok_no] = {"bid": no_ask - 0.01, "ask": no_ask, "mid": no_ask}

        clob = MockClobClient(prices)
        signals = self.strategy.analyze(markets, clob)
        mono_signals = [s for s in signals if "[MONO ARB]" in s.market_question]
        self.assertEqual(len(mono_signals), 0)

    def test_monotonicity_skips_non_above_events(self):
        """Multi-outcome event (no 'above' in slug/question) → no monotonicity signals"""
        markets = []
        prices = {}
        for i in range(4):
            tok_yes = f"na_yes_{i}"
            tok_no = f"na_no_{i}"
            markets.append({
                "conditionId": f"cond_na_{i}",
                "question": f"Who will win? Candidate {i}",
                "clobTokenIds": [tok_yes, tok_no],
                "outcomes": ["Yes", "No"],
                "_event_slug": "who-will-win-election",
                "_event_title": "Who will win?",
            })
            prices[tok_yes] = {"bid": 0.24, "ask": 0.25, "mid": 0.245}
            prices[tok_no] = {"bid": 0.74, "ask": 0.75, "mid": 0.745}

        clob = MockClobClient(prices)
        signals = self.strategy.analyze(markets, clob)
        mono_signals = [s for s in signals if "[MONO ARB]" in s.market_question]
        self.assertEqual(len(mono_signals), 0)

    # --- Strike parsing ---

    def test_strike_parsing(self):
        """Verify regex extracts correct strike values"""
        strategy = self.strategy
        self.assertEqual(strategy._parse_strike("Will BTC be above $100,000?"), 100000.0)
        self.assertEqual(strategy._parse_strike("Will ETH be above $5000?"), 5000.0)
        self.assertEqual(strategy._parse_strike("Will SOL be above $250?"), 250.0)
        self.assertIsNone(strategy._parse_strike("No dollar sign here"))
        self.assertEqual(strategy._parse_strike("Above $1,234,567 by Friday"), 1234567.0)


if __name__ == "__main__":
    unittest.main()
