"""Tests for duplicate order prevention across engine and risk manager."""
import sqlite3
import unittest
from collections import Counter
from unittest.mock import MagicMock, patch

from src.config.settings import Settings
from src.execution.paper_executor import PaperExecutor
from src.risk.risk_manager import RiskManager, TradeSignal
from src.risk.circuit_breaker import CircuitBreaker
from src.storage.models import SCHEMA_SQL
from src.storage.trade_log import TradeLog


def _make_signal(market_id="mkt_1", token_id="tok_1", strategy="high_probability",
                 outcome="Yes", price=0.93, confidence=0.95, resolution_ts=0.0):
    return TradeSignal(
        market_id=market_id,
        token_id=token_id,
        market_question=f"Test market {market_id}?",
        side="BUY",
        outcome=outcome,
        price=price,
        confidence=confidence,
        strategy=strategy,
        expected_value=confidence * (1 - price),
        resolution_ts=resolution_ts,
    )


class TestRiskManagerDedup(unittest.TestCase):
    """Layer 2: per-market concentration check in the risk manager."""

    def setUp(self):
        self.settings = Settings(
            starting_capital=10.0,
            hard_floor_pct=0.20,
            max_single_trade_pct=0.10,
            max_portfolio_exposure_pct=0.60,
            max_open_positions=5,
            min_trade_size=0.50,
            max_positions_per_market=1,
        )
        self.cb = CircuitBreaker()
        self.cb.set_start_of_day_balance(10.0)
        self.rm = RiskManager(self.settings, self.cb)

    def test_blocks_duplicate_market(self):
        """Signal rejected when we already hold a position on the same market."""
        signal = _make_signal(market_id="mkt_1")
        existing = [{"market_id": "mkt_1", "cost": 1.0}]
        result = self.rm.evaluate(signal, balance=10.0, open_positions=existing, portfolio_exposure=1.0)
        self.assertIsNone(result)

    def test_allows_different_market(self):
        """Signal approved when existing position is on a different market."""
        signal = _make_signal(market_id="mkt_2")
        existing = [{"market_id": "mkt_1", "cost": 1.0}]
        result = self.rm.evaluate(signal, balance=10.0, open_positions=existing, portfolio_exposure=1.0)
        self.assertIsNotNone(result)

    def test_allows_first_position_on_market(self):
        """Signal approved when no position on this market yet."""
        signal = _make_signal(market_id="mkt_1")
        result = self.rm.evaluate(signal, balance=10.0, open_positions=[], portfolio_exposure=0)
        self.assertIsNotNone(result)

    def test_arbitrage_allows_second_position(self):
        """Arbitrage strategy can hold 2 positions (YES+NO) on the same market."""
        signal = _make_signal(market_id="mkt_1", strategy="arbitrage", outcome="No",
                              token_id="tok_2", price=0.05, confidence=0.98)
        existing = [{"market_id": "mkt_1", "cost": 0.5}]  # already have YES
        result = self.rm.evaluate(signal, balance=10.0, open_positions=existing, portfolio_exposure=0.5)
        self.assertIsNotNone(result)

    def test_arbitrage_blocks_third_position(self):
        """Even arbitrage is capped at 2 per market."""
        signal = _make_signal(market_id="mkt_1", strategy="arbitrage", outcome="Yes",
                              token_id="tok_3", price=0.05, confidence=0.98)
        existing = [
            {"market_id": "mkt_1", "cost": 0.5},
            {"market_id": "mkt_1", "cost": 0.5},
        ]
        result = self.rm.evaluate(signal, balance=10.0, open_positions=existing, portfolio_exposure=1.0)
        self.assertIsNone(result)

    def test_blocks_flip_bet(self):
        """Non-arbitrage NO signal blocked when we already hold YES on same market."""
        signal = _make_signal(market_id="mkt_1", outcome="No", token_id="tok_2")
        existing = [{"market_id": "mkt_1", "cost": 1.0}]  # holding YES
        result = self.rm.evaluate(signal, balance=10.0, open_positions=existing, portfolio_exposure=1.0)
        self.assertIsNone(result)


