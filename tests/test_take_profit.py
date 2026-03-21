"""Tests for take-profit sell feature."""
import time
from unittest.mock import MagicMock, patch
import pytest


def make_trade_log():
    """Create a mock trade log with basic position tracking."""
    trade_log = MagicMock()
    trade_log.log_trade.return_value = 1
    trade_log.compute_paper_balance.return_value = 10.0
    return trade_log


class TestPaperSellPosition:
    def test_sell_closes_position_with_profit(self):
        from src.execution.paper_executor import PaperExecutor

        trade_log = make_trade_log()
        executor = PaperExecutor(starting_balance=10.0, trade_log=trade_log)

        position = {
            "id": 1, "market_id": "m1", "token_id": "t1",
            "outcome": "Down", "entry_price": 0.50, "size": 5.0,
            "strategy": "btc_updown", "market_question": "BTC?",
            "resolution_ts": time.time() + 3600,
        }

        result = executor.sell_position(position, sell_price=0.70)

        assert result["status"] == "filled"
        assert result["pnl"] > 0  # sold above entry
        trade_log.close_position.assert_called_once_with(1, pytest.approx(result["pnl"]))
        # Verify a SELL trade was logged
        trade_log.log_trade.assert_called_once()
        logged = trade_log.log_trade.call_args[0][0]
        assert logged["side"] == "SELL"
        assert logged["notes"] == "take_profit_sell"

    def test_sell_applies_slippage(self):
        from src.execution.paper_executor import PaperExecutor

        trade_log = make_trade_log()
        executor = PaperExecutor(starting_balance=10.0, trade_log=trade_log)

        position = {
            "id": 1, "market_id": "m1", "token_id": "t1",
            "outcome": "Up", "entry_price": 0.40, "size": 5.0,
            "strategy": "btc_updown", "market_question": "ETH?",
            "resolution_ts": 0,
        }

        result = executor.sell_position(position, sell_price=0.60)
        assert result["status"] == "filled"
        logged = trade_log.log_trade.call_args[0][0]
        # fill_price should be slightly below sell_price due to slippage
        assert logged["fill_price"] < 0.60


class TestLiveSellPosition:
    def test_sell_posts_fok_sell_order(self):
        from src.execution.live_executor import LiveExecutor

        trade_log = make_trade_log()
        clob = MagicMock()
        clob.post_order.return_value = {"orderID": "abc"}
        executor = LiveExecutor(clob_client=clob, trade_log=trade_log)

        position = {
            "id": 1, "market_id": "m1", "token_id": "t1",
            "outcome": "Down", "entry_price": 0.50, "size": 5.0,
            "strategy": "btc_updown", "market_question": "BTC?",
            "resolution_ts": 0,
        }

        result = executor.sell_position(position, sell_price=0.70)

        assert result["status"] == "filled"
        assert result["pnl"] == pytest.approx(1.0)  # (0.70 - 0.50) * 5
        clob.post_order.assert_called_once_with(
            token_id="t1", side="SELL", price=0.70, size=5.0, order_type="FOK",
        )
        trade_log.close_position.assert_called_once()

    def test_sell_handles_clob_error(self):
        from src.execution.live_executor import LiveExecutor

        trade_log = make_trade_log()
        clob = MagicMock()
        clob.post_order.side_effect = Exception("FOK rejected")
        executor = LiveExecutor(clob_client=clob, trade_log=trade_log)

        position = {
            "id": 1, "market_id": "m1", "token_id": "t1",
            "outcome": "Up", "entry_price": 0.50, "size": 5.0,
            "strategy": "btc_updown", "market_question": "Q?",
            "resolution_ts": 0,
        }

        result = executor.sell_position(position, sell_price=0.70)
        assert result["status"] == "error"
        trade_log.close_position.assert_not_called()


