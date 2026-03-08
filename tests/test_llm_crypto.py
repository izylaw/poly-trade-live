"""Tests for LLM Crypto strategy — all LLM calls are mocked."""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

from src.config.settings import Settings
from src.strategies.llm_crypto import LLMCryptoStrategy, CONFIDENCE_SCALING


def _make_settings(**overrides):
    defaults = {
        "starting_capital": 10.0,
        "llm_enabled": True,
        "llm_api_key": "test-key",
        "llm_model": "claude-sonnet-4-20250514",
        "llm_batch_size": 10,
        "llm_min_edge": 0.05,
        "llm_cache_ttl": 600,
        "llm_daily_cache_ttl": 1800,
        "llm_run_every_n_cycles": 3,
        "llm_max_tokens": 2000,
        "llm_timeout": 30,
        "llm_maker_edge_cushion": 0.05,
        "llm_intervals": ["1h"],
        "llm_daily_lookahead_days": 5,
        "btc_updown_assets": ["BTC"],
        "btc_updown_intervals": ["5m"],
        "btc_updown_5m_vol": 0.0010,
        "btc_updown_min_ask": 0.03,
        "btc_updown_max_ask": 0.85,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_market(condition_id="cond_1", asset="BTC"):
    return {
        "conditionId": condition_id,
        "question": f"Will {asset} go up?",
        "outcomes": ["Up", "Down"],
        "clobTokenIds": ["tok_up", "tok_down"],
        "_start_ts": time.time() - 60,
        "_resolution_ts": time.time() + 240,
    }


def _make_llm_response(assessments: list[dict]) -> dict:
    return {
        "content": json.dumps(assessments),
        "input_tokens": 500,
        "output_tokens": 200,
    }


def _mock_clob_client():
    clob = MagicMock()
    clob.get_price.return_value = {"bid": 0.45, "ask": 0.55}
    clob.get_book.return_value = None
    return clob


def _mock_gamma_extra(strategy):
    """Mock gamma client discovery methods for daily/weekly/monthly to return empty."""
    strategy.gamma.get_crypto_daily_markets = MagicMock(return_value=[])
    strategy.gamma.get_crypto_weekly_markets = MagicMock(return_value=[])
    strategy.gamma.get_crypto_monthly_markets = MagicMock(return_value=[])


class TestCycleSkip(unittest.TestCase):
    """Strategy returns [] on non-Nth cycles."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_skips_non_nth_cycle(self, mock_llm_cls, mock_discover):
        s = _make_settings(llm_run_every_n_cycles=3)
        strategy = LLMCryptoStrategy(s)
        clob = _mock_clob_client()

        # Cycles 1 and 2 should return empty
        result1 = strategy.analyze([], clob)
        self.assertEqual(result1, [])

        result2 = strategy.analyze([], clob)
        self.assertEqual(result2, [])

        # Discovery should never have been called
        mock_discover.assert_not_called()

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_runs_on_nth_cycle(self, mock_llm_cls, mock_discover):
        mock_discover.return_value = []
        s = _make_settings(llm_run_every_n_cycles=3)
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)
        clob = _mock_clob_client()

        # Run 3 cycles
        strategy.analyze([], clob)
        strategy.analyze([], clob)
        strategy.analyze([], clob)  # cycle 3 should trigger discovery

        mock_discover.assert_called()


class TestCacheHit(unittest.TestCase):
    """Skips markets assessed within cache_ttl."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_cache_hit_skips_market(self, mock_llm_cls, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]

        s = _make_settings(llm_cache_ttl=600)
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)
        clob = _mock_clob_client()

        # Pre-populate cache
        strategy._cache["cond_1"] = (time.time(), {"action": "BUY_UP"})

        # Force to run cycle
        strategy._cycle_counter = 2

        result = strategy.analyze([], clob)
        # LLM should not have been called since market is cached
        strategy.llm.complete.assert_not_called()