class TestEngineDedup(unittest.TestCase):
    """Layer 1: market-level dedup in the engine signal loop."""

    def test_counter_skips_duplicate(self):
        """Simulate the engine's counter logic: signal skipped when market already held."""
        open_positions = [{"market_id": "mkt_1"}, {"market_id": "mkt_2"}]
        pending_orders = [{"market_id": "mkt_3"}]

        market_pos_count = Counter(p["market_id"] for p in open_positions if p.get("market_id"))
        for o in pending_orders:
            if o.get("market_id"):
                market_pos_count[o["market_id"]] += 1

        signal_mkt1 = _make_signal(market_id="mkt_1")
        signal_mkt3 = _make_signal(market_id="mkt_3")
        signal_mkt4 = _make_signal(market_id="mkt_4")

        # mkt_1: already in open positions → skip
        max_for = 1
        self.assertTrue(market_pos_count.get(signal_mkt1.market_id, 0) >= max_for)

        # mkt_3: pending order → skip
        self.assertTrue(market_pos_count.get(signal_mkt3.market_id, 0) >= max_for)

        # mkt_4: no position → allow
        self.assertFalse(market_pos_count.get(signal_mkt4.market_id, 0) >= max_for)

    def test_counter_increments_on_execution(self):
        """After a successful execution, the counter prevents a second signal on the same market."""
        market_pos_count = Counter()

        signal_a = _make_signal(market_id="mkt_1")
        signal_b = _make_signal(market_id="mkt_1", token_id="tok_2")

        # First signal: allowed
        self.assertFalse(market_pos_count.get(signal_a.market_id, 0) >= 1)
        # Simulate execution
        market_pos_count[signal_a.market_id] += 1

        # Second signal same market: blocked
        self.assertTrue(market_pos_count.get(signal_b.market_id, 0) >= 1)

    def test_arbitrage_allows_two_in_counter(self):
        """Arbitrage strategy has max_for_market=2, so second signal passes."""
        market_pos_count = Counter()

        signal_yes = _make_signal(market_id="mkt_1", strategy="arbitrage", outcome="Yes")
        signal_no = _make_signal(market_id="mkt_1", strategy="arbitrage", outcome="No", token_id="tok_2")

        # First arb signal: allowed
        max_for = 2
        self.assertFalse(market_pos_count.get(signal_yes.market_id, 0) >= max_for)
        market_pos_count[signal_yes.market_id] += 1

        # Second arb signal: still allowed (count=1, max=2)
        self.assertFalse(market_pos_count.get(signal_no.market_id, 0) >= max_for)
        market_pos_count[signal_no.market_id] += 1

        # Third would be blocked
        self.assertTrue(market_pos_count.get("mkt_1", 0) >= max_for)

    def test_pending_orders_counted(self):
        """Pending orders contribute to the market position count."""
        open_positions = []
        pending_orders = [{"market_id": "mkt_1"}]

        market_pos_count = Counter(p["market_id"] for p in open_positions if p.get("market_id"))
        for o in pending_orders:
            if o.get("market_id"):
                market_pos_count[o["market_id"]] += 1

        # mkt_1 has a pending order → blocked for non-arbitrage
        self.assertTrue(market_pos_count.get("mkt_1", 0) >= 1)


class TestLiveExecutorMarketId(unittest.TestCase):
    """Verify live executor returns market_id for pending orders."""

    def test_pending_order_has_market_id(self):
        """GTC orders should include market_id in the return dict."""
        from src.execution.live_executor import LiveExecutor

        mock_clob = MagicMock()
        mock_clob.post_order.return_value = {"orderID": "ord_123"}
        mock_trade_log = MagicMock()
        mock_trade_log.log_trade.return_value = 1

        executor = LiveExecutor(clob_client=mock_clob, trade_log=mock_trade_log)

        signal = _make_signal(market_id="mkt_abc")
        signal.order_type = "GTC"
        trade = MagicMock()
        trade.signal = signal
        trade.size = 1.0
        trade.cost = 0.93
        trade.kelly_fraction = 0.1

        result = executor.execute(trade)
        self.assertEqual(result["status"], "pending")
        self.assertEqual(result["market_id"], "mkt_abc")

    def test_filled_order_no_market_id(self):
        """Taker (non-GTC) orders don't need market_id in return dict."""
        from src.execution.live_executor import LiveExecutor

        mock_clob = MagicMock()
        mock_clob.post_order.return_value = {"orderID": "ord_456"}
        mock_trade_log = MagicMock()
        mock_trade_log.log_trade.return_value = 2
        mock_trade_log.save_position.return_value = 10

        executor = LiveExecutor(clob_client=mock_clob, trade_log=mock_trade_log)

        signal = _make_signal(market_id="mkt_xyz")
        signal.order_type = "FOK"  # fill-or-kill = taker
        trade = MagicMock()
        trade.signal = signal
        trade.size = 1.0
        trade.cost = 0.93
        trade.kelly_fraction = 0.1

        result = executor.execute(trade)
        self.assertEqual(result["status"], "filled")
        self.assertNotIn("market_id", result)


class TestSettingsDefault(unittest.TestCase):
    """Verify the new config field exists with the correct default."""

    def test_default_max_positions_per_market(self):
        s = Settings(starting_capital=10.0)
        self.assertEqual(s.max_positions_per_market, 1)

    def test_override_max_positions_per_market(self):
        s = Settings(starting_capital=10.0, max_positions_per_market=3)
        self.assertEqual(s.max_positions_per_market, 3)


