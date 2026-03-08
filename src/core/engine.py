import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
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
from src.market_data.gamma_client import GammaClient

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
        self._gamma = GammaClient()

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

        # 2. ANALYZE - collect signals from enabled strategies
        enabled = self.aggression_tuner.get_enabled_strategies()
        if self.settings.only_strategies:
            enabled = [s for s in enabled if s in self.settings.only_strategies]

        logger.info(f"Cycle #{self._cycle_count} | strategies={enabled} | balance=${balance:.2f}")

        # 1. SCAN - skip if only self-discovering strategies are enabled
        needs_scanner = any(
            not self.strategies[n].self_discovering
            for n in enabled if n in self.strategies
        )
        if needs_scanner:
            markets = self.scanner.scan()
            if not markets:
                markets = []
        else:
            markets = []
        all_signals: list[TradeSignal] = []

        # Run strategies concurrently
        runnable = [
            (name, self.strategies[name])
            for name in enabled
            if name in self.strategies and self.strategies[name].enabled
        ]

        def _run_strategy(name: str, strategy: Strategy) -> tuple[str, list[TradeSignal]]:
            return name, strategy.analyze(markets, self.clob)

        if len(runnable) > 1:
            with ThreadPoolExecutor(max_workers=len(runnable)) as pool:
                futures = {
                    pool.submit(_run_strategy, name, strat): name
                    for name, strat in runnable
                }
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        _, signals = future.result()
                        all_signals.extend(signals)
                    except Exception as e:
                        logger.warning(f"Strategy '{name}' failed: {e}")
        elif runnable:
            name, strat = runnable[0]
            all_signals.extend(strat.analyze(markets, self.clob))

        # Log predictions from strategies that support tracking
        for name, strategy in runnable:
            if hasattr(strategy, '_pending_predictions') and strategy._pending_predictions:
                for pred in strategy._pending_predictions:
                    try:
                        self.trade_log.log_prediction(pred)
                    except Exception as e:
                        logger.debug(f"Failed to log prediction: {e}")

        # Resolve past predictions and open positions (every 5th cycle)
        if self._cycle_count % 5 == 0:
            self._resolve_predictions()
            self._resolve_positions()

        if not all_signals:
            logger.info("No trade signals this cycle")
            return

        # 3. RANK by expected value
        all_signals.sort(key=lambda s: s.expected_value, reverse=True)

        # 4. RISK + 5. EXECUTE
        open_positions = self.executor.get_open_positions()
        exposure = self.balance_mgr.portfolio_exposure(open_positions)

        # Build per-market position count (open positions + pending orders)
        market_pos_count = Counter(p["market_id"] for p in open_positions if p.get("market_id"))
        for o in self.order_manager.get_pending_orders():
            if o.get("market_id"):
                market_pos_count[o["market_id"]] += 1

        for signal in all_signals:
            # Duplicate market check
            max_for_market = 2 if signal.strategy == "arbitrage" else self.settings.max_positions_per_market
            if market_pos_count.get(signal.market_id, 0) >= max_for_market:
                logger.debug(f"SKIP duplicate market: {signal.market_question[:50]}")
                continue

            approved = self.risk_manager.evaluate(signal, balance, open_positions, exposure)
            if approved is None:
                continue

            result = self.executor.execute(approved)
            if result.get("status") in ("filled", "pending"):
                market_pos_count[signal.market_id] += 1
                if result.get("status") == "pending":
                    self.order_manager.track_order(result)
                # Update running state
                balance = self.executor.get_balance()
                self.balance_mgr.update(balance)
                open_positions = self.executor.get_open_positions()
                exposure = self.balance_mgr.portfolio_exposure(open_positions)

        # Check pending maker orders for fills/timeouts
        if self.order_manager.get_pending_orders():
            self.order_manager.check_pending_orders(
                self.clob, self.executor,
                paper_mode=self.settings.paper_trading,
            )
            balance = self.executor.get_balance()
            self.balance_mgr.update(balance)

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

    def _resolve_positions(self):
        """Check open positions against resolved markets. Close with PnL."""
        try:
            positions = self.position_tracker.get_open_positions()
        except Exception:
            return

        if not positions:
            return

        resolved_count = 0
        for pos in positions:
            market_id = pos.get("market_id", "")
            if not market_id:
                continue

            try:
                result = self._gamma.get_market_resolution(market_id)
            except Exception as e:
                logger.debug(f"Failed to check resolution for position #{pos['id']}: {e}")
                continue

            if result is None or not result.get("resolved"):
                continue

            winning_outcome = result.get("winning_outcome")
            if winning_outcome is None:
                continue

            # Determine win/loss
            pos_outcome = pos.get("outcome", "")
            won = pos_outcome.lower() == winning_outcome.lower()
            entry_price = pos.get("entry_price", 0)
            size = pos.get("size", 0)

            if won:
                pnl = size * (1.0 - entry_price)  # pays $1, cost was entry_price
            else:
                pnl = -(size * entry_price)  # lose the cost

            # Close the position
            self.position_tracker.close_position(pos["id"], pnl)

            # Update paper balance
            if self.settings.paper_trading and self.executor.paper:
                if won:
                    self.executor.paper.balance += size  # receive $1 per share
                # On loss: cost already deducted at fill time

            # Update the trade record's PnL
            try:
                trade = self.trade_log.get_trade_for_position(market_id, pos.get("token_id", ""))
                if trade:
                    self.trade_log.update_trade_status(trade["id"], "filled", pnl=pnl)
            except Exception:
                pass

            # Record win/loss for circuit breaker
            if won:
                self.circuit_breaker.record_win()
            else:
                self.circuit_breaker.record_loss()

            resolved_count += 1
            logger.info(
                f"POSITION RESOLVED: #{pos['id']} {pos_outcome} | "
                f"{'WIN' if won else 'LOSS'} | PnL=${pnl:+.2f} | "
                f"entry=${entry_price:.3f} size={size:.2f}"
            )

        if resolved_count > 0:
            balance = self.executor.get_balance()
            self.balance_mgr.update(balance)
            logger.info(f"RESOLVED {resolved_count} positions | balance=${balance:.2f}")

    def _resolve_predictions(self):
        """Check unresolved predictions past their resolution_ts and update outcomes."""
        try:
            unresolved = self.trade_log.get_unresolved_predictions()
        except Exception:
            return

        now = time.time()
        resolved_count = 0
        for pred in unresolved:
            resolution_ts = pred.get("resolution_ts")
            if resolution_ts is None or now < resolution_ts + 30:
                continue

            market_id = pred["market_id"]
            try:
                result = self._gamma.get_market_resolution(market_id)
            except Exception as e:
                logger.debug(f"Failed to resolve prediction {pred['id']}: {e}")
                continue

            if result is None or not result.get("resolved"):
                continue

            winning_outcome = result.get("winning_outcome")
            if winning_outcome is None:
                continue

            actual_correct = pred["outcome"].lower() == winning_outcome.lower()
            pnl = None
            if pred.get("traded") and pred.get("bid_price") is not None:
                bid = pred["bid_price"]
                pnl = (1.0 - bid) if actual_correct else -bid

            self.trade_log.resolve_prediction(pred["id"], actual_correct, pnl)
            resolved_count += 1

        if resolved_count > 0:
            logger.info(f"PREDICTIONS | resolved {resolved_count} predictions")

        # Log calibration every 100 cycles
        if self._cycle_count % 100 == 0:
            self._log_calibration()

    def _log_calibration(self):
        try:
            stats = self.trade_log.get_calibration_stats()
        except Exception:
            return
        if not stats:
            return
        for bucket, data in stats.items():
            logger.info(
                f"CALIBRATION | bucket={bucket} | n={data['count']} | "
                f"wins={data['wins']} | rate={data['win_rate']:.0%} | "
                f"avg_pred={data['avg_prob']:.2f}"
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
