import time
import logging
from datetime import datetime, timezone
from src.config.settings import Settings
from src.market_data.market_scanner import MarketScanner
from src.market_data.clob_client import PolymarketClobClient
from src.risk.risk_manager import RiskManager, TradeSignal
from src.risk.circuit_breaker import CircuitBreaker
from src.execution.executor import Executor
from src.adaptive.goal_tracker import GoalTracker
from src.adaptive.aggression_tuner import AggressionTuner
from src.core.balance_manager import BalanceManager
from src.core.position_tracker import PositionTracker
from src.core.order_manager import OrderManager
from src.storage.trade_log import TradeLog
from src.strategies.base import Strategy

logger = logging.getLogger("poly-trade")


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        scanner: MarketScanner,
        clob_client: PolymarketClobClient,
        strategies: list[Strategy],
        risk_manager: RiskManager,
        circuit_breaker: CircuitBreaker,
        executor: Executor,
        goal_tracker: GoalTracker,
        aggression_tuner: AggressionTuner,
        balance_manager: BalanceManager,
        position_tracker: PositionTracker,
        order_manager: OrderManager,
        trade_log: TradeLog,
    ):
        self.settings = settings
        self.scanner = scanner
        self.clob = clob_client
        self.strategies = {s.name: s for s in strategies}
        self.risk_manager = risk_manager
        self.circuit_breaker = circuit_breaker
        self.executor = executor
        self.goal_tracker = goal_tracker
        self.aggression_tuner = aggression_tuner
        self.balance_mgr = balance_manager
        self.position_tracker = position_tracker
        self.order_manager = order_manager
        self.trade_log = trade_log

        self._running = False
        self._last_adapt_time = 0.0
        self._cycle_count = 0

    def start(self):
        self._running = True
        balance = self.executor.get_balance()
        self.balance_mgr.update(balance)
        self.circuit_breaker.set_start_of_day_balance(balance)
        self.goal_tracker.record_balance(balance)

        logger.info(f"Engine started | mode={self.executor.mode} | balance=${balance:.2f}")

        while self._running:
            try:
                self._cycle()
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt, stopping engine")
                self.stop()
                break
            except Exception as e:
                logger.error(f"Engine cycle error: {e}", exc_info=True)
                self.circuit_breaker.record_api_error()

            time.sleep(self.settings.scan_interval_seconds)

    def stop(self):
        self._running = False
        self.order_manager.cancel_all_pending()
        balance = self.executor.get_balance()
        self._save_daily_snapshot(balance)
        logger.info(f"Engine stopped | balance=${balance:.2f}")

    def _cycle(self):
        self._cycle_count += 1

        # Check circuit breaker
        if self.circuit_breaker.is_paused:
            remaining = self.circuit_breaker.pause_remaining_seconds
            if remaining < float("inf"):
                logger.debug(f"Paused for {remaining:.0f}s more")
            return

        # Update balance
        balance = self.executor.get_balance()
        prev_balance = self.balance_mgr.balance
        self.balance_mgr.update(balance)

        # Catastrophic drop check
        if not self.circuit_breaker.check_catastrophic_drop(balance, prev_balance):
            return

        # 1. SCAN
        markets = self.scanner.scan()
        if not markets:
            logger.debug("No tradeable markets found")
            return

        # 2. ANALYZE - collect signals from enabled strategies
        enabled = self.aggression_tuner.get_enabled_strategies()
        all_signals: list[TradeSignal] = []
        for name in enabled:
            strategy = self.strategies.get(name)
            if strategy and strategy.enabled:
                signals = strategy.analyze(markets, self.clob)
                all_signals.extend(signals)

        if not all_signals:
            logger.debug("No trade signals generated")
            return

        # 3. RANK by expected value
        all_signals.sort(key=lambda s: s.expected_value, reverse=True)

        # 4. RISK + 5. EXECUTE
        open_positions = self.executor.get_open_positions()
        exposure = self.balance_mgr.portfolio_exposure(open_positions)

        for signal in all_signals:
            approved = self.risk_manager.evaluate(signal, balance, open_positions, exposure)
            if approved is None:
                continue

            result = self.executor.execute(approved)
            if result.get("status") in ("filled", "pending"):
                if result.get("status") == "pending":
                    self.order_manager.track_order(result)
                # Update running state
                balance = self.executor.get_balance()
                self.balance_mgr.update(balance)
                open_positions = self.executor.get_open_positions()
                exposure = self.balance_mgr.portfolio_exposure(open_positions)

        # 6. ADAPT (every adapt_interval)
        now = time.time()
        if now - self._last_adapt_time >= self.settings.adapt_interval_seconds:
            self._adapt(balance)
            self._last_adapt_time = now

        if self._cycle_count % 10 == 0:
            logger.info(
                f"Cycle #{self._cycle_count} | balance=${balance:.2f} | "
                f"positions={len(open_positions)} | aggression={self.aggression_tuner.current_level}"
            )

    def _adapt(self, balance: float):
        self.goal_tracker.record_balance(balance)
        level = self.aggression_tuner.update(balance)
        status = self.goal_tracker.get_status(balance)
        logger.info(
            f"ADAPT | progress={status.progress_pct:.1f}% | "
            f"required_daily={status.required_daily_rate:.2%} | "
            f"actual_7d={status.actual_7day_rate:.2%} | level={level}"
        )

    def _save_daily_snapshot(self, balance: float):
        today_trades = self.trade_log.get_today_trades()
        wins = sum(1 for t in today_trades if (t.get("pnl") or 0) > 0)
        losses = sum(1 for t in today_trades if (t.get("pnl") or 0) < 0)
        total_pnl = sum(t.get("pnl", 0) or 0 for t in today_trades)

        self.trade_log.save_daily_snapshot({
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "balance": balance,
            "portfolio_value": balance + self.position_tracker.total_exposure(),
            "total_pnl": total_pnl,
            "trades_count": len(today_trades),
            "wins": wins,
            "losses": losses,
            "daily_return_pct": total_pnl / self.settings.starting_capital * 100 if self.settings.starting_capital > 0 else 0,
            "aggression_level": self.aggression_tuner.current_level,
        })