class TestPaperExecutorRestart(unittest.TestCase):
    """Verify PaperExecutor loads positions from DB on restart."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.trade_log = TradeLog(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make_approved_trade(self, market_id="mkt_1", token_id="tok_1",
                             price=0.90, size=1.0):
        signal = _make_signal(market_id=market_id, token_id=token_id, price=price)
        trade = MagicMock()
        trade.signal = signal
        trade.size = size
        trade.kelly_fraction = 0.1
        return trade

    def test_restart_loads_positions(self):
        """New PaperExecutor should see positions saved by a previous instance."""
        # First executor: place a trade
        exec1 = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        trade = self._make_approved_trade()
        result = exec1.execute(trade)
        self.assertEqual(result["status"], "filled")
        self.assertEqual(len(exec1.get_open_positions()), 1)

        # Second executor (simulates restart): should load the position from DB
        exec2 = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        positions = exec2.get_open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["market_id"], "mkt_1")

    def test_restart_adjusts_balance(self):
        """Balance should account for open position costs after restart."""
        exec1 = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        trade = self._make_approved_trade(price=0.90, size=2.0)
        exec1.execute(trade)
        balance_after_trade = exec1.get_balance()

        exec2 = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        # Balance should be reduced by the open position cost
        self.assertAlmostEqual(exec2.get_balance(), balance_after_trade, places=2)

    def test_restart_no_positions_empty(self):
        """Fresh start with no DB positions should have empty list."""
        executor = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        self.assertEqual(len(executor.get_open_positions()), 0)
        self.assertEqual(executor.get_balance(), 10.0)


class TestFillTimePositionLimit(unittest.TestCase):
    """fill_order() rejects fills when position limit is reached."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.trade_log = TradeLog(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make_approved_trade(self, market_id="mkt_1", token_id="tok_1",
                             price=0.90, size=1.0):
        signal = _make_signal(market_id=market_id, token_id=token_id, price=price)
        trade = MagicMock()
        trade.signal = signal
        trade.size = size
        trade.kelly_fraction = 0.1
        return trade

    def _make_pending_order(self, trade_id, market_id="mkt_x", outcome="Yes"):
        return {
            "trade_id": trade_id,
            "market_id": market_id,
            "token_id": f"tok_{market_id}",
            "outcome": outcome,
            "fill_price": 0.50,
            "size": 0.5,
            "market_question": f"Test {market_id}?",
        }

    def test_fill_rejected_at_limit(self):
        """fill_order() returns rejected when max_open_positions reached."""
        executor = PaperExecutor(starting_balance=100.0, trade_log=self.trade_log,
                                 max_open_positions=2)
        # Fill two positions via taker trades
        for i in range(2):
            trade = self._make_approved_trade(market_id=f"mkt_{i}", token_id=f"tok_{i}")
            result = executor.execute(trade)
            self.assertEqual(result["status"], "filled")

        self.assertEqual(len(executor.get_open_positions()), 2)

        # Now a pending order tries to fill — should be rejected
        order = self._make_pending_order(trade_id=99, market_id="mkt_extra")
        result = executor.fill_order(order)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "max_positions_reached")
        # Still only 2 positions
        self.assertEqual(len(executor.get_open_positions()), 2)

    def test_fill_accepted_below_limit(self):
        """fill_order() succeeds when under the position limit."""
        executor = PaperExecutor(starting_balance=100.0, trade_log=self.trade_log,
                                 max_open_positions=3)
        trade = self._make_approved_trade(market_id="mkt_0", token_id="tok_0")
        executor.execute(trade)
        self.assertEqual(len(executor.get_open_positions()), 1)

        order = self._make_pending_order(trade_id=99, market_id="mkt_1")
        result = executor.fill_order(order)
        self.assertEqual(result["status"], "filled")
        self.assertEqual(len(executor.get_open_positions()), 2)

    def test_fill_allowed_after_close(self):
        """Closing a position frees a slot for new fills."""
        executor = PaperExecutor(starting_balance=100.0, trade_log=self.trade_log,
                                 max_open_positions=1)
        trade = self._make_approved_trade(market_id="mkt_0", token_id="tok_0")
        result = executor.execute(trade)
        pos_id = result["position_id"]

        # At limit — fill rejected
        order = self._make_pending_order(trade_id=99, market_id="mkt_1")
        self.assertEqual(executor.fill_order(order)["status"], "rejected")

        # Close the position
        executor.close_position(pos_id, exit_price=0.95)

        # Now fill should succeed
        result = executor.fill_order(order)
        self.assertEqual(result["status"], "filled")


