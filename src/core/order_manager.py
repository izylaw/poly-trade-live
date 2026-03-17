import logging
import random
import time
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")

PAPER_BASE_FILL_RATE = 0.35  # 35% chance per cycle (was 15% — too low for short windows)


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
                self._cancel_order(order, clob_client, paper_mode)
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
                        to_remove.append(order)
                        continue

                # Probabilistic fill: base rate boosted by confidence
                # Higher confidence = bid closer to fair value = more likely to fill
                confidence = order.get("confidence", 0.6)
                confidence_boost = min(confidence, 0.9)  # cap at 0.9
                fill_prob = PAPER_BASE_FILL_RATE * (1 + confidence_boost)
                if random.random() < fill_prob:
                    result = executor.paper.fill_order(order)
                    if result.get("status") == "rejected":
                        self._cancel_order(order, clob_client, paper_mode)
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
                        self._cancel_order(order, clob_client, paper_mode)
                        self._pending_orders.remove(order)
                    if to_cancel:
                        logger.info(
                            f"Cancelled {len(to_cancel)} pending {strategy} orders — "
                            f"position limit {limit} reached on fill"
                        )

        return filled

    def _cancel_order(self, order: dict, clob_client, paper_mode: bool = False):
        tid = order.get("trade_id")
        if tid:
            self.trade_log.update_trade_status(tid, "cancelled")

        if not paper_mode:
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
