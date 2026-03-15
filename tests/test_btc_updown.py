import json
import math
import sqlite3
import time
import random
import pytest
from unittest.mock import patch, MagicMock
from src.strategies.btc_updown import BtcUpdownStrategy, INTERVAL_WINDOWS, _clamp
from src.config.settings import Settings
from src.risk.risk_manager import RiskManager, TradeSignal
from src.risk.circuit_breaker import CircuitBreaker


def make_settings(**overrides):
    defaults = {
        "btc_updown_assets": ["BTC"],
        "btc_updown_intervals": ["5m"],
        "btc_updown_min_edge": 0.05,
        "btc_updown_5m_vol": 0.0010,
        "btc_updown_logistic_k": 1.5,
        "btc_updown_momentum_weight": 0.3,
        "btc_updown_min_ask": 0.03,
        "btc_updown_max_ask": 0.85,
        "btc_updown_taker_fee_rate": 0.0625,
        "btc_updown_maker_edge_cushion": 0.05,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_klines(closes, volumes=None, base_time_ms=1000000):
    candles = []
    for i, c in enumerate(closes):
        vol = volumes[i] if volumes else 100.0
        candles.append({
            "open_time": base_time_ms + i * 60000,
            "open": c - 0.5,
            "high": c + 1.0,
            "low": c - 1.0,
            "close": c,
            "volume": vol,
            "close_time": base_time_ms + (i + 1) * 60000 - 1,
        })
    return candles


def make_strategy(**settings_overrides):
    """Create a strategy with mocked Binance/Gamma clients."""
    with patch("src.strategies.btc_updown.BinanceClient"), \
         patch("src.strategies.btc_updown.GammaClient"):
        settings = make_settings(**settings_overrides)
        return BtcUpdownStrategy(settings)


def make_mock_clob(**overrides):
    """Create a mock CLOB client with get_book returning None by default."""
    mock = MagicMock()
    mock.get_price.return_value = overrides.get("price", {"ask": 0.99, "bid": 0.01, "mid": 0.50})
    mock.get_book.return_value = overrides.get("book", None)
    return mock


# --- Probability model tests ---

class TestEstimateProbability:
    def test_no_delta_returns_base(self):
        strategy = make_strategy()
        prob = strategy._estimate_outcome_probability("Up", 0.0, 0.0, 0.0)
        assert prob == pytest.approx(0.50, abs=0.01)

    def test_no_delta_midwindow_no_momentum(self):
        strategy = make_strategy()
        prob = strategy._estimate_outcome_probability("Up", 0.0, 0.5, 0.0)
        assert prob == pytest.approx(0.50, abs=0.01)

    def test_late_window_strong_up(self):
        strategy = make_strategy()
        # +0.15% delta at 80% through window
        prob = strategy._estimate_outcome_probability("Up", 0.0015, 0.8, 0.0)
        assert prob > 0.80

    def test_late_window_strong_down(self):
        strategy = make_strategy()
        # -0.15% delta at 80% through window, evaluating "Down"
        prob = strategy._estimate_outcome_probability("Down", -0.0015, 0.8, 0.0)
        assert prob > 0.80

    def test_up_prob_and_down_prob_complement(self):
        strategy = make_strategy()
        delta = 0.0010
        progress = 0.6
        prob_up = strategy._estimate_outcome_probability("Up", delta, progress, 0.0)
        prob_down = strategy._estimate_outcome_probability("Down", delta, progress, 0.0)
        assert prob_up + prob_down == pytest.approx(1.0, abs=0.01)

    def test_momentum_boosts_probability(self):
        strategy = make_strategy()
        prob_no_mom = strategy._estimate_outcome_probability("Up", 0.0005, 0.5, 0.0)
        prob_with_mom = strategy._estimate_outcome_probability("Up", 0.0005, 0.5, 0.5)
        assert prob_with_mom > prob_no_mom

    def test_clamped_to_bounds(self):
        strategy = make_strategy()
        # Extreme delta should be clamped to [0.05, 0.95]
        prob = strategy._estimate_outcome_probability("Up", 0.01, 1.0, 1.0)
        assert prob <= 0.95
        prob_low = strategy._estimate_outcome_probability("Up", -0.01, 1.0, -1.0)
        assert prob_low >= 0.05


# --- Both-sides evaluation tests (now maker) ---

class TestEvaluateBothSides:
    def test_maker_bid_from_probability(self):
        """Bid price should be est_prob - maker_edge_cushion, not the ask."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        # Strong downward delta → Down est_prob ~ 0.88
        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }
        momentum = -0.3

        signal, predictions = strategy._evaluate_both_sides(market, "BTC", delta_info, momentum, mock_clob)
        assert signal is not None
        assert signal.outcome == "Down"
        # bid = est_prob - 0.05, should NOT be 0.99
        assert signal.price < 0.90
        assert signal.price == pytest.approx(signal.confidence - 0.05, abs=0.01)

    def test_no_edge_returns_none(self):
        strategy = make_strategy()
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob(price={"ask": 0.50, "bid": 0.48, "mid": 0.49})

        delta_info = {
            "delta_pct": 0.0, "window_progress": 0.0,
            "time_remaining": 270, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 270,
        }
        momentum = 0.0

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, momentum, mock_clob)
        assert signal is None

    def test_min_edge_filter(self):
        strategy = make_strategy(btc_updown_min_edge=0.15)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob(price={"ask": 0.48, "bid": 0.46, "mid": 0.47})

        delta_info = {
            "delta_pct": 0.0002, "window_progress": 0.2,
            "time_remaining": 240, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 240,
        }
        momentum = 0.0

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, momentum, mock_clob)
        assert signal is None


# --- Price delta computation tests ---

class TestPriceDelta:
    def test_computes_delta_from_klines(self):
        strategy = make_strategy()
        now = time.time()
        start_ts = now - 180  # 3 min ago
        start_ts_ms = start_ts * 1000

        # Create klines where one covers start_ts
        klines = [{
            "open_time": int(start_ts_ms) - 30000,
            "open": 68000.0,
            "high": 68100.0,
            "low": 67900.0,
            "close": 68050.0,
            "volume": 100.0,
            "close_time": int(start_ts_ms) + 30000,
        }]

        current_price = 68150.0
        atr = 68.0
        atr_vol = atr / current_price

        market = {"_start_ts": start_ts, "_resolution_ts": start_ts + 300}
        result = strategy._compute_delta_from_prefetched(current_price, klines, atr_vol, market)

        assert result["reference_price"] == 68000.0
        assert result["current_price"] == 68150.0
        expected_delta = (68150.0 - 68000.0) / 68000.0
        assert result["delta_pct"] == pytest.approx(expected_delta, rel=0.01)
        assert 0.0 < result["window_progress"] < 1.0
        assert "dynamic_vol" in result
        assert result["dynamic_vol"] == pytest.approx(68.0 / 68150.0, rel=0.01)
        assert "resolution_ts" in result


# --- Market discovery tests ---

class TestMarketDiscovery:
    def test_discover_markets_timing_window(self):
        strategy = make_strategy()
        now = time.time()

        # Market started 60s ago, resolves in 240s (within 30-330 window)
        good_start = now - 60
        # Market started 400s ago, resolves in -100s (already resolved)
        bad_start = now - 400

        strategy.gamma.get_crypto_updown_markets = MagicMock(return_value=[
            {
                "_start_ts": good_start,
                "clobTokenIds": json.dumps(["token_up", "token_down"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "conditionId": "cond1",
            },
            {
                "_start_ts": bad_start,
                "clobTokenIds": json.dumps(["token_up2", "token_down2"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "conditionId": "cond2",
            },
        ])

        result = strategy._discover_markets("BTC", "5m")
        assert len(result) == 1
        assert result[0]["conditionId"] == "cond1"

    def test_discover_markets_parses_json_fields(self):
        strategy = make_strategy()
        now = time.time()
        start_ts = now - 60

        strategy.gamma.get_crypto_updown_markets = MagicMock(return_value=[
            {
                "_start_ts": start_ts,
                "clobTokenIds": '["token_a", "token_b"]',
                "outcomes": '["Up", "Down"]',
                "conditionId": "cond1",
            },
        ])

        result = strategy._discover_markets("BTC", "5m")
        assert len(result) == 1
        assert result[0]["clobTokenIds"] == ["token_a", "token_b"]
        assert result[0]["outcomes"] == ["Up", "Down"]


# --- Risk manager strategy-specific confidence tests ---

class TestRiskManagerBtcUpdown:
    def test_accepts_btc_updown_at_lower_confidence(self):
        settings = make_settings(
            starting_capital=10.0,
            hard_floor_pct=0.20,
            max_open_positions=5,
            max_portfolio_exposure_pct=0.60,
            min_trade_size=0.50,
            max_single_trade_pct=0.10,
        )
        cb = CircuitBreaker(settings)
        rm = RiskManager(settings, cb)
        rm.min_confidence = 0.90  # emergency level

        signal = TradeSignal(
            market_id="cond1",
            token_id="token_up",
            market_question="Will BTC go up?",
            side="BUY",
            outcome="Up",
            price=0.40,
            confidence=0.60,
            strategy="btc_updown",
            expected_value=0.05,
            order_type="GTC",
        )

        result = rm.evaluate(signal, balance=9.89, open_positions=[], portfolio_exposure=0.0)
        # Should pass because btc_updown override is 0.55, not 0.90
        assert result is not None

    def test_rejects_other_strategy_at_low_confidence(self):
        settings = make_settings(
            starting_capital=10.0,
            hard_floor_pct=0.20,
            max_open_positions=5,
            max_portfolio_exposure_pct=0.60,
            min_trade_size=0.50,
            max_single_trade_pct=0.10,
        )
        cb = CircuitBreaker(settings)
        rm = RiskManager(settings, cb)
        rm.min_confidence = 0.90

        signal = TradeSignal(
            market_id="cond1",
            token_id="token1",
            market_question="Some market",
            side="BUY",
            outcome="Yes",
            price=0.40,
            confidence=0.60,
            strategy="some_other_strategy",
            expected_value=0.05,
            order_type="GTC",
        )

        result = rm.evaluate(signal, balance=9.89, open_positions=[], portfolio_exposure=0.0)
        assert result is None


# --- Helper function tests ---

class TestHelpers:
    def test_clamp(self):
        assert _clamp(0.5, -1, 1) == 0.5
        assert _clamp(1.5, -1, 1) == 1.0
        assert _clamp(-1.5, -1, 1) == -1.0


# --- Maker order tests ---

class TestMakerBidPrice:
    def test_maker_bid_price_from_probability(self):
        """bid = est_prob - cushion, not ask."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        # Strong down delta: est_prob for Down ~0.88
        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        assert signal is not None
        # Bid is derived from model, not from CLOB ask
        assert signal.price == round(signal.confidence - 0.05, 2)

    def test_maker_no_fee_in_ev(self):
        """EV has no taker fee deducted for maker orders."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        assert signal is not None

        # Verify EV matches zero-fee formula
        bid = signal.price
        prob = signal.confidence
        expected_ev = prob * (1.0 - bid) - (1.0 - prob) * bid
        assert signal.expected_value == pytest.approx(expected_ev, abs=0.001)

    def test_maker_bid_out_of_range_skipped(self):
        """bid < min_ask or > max_ask should be skipped."""
        # With cushion=0.05 and est_prob~0.50, bid~0.45 is fine
        # But with min_ask=0.50, bid=0.45 should be skipped
        strategy = make_strategy(
            btc_updown_maker_edge_cushion=0.05,
            btc_updown_min_ask=0.50,
        )
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        # No delta → est_prob ~0.50, bid ~0.45 < min_ask 0.50
        delta_info = {
            "delta_pct": 0.0, "window_progress": 0.0,
            "time_remaining": 270, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 270,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, 0.0, mock_clob)
        assert signal is None

    def test_maker_signal_has_gtc_and_post_only(self):
        """Signal should have order_type='GTC' and post_only=True."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        assert signal is not None
        assert signal.order_type == "GTC"
        assert signal.post_only is True

    def test_maker_cancel_after_ts_set(self):
        """cancel_after_ts should be resolution_ts - 30."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        resolution_ts = time.time() + 120
        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 120, "dynamic_vol": 0.0010,
            "resolution_ts": resolution_ts,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        assert signal is not None
        assert signal.cancel_after_ts == pytest.approx(resolution_ts - 30, abs=0.1)


# --- Paper executor maker tests ---

class TestPaperMakerOrders:
    def test_paper_maker_returns_pending(self):
        """Paper execute() returns status='pending' for maker orders."""
        from src.execution.paper_executor import PaperExecutor
        from src.risk.risk_manager import ApprovedTrade

        trade_log = MagicMock()
        trade_log.log_trade.return_value = 1
        trade_log.compute_paper_balance.return_value = 10.0

        executor = PaperExecutor(starting_balance=10.0, trade_log=trade_log)

        signal = TradeSignal(
            market_id="cond1", token_id="token_down",
            market_question="Will BTC go up?",
            side="BUY", outcome="Down",
            price=0.55, confidence=0.60,
            strategy="btc_updown", expected_value=0.05,
            order_type="GTC", post_only=True,
            cancel_after_ts=time.time() + 90,
        )
        trade = ApprovedTrade(signal=signal, size=1.50, cost=0.83, kelly_fraction=0.10)

        result = executor.execute(trade)
        assert result["status"] == "pending"
        assert result["trade_id"] == 1
        assert result["fill_price"] == 0.55
        assert result["size"] == 1.50
        # Balance not deducted yet (derived from DB)
        assert executor.get_balance() == 10.0

    def test_paper_fill_order_creates_position(self):
        """fill_order() creates position and updates trade status."""
        from src.execution.paper_executor import PaperExecutor

        trade_log = MagicMock()
        trade_log.save_position.return_value = 42
        trade_log.compute_paper_balance.return_value = 10.0 - 1.50 * 0.55

        executor = PaperExecutor(starting_balance=10.0, trade_log=trade_log)

        order = {
            "trade_id": 1,
            "fill_price": 0.55,
            "token_id": "token_down",
            "size": 1.50,
            "market_id": "cond1",
            "outcome": "Down",
            "market_question": "Will BTC go up?",
        }

        result = executor.fill_order(order)
        assert result["status"] == "filled"
        assert result["position_id"] == 42
        trade_log.save_position.assert_called_once()
        trade_log.update_trade_status.assert_called_once_with(1, "filled")


# --- Order manager tests ---

class TestOrderManager:
    def test_order_manager_probabilistic_fill(self):
        """Mock random to verify fill triggers at right probability."""
        from src.core.order_manager import OrderManager, PAPER_BASE_FILL_RATE

        trade_log = MagicMock()
        om = OrderManager(trade_log)

        order = {
            "trade_id": 1, "fill_price": 0.55, "token_id": "t1",
            "size": 1.5, "market_id": "c1", "outcome": "Down",
            "market_question": "Q?", "confidence": 0.60,
            "cancel_after_ts": time.time() + 1000,
        }
        om.track_order(order)

        executor = MagicMock()
        executor.paper = MagicMock()

        # fill_prob = 0.35 * (1 + 0.60) = 0.56
        # random returns 0.20 < 0.56 → should fill
        with patch("src.core.order_manager.random.random", return_value=0.20):
            filled = om.check_pending_orders(None, executor, paper_mode=True)

        assert len(filled) == 1
        executor.paper.fill_order.assert_called_once_with(order)
        assert len(om.get_pending_orders()) == 0

    def test_order_manager_probabilistic_no_fill(self):
        """When random is above threshold, order stays pending."""
        from src.core.order_manager import OrderManager

        trade_log = MagicMock()
        om = OrderManager(trade_log)

        order = {
            "trade_id": 1, "fill_price": 0.55, "token_id": "t1",
            "size": 1.5, "market_id": "c1", "outcome": "Down",
            "market_question": "Q?", "confidence": 0.60,
            "cancel_after_ts": time.time() + 1000,
        }
        om.track_order(order)

        executor = MagicMock()
        executor.paper = MagicMock()

        # fill_prob = 0.35 * (1 + 0.60) = 0.56
        # random returns 0.80 → should NOT fill
        with patch("src.core.order_manager.random.random", return_value=0.80):
            filled = om.check_pending_orders(None, executor, paper_mode=True)

        assert len(filled) == 0
        executor.paper.fill_order.assert_not_called()
        assert len(om.get_pending_orders()) == 1

    def test_order_manager_auto_cancel(self):
        """Pending order cancelled after deadline."""
        from src.core.order_manager import OrderManager

        trade_log = MagicMock()
        om = OrderManager(trade_log)

        # Deadline already passed
        order = {
            "trade_id": 1, "fill_price": 0.55, "token_id": "t1",
            "size": 1.5, "market_id": "c1", "outcome": "Down",
            "cancel_after_ts": time.time() - 10,  # already expired
        }
        om.track_order(order)

        executor = MagicMock()
        filled = om.check_pending_orders(None, executor, paper_mode=True)

        assert len(filled) == 0
        assert len(om.get_pending_orders()) == 0
        trade_log.update_trade_status.assert_called_once_with(1, "cancelled")


# --- Dynamic vol tests ---

class TestDynamicVol:
    def test_dynamic_vol_used_in_probability(self):
        strategy = make_strategy()
        # Higher vol → same delta produces lower confidence
        prob_low_vol = strategy._estimate_outcome_probability("Up", 0.0010, 0.8, 0.0, dynamic_vol=0.0010)
        prob_high_vol = strategy._estimate_outcome_probability("Up", 0.0010, 0.8, 0.0, dynamic_vol=0.0020)
        assert prob_low_vol > prob_high_vol

    def test_dynamic_vol_fallback(self):
        strategy = make_strategy()
        # None → falls back to self.btc_5m_vol
        prob_default = strategy._estimate_outcome_probability("Up", 0.0010, 0.8, 0.0)
        prob_explicit = strategy._estimate_outcome_probability("Up", 0.0010, 0.8, 0.0, dynamic_vol=0.0010)
        assert prob_default == pytest.approx(prob_explicit, abs=0.001)

    def test_high_vol_widens_min_edge(self):
        strategy = make_strategy(btc_updown_min_edge=0.05, btc_updown_max_ask=0.90)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        # Moderate delta for Down — would pass at normal vol
        delta_info_normal = {
            "delta_pct": -0.0012, "window_progress": 0.7,
            "time_remaining": 90, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 90,
        }
        signal_normal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info_normal, -0.2, mock_clob)

        # Same delta but 3x vol → effective_min_edge = 0.15, much harder to pass
        delta_info_high_vol = {
            "delta_pct": -0.0012, "window_progress": 0.7,
            "time_remaining": 90, "dynamic_vol": 0.0030,
            "resolution_ts": time.time() + 90,
        }
        signal_high_vol, _ = strategy._evaluate_both_sides(market, "BTC", delta_info_high_vol, -0.2, mock_clob)

        # At normal vol it should trade; at 3x vol the probability is lower AND
        # the min_edge is higher, so it may be rejected
        assert signal_normal is not None
        if signal_high_vol is not None:
            assert signal_high_vol.expected_value < signal_normal.expected_value

    def test_compute_atr_basic(self):
        from src.market_data.binance_client import BinanceClient
        client = BinanceClient()

        klines = [
            {"open_time": 0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 10, "close_time": 1},
            {"open_time": 1, "open": 102.0, "high": 108.0, "low": 99.0, "close": 104.0, "volume": 10, "close_time": 2},
            {"open_time": 2, "open": 104.0, "high": 107.0, "low": 101.0, "close": 103.0, "volume": 10, "close_time": 3},
        ]
        client.get_klines = MagicMock(return_value=klines)
        atr = client.compute_atr("BTC", "5m", 2)

        # TR for kline[1]: max(108-99, |108-102|, |99-102|) = max(9, 6, 3) = 9
        # TR for kline[2]: max(107-101, |107-104|, |101-104|) = max(6, 3, 3) = 6
        # ATR = (9 + 6) / 2 = 7.5
        assert atr == pytest.approx(7.5, abs=0.01)

    def test_compute_atr_insufficient_data(self):
        from src.market_data.binance_client import BinanceClient
        client = BinanceClient()
        client.get_klines = MagicMock(return_value=[
            {"open_time": 0, "open": 100.0, "high": 105.0, "low": 95.0, "close": 102.0, "volume": 10, "close_time": 1},
        ])
        assert client.compute_atr("BTC", "5m", 14) is None


class TestIntegration:
    def test_full_analyze_produces_signal(self):
        strategy = make_strategy(btc_updown_min_edge=0.03, btc_updown_max_ask=0.90)
        now = time.time()
        start_ts = now - 150  # started 2.5 min ago (progress ~0.5)
        start_ts_ms = start_ts * 1000

        # Klines covering start_ts, reference open = 68000
        klines = [{
            "open_time": int(start_ts_ms) - 30000,
            "open": 68000.0,
            "high": 68100.0,
            "low": 67900.0,
            "close": 68050.0,
            "volume": 100.0,
            "close_time": int(start_ts_ms) + 30000,
        }]

        # Moderate delta: +0.07% (not extreme)
        strategy.binance.get_price = MagicMock(return_value=68050.0)
        strategy.binance.get_klines = MagicMock(return_value=klines)
        strategy.binance.compute_atr = MagicMock(return_value=68.0)  # ATR ~$68 → vol ~0.001
        strategy.binance.get_recent_trades = MagicMock(return_value=[
            {"qty": "0.7", "isBuyerMaker": False},
            {"qty": "0.3", "isBuyerMaker": True},
        ])
        strategy.binance.get_orderbook = MagicMock(return_value={
            "bids": [["68040.0", "10.0"]],
            "asks": [["68060.0", "3.0"]],
        })

        strategy.gamma.get_crypto_updown_markets = MagicMock(return_value=[
            {
                "_start_ts": start_ts,
                "clobTokenIds": json.dumps(["token_up", "token_down"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "conditionId": "cond1",
                "question": "Will BTC go up in the next 5 minutes?",
            },
        ])

        mock_clob = make_mock_clob()

        signals = strategy.analyze([], mock_clob)
        assert len(signals) >= 1

        sig = signals[0]
        assert sig.strategy == "btc_updown"
        assert sig.order_type == "GTC"
        assert sig.post_only is True
        assert sig.expected_value > 0
        # Price should be our bid, not 0.99
        assert sig.price < 0.90


# --- Improvement 1: Multi-asset & Multi-interval tests ---

class TestMultiAssetInterval:
    def test_interval_windows_15m_defined(self):
        """INTERVAL_WINDOWS has 15m entry with correct bounds."""
        assert "15m" in INTERVAL_WINDOWS
        min_time, max_time = INTERVAL_WINDOWS["15m"]
        assert min_time == 60
        assert max_time == 870

    def test_analyze_multi_asset(self):
        """Strategy iterates ETH, SOL alongside BTC."""
        strategy = make_strategy(
            btc_updown_assets=["BTC", "ETH", "SOL"],
            btc_updown_intervals=["5m"],
            btc_updown_min_edge=0.03,
            btc_updown_max_ask=0.90,
        )
        now = time.time()
        start_ts = now - 150

        start_ts_ms = start_ts * 1000
        klines = [{
            "open_time": int(start_ts_ms) - 30000,
            "open": 68000.0, "high": 68100.0, "low": 67900.0,
            "close": 68050.0, "volume": 100.0,
            "close_time": int(start_ts_ms) + 30000,
        }]

        strategy.binance.get_price = MagicMock(return_value=68050.0)
        strategy.binance.get_klines = MagicMock(return_value=klines)
        strategy.binance.compute_atr = MagicMock(return_value=68.0)
        strategy.binance.get_recent_trades = MagicMock(return_value=[
            {"qty": "0.7", "isBuyerMaker": False},
            {"qty": "0.3", "isBuyerMaker": True},
        ])
        strategy.binance.get_orderbook = MagicMock(return_value={
            "bids": [["68040.0", "10.0"]],
            "asks": [["68060.0", "3.0"]],
        })

        strategy.gamma.get_crypto_updown_markets = MagicMock(return_value=[
            {
                "_start_ts": start_ts,
                "clobTokenIds": json.dumps(["token_up", "token_down"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "conditionId": "cond1",
                "question": "Will crypto go up?",
            },
        ])

        mock_clob = make_mock_clob()
        strategy.analyze([], mock_clob)

        # Gamma should be called for each asset
        calls = strategy.gamma.get_crypto_updown_markets.call_args_list
        assets_called = [c.args[0] for c in calls]
        assert "BTC" in assets_called
        assert "ETH" in assets_called
        assert "SOL" in assets_called


# --- Improvement 2: Prediction tracking tests ---

class TestPredictionTracking:
    def test_prediction_logged_for_each_candidate(self):
        """Every evaluated outcome gets a prediction entry."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, predictions = strategy._evaluate_both_sides(
            market, "BTC", delta_info, -0.3, mock_clob, interval="5m",
        )
        # Should have a prediction for both Up and Down
        assert len(predictions) == 2
        outcomes_logged = {p["outcome"] for p in predictions}
        assert outcomes_logged == {"Up", "Down"}
        # Each prediction has required fields
        for pred in predictions:
            assert "est_prob" in pred
            assert "asset" in pred
            assert "interval" in pred
            assert "market_id" in pred
            assert pred["strategy"] == "btc_updown"

    def test_prediction_traded_flag(self):
        """Prediction that passes edge filter has traded=True."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, predictions = strategy._evaluate_both_sides(
            market, "BTC", delta_info, -0.3, mock_clob, interval="5m",
        )
        assert signal is not None
        # The winning side should be marked as traded
        traded = [p for p in predictions if p["traded"]]
        assert len(traded) >= 1
        assert traded[0]["outcome"] == signal.outcome

    def test_predictions_collected_in_analyze(self):
        """analyze() populates _pending_predictions."""
        strategy = make_strategy(btc_updown_min_edge=0.03, btc_updown_max_ask=0.90)
        now = time.time()
        start_ts = now - 150
        start_ts_ms = start_ts * 1000

        klines = [{
            "open_time": int(start_ts_ms) - 30000,
            "open": 68000.0, "high": 68100.0, "low": 67900.0,
            "close": 68050.0, "volume": 100.0,
            "close_time": int(start_ts_ms) + 30000,
        }]
        strategy.binance.get_price = MagicMock(return_value=68050.0)
        strategy.binance.get_klines = MagicMock(return_value=klines)
        strategy.binance.compute_atr = MagicMock(return_value=68.0)
        strategy.binance.get_recent_trades = MagicMock(return_value=[
            {"qty": "0.7", "isBuyerMaker": False},
            {"qty": "0.3", "isBuyerMaker": True},
        ])
        strategy.binance.get_orderbook = MagicMock(return_value={
            "bids": [["68040.0", "10.0"]],
            "asks": [["68060.0", "3.0"]],
        })
        strategy.gamma.get_crypto_updown_markets = MagicMock(return_value=[
            {
                "_start_ts": start_ts,
                "clobTokenIds": json.dumps(["token_up", "token_down"]),
                "outcomes": json.dumps(["Up", "Down"]),
                "conditionId": "cond1",
                "question": "Will BTC go up?",
            },
        ])

        mock_clob = make_mock_clob()
        strategy.analyze([], mock_clob)

        assert len(strategy._pending_predictions) >= 2

    def test_prediction_resolved_after_window(self):
        """TradeLog resolve_prediction updates actual_correct."""
        from src.storage.models import SCHEMA_SQL

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)

        from src.storage.trade_log import TradeLog
        tl = TradeLog(conn)

        pred_id = tl.log_prediction({
            "strategy": "btc_updown",
            "asset": "BTC",
            "interval": "5m",
            "market_id": "cond1",
            "token_id": "token_up",
            "outcome": "Up",
            "est_prob": 0.75,
            "bid_price": 0.70,
            "resolution_ts": time.time() - 10,
        })

        unresolved = tl.get_unresolved_predictions()
        assert len(unresolved) == 1
        assert unresolved[0]["id"] == pred_id

        tl.resolve_prediction(pred_id, actual_correct=True, pnl=0.30)

        unresolved_after = tl.get_unresolved_predictions()
        assert len(unresolved_after) == 0

        row = conn.execute("SELECT * FROM predictions WHERE id=?", (pred_id,)).fetchone()
        assert row["resolved"] == 1
        assert row["actual_correct"] == 1
        assert row["pnl"] == pytest.approx(0.30)

    def test_calibration_stats_buckets(self):
        """Calibration stats group by probability buckets."""
        from src.storage.models import SCHEMA_SQL

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)

        from src.storage.trade_log import TradeLog
        tl = TradeLog(conn)

        # Log some resolved predictions
        for prob, correct in [(0.55, True), (0.58, False), (0.65, True), (0.67, True), (0.72, True)]:
            pid = tl.log_prediction({
                "strategy": "btc_updown", "asset": "BTC", "interval": "5m",
                "market_id": "c1", "token_id": "t1", "outcome": "Up",
                "est_prob": prob, "resolution_ts": time.time() - 10,
            })
            tl.resolve_prediction(pid, actual_correct=correct)

        stats = tl.get_calibration_stats()
        assert "0.50-0.60" in stats
        assert stats["0.50-0.60"]["count"] == 2
        assert stats["0.50-0.60"]["wins"] == 1

        assert "0.60-0.70" in stats
        assert stats["0.60-0.70"]["count"] == 2
        assert stats["0.60-0.70"]["wins"] == 2

        assert "0.70-0.80" in stats
        assert stats["0.70-0.80"]["count"] == 1
        assert stats["0.70-0.80"]["wins"] == 1


# --- Improvement 3: Smart bid placement tests ---

class TestSmartBid:
    def test_smart_bid_undercuts_ask(self):
        """When best_ask < fair_bid, bid = ask - tick."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05, btc_updown_min_edge=0.05)

        # est_prob=0.75, fair_bid = 0.75 - 0.05 = 0.70
        # best_ask=0.60, which is < 0.70
        # undercut_bid = 0.60 - 0.01 = 0.59
        # edge = 0.75 - 0.59 = 0.16 >= 0.05 min_edge → valid
        mock_book = MagicMock()
        mock_book.asks = [MagicMock(price="0.60")]

        bid = strategy._get_smart_bid(0.75, mock_book)
        assert bid == 0.59

    def test_smart_bid_respects_min_edge(self):
        """Undercutting won't produce a bid below min_edge from est_prob."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05, btc_updown_min_edge=0.10)

        # est_prob=0.65, fair_bid = 0.65 - 0.05 = 0.60
        # best_ask=0.58, undercut = 0.57
        # edge = 0.65 - 0.57 = 0.08 < 0.10 min_edge → fall back to fair_bid
        mock_book = MagicMock()
        mock_book.asks = [MagicMock(price="0.58")]

        bid = strategy._get_smart_bid(0.65, mock_book)
        assert bid == 0.60  # fair_bid, not undercut

    def test_smart_bid_fallback_no_book(self):
        """Empty book uses static formula."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        bid = strategy._get_smart_bid(0.75, None)
        assert bid == 0.70  # fair_bid = 0.75 - 0.05

    def test_smart_bid_ask_above_fair(self):
        """When best_ask >= fair_bid, use fair_bid (no undercut needed)."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        # est_prob=0.75, fair_bid=0.70
        # best_ask=0.80 >= 0.70 → no undercut
        mock_book = MagicMock()
        mock_book.asks = [MagicMock(price="0.80")]

        bid = strategy._get_smart_bid(0.75, mock_book)
        assert bid == 0.70

    def test_smart_bid_empty_asks(self):
        """Book with no asks uses fair_bid."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        mock_book = MagicMock()
        mock_book.asks = []

        bid = strategy._get_smart_bid(0.75, mock_book)
        assert bid == 0.70

    def test_smart_bid_in_evaluate_both_sides(self):
        """Smart bid is used in _evaluate_both_sides when book is available."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05, btc_updown_min_edge=0.05)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        # Book with ask at 0.60 for the Down token
        mock_book = MagicMock()
        mock_book.asks = [MagicMock(price="0.60")]

        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.60, "bid": 0.55, "mid": 0.575}
        mock_clob.get_book.return_value = mock_book

        # Strong down delta → Down est_prob ~ 0.88
        # fair_bid = 0.88 - 0.05 = 0.83
        # best_ask = 0.60 < 0.83 → undercut to 0.59
        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        assert signal is not None
        assert signal.outcome == "Down"
        # Should be undercut bid (0.59), not fair_bid (0.83)
        assert signal.price == 0.59