class TestLiveExecutorPositionLimit(unittest.TestCase):
    """Live executor rejects orders when at position limit."""

    def test_rejected_at_limit(self):
        from src.execution.live_executor import LiveExecutor

        mock_clob = MagicMock()
        mock_trade_log = MagicMock()
        mock_trade_log.get_open_positions.return_value = [
            {"market_id": f"mkt_{i}"} for i in range(3)
        ]

        executor = LiveExecutor(clob_client=mock_clob, trade_log=mock_trade_log,
                                max_open_positions=3)

        signal = _make_signal(market_id="mkt_new")
        signal.order_type = "GTC"
        trade = MagicMock()
        trade.signal = signal
        trade.size = 1.0
        trade.cost = 0.93
        trade.kelly_fraction = 0.1

        result = executor.execute(trade)
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], "max_positions_reached")
        mock_clob.post_order.assert_not_called()

    def test_allowed_below_limit(self):
        from src.execution.live_executor import LiveExecutor

        mock_clob = MagicMock()
        mock_clob.post_order.return_value = {"orderID": "ord_789"}
        mock_trade_log = MagicMock()
        mock_trade_log.get_open_positions.return_value = [
            {"market_id": "mkt_0"}
        ]
        mock_trade_log.log_trade.return_value = 5

        executor = LiveExecutor(clob_client=mock_clob, trade_log=mock_trade_log,
                                max_open_positions=3)

        signal = _make_signal(market_id="mkt_new")
        signal.order_type = "GTC"
        trade = MagicMock()
        trade.signal = signal
        trade.size = 1.0
        trade.cost = 0.93
        trade.kelly_fraction = 0.1

        result = executor.execute(trade)
        self.assertEqual(result["status"], "pending")
        mock_clob.post_order.assert_called_once()


class TestOrderManagerCancelOnLimit(unittest.TestCase):
    """OrderManager cancels remaining pending CLOB orders after fills reach the limit."""

    def test_cancels_remaining_after_fill_at_limit(self):
        from src.core.order_manager import OrderManager

        mock_trade_log = MagicMock()
        om = OrderManager(mock_trade_log, max_open_positions=2)

        # Two pending orders
        order_a = {"trade_id": 1, "order_id": "ord_a", "market_id": "mkt_a",
                    "confidence": 0.8}
        order_b = {"trade_id": 2, "order_id": "ord_b", "market_id": "mkt_b",
                    "confidence": 0.8}
        om.track_order(order_a)
        om.track_order(order_b)

        mock_clob = MagicMock()
        # order_a is filled, order_b still open
        mock_clob.get_order.side_effect = lambda oid: (
            {"status": "filled"} if oid == "ord_a" else {"status": "live"}
        )

        mock_executor = MagicMock()
        # After fill, executor reports 1 existing position (the fill adds another)
        mock_executor.get_open_positions.return_value = [{"market_id": "mkt_0"}]

        filled = om.check_pending_orders(mock_clob, mock_executor, paper_mode=False)

        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["order_id"], "ord_a")
        # order_b should have been cancelled (pending list cleared)
        self.assertEqual(len(om.get_pending_orders()), 0)
        # cancel_order called for order_b on the CLOB
        mock_clob.cancel_order.assert_called_once_with("ord_b")
        # trade_log updated to cancelled for order_b
        mock_trade_log.update_trade_status.assert_any_call(2, "cancelled")

    def test_no_cancel_when_below_limit(self):
        from src.core.order_manager import OrderManager

        mock_trade_log = MagicMock()
        om = OrderManager(mock_trade_log, max_open_positions=5)

        order_a = {"trade_id": 1, "order_id": "ord_a", "market_id": "mkt_a",
                    "confidence": 0.8}
        order_b = {"trade_id": 2, "order_id": "ord_b", "market_id": "mkt_b",
                    "confidence": 0.8}
        om.track_order(order_a)
        om.track_order(order_b)

        mock_clob = MagicMock()
        mock_clob.get_order.side_effect = lambda oid: (
            {"status": "filled"} if oid == "ord_a" else {"status": "live"}
        )

        mock_executor = MagicMock()
        mock_executor.get_open_positions.return_value = [{"market_id": "mkt_0"}]

        filled = om.check_pending_orders(mock_clob, mock_executor, paper_mode=False)

        self.assertEqual(len(filled), 1)
        # order_b should still be pending (limit=5, total=2)
        self.assertEqual(len(om.get_pending_orders()), 1)
        mock_clob.cancel_order.assert_not_called()


