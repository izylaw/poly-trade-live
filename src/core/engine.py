import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
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
        self._asset_cooldowns: dict[str, int] = {}  # asset → cycle when cooldown expires

    def start(self):
        self._running = True
        balance = self.executor.get_balance()
        self.balance_mgr.update(balance)
        self.circuit_breaker.set_start_of_day_balance(balance)
        self.goal_tracker.record_balance(balance)

        # Startup cleanup: expire stale pending orders from DB
        self._cleanup_stale_db_orders()

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

        # Clean up stale pending orders whose markets have resolved
        self.order_manager.cleanup_expired_orders()

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

        # Take-profit check
        if self.settings.take_profit_enabled:
            self._check_take_profit()
            # Refresh balance — take-profit sells may have changed it
            balance = self.executor.get_balance()
            self.balance_mgr.update(balance)

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
                        pred["paper_trade"] = self.settings.paper_trading
                        self.trade_log.log_prediction(pred)
                    except Exception as e:
                        logger.debug(f"Failed to log prediction: {e}")

        # Resolve past predictions and open positions (first cycle + every 5th)
        if self._cycle_count == 1 or self._cycle_count % 5 == 0:
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
        # Include pending orders as pseudo-positions so per-strategy + global
        # limits in risk_manager count them (pending orders haven't created
        # DB positions yet, but they occupy slots).
        # Exception: high_probability uses fill-time enforcement — pending HP
        # orders don't count against per-strategy limits (more orderbook coverage),
        # but still count for per-market dedup (no duplicate market bids).
        pending_orders = self.order_manager.get_pending_orders()
        market_pos_count = Counter(p["market_id"] for p in open_positions if p.get("market_id"))
        for o in pending_orders:
            if o.get("market_id"):
                market_pos_count[o["market_id"]] += 1

        positions_for_risk = open_positions + [
            {"market_id": o.get("market_id", ""), "strategy": o.get("strategy", "unknown"),
             "is_long_term": 0, "cost": o.get("cost", 0)}
            for o in pending_orders
            if o.get("strategy") != "high_probability"
        ]

        # Build per-asset position count for diversification
        asset_pos_count: Counter = Counter()
        for p in open_positions:
            a = p.get("asset", "")
            if a:
                asset_pos_count[a] += 1
        for o in pending_orders:
            a = o.get("asset", "")
            if a:
                asset_pos_count[a] += 1

        max_signals = self.settings.max_signals_per_cycle

        executed_count = 0
        for signal in all_signals:
            # Max signals per cycle limit (0 = unlimited)
            if max_signals > 0 and executed_count >= max_signals:
                logger.info(f"SKIP remaining signals — max_signals_per_cycle={max_signals} reached")
                break

            # Duplicate market check (strategy-aware)
            max_for_market = RiskManager.STRATEGY_MAX_PER_MARKET.get(signal.strategy, self.settings.max_positions_per_market)
            if market_pos_count.get(signal.market_id, 0) >= max_for_market:
                logger.debug(f"SKIP duplicate market: {signal.market_question[:50]}")
                continue

            # Per-asset diversification: max N positions per asset
            signal_asset = getattr(signal, 'asset', '')
            if signal_asset:
                max_per_asset = self.settings.max_positions_per_asset
                if asset_pos_count.get(signal_asset, 0) >= max_per_asset:
                    logger.info(f"SKIP {signal_asset} — already {max_per_asset} position(s) on this asset")
                    continue

            # Per-asset cooldown after loss/win
            if signal_asset and signal_asset in self._asset_cooldowns:
                if self._cycle_count < self._asset_cooldowns[signal_asset]:
                    remaining = self._asset_cooldowns[signal_asset] - self._cycle_count
                    logger.info(f"SKIP {signal_asset} — cooldown for {remaining} more cycle(s)")
                    continue

            approved = self.risk_manager.evaluate(signal, balance, positions_for_risk, exposure)
            if approved is None:
                continue

            result = self.executor.execute(approved)
            if result.get("status") in ("filled", "pending"):
                executed_count += 1
                market_pos_count[signal.market_id] += 1
                if signal_asset:
                    asset_pos_count[signal_asset] += 1
                cost = getattr(approved, 'cost', 0)
                positions_for_risk.append(
                    {"market_id": signal.market_id, "strategy": signal.strategy,
                     "is_long_term": 0, "cost": cost}
                )
                if result.get("status") == "pending":
                    self.order_manager.track_order(result)
                elif result.get("status") == "filled":
                    # Deduct estimated cost from balance for risk checks
                    balance -= cost
                    exposure = self.balance_mgr.portfolio_exposure(positions_for_risk)

        # Refresh actual state once after all executions
        if executed_count > 0:
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
                result = self._gamma.get_market_resolution(market_id, token_id=pos.get("token_id", ""))
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

            # Per-asset cooldown: 2 cycles after win, 5 after loss
            pos_asset = self._extract_asset_from_position(pos)
            if pos_asset:
                cooldown_cycles = 2 if won else 5
                self._asset_cooldowns[pos_asset] = self._cycle_count + cooldown_cycles
                logger.info(f"Asset cooldown: {pos_asset} for {cooldown_cycles} cycles ({'win' if won else 'loss'})")

            resolved_count += 1
            logger.info(
                f"POSITION RESOLVED: #{pos['id']} {pos_outcome} | "
                f"{'WIN' if won else 'LOSS'} | PnL=${pnl:+.2f} | "
                f"entry=${entry_price:.3f} size={size:.2f}"
            )

            # Auto-redeem winning positions on-chain (skip losses to save gas)
            if not self.settings.paper_trading and won:
                self.clob.redeem_positions(market_id)

        if resolved_count > 0:
            balance = self.executor.get_balance()
            self.balance_mgr.update(balance)
            logger.info(f"RESOLVED {resolved_count} positions | balance=${balance:.2f}")

    def _resolve_predictions(self, max_markets_per_cycle: int = 50):
        """Check unresolved predictions past their resolution_ts and update outcomes.

        Deduplicates Gamma lookups by market_id and limits API calls per cycle.
        """
        try:
            unresolved = self.trade_log.get_unresolved_predictions()
        except Exception:
            return

        now = time.time()
        # Filter to past-due predictions
        past_due = [
            p for p in unresolved
            if p.get("resolution_ts") is not None and now >= p["resolution_ts"] + 30
        ]
        if not past_due:
            return

        # Group by market_id to deduplicate Gamma API calls
        by_market: dict[str, list[dict]] = defaultdict(list)
        for pred in past_due:
            by_market[pred["market_id"]].append(pred)

        # Resolve up to max_markets_per_cycle unique markets per cycle
        market_ids = list(by_market.keys())[:max_markets_per_cycle]
        resolved_count = 0

        for market_id in market_ids:
            preds = by_market[market_id]
            # Use first prediction's token_id for fallback lookup
            token_id = preds[0].get("token_id", "")
            try:
                result = self._gamma.get_market_resolution(market_id, token_id=token_id)
            except Exception as e:
                logger.debug(f"Failed to resolve market {market_id[:20]}: {e}")
                continue

            if result is None or not result.get("resolved"):
                continue

            winning_outcome = result.get("winning_outcome")
            if winning_outcome is None:
                continue

            # Apply resolution to all predictions for this market
            for pred in preds:
                actual_correct = pred["outcome"].lower() == winning_outcome.lower()
                pnl = None
                if pred.get("traded") and pred.get("bid_price") is not None:
                    bid = pred["bid_price"]
                    pnl = (1.0 - bid) if actual_correct else -bid

                self.trade_log.resolve_prediction(pred["id"], actual_correct, pnl)
                resolved_count += 1

        if resolved_count > 0:
            logger.info(f"PREDICTIONS | resolved {resolved_count} predictions ({len(market_ids)}/{len(by_market)} markets checked)")
        elif len(by_market) > max_markets_per_cycle:
            logger.debug(f"PREDICTIONS | {len(by_market)} markets pending, checked {max_markets_per_cycle}")

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

    def _check_take_profit(self):
        """Sell open positions that have gained above the take-profit threshold."""
        tp_strategies = set(self.settings.take_profit_strategies)
        positions = self.position_tracker.get_open_positions()
        candidates = [p for p in positions if p.get("strategy") in tp_strategies]
        if not candidates:
            return

        # Batch fetch prices (single parallel request instead of sequential calls)
        token_ids = list(set(p["token_id"] for p in candidates))
        try:
            prices = self.clob.get_orderbooks_batch(token_ids)
        except Exception:
            prices = {}

        sold_count = 0
        for pos in candidates:
            price_data = prices.get(pos["token_id"])
            if not price_data:
                continue

            current_bid = price_data.get("bid", 0)
            if current_bid < self.settings.take_profit_min_bid:
                continue

            entry_price = pos.get("entry_price", 0)
            if entry_price <= 0:
                continue

            gain_pct = (current_bid - entry_price) / entry_price
            if gain_pct < self.settings.take_profit_pct:
                continue

            sell_price = round(current_bid, 2)
            result = self.executor.sell_position(pos, sell_price)
            if result.get("status") == "filled":
                sold_count += 1
                self.circuit_breaker.record_win()
                pos_asset = self._extract_asset_from_position(pos)
                if pos_asset:
                    self._asset_cooldowns[pos_asset] = self._cycle_count + 1
                logger.info(
                    f"TAKE-PROFIT: pos#{pos['id']} {pos.get('outcome')} | "
                    f"entry=${entry_price:.4f} bid=${current_bid:.4f} "
                    f"gain={gain_pct:.1%} | PnL=${result.get('pnl', 0):+.2f}"
                )

        if sold_count > 0:
            balance = self.executor.get_balance()
            self.balance_mgr.update(balance)
            logger.info(f"TAKE-PROFIT: sold {sold_count} position(s) | balance=${balance:.2f}")

    @staticmethod
    def _extract_asset_from_position(pos: dict) -> str:
        """Extract asset name (BTC, ETH, etc.) from position data."""
        asset = pos.get("asset", "")
        if asset:
            return asset
        question = pos.get("market_question", "")
        for token in ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"):
            if token in question.upper():
                return token
        return ""

    def _cleanup_stale_db_orders(self):
        """On startup, expire DB trades that are still 'pending' but market has resolved."""
        try:
            stale = self.trade_log.get_stale_pending_trades()
        except Exception:
            stale = []
        if not stale:
            return
        now = time.time()
        count = 0
        for trade in stale:
            res_ts = trade.get("resolution_ts", 0)
            if res_ts > 0 and now > res_ts:
                self.trade_log.update_trade_status(trade["id"], "expired")
                count += 1
        if count:
            logger.info(f"Startup cleanup: expired {count} stale pending trades")
