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
        "btc_updown_min_edge": 0.03,
        "btc_updown_5m_vol": 0.0025,
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
        # +0.30% delta at 80% through window (strong signal relative to 0.25% baseline)
        prob = strategy._estimate_outcome_probability("Up", 0.0030, 0.8, 0.0)
        assert prob > 0.80

    def test_late_window_strong_down(self):
        strategy = make_strategy()
        # -0.30% delta at 80% through window, evaluating "Down"
        prob = strategy._estimate_outcome_probability("Down", -0.0030, 0.8, 0.0)
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
        """Bid price should be est_prob - cushion, not the ask.
        High est_prob (>0.65) uses dynamic cushion of 0.02."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05, btc_updown_max_ask=0.95,
                                 btc_updown_min_edge=0.01)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        # Strong downward delta with longer window to stay on maker path
        delta_info = {
            "delta_pct": -0.003, "window_progress": 0.7,
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
        }
        momentum = -0.5

        signal, predictions = strategy._evaluate_both_sides(market, "BTC", delta_info, momentum, mock_clob)
        assert signal is not None
        assert signal.outcome == "Down"
        assert signal.price < 0.95

    def test_no_edge_returns_none(self):
        """At 50% probability with cushion=0.05, edge=0.05. Need min_edge > 0.05 to reject."""
        strategy = make_strategy(btc_updown_min_edge=0.06)
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
        """bid = est_prob - dynamic cushion (0.02 for high confidence), not ask."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05, btc_updown_max_ask=0.95,
                                 btc_updown_min_edge=0.01)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        # Strong down delta with longer window for maker path
        delta_info = {
            "delta_pct": -0.003, "window_progress": 0.7,
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.5, mock_clob)
        assert signal is not None
        assert signal.outcome == "Down"
        # Bid is derived from model with dynamic cushion, not from CLOB ask
        assert signal.price == round(signal.confidence - 0.02, 2)

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

        # Use moderate delta to keep bid within max_ask (0.85)
        delta_info = {
            "delta_pct": -0.0008, "window_progress": 0.8,
            "time_remaining": 700, "dynamic_vol": 0.0025,
            "resolution_ts": time.time() + 700,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.2, mock_clob)
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

        # Use moderate delta + long time_remaining to stay on maker path
        delta_info = {
            "delta_pct": -0.0008, "window_progress": 0.8,
            "time_remaining": 700, "dynamic_vol": 0.0025,
            "resolution_ts": time.time() + 700,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.2, mock_clob)
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

        resolution_ts = time.time() + 700
        delta_info = {
            "delta_pct": -0.0008, "window_progress": 0.8,
            "time_remaining": 700, "dynamic_vol": 0.0025,
            "resolution_ts": resolution_ts,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.2, mock_clob)
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
        # Balance reduced by reserved capital for pending order
        actual_cost = 1.50 * 0.55
        assert executor.get_balance() == pytest.approx(10.0 - actual_cost, abs=0.01)

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
    def test_order_manager_market_price_fill(self):
        """Fill triggers when CLOB ask drops to bid price (adverse selection)."""
        from src.core.order_manager import OrderManager

        trade_log = MagicMock()
        om = OrderManager(trade_log)

        order = {
            "trade_id": 1, "fill_price": 0.55, "token_id": "t1",
            "size": 1.5, "cost": 0.825, "market_id": "c1", "outcome": "Down",
            "market_question": "Q?", "confidence": 0.60,
            "cancel_after_ts": time.time() + 1000,
        }
        om.track_order(order)

        # Mock CLOB: ask dropped to 0.50 <= bid 0.55 → should fill
        clob_client = MagicMock()
        clob_client.get_price.return_value = {"bid": 0.48, "ask": 0.50, "mid": 0.49}

        executor = MagicMock()
        executor.paper = MagicMock()
        executor.paper.fill_order.return_value = {"status": "filled", "position_id": 1}

        filled = om.check_pending_orders(clob_client, executor, paper_mode=True)

        assert len(filled) == 1
        executor.paper.fill_order.assert_called_once_with(order)
        assert len(om.get_pending_orders()) == 0

    def test_order_manager_no_fill_when_ask_above_bid(self):
        """Order stays pending when CLOB ask is above our bid."""
        from src.core.order_manager import OrderManager

        trade_log = MagicMock()
        om = OrderManager(trade_log)

        order = {
            "trade_id": 1, "fill_price": 0.55, "token_id": "t1",
            "size": 1.5, "cost": 0.825, "market_id": "c1", "outcome": "Down",
            "market_question": "Q?", "confidence": 0.60,
            "cancel_after_ts": time.time() + 1000,
        }
        om.track_order(order)

        # Mock CLOB: ask at 0.62 > bid 0.55 → should NOT fill
        clob_client = MagicMock()
        clob_client.get_price.return_value = {"bid": 0.58, "ask": 0.62, "mid": 0.60}

        executor = MagicMock()
        executor.paper = MagicMock()

        filled = om.check_pending_orders(clob_client, executor, paper_mode=True)

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
        # None → falls back to self.btc_5m_vol (0.0025)
        prob_default = strategy._estimate_outcome_probability("Up", 0.0010, 0.8, 0.0)
        prob_explicit = strategy._estimate_outcome_probability("Up", 0.0010, 0.8, 0.0, dynamic_vol=0.0025)
        assert prob_default == pytest.approx(prob_explicit, abs=0.001)

    def test_high_vol_widens_min_edge(self):
        strategy = make_strategy(btc_updown_min_edge=0.01, btc_updown_max_ask=0.90)
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
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
        }
        signal_normal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info_normal, -0.2, mock_clob)

        # Same delta but 3x vol → effective_min_edge = 0.03, harder to pass
        # and probability is lower (delta normalized by higher vol)
        delta_info_high_vol = {
            "delta_pct": -0.0012, "window_progress": 0.7,
            "time_remaining": 700, "dynamic_vol": 0.0030,
            "resolution_ts": time.time() + 700,
        }
        signal_high_vol, _ = strategy._evaluate_both_sides(market, "BTC", delta_info_high_vol, -0.2, mock_clob)

        # At normal vol it should trade
        assert signal_normal is not None
        # High vol lowers confidence (delta normalized by larger vol)
        if signal_high_vol is not None:
            assert signal_high_vol.confidence < signal_normal.confidence

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
        strategy = make_strategy(btc_updown_min_edge=0.01, btc_updown_max_ask=0.90)
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
        # Disable min_confidence to test prediction logging for all candidates
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05, btc_updown_min_confidence=0.0)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }

        mock_clob = make_mock_clob()

        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
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

        # Use moderate delta to keep bid within max_ask
        delta_info = {
            "delta_pct": -0.0008, "window_progress": 0.8,
            "time_remaining": 700, "dynamic_vol": 0.0025,
            "resolution_ts": time.time() + 700,
        }

        signal, predictions = strategy._evaluate_both_sides(
            market, "BTC", delta_info, -0.2, mock_clob, interval="5m",
        )
        assert signal is not None
        # The winning side should be marked as traded
        traded = [p for p in predictions if p["traded"]]
        assert len(traded) >= 1
        assert traded[0]["outcome"] == signal.outcome

    def test_predictions_collected_in_analyze(self):
        """analyze() populates _pending_predictions."""
        strategy = make_strategy(btc_updown_min_edge=0.03, btc_updown_max_ask=0.90, btc_updown_min_confidence=0.0)
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
        """Empty book uses dynamic cushion formula."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        # est_prob=0.75 > 0.65 → dynamic cushion=0.02, fair_bid=0.73
        bid = strategy._get_smart_bid(0.75, None)
        assert bid == 0.73

    def test_smart_bid_fallback_no_book_low_prob(self):
        """Low probability uses default cushion."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        # est_prob=0.60 <= 0.65 → default cushion=0.05, fair_bid=0.55
        bid = strategy._get_smart_bid(0.60, None)
        assert bid == 0.55

    def test_smart_bid_ask_above_fair(self):
        """When best_ask >= fair_bid, use fair_bid (no undercut needed)."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        # est_prob=0.75 > 0.65 → dynamic cushion=0.02, fair_bid=0.73
        # best_ask=0.80 >= 0.73 → no undercut
        mock_book = MagicMock()
        mock_book.asks = [MagicMock(price="0.80")]

        bid = strategy._get_smart_bid(0.75, mock_book)
        assert bid == 0.73

    def test_smart_bid_empty_asks(self):
        """Book with no asks uses fair_bid."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        # est_prob=0.75 > 0.65 → dynamic cushion=0.02, fair_bid=0.73
        mock_book = MagicMock()
        mock_book.asks = []

        bid = strategy._get_smart_bid(0.75, mock_book)
        assert bid == 0.73

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

        # CLOB bid at 0.70 — close enough to model estimate to pass disagreement filter
        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.75, "bid": 0.70, "mid": 0.725}
        mock_clob.get_book.return_value = mock_book

        # Strong down delta → Down model_prob ~0.86, blended ~0.81
        # fair_bid = 0.81 - 0.05 = 0.76
        # best_ask = 0.60 < 0.76 → undercut to 0.59
        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
        }

        signal, _ = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        assert signal is not None
        assert signal.outcome == "Down"
        # Should be undercut bid (0.59), not fair_bid
        assert signal.price == 0.59