class TestPerStrategyPositionLimit(unittest.TestCase):
    """Per-strategy position limits with three-dimensional model."""

    def setUp(self):
        self.settings = Settings(
            starting_capital=100.0,
            hard_floor_pct=0.01,
            max_single_trade_pct=0.10,
            max_portfolio_exposure_pct=0.90,
            max_open_positions=20,
            high_prob_max_positions=8,
            btc_updown_max_positions=3,
            safe_compounder_max_positions=3,
            sports_daily_max_positions=4,
            llm_max_positions=2,
            max_long_term_positions=5,
            long_term_threshold_days=7,
            min_trade_size=0.50,
            max_positions_per_market=1,
        )
        self.cb = CircuitBreaker()
        self.cb.set_start_of_day_balance(100.0)
        self.rm = RiskManager(self.settings, self.cb)

    def _positions(self, n, strategy="high_probability", is_long_term=False):
        return [{"market_id": f"mkt_{strategy}_{i}", "cost": 1.0, "strategy": strategy,
                 "is_long_term": 1 if is_long_term else 0}
                for i in range(n)]

    def test_high_prob_blocked_at_8(self):
        """8 high_probability positions → next high_probability signal rejected."""
        signal = _make_signal(market_id="mkt_hp_new", token_id="tok_hp_new", strategy="high_probability")
        positions = self._positions(8, "high_probability")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=8.0)
        self.assertIsNone(result)

    def test_high_prob_allows_other(self):
        """8 high_probability positions → btc_updown signal still allowed."""
        signal = _make_signal(market_id="mkt_btc_new", token_id="tok_btc_new",
                              strategy="btc_updown", price=0.50, confidence=0.70)
        positions = self._positions(8, "high_probability")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=8.0)
        self.assertIsNotNone(result)

    def test_btc_updown_blocked_at_3(self):
        """3 btc_updown positions → next btc_updown signal rejected."""
        signal = _make_signal(market_id="mkt_btc_new", token_id="tok_btc_new",
                              strategy="btc_updown", price=0.50, confidence=0.70)
        positions = self._positions(3, "btc_updown")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=3.0)
        self.assertIsNone(result)

    def test_btc_updown_allows_high_prob(self):
        """3 btc_updown positions → high_probability signal still allowed."""
        signal = _make_signal(market_id="mkt_hp_new", token_id="tok_hp_new", strategy="high_probability")
        positions = self._positions(3, "btc_updown")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=3.0)
        self.assertIsNotNone(result)

    def test_global_cap_at_20(self):
        """20 short-term non-arb positions → any signal rejected (global limit)."""
        signal = _make_signal(market_id="mkt_new", token_id="tok_new", strategy="high_probability")
        positions = self._positions(8, "high_probability") + self._positions(3, "btc_updown") + \
                    self._positions(3, "safe_compounder") + self._positions(4, "sports_daily") + \
                    self._positions(2, "llm_crypto")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=20.0)
        self.assertIsNone(result)


class TestConcurrentSessionPositionLimit(unittest.TestCase):
    """Two PaperExecutor instances sharing the same DB see each other's positions."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.trade_log = TradeLog(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make_approved_trade(self, market_id="mkt_1", token_id="tok_1",
                             price=0.90, size=1.0):
        signal = _make_signal(market_id=market_id, token_id=token_id, price=price)
        trade = MagicMock()
        trade.signal = signal
        trade.size = size
        trade.kelly_fraction = 0.1
        return trade

    def test_two_sessions_share_db_positions(self):
        """Second executor sees positions created by the first via get_open_positions()."""
        exec1 = PaperExecutor(starting_balance=100.0, trade_log=self.trade_log,
                               max_open_positions=5)
        exec2 = PaperExecutor(starting_balance=100.0, trade_log=self.trade_log,
                               max_open_positions=5)

        # Session 1 creates 3 positions
        for i in range(3):
            trade = self._make_approved_trade(market_id=f"mkt_s1_{i}", token_id=f"tok_s1_{i}")
            result = exec1.execute(trade)
            self.assertEqual(result["status"], "filled")

        # Session 2 should see all 3 positions from session 1
        self.assertEqual(len(exec2.get_open_positions()), 3)

        # Session 2 creates 2 more
        for i in range(2):
            trade = self._make_approved_trade(market_id=f"mkt_s2_{i}", token_id=f"tok_s2_{i}")
            result = exec2.execute(trade)
            self.assertEqual(result["status"], "filled")

        # Both sessions see all 5 positions
        self.assertEqual(len(exec1.get_open_positions()), 5)
        self.assertEqual(len(exec2.get_open_positions()), 5)


class TestConcurrentSessionBalance(unittest.TestCase):
    """Two PaperExecutor instances sharing the same DB have consistent balances."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA_SQL)
        self.trade_log = TradeLog(self.conn)

    def tearDown(self):
        self.conn.close()

    def _make_approved_trade(self, market_id="mkt_1", token_id="tok_1",
                             price=0.90, size=1.0):
        signal = _make_signal(market_id=market_id, token_id=token_id, price=price)
        trade = MagicMock()
        trade.signal = signal
        trade.size = size
        trade.kelly_fraction = 0.1
        return trade

    def test_concurrent_sessions_consistent_balance(self):
        """Session A trades, session B's get_balance() reflects the deduction."""
        exec_a = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        exec_b = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)

        self.assertAlmostEqual(exec_a.get_balance(), 10.0)
        self.assertAlmostEqual(exec_b.get_balance(), 10.0)

        # Session A places a trade
        trade = self._make_approved_trade(price=0.90, size=2.0)
        result = exec_a.execute(trade)
        self.assertEqual(result["status"], "filled")

        # Both sessions see the same reduced balance
        self.assertAlmostEqual(exec_a.get_balance(), exec_b.get_balance(), places=2)
        self.assertLess(exec_b.get_balance(), 10.0)

    def test_balance_after_close(self):
        """Balance correctly increases when a position is closed with profit."""
        executor = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)

        trade = self._make_approved_trade(price=0.50, size=2.0)
        result = executor.execute(trade)
        pos_id = result["position_id"]

        balance_before_close = executor.get_balance()

        # Close at a profit (exit_price > entry_price)
        pnl = executor.close_position(pos_id, exit_price=0.80)
        self.assertGreater(pnl, 0)

        balance_after_close = executor.get_balance()
        # Balance should increase: cost returned + pnl
        self.assertGreater(balance_after_close, balance_before_close)

    def test_close_nonexistent_position(self):
        """Closing a non-existent position returns 0.0, balance unchanged."""
        executor = PaperExecutor(starting_balance=10.0, trade_log=self.trade_log)
        pnl = executor.close_position(999, exit_price=1.0)
        self.assertEqual(pnl, 0.0)
        self.assertAlmostEqual(executor.get_balance(), 10.0)