class TestExecutorFacade:
    def test_routes_to_paper(self):
        from src.execution.executor import Executor
        from src.config.settings import Settings

        settings = MagicMock(spec=Settings)
        settings.paper_trading = True
        paper = MagicMock()
        paper.sell_position.return_value = {"status": "filled", "pnl": 1.0}

        executor = Executor(settings=settings, paper=paper)
        result = executor.sell_position({"id": 1}, 0.70)

        assert result["status"] == "filled"
        paper.sell_position.assert_called_once()

    def test_routes_to_live(self):
        from src.execution.executor import Executor
        from src.config.settings import Settings

        settings = MagicMock(spec=Settings)
        settings.paper_trading = False
        live = MagicMock()
        live.sell_position.return_value = {"status": "filled", "pnl": 1.0}

        executor = Executor(settings=settings, live=live)
        result = executor.sell_position({"id": 1}, 0.70)

        assert result["status"] == "filled"
        live.sell_position.assert_called_once()


class TestEngineTakeProfit:
    def _make_engine(self, positions, prices, tp_pct=0.30):
        """Build a minimal mock engine for take-profit testing."""
        from src.core.engine import TradingEngine

        settings = MagicMock()
        settings.take_profit_enabled = True
        settings.take_profit_pct = tp_pct
        settings.take_profit_strategies = ["btc_updown"]
        settings.take_profit_min_bid = 0.02

        engine = TradingEngine.__new__(TradingEngine)
        engine.settings = settings
        engine.position_tracker = MagicMock()
        engine.position_tracker.get_open_positions.return_value = positions
        engine.clob = MagicMock()
        engine.clob.get_price.side_effect = lambda tid: prices.get(tid)
        engine.executor = MagicMock()
        engine.executor.sell_position.return_value = {"status": "filled", "pnl": 1.0}
        engine.executor.get_balance.return_value = 15.0
        engine.circuit_breaker = MagicMock()
        engine.balance_mgr = MagicMock()
        engine._cycle_count = 1
        engine._asset_cooldowns = {}
        return engine

    def test_sells_when_gain_exceeds_threshold(self):
        positions = [{
            "id": 1, "token_id": "t1", "entry_price": 0.50, "size": 5.0,
            "outcome": "Down", "strategy": "btc_updown",
            "market_question": "BTC Up or Down",
        }]
        prices = {"t1": {"bid": 0.70, "ask": 0.80}}  # 40% gain

        engine = self._make_engine(positions, prices)
        engine._check_take_profit()

        engine.executor.sell_position.assert_called_once()
        engine.circuit_breaker.record_win.assert_called_once()

    def test_skips_when_below_threshold(self):
        positions = [{
            "id": 1, "token_id": "t1", "entry_price": 0.50, "size": 5.0,
            "outcome": "Down", "strategy": "btc_updown",
            "market_question": "BTC Up or Down",
        }]
        prices = {"t1": {"bid": 0.60, "ask": 0.65}}  # 20% gain < 30%

        engine = self._make_engine(positions, prices)
        engine._check_take_profit()

        engine.executor.sell_position.assert_not_called()

    def test_skips_non_eligible_strategy(self):
        positions = [{
            "id": 1, "token_id": "t1", "entry_price": 0.50, "size": 5.0,
            "outcome": "Yes", "strategy": "sports_daily",
            "market_question": "Lakers win?",
        }]
        prices = {"t1": {"bid": 0.90, "ask": 0.95}}  # 80% gain but wrong strategy

        engine = self._make_engine(positions, prices)
        engine._check_take_profit()

        engine.executor.sell_position.assert_not_called()

    def test_skips_thin_bid(self):
        positions = [{
            "id": 1, "token_id": "t1", "entry_price": 0.50, "size": 5.0,
            "outcome": "Down", "strategy": "btc_updown",
            "market_question": "BTC?",
        }]
        prices = {"t1": {"bid": 0.01, "ask": 0.99}}  # dead book

        engine = self._make_engine(positions, prices)
        engine._check_take_profit()

        engine.executor.sell_position.assert_not_called()

    def test_sets_asset_cooldown_after_tp(self):
        positions = [{
            "id": 1, "token_id": "t1", "entry_price": 0.50, "size": 5.0,
            "outcome": "Down", "strategy": "btc_updown",
            "market_question": "BTC Up or Down",
        }]
        prices = {"t1": {"bid": 0.70, "ask": 0.80}}

        engine = self._make_engine(positions, prices)
        engine._check_take_profit()

        assert "BTC" in engine._asset_cooldowns
        assert engine._asset_cooldowns["BTC"] == 2  # cycle 1 + 1
