import logging
import time
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")


class OrderManager:
    def __init__(self, trade_log: TradeLog, max_open_positions: int = 10,
                 strategy_limits: dict[str, int] | None = None):
        self.trade_log = trade_log
        self.max_open_positions = max_open_positions
        self.strategy_limits = strategy_limits or {}
        self._pending_orders: list[dict] = []

    def track_order(self, order: dict):
        self._pending_orders.append(order)

    def update_order_status(self, trade_id: int, status: str, pnl: float | None = None):
        self.trade_log.update_trade_status(trade_id, status, pnl)
        self._pending_orders = [o for o in self._pending_orders if o.get("trade_id") != trade_id]

    def get_pending_orders(self) -> list[dict]:
        return list(self._pending_orders)

    def cleanup_expired_orders(self):
        """Mark orders as expired if their market has resolved."""
        now = time.time()
        to_remove = []
        for order in self._pending_orders:
            resolution_ts = order.get("resolution_ts", 0)
            if resolution_ts > 0 and now > resolution_ts:
                tid = order.get("trade_id")
                if tid:
                    self.trade_log.update_trade_status(tid, "expired")
                to_remove.append(order)
                logger.info(f"Expired stale order trade_id={tid} (market resolved)")
        for order in to_remove:
            self._pending_orders.remove(order)
        if to_remove:
            logger.info(f"Cleaned up {len(to_remove)} expired pending orders")

    def check_pending_orders(self, clob_client, executor, paper_mode: bool = False) -> list[dict]:
        """Poll pending orders, handle fills and timeouts."""
        filled = []
        now = time.time()
        to_remove = []
        # Track per-strategy fill counts for inline limit enforcement (paper mode)
        strategy_fill_counts: dict[str, int] = {}

        for order in list(self._pending_orders):
            cancel_after = order.get("cancel_after_ts", 0)

            # Auto-cancel if past deadline
            if cancel_after and now >= cancel_after:
                self._cancel_order(order, clob_client, paper_mode, executor=executor)
                to_remove.append(order)
                continue

            if paper_mode:
                # Check if this strategy already hit its fill-time limit this cycle
                strategy = order.get("strategy", "")
                if strategy in self.strategy_limits:
                    if strategy not in strategy_fill_counts:
                        # Count existing DB positions for this strategy
                        all_pos = executor.get_open_positions()
                        strategy_fill_counts[strategy] = sum(
                            1 for p in all_pos
                            if p.get("strategy") == strategy and not p.get("is_long_term")
                        )
                    if strategy_fill_counts[strategy] >= self.strategy_limits[strategy]:
                        self._cancel_order(order, clob_client, paper_mode)
                        executor.paper.release_reserved(order.get("cost", 0))
                        to_remove.append(order)
                        continue

                # Market-price fill: check if CLOB ask dropped to our bid
                # This simulates adverse selection — you fill when price moves against you
                token_id = order.get("token_id")
                bid_price = order.get("fill_price", 0)
                should_fill = False
                if token_id:
                    try:
                        price_data = clob_client.get_price(token_id)
                        if price_data:
                            current_ask = price_data.get("ask", 1.0)
                            # Fill when the ask has dropped to or below our bid
                            if current_ask <= bid_price:
                                should_fill = True
                                logger.info(
                                    f"Paper fill triggered: ask={current_ask:.4f} <= bid={bid_price:.4f} "
                                    f"for {order.get('outcome', '?')}"
                                )
                    except Exception as e:
                        logger.debug(f"Paper: failed to check price for {token_id[:16]}: {e}")

                if should_fill:
                    result = executor.paper.fill_order(order)
                    if result.get("status") == "rejected":
                        self._cancel_order(order, clob_client, paper_mode)
                        executor.paper.release_reserved(order.get("cost", 0))
                        to_remove.append(order)
                        continue
                    self.trade_log.update_trade_status(order["trade_id"], "filled")
                    to_remove.append(order)
                    filled.append(order)
                    # Update fill count for inline limit check
                    if strategy in self.strategy_limits:
                        strategy_fill_counts[strategy] = strategy_fill_counts.get(strategy, 0) + 1
            else:
                # Live: poll CLOB for actual fill status
                order_id = order.get("order_id")
                if order_id:
                    status = clob_client.get_order(order_id)
                    if status and _is_filled(status):
                        self.trade_log.update_trade_status(order["trade_id"], "filled")
                        # Create position record for filled live order
                        resolution_ts = order.get("resolution_ts", 0)
                        long_term_days = 7
                        is_lt = 1 if resolution_ts > 0 and resolution_ts - time.time() > long_term_days * 86400 else 0
                        position = {
                            "market_id": order.get("market_id", ""),
                            "token_id": order.get("token_id", ""),
                            "outcome": order.get("outcome", ""),
                            "market_question": order.get("market_question", ""),
                            "strategy": order.get("strategy", ""),
                            "entry_price": order.get("entry_price", 0),
                            "size": order.get("size", 0),
                            "cost": order.get("cost", 0),
                            "paper_trade": False,
                            "resolution_ts": resolution_ts,
                            "is_long_term": is_lt,
                            "slug": order.get("slug", ""),
                        }
                        self.trade_log.save_position(position)
                        logger.info(f"LIVE FILL: {order.get('outcome')}@{order.get('entry_price', 0):.4f} x{order.get('size', 0)} cost=${order.get('cost', 0):.2f}")
                        to_remove.append(order)
                        filled.append(order)

        for order in to_remove:
            if order in self._pending_orders:
                self._pending_orders.remove(order)

        if not paper_mode and filled:
            all_positions = executor.get_open_positions()
            non_arb_count = sum(1 for p in all_positions if p.get("strategy") != "arbitrage")
            non_arb_filled = sum(1 for o in filled if o.get("strategy") != "arbitrage")
            total = non_arb_count + non_arb_filled
            if total >= self.max_open_positions and self._pending_orders:
                non_arb_pending = [o for o in self._pending_orders if o.get("strategy") != "arbitrage"]
                for order in non_arb_pending:
                    self._cancel_order(order, clob_client, paper_mode=False)
                    self._pending_orders.remove(order)
                if non_arb_pending:
                    logger.info(f"Cancelled {len(non_arb_pending)} non-arb pending orders — position limit {self.max_open_positions} reached")

        # Fill-time per-strategy enforcement: cancel remaining pending orders
        # for strategies that hit their position limit after fills
        if filled and self.strategy_limits and self._pending_orders:
            all_positions = executor.get_open_positions()
            for strategy, limit in self.strategy_limits.items():
                strategy_positions = sum(1 for p in all_positions
                                         if p.get("strategy") == strategy
                                         and not p.get("is_long_term"))
                if strategy_positions >= limit:
                    to_cancel = [o for o in self._pending_orders
                                 if o.get("strategy") == strategy]
                    for order in to_cancel:
                        self._cancel_order(order, clob_client, paper_mode, executor=executor)
                        self._pending_orders.remove(order)
                    if to_cancel:
                        logger.info(
                            f"Cancelled {len(to_cancel)} pending {strategy} orders — "
                            f"position limit {limit} reached on fill"
                        )

        return filled

    def _cancel_order(self, order: dict, clob_client, paper_mode: bool = False,
                      executor=None):
        tid = order.get("trade_id")
        if tid:
            self.trade_log.update_trade_status(tid, "cancelled")

        if paper_mode and executor and hasattr(executor, 'paper'):
            executor.paper.release_reserved(order.get("cost", 0))
        elif not paper_mode:
            order_id = order.get("order_id")
            if order_id:
                try:
                    clob_client.cancel_order(order_id)
                except Exception as e:
                    logger.warning(f"Failed to cancel order {order_id}: {e}")

        logger.info(f"Auto-cancelled order trade_id={tid} (deadline passed)")

    def cancel_all_pending(self):
        for order in self._pending_orders:
            tid = order.get("trade_id")
            if tid:
                self.trade_log.update_trade_status(tid, "cancelled")
        count = len(self._pending_orders)
        self._pending_orders.clear()
        if count:
            logger.info(f"Cancelled {count} pending orders")


def _is_filled(status: dict) -> bool:
    s = status.get("status", "").lower()
    return s in ("filled", "matched")