class TestLongTermPositionBucket(unittest.TestCase):
    """Long-term positions use a separate additive bucket."""

    def setUp(self):
        import time
        self.settings = Settings(
            starting_capital=100.0,
            hard_floor_pct=0.01,
            max_single_trade_pct=0.10,
            max_portfolio_exposure_pct=0.90,
            max_open_positions=20,
            high_prob_max_positions=8,
            btc_updown_max_positions=3,
            max_long_term_positions=5,
            long_term_threshold_days=7,
            min_trade_size=0.50,
            max_positions_per_market=1,
        )
        self.cb = CircuitBreaker()
        self.cb.set_start_of_day_balance(100.0)
        self.rm = RiskManager(self.settings, self.cb)
        # Timestamp 30 days in the future (definitely long-term)
        self.far_future = time.time() + 30 * 86400

    def test_long_term_not_counted_against_strategy(self):
        """Positions with is_long_term=1 don't block short-term signals."""
        # 8 long-term high_prob positions
        positions = [{"market_id": f"mkt_lt_{i}", "cost": 1.0, "strategy": "high_probability",
                       "is_long_term": 1} for i in range(8)]
        # Short-term high_prob signal should pass (not counted against per-strategy limit)
        signal = _make_signal(market_id="mkt_new", token_id="tok_new", strategy="high_probability")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=8.0)
        self.assertIsNotNone(result)

    def test_long_term_bucket_blocks_at_limit(self):
        """5 long-term positions → next long-term signal rejected."""
        positions = [{"market_id": f"mkt_lt_{i}", "cost": 1.0, "strategy": "high_probability",
                       "is_long_term": 1} for i in range(5)]
        signal = _make_signal(market_id="mkt_lt_new", token_id="tok_lt_new",
                              strategy="high_probability", resolution_ts=self.far_future)
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=5.0)
        self.assertIsNone(result)

    def test_short_term_allowed_when_long_term_full(self):
        """Short-term signals still pass when long-term bucket is full."""
        positions = [{"market_id": f"mkt_lt_{i}", "cost": 1.0, "strategy": "high_probability",
                       "is_long_term": 1} for i in range(5)]
        # Short-term signal (resolution_ts=0 → short-term)
        signal = _make_signal(market_id="mkt_st_new", token_id="tok_st_new", strategy="high_probability")
        result = self.rm.evaluate(signal, balance=100.0, open_positions=positions, portfolio_exposure=5.0)
        self.assertIsNotNone(result)