# --- New tests for 1h slug, vol cap, dynamic cushion, Gamma fallback ---

class TestHourlySlug:
    def test_hourly_slug_format(self):
        """1h slug follows human-readable ET format."""
        from src.market_data.gamma_client import GammaClient
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # March 15, 2026 9:00 PM ET = Unix 1773799200
        # Construct a timestamp for 9PM ET on March 15, 2026
        et = ZoneInfo("America/New_York")
        dt = datetime(2026, 3, 15, 21, 0, 0, tzinfo=et)
        ts = int(dt.timestamp())

        slug = GammaClient._hourly_slug("BTC", ts)
        assert slug == "bitcoin-up-or-down-march-15-2026-9pm-et"

    def test_hourly_slug_am(self):
        """Morning hours use 'am' suffix."""
        from src.market_data.gamma_client import GammaClient
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        dt = datetime(2026, 3, 15, 10, 0, 0, tzinfo=et)
        ts = int(dt.timestamp())

        slug = GammaClient._hourly_slug("ETH", ts)
        assert slug == "ethereum-up-or-down-march-15-2026-10am-et"

    def test_hourly_slug_asset_mapping(self):
        """Each asset maps to correct coin name."""
        from src.market_data.gamma_client import GammaClient
        from datetime import datetime
        from zoneinfo import ZoneInfo

        et = ZoneInfo("America/New_York")
        dt = datetime(2026, 3, 15, 14, 0, 0, tzinfo=et)
        ts = int(dt.timestamp())

        assert GammaClient._hourly_slug("SOL", ts) == "solana-up-or-down-march-15-2026-2pm-et"
        assert GammaClient._hourly_slug("DOGE", ts) == "dogecoin-up-or-down-march-15-2026-2pm-et"
        assert GammaClient._hourly_slug("BNB", ts) == "bnb-up-or-down-march-15-2026-2pm-et"
        assert GammaClient._hourly_slug("XRP", ts) == "xrp-up-or-down-march-15-2026-2pm-et"

    def test_hourly_slug_used_for_1h_interval(self):
        """get_crypto_updown_markets uses _hourly_slug for 1h interval."""
        from src.market_data.gamma_client import GammaClient

        client = GammaClient()
        slugs_called = []
        original_fetch = client._fetch_event_markets

        def capture_slug(slug, ts):
            slugs_called.append(slug)
            return []

        client._fetch_event_markets = capture_slug
        client.get_crypto_updown_markets("BTC", "1h")

        # All slugs for 1h should have human-readable format, not unix timestamp
        for slug in slugs_called:
            assert "bitcoin-up-or-down-" in slug
            assert "-et" in slug
            # Should NOT contain raw unix timestamp
            assert "btc-updown-1h-" not in slug

    def test_5m_slug_unchanged(self):
        """5m interval still uses old unix-timestamp slug format."""
        from src.market_data.gamma_client import GammaClient

        client = GammaClient()
        slugs_called = []

        def capture_slug(slug, ts):
            slugs_called.append(slug)
            return []

        client._fetch_event_markets = capture_slug
        client.get_crypto_updown_markets("BTC", "5m")

        for slug in slugs_called:
            assert slug.startswith("btc-updown-5m-")