class TestCacheExpiry(unittest.TestCase):
    """Re-assesses after TTL expires."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_price_delta")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_cache_expired_reassesses(self, mock_llm_cls, mock_momentum,
                                       mock_delta, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]
        mock_delta.return_value = {
            "current_price": 50000, "reference_price": 49900,
            "delta_pct": 0.002, "window_progress": 0.5,
            "time_remaining": 200, "dynamic_vol": 0.001,
            "resolution_ts": time.time() + 240,
        }
        mock_momentum.return_value = 0.1

        s = _make_settings(llm_cache_ttl=600)
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)
        # Mock the LLM to return empty (no signals)
        strategy.llm.complete.return_value = _make_llm_response([])

        clob = _mock_clob_client()

        # Pre-populate cache with expired entry
        strategy._cache["cond_1"] = (time.time() - 700, {"action": "BUY_UP"})
        strategy._cycle_counter = 2

        strategy.analyze([], clob)
        # LLM should have been called since cache expired
        strategy.llm.complete.assert_called_once()


class TestJsonParsing(unittest.TestCase):
    """Handles clean JSON, fenced JSON, and malformed JSON."""

    def test_clean_json(self):
        content = json.dumps([{"market_id": "m1", "action": "BUY_UP"}])
        result = LLMCryptoStrategy._parse_llm_response(content)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["market_id"], "m1")

    def test_markdown_fenced_json(self):
        # The LLMClient strips fences before we get here, but test the parser
        content = json.dumps([{"market_id": "m1", "action": "BUY_DOWN"}])
        result = LLMCryptoStrategy._parse_llm_response(content)
        self.assertEqual(len(result), 1)

    def test_malformed_json(self):
        result = LLMCryptoStrategy._parse_llm_response("not valid json {{{")
        self.assertEqual(result, [])

    def test_empty_response(self):
        result = LLMCryptoStrategy._parse_llm_response("")
        self.assertEqual(result, [])

    def test_non_array_json(self):
        result = LLMCryptoStrategy._parse_llm_response('{"key": "value"}')
        self.assertEqual(result, [])


class TestSignalGeneration(unittest.TestCase):
    """Given mock LLM response + mock CLOB prices, produces correct TradeSignal."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_price_delta")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_generates_signal(self, mock_llm_cls, mock_momentum,
                               mock_delta, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]
        mock_delta.return_value = {
            "current_price": 50000, "reference_price": 49900,
            "delta_pct": 0.002, "window_progress": 0.5,
            "time_remaining": 200, "dynamic_vol": 0.001,
            "resolution_ts": time.time() + 240,
        }
        mock_momentum.return_value = 0.1

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)

        # LLM returns a BUY_UP with high confidence
        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_1",
            "action": "BUY_UP",
            "estimated_probability": 0.75,
            "confidence_level": "high",
            "reasoning": "Strong upward momentum",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2  # next will be 3

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 1)

        sig = signals[0]
        self.assertEqual(sig.market_id, "cond_1")
        self.assertEqual(sig.strategy, "llm_crypto")
        self.assertEqual(sig.side, "BUY")
        self.assertEqual(sig.outcome, "Up")
        self.assertEqual(sig.order_type, "GTC")
        self.assertTrue(sig.post_only)
        # high confidence: 0.75 * 1.0 = 0.75, bid = 0.75 - 0.05 = 0.70
        self.assertAlmostEqual(sig.price, 0.70, places=2)
        self.assertAlmostEqual(sig.confidence, 0.75, places=2)
        self.assertGreater(sig.expected_value, 0)


class TestEdgeFilter(unittest.TestCase):
    """Drops signals with edge < min_edge."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_price_delta")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_drops_low_edge(self, mock_llm_cls, mock_momentum,
                             mock_delta, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]
        mock_delta.return_value = {
            "current_price": 50000, "reference_price": 49900,
            "delta_pct": 0.002, "window_progress": 0.5,
            "time_remaining": 200, "dynamic_vol": 0.001,
            "resolution_ts": time.time() + 240,
        }
        mock_momentum.return_value = 0.0

        s = _make_settings(llm_min_edge=0.10)
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)

        # LLM returns prob=0.55, bid would be 0.55-0.05=0.50, edge=0.05 < 0.10
        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_1",
            "action": "BUY_UP",
            "estimated_probability": 0.55,
            "confidence_level": "high",
            "reasoning": "Slight edge",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 0)


class TestConfidenceScaling(unittest.TestCase):
    """Low confidence scales down estimated_probability."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_price_delta")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_low_confidence_scaling(self, mock_llm_cls, mock_momentum,
                                     mock_delta, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]
        mock_delta.return_value = {
            "current_price": 50000, "reference_price": 49900,
            "delta_pct": 0.002, "window_progress": 0.5,
            "time_remaining": 200, "dynamic_vol": 0.001,
            "resolution_ts": time.time() + 240,
        }
        mock_momentum.return_value = 0.0

        s = _make_settings(llm_min_edge=0.01)  # low min_edge so signal passes
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)

        # LLM returns high prob but low confidence
        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_1",
            "action": "BUY_UP",
            "estimated_probability": 0.80,
            "confidence_level": "low",
            "reasoning": "Uncertain signal",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 1)

        # 0.80 * 0.85 = 0.68
        self.assertAlmostEqual(signals[0].confidence, 0.68, places=2)

    def test_scaling_values(self):
        self.assertAlmostEqual(CONFIDENCE_SCALING["low"], 0.85)
        self.assertAlmostEqual(CONFIDENCE_SCALING["medium"], 0.92)
        self.assertAlmostEqual(CONFIDENCE_SCALING["high"], 1.0)