class TestHighProbFillTimeEnforcement(unittest.TestCase):
    """high_probability uses fill-time enforcement: pending orders don't count
    against per-strategy limits, but remaining pending orders are cancelled
    when fills push the strategy to its position limit."""

    def test_hp_pending_not_counted_in_positions_for_risk(self):
        """HP pending orders excluded from positions_for_risk in engine dedup logic."""
        open_positions = [{"market_id": "mkt_0", "strategy": "high_probability", "cost": 1.0}]
        pending_orders = [
            {"market_id": "mkt_1", "strategy": "high_probability"},
            {"market_id": "mkt_2", "strategy": "high_probability"},
            {"market_id": "mkt_3", "strategy": "btc_updown"},
        ]

        # Engine logic: exclude HP from positions_for_risk
        positions_for_risk = open_positions + [
            {"market_id": o.get("market_id", ""), "strategy": o.get("strategy", "unknown"),
             "is_long_term": 0, "cost": 0}
            for o in pending_orders
            if o.get("strategy") != "high_probability"
        ]

        # Only 1 DB position + 1 btc_updown pending = 2 (HP pending excluded)
        self.assertEqual(len(positions_for_risk), 2)
        strategies = [p["strategy"] for p in positions_for_risk]
        self.assertNotIn("high_probability", [s for s in strategies if s != "high_probability"]
                         or strategies)
        # HP pending still counted for per-market dedup
        from collections import Counter
        market_pos_count = Counter(p["market_id"] for p in open_positions if p.get("market_id"))
        for o in pending_orders:
            if o.get("market_id"):
                market_pos_count[o["market_id"]] += 1
        self.assertEqual(market_pos_count["mkt_1"], 1)  # HP pending blocks same market

    def test_other_strategy_pending_still_counted(self):
        """Non-HP pending orders still count against per-strategy limits."""
        pending_orders = [
            {"market_id": "mkt_1", "strategy": "btc_updown"},
            {"market_id": "mkt_2", "strategy": "btc_updown"},
            {"market_id": "mkt_3", "strategy": "high_probability"},
        ]

        positions_for_risk = [
            {"market_id": o.get("market_id", ""), "strategy": o.get("strategy", "unknown"),
             "is_long_term": 0, "cost": 0}
            for o in pending_orders
            if o.get("strategy") != "high_probability"
        ]

        # 2 btc_updown pending counted, HP excluded
        self.assertEqual(len(positions_for_risk), 2)
        self.assertTrue(all(p["strategy"] == "btc_updown" for p in positions_for_risk))

    def test_order_manager_cancels_hp_on_fill_at_limit(self):
        """When HP fills reach the limit, remaining HP pending orders are cancelled."""
        from src.core.order_manager import OrderManager

        mock_trade_log = MagicMock()
        om = OrderManager(mock_trade_log, max_open_positions=20,
                          strategy_limits={"high_probability": 2})

        # 3 HP pending orders
        for i in range(3):
            om.track_order({
                "trade_id": i + 1, "order_id": f"ord_{i}",
                "market_id": f"mkt_{i}", "strategy": "high_probability",
                "confidence": 0.8,
            })

        mock_clob = MagicMock()
        # First 2 fill, third stays live
        mock_clob.get_order.side_effect = lambda oid: (
            {"status": "filled"} if oid in ("ord_0", "ord_1") else {"status": "live"}
        )

        mock_executor = MagicMock()
        # After fills: 2 HP positions in DB
        mock_executor.get_open_positions.return_value = [
            {"market_id": "mkt_0", "strategy": "high_probability", "is_long_term": 0},
            {"market_id": "mkt_1", "strategy": "high_probability", "is_long_term": 0},
        ]

        filled = om.check_pending_orders(mock_clob, mock_executor, paper_mode=False)

        self.assertEqual(len(filled), 2)
        # ord_2 should be cancelled (HP at limit)
        self.assertEqual(len(om.get_pending_orders()), 0)
        mock_clob.cancel_order.assert_called_once_with("ord_2")
        mock_trade_log.update_trade_status.assert_any_call(3, "cancelled")

    def test_order_manager_no_cancel_below_limit(self):
        """HP pending orders kept when fills haven't reached the limit."""
        from src.core.order_manager import OrderManager

        mock_trade_log = MagicMock()
        om = OrderManager(mock_trade_log, max_open_positions=20,
                          strategy_limits={"high_probability": 8})

        for i in range(3):
            om.track_order({
                "trade_id": i + 1, "order_id": f"ord_{i}",
                "market_id": f"mkt_{i}", "strategy": "high_probability",
                "confidence": 0.8,
            })

        mock_clob = MagicMock()
        mock_clob.get_order.side_effect = lambda oid: (
            {"status": "filled"} if oid == "ord_0" else {"status": "live"}
        )

        mock_executor = MagicMock()
        mock_executor.get_open_positions.return_value = [
            {"market_id": "mkt_0", "strategy": "high_probability", "is_long_term": 0},
        ]

        filled = om.check_pending_orders(mock_clob, mock_executor, paper_mode=False)

        self.assertEqual(len(filled), 1)
        # 2 remaining HP pending orders should stay (only 1/8 filled)
        self.assertEqual(len(om.get_pending_orders()), 2)
        mock_clob.cancel_order.assert_not_called()

    def test_non_hp_strategy_not_affected(self):
        """strategy_limits only cancels the strategy that hit its limit."""
        from src.core.order_manager import OrderManager

        mock_trade_log = MagicMock()
        om = OrderManager(mock_trade_log, max_open_positions=20,
                          strategy_limits={"high_probability": 2})

        om.track_order({"trade_id": 1, "order_id": "ord_hp", "market_id": "mkt_hp",
                         "strategy": "high_probability", "confidence": 0.8})
        om.track_order({"trade_id": 2, "order_id": "ord_btc", "market_id": "mkt_btc",
                         "strategy": "btc_updown", "confidence": 0.8})

        mock_clob = MagicMock()
        mock_clob.get_order.side_effect = lambda oid: {"status": "filled"}

        mock_executor = MagicMock()
        mock_executor.get_open_positions.return_value = [
            {"market_id": "mkt_hp", "strategy": "high_probability", "is_long_term": 0},
            {"market_id": "mkt_existing_hp", "strategy": "high_probability", "is_long_term": 0},
            {"market_id": "mkt_btc", "strategy": "btc_updown", "is_long_term": 0},
        ]

        filled = om.check_pending_orders(mock_clob, mock_executor, paper_mode=False)

        self.assertEqual(len(filled), 2)
        # No pending orders left (both filled), no extra cancellations
        self.assertEqual(len(om.get_pending_orders()), 0)