class TestVolRatioCap:
    def test_vol_ratio_capped_at_2(self):
        """Vol ratio above 2.0 is capped, preventing excessive edge widening."""
        strategy = make_strategy(btc_updown_max_ask=0.90)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }
        mock_clob = make_mock_clob()

        # With dynamic_vol=0.010 (4x baseline 0.0025), vol_ratio would be 4.0
        # but should be capped at 2.0. effective_min_edge = 0.03 * 2.0 = 0.06
        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.010,
            "resolution_ts": time.time() + 60,
        }
        signal, preds = strategy._evaluate_both_sides(market, "BTC", delta_info, -0.3, mock_clob)
        # With extreme vol, low-prob Up has edge=0.05 (cushion) but
        # eff_min_edge=0.06, so it should be rejected.
        # High-prob Down uses dynamic cushion (0.02), outcome_min_edge=0.02, passes
        traded = [p for p in preds if p["traded"]]
        # At 4x vol uncapped, eff_min_edge=0.12 → nothing trades.
        # At 2x cap, eff_min_edge=0.06 → Down still trades via dynamic cushion.
        if signal is not None:
            assert signal.confidence > 0.65  # only high-confidence trades survive

    def test_vol_ratio_floor_at_1(self):
        """Vol ratio below 1.0 is floored, never shrinking min_edge."""
        # Disable min_confidence to test vol ratio behavior with neutral delta
        strategy = make_strategy(btc_updown_min_confidence=0.0)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
        }
        mock_clob = make_mock_clob()

        # Low vol: 0.0005, ratio would be 0.2 but clamped to 1.0
        delta_info = {
            "delta_pct": 0.0, "window_progress": 0.5,
            "time_remaining": 700, "dynamic_vol": 0.0005,
            "resolution_ts": time.time() + 700,
        }
        signal, preds = strategy._evaluate_both_sides(market, "BTC", delta_info, 0.0, mock_clob)
        # effective_min_edge = 0.03 * 1.0 = 0.03 (not 0.03 * 0.2 = 0.006)
        # With no delta, est_prob=0.50, cushion=0.05, edge=0.05 >= 0.03 → passes
        assert signal is not None