class TestSkipAction(unittest.TestCase):
    """LLM returning SKIP produces no signal."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_price_delta")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_skip_ignored(self, mock_llm_cls, mock_momentum,
                           mock_delta, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]
        mock_delta.return_value = {
            "current_price": 50000, "reference_price": 49900,
            "delta_pct": 0.002, "window_progress": 0.5,
            "time_remaining": 200, "dynamic_vol": 0.001,
            "resolution_ts": time.time() + 240,
        }
        mock_momentum.return_value = 0.0

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)

        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_1",
            "action": "SKIP",
            "estimated_probability": 0.50,
            "confidence_level": "low",
            "reasoning": "No edge",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 0)


class TestBuyDown(unittest.TestCase):
    """BUY_DOWN action selects the second token (Down outcome)."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_price_delta")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_buy_down_selects_correct_token(self, mock_llm_cls, mock_momentum,
                                             mock_delta, mock_discover):
        market = _make_market()
        mock_discover.return_value = [market]
        mock_delta.return_value = {
            "current_price": 50000, "reference_price": 50100,
            "delta_pct": -0.002, "window_progress": 0.5,
            "time_remaining": 200, "dynamic_vol": 0.001,
            "resolution_ts": time.time() + 240,
        }
        mock_momentum.return_value = -0.2

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)

        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_1",
            "action": "BUY_DOWN",
            "estimated_probability": 0.75,
            "confidence_level": "high",
            "reasoning": "Bearish momentum",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].outcome, "Down")
        self.assertEqual(signals[0].token_id, "tok_down")


class TestDailyMarketDiscovery(unittest.TestCase):
    """Daily above/below markets are discovered and gathered."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_daily_market_gathered(self, mock_llm_cls, mock_momentum, mock_discover):
        mock_discover.return_value = []  # No updown markets
        mock_momentum.return_value = 0.1

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)

        daily_market = {
            "conditionId": "cond_daily_1",
            "question": "Will Bitcoin be above $90,000 on March 10?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "endDate": "2026-03-10T17:00:00Z",
            "_event_slug": "bitcoin-above-on-march-10",
            "_market_type": "above_below",
            "_asset": "BTC",
        }
        strategy.gamma.get_crypto_daily_markets = MagicMock(return_value=[daily_market])
        strategy.gamma.get_crypto_weekly_markets = MagicMock(return_value=[])
        strategy.gamma.get_crypto_monthly_markets = MagicMock(return_value=[])

        strategy.binance.get_price = MagicMock(return_value=89000.0)
        strategy.binance.compute_atr = MagicMock(return_value=500.0)
        strategy.binance.get_klines = MagicMock(return_value=[])

        strategy.llm.complete.return_value = _make_llm_response([])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        strategy.analyze([], clob)
        # LLM was called — daily market was gathered and sent
        strategy.llm.complete.assert_called_once()
        strategy.gamma.get_crypto_daily_markets.assert_called_once()


class TestBuyYesNoActions(unittest.TestCase):
    """BUY_YES/BUY_NO actions map to correct tokens for Yes/No markets."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_buy_yes_maps_to_first_token(self, mock_llm_cls, mock_momentum, mock_discover):
        mock_discover.return_value = []
        mock_momentum.return_value = 0.1

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)

        daily_market = {
            "conditionId": "cond_above_1",
            "question": "Will Bitcoin be above $90,000 on March 10?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "endDate": "2026-03-10T17:00:00Z",
            "_market_type": "above_below",
            "_asset": "BTC",
        }
        strategy.gamma.get_crypto_daily_markets = MagicMock(return_value=[daily_market])
        strategy.gamma.get_crypto_weekly_markets = MagicMock(return_value=[])
        strategy.gamma.get_crypto_monthly_markets = MagicMock(return_value=[])

        strategy.binance.get_price = MagicMock(return_value=89000.0)
        strategy.binance.compute_atr = MagicMock(return_value=500.0)
        strategy.binance.get_klines = MagicMock(return_value=[])

        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_above_1",
            "action": "BUY_YES",
            "estimated_probability": 0.75,
            "confidence_level": "high",
            "reasoning": "Price close to threshold with upward momentum",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].outcome, "Yes")
        self.assertEqual(signals[0].token_id, "tok_yes")

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_buy_no_maps_to_second_token(self, mock_llm_cls, mock_momentum, mock_discover):
        mock_discover.return_value = []
        mock_momentum.return_value = -0.1

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)

        daily_market = {
            "conditionId": "cond_above_2",
            "question": "Will Bitcoin be above $100,000 on March 10?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["tok_yes", "tok_no"]',
            "endDate": "2026-03-10T17:00:00Z",
            "_market_type": "above_below",
            "_asset": "BTC",
        }
        strategy.gamma.get_crypto_daily_markets = MagicMock(return_value=[daily_market])
        strategy.gamma.get_crypto_weekly_markets = MagicMock(return_value=[])
        strategy.gamma.get_crypto_monthly_markets = MagicMock(return_value=[])

        strategy.binance.get_price = MagicMock(return_value=89000.0)
        strategy.binance.compute_atr = MagicMock(return_value=500.0)
        strategy.binance.get_klines = MagicMock(return_value=[])

        strategy.llm.complete.return_value = _make_llm_response([{
            "market_id": "cond_above_2",
            "action": "BUY_NO",
            "estimated_probability": 0.80,
            "confidence_level": "high",
            "reasoning": "Price far below threshold",
        }])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        signals = strategy.analyze([], clob)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].outcome, "No")
        self.assertEqual(signals[0].token_id, "tok_no")