class TestStrategyAwarePerMarketLimit(unittest.TestCase):
    """Per-market limits vary by strategy."""

    def setUp(self):
        self.settings = Settings(
            starting_capital=100.0,
            hard_floor_pct=0.01,
            max_single_trade_pct=0.10,
            max_portfolio_exposure_pct=0.90,
            max_open_positions=20,
            safe_compounder_max_positions=3,
            sports_daily_max_positions=4,
            btc_updown_max_positions=3,
            min_trade_size=0.50,
            max_positions_per_market=1,
        )
        self.cb = CircuitBreaker()
        self.cb.set_start_of_day_balance(100.0)
        self.rm = RiskManager(self.settings, self.cb)

    def test_safe_compounder_dual_side_per_market(self):
        """safe_compounder can have 2 positions on the same market (dual-side quoting)."""
        # First position on market
        existing = [{"market_id": "mkt_crypto_1", "cost": 0.5, "strategy": "safe_compounder"}]
        # Second signal on same market should pass (per-market limit = 2)
        signal = _make_signal(market_id="mkt_crypto_1", token_id="tok_2",
                              strategy="safe_compounder", outcome="No", price=0.10, confidence=0.80)
        result = self.rm.evaluate(signal, balance=100.0, open_positions=existing, portfolio_exposure=0.5)
        self.assertIsNotNone(result)

    def test_safe_compounder_blocked_at_third(self):
        """safe_compounder is still capped at 2 per market."""
        existing = [
            {"market_id": "mkt_crypto_1", "cost": 0.5, "strategy": "safe_compounder"},
            {"market_id": "mkt_crypto_1", "cost": 0.5, "strategy": "safe_compounder"},
        ]
        signal = _make_signal(market_id="mkt_crypto_1", token_id="tok_3",
                              strategy="safe_compounder", outcome="Yes", price=0.10, confidence=0.80)
        result = self.rm.evaluate(signal, balance=100.0, open_positions=existing, portfolio_exposure=1.0)
        self.assertIsNone(result)

    def test_sports_daily_both_outcomes_per_market(self):
        """sports_daily can have 2 positions on same market (both outcomes)."""
        existing = [{"market_id": "mkt_sport_1", "cost": 0.5, "strategy": "sports_daily"}]
        signal = _make_signal(market_id="mkt_sport_1", token_id="tok_2",
                              strategy="sports_daily", outcome="No", price=0.20, confidence=0.70)
        result = self.rm.evaluate(signal, balance=100.0, open_positions=existing, portfolio_exposure=0.5)
        self.assertIsNotNone(result)

    def test_btc_updown_single_per_market(self):
        """btc_updown is limited to 1 per market (default)."""
        existing = [{"market_id": "mkt_btc_1", "cost": 0.5, "strategy": "btc_updown"}]
        signal = _make_signal(market_id="mkt_btc_1", token_id="tok_2",
                              strategy="btc_updown", outcome="No", price=0.50, confidence=0.70)
        result = self.rm.evaluate(signal, balance=100.0, open_positions=existing, portfolio_exposure=0.5)
        self.assertIsNone(result)


class TestUnifiedDbPath(unittest.TestCase):
    """All strategy configurations should use the same DB path."""

    def test_db_path_ignores_only_strategies(self):
        s1 = Settings(only_strategies=["btc_updown"])
        s2 = Settings(only_strategies=["high_probability", "arbitrage"])
        s3 = Settings(only_strategies=[])
        self.assertEqual(s1.db_path, s2.db_path)
        self.assertEqual(s2.db_path, s3.db_path)
        self.assertEqual(str(s3.db_path), "data/poly_trade.db")


if __name__ == "__main__":
    unittest.main()