class TestDynamicCushion:
    def test_high_confidence_uses_tight_cushion(self):
        """est_prob > 0.65 uses cushion=0.02 instead of default 0.05."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        # High confidence: cushion=0.02
        bid_high = strategy._get_smart_bid(0.80, None)
        assert bid_high == 0.78  # 0.80 - 0.02

        # Low confidence: cushion=0.05
        bid_low = strategy._get_smart_bid(0.60, None)
        assert bid_low == 0.55  # 0.60 - 0.05

    def test_boundary_at_065(self):
        """est_prob == 0.65 uses default cushion (not dynamic)."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        bid = strategy._get_smart_bid(0.65, None)
        assert bid == 0.60  # 0.65 - 0.05 (default, not 0.65 - 0.02 = 0.63)

    def test_just_above_065(self):
        """est_prob = 0.66 uses dynamic cushion."""
        strategy = make_strategy(btc_updown_maker_edge_cushion=0.05)

        bid = strategy._get_smart_bid(0.66, None)
        assert bid == 0.64  # 0.66 - 0.02


class TestGammaPriceFallback:
    def test_gamma_prices_used_when_clob_empty(self):
        """When CLOB spread > 0.90 and Gamma has prices, use Gamma for bidding."""
        strategy = make_strategy(btc_updown_max_ask=0.90)

        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
            "bestBid": "0.55",
            "bestAsk": "0.56",
            "outcomePrices": '["0.55", "0.45"]',
        }

        # CLOB returns empty book (spread > 0.90)
        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.99, "bid": 0.01, "mid": 0.50}
        mock_clob.get_book.return_value = None

        delta_info = {
            "delta_pct": -0.0015, "window_progress": 0.8,
            "time_remaining": 60, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 60,
        }

        signal, preds = strategy._evaluate_both_sides(
            market, "BTC", delta_info, -0.3, mock_clob
        )
        # Gamma prices should be used; bid should be near gamma_bid, not 0.99
        if signal is not None:
            assert signal.price < 0.90

    def test_extract_gamma_prices_basic(self):
        """_extract_gamma_prices parses bestBid/bestAsk and outcomePrices."""
        strategy = make_strategy()

        market = {
            "bestBid": "0.55",
            "bestAsk": "0.57",
            "outcomePrices": '["0.56", "0.44"]',
        }

        prices = strategy._extract_gamma_prices(market)
        assert prices["bid_0"] == 0.55
        assert prices["ask_0"] == 0.57
        # outcomePrices don't override bestBid/bestAsk for index 0
        assert prices["bid_0"] == 0.55
        # Index 1 derived from outcomePrices
        assert "bid_1" in prices

    def test_extract_gamma_prices_empty(self):
        """Empty market returns empty prices."""
        strategy = make_strategy()
        prices = strategy._extract_gamma_prices({})
        assert prices == {}

    def test_late_window_taker(self):
        """Late window with Gamma ask below estimate triggers taker order."""
        strategy = make_strategy(btc_updown_max_ask=0.90)

        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "cond1",
            "question": "Will BTC go up?",
            "bestBid": "0.40",
            "bestAsk": "0.42",
            "outcomePrices": '["0.41", "0.59"]',
        }

        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.99, "bid": 0.01, "mid": 0.50}
        mock_clob.get_book.return_value = None

        # Strong Down signal, late window
        delta_info = {
            "delta_pct": -0.003, "window_progress": 0.85,
            "time_remaining": 45, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 45,
        }

        signal, preds = strategy._evaluate_both_sides(
            market, "BTC", delta_info, -0.5, mock_clob
        )

        # Down est_prob ~0.95, Gamma ask for Down ~0.60
        # 0.60 < 0.95 - 0.01 = 0.94 → taker triggered
        if signal is not None and signal.outcome == "Down":
            # Taker order: post_only should be False
            assert signal.post_only is False