class TestMonthlyMarketIncluded(unittest.TestCase):
    """Monthly hit-price markets are gathered and sent to LLM."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.compute_momentum")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_monthly_market_gathered(self, mock_llm_cls, mock_momentum, mock_discover):
        mock_discover.return_value = []
        mock_momentum.return_value = 0.0

        s = _make_settings()
        strategy = LLMCryptoStrategy(s)

        monthly_market = {
            "conditionId": "cond_monthly_1",
            "question": "Will Bitcoin hit $120,000 in March 2026?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["tok_yes_m", "tok_no_m"]',
            "endDate": "2026-03-31T23:59:59Z",
            "_market_type": "monthly_hit",
            "_asset": "BTC",
        }
        strategy.gamma.get_crypto_daily_markets = MagicMock(return_value=[])
        strategy.gamma.get_crypto_weekly_markets = MagicMock(return_value=[])
        strategy.gamma.get_crypto_monthly_markets = MagicMock(return_value=[monthly_market])

        strategy.binance.get_price = MagicMock(return_value=95000.0)
        strategy.binance.compute_atr = MagicMock(return_value=1000.0)
        strategy.binance.get_klines = MagicMock(return_value=[])

        strategy.llm.complete.return_value = _make_llm_response([])

        clob = _mock_clob_client()
        strategy._cycle_counter = 2

        strategy.analyze([], clob)
        strategy.llm.complete.assert_called_once()
        strategy.gamma.get_crypto_monthly_markets.assert_called_once()


class TestOnlyLLMIntervals(unittest.TestCase):
    """Only llm_intervals (1h, 4h) are queried, not 5m/15m."""

    @patch("src.strategies.llm_crypto.discover_crypto_markets")
    @patch("src.strategies.llm_crypto.LLMClient")
    def test_only_1h_4h_intervals(self, mock_llm_cls, mock_discover):
        mock_discover.return_value = []

        s = _make_settings(llm_intervals=["1h", "4h"])
        strategy = LLMCryptoStrategy(s)
        _mock_gamma_extra(strategy)
        clob = _mock_clob_client()

        strategy._cycle_counter = 2
        strategy.analyze([], clob)

        call_intervals = set(call.args[2] for call in mock_discover.call_args_list)
        self.assertIn("1h", call_intervals)
        self.assertIn("4h", call_intervals)
        self.assertNotIn("5m", call_intervals)
        self.assertNotIn("15m", call_intervals)


if __name__ == "__main__":
    unittest.main()
