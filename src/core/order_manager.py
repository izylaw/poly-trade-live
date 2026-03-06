import logging
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")


class OrderManager:
    def __init__(self, trade_log: TradeLog):
        self.trade_log = trade_log
        self._pending_orders: list[dict] = []

    def track_order(self, order: dict):
        self._pending_orders.append(order)

    def update_order_status(self, trade_id: int, status: str, pnl: float | None = None):
        self.trade_log.update_trade_status(trade_id, status, pnl)
        self._pending_orders = [o for o in self._pending_orders if o.get("trade_id") != trade_id]

    def get_pending_orders(self) -> list[dict]:
        return list(self._pending_orders)

    def cancel_all_pending(self):
        for order in self._pending_orders:
            tid = order.get("trade_id")
            if tid:
                self.trade_log.update_trade_status(tid, "cancelled")
        count = len(self._pending_orders)
        self._pending_orders.clear()
        if count:
            logger.info(f"Cancelled {count} pending orders")