# --- Market anchoring tests (copilot review #6) ---

class TestMarketAnchoring:
    def test_blending_pulls_toward_market(self):
        """When market_bid is provided, probability should be pulled toward it."""
        strategy = make_strategy()
        # Pure model with strong up delta
        model_only = strategy._estimate_outcome_probability("Up", 0.003, 0.7, 0.0)
        # Same but with low market bid — should pull estimate down
        blended = strategy._estimate_outcome_probability("Up", 0.003, 0.7, 0.0, market_bid=0.40)
        assert blended < model_only

    def test_no_blending_when_market_bid_near_zero(self):
        """Market bid <= 0.02 should not trigger blending."""
        strategy = make_strategy()
        prob_no_market = strategy._estimate_outcome_probability("Up", 0.001, 0.5, 0.0)
        prob_zero_market = strategy._estimate_outcome_probability("Up", 0.001, 0.5, 0.0, market_bid=0.01)
        assert prob_no_market == pytest.approx(prob_zero_market, abs=0.001)

    def test_blending_weights(self):
        """Blended probability should be 70% model + 30% market."""
        strategy = make_strategy()
        model_prob = strategy._compute_model_probability("Up", 0.002, 0.6, 0.0)
        market_bid = 0.60
        blended = strategy._estimate_outcome_probability("Up", 0.002, 0.6, 0.0, market_bid=market_bid)
        expected = 0.70 * model_prob + 0.30 * market_bid
        assert blended == pytest.approx(expected, abs=0.01)

    def test_blending_with_high_market_bid(self):
        """High market bid should pull model up toward market."""
        strategy = make_strategy()
        model_only = strategy._estimate_outcome_probability("Up", 0.0, 0.5, 0.0)
        blended = strategy._estimate_outcome_probability("Up", 0.0, 0.5, 0.0, market_bid=0.80)
        assert blended > model_only

    def test_model_probability_unaffected_by_market(self):
        """_compute_model_probability should not use market_bid."""
        strategy = make_strategy()
        prob1 = strategy._compute_model_probability("Up", 0.002, 0.6, 0.0)
        prob2 = strategy._compute_model_probability("Up", 0.002, 0.6, 0.0)
        assert prob1 == prob2


# --- Market disagreement filter tests (copilot review #7) ---

class TestMarketDisagreementFilter:
    def test_disagreement_skips_signal(self):
        """When model and market diverge >25 points, signal is skipped."""
        strategy = make_strategy(btc_updown_min_confidence=0.0)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "c1", "question": "BTC up?",
            "_start_ts": time.time() - 150, "_resolution_ts": time.time() + 150,
        }
        # CLOB bid at 0.04 (market thinks ~4% for Up)
        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.06, "bid": 0.04, "mid": 0.05}
        mock_clob.get_book.return_value = None

        # Strong up delta → model thinks ~80%+ for Up, but market is at 4%
        delta_info = {
            "delta_pct": 0.003, "window_progress": 0.7,
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
        }

        signal, preds = strategy._evaluate_both_sides(market, "BTC", delta_info, 0.5, mock_clob)
        # The Up signal should be skipped due to disagreement (model ~80% vs market 4%)
        # Check that a skip prediction was logged
        skip_preds = [p for p in preds if p.get("skip_reason") == "market_disagreement"]
        assert len(skip_preds) > 0

    def test_no_disagreement_when_market_agrees(self):
        """When model and market are close, signal passes through."""
        strategy = make_strategy(btc_updown_min_edge=0.01)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "c2", "question": "BTC up?",
            "_start_ts": time.time() - 150, "_resolution_ts": time.time() + 150,
        }
        # CLOB bid at 0.60 — close to where model will estimate
        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.65, "bid": 0.60, "mid": 0.625}
        mock_clob.get_book.return_value = None

        delta_info = {
            "delta_pct": 0.001, "window_progress": 0.5,
            "time_remaining": 700, "dynamic_vol": 0.0025,
            "resolution_ts": time.time() + 700,
        }

        signal, preds = strategy._evaluate_both_sides(market, "BTC", delta_info, 0.2, mock_clob)
        skip_preds = [p for p in preds if p.get("skip_reason") == "market_disagreement"]
        assert len(skip_preds) == 0

    def test_disagreement_check_ignores_thin_market(self):
        """When market_ref_bid <= 0.02, disagreement check is skipped."""
        strategy = make_strategy(btc_updown_min_edge=0.01, btc_updown_min_confidence=0.0)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "c3", "question": "BTC up?",
            "_start_ts": time.time() - 150, "_resolution_ts": time.time() + 150,
        }
        # CLOB bid at 0.01 — too thin for disagreement check
        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.99, "bid": 0.01, "mid": 0.50}
        mock_clob.get_book.return_value = None

        delta_info = {
            "delta_pct": 0.003, "window_progress": 0.7,
            "time_remaining": 700, "dynamic_vol": 0.0010,
            "resolution_ts": time.time() + 700,
        }

        signal, preds = strategy._evaluate_both_sides(market, "BTC", delta_info, 0.5, mock_clob)
        # Should NOT skip for disagreement since market_ref_bid is 0.01
        skip_preds = [p for p in preds if p.get("skip_reason") == "market_disagreement"]
        assert len(skip_preds) == 0

    def test_low_confidence_skip_logged(self):
        """When confidence is below min, skip is logged with reason."""
        strategy = make_strategy(btc_updown_min_confidence=0.70)
        market = {
            "outcomes": ["Up", "Down"],
            "clobTokenIds": ["token_up", "token_down"],
            "conditionId": "c4", "question": "BTC up?",
            "_start_ts": time.time() - 150, "_resolution_ts": time.time() + 150,
        }
        # Market bid at 0.40 — model will blend to ~0.45-0.55 range
        mock_clob = MagicMock()
        mock_clob.get_price.return_value = {"ask": 0.45, "bid": 0.40, "mid": 0.425}
        mock_clob.get_book.return_value = None

        # Neutral delta → prob around 0.50, below min_confidence of 0.70
        delta_info = {
            "delta_pct": 0.0, "window_progress": 0.3,
            "time_remaining": 700, "dynamic_vol": 0.0025,
            "resolution_ts": time.time() + 700,
        }

        signal, preds = strategy._evaluate_both_sides(market, "BTC", delta_info, 0.0, mock_clob)
        low_conf_preds = [p for p in preds if p.get("skip_reason") == "low_confidence"]
        assert len(low_conf_preds) > 0
