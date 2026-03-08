import logging
import random
from datetime import datetime, timezone
from src.risk.risk_manager import ApprovedTrade
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")


class PaperExecutor:
    def __init__(self, starting_balance: float, trade_log: TradeLog):
        self.balance = starting_balance
        self.trade_log = trade_log
        self.positions: list[dict] = []

    def execute(self, trade: ApprovedTrade) -> dict:
        if trade.signal.post_only:
            return self._execute_maker(trade)
        return self._execute_taker(trade)

    def _execute_maker(self, trade: ApprovedTrade) -> dict:
        """Maker order: don't fill immediately, return pending."""
        actual_cost = trade.size * trade.signal.price

        if actual_cost > self.balance:
            logger.warning(f"Paper: insufficient balance ${self.balance:.2f} for cost ${actual_cost:.2f}")
            return {"status": "rejected", "reason": "insufficient_balance"}

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": trade.signal.market_id,
            "market_question": trade.signal.market_question,
            "token_id": trade.signal.token_id,
            "side": trade.signal.side,
            "outcome": trade.signal.outcome,
            "price": trade.signal.price,
            "size": trade.size,
            "cost": round(actual_cost, 4),
            "strategy": trade.signal.strategy,
            "confidence": trade.signal.confidence,
            "kelly_fraction": trade.kelly_fraction,
            "order_type": trade.signal.order_type,
            "status": "pending",
            "fill_price": trade.signal.price,
            "paper_trade": True,
        }
        trade_id = self.trade_log.log_trade(trade_record)

        logger.info(
            f"PENDING: maker bid {trade.signal.outcome}@{trade.signal.price:.4f} "
            f"x{trade.size:.2f} | balance=${self.balance:.2f}"
        )
        return {
            "status": "pending",
            "trade_id": trade_id,
            "fill_price": trade.signal.price,
            "token_id": trade.signal.token_id,
            "size": trade.size,
            "cost": actual_cost,
            "cancel_after_ts": trade.signal.cancel_after_ts,
            "market_id": trade.signal.market_id,
            "outcome": trade.signal.outcome,
            "market_question": trade.signal.market_question,
            "confidence": trade.signal.confidence,
        }

    def _execute_taker(self, trade: ApprovedTrade) -> dict:
        """Taker order: instant fill with slippage."""
        slippage = 0.001
        fill_price = trade.signal.price * (1 + slippage)
        actual_cost = trade.size * fill_price

        if actual_cost > self.balance:
            logger.warning(f"Paper: insufficient balance ${self.balance:.2f} for cost ${actual_cost:.2f}")
            return {"status": "rejected", "reason": "insufficient_balance"}

        self.balance -= actual_cost

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": trade.signal.market_id,
            "market_question": trade.signal.market_question,
            "token_id": trade.signal.token_id,
            "side": trade.signal.side,
            "outcome": trade.signal.outcome,
            "price": trade.signal.price,
            "size": trade.size,
            "cost": round(actual_cost, 4),
            "strategy": trade.signal.strategy,
            "confidence": trade.signal.confidence,
            "kelly_fraction": trade.kelly_fraction,
            "order_type": trade.signal.order_type,
            "status": "filled",
            "fill_price": fill_price,
            "paper_trade": True,
        }
        trade_id = self.trade_log.log_trade(trade_record)

        position = {
            "market_id": trade.signal.market_id,
            "token_id": trade.signal.token_id,
            "outcome": trade.signal.outcome,
            "market_question": trade.signal.market_question,
            "entry_price": fill_price,
            "size": trade.size,
            "cost": round(actual_cost, 4),
            "trade_id": trade_id,
        }
        pos_id = self.trade_log.save_position(position)
        position["id"] = pos_id
        self.positions.append(position)

        logger.info(
            f"PAPER FILL: {trade.signal.outcome}@{fill_price:.4f} x{trade.size:.2f} "
            f"cost=${actual_cost:.2f} | balance=${self.balance:.2f}"
        )
        return {"status": "filled", "trade_id": trade_id, "position_id": pos_id, "fill_price": fill_price}

    def fill_order(self, order: dict) -> dict:
        """Called by order_manager when probabilistic fill triggers."""
        fill_price = order["fill_price"]
        actual_cost = order["size"] * fill_price

        self.balance -= actual_cost

        position = {
            "market_id": order["market_id"],
            "token_id": order["token_id"],
            "outcome": order["outcome"],
            "market_question": order.get("market_question", ""),
            "entry_price": fill_price,
            "size": order["size"],
            "cost": round(actual_cost, 4),
            "trade_id": order["trade_id"],
        }
        pos_id = self.trade_log.save_position(position)
        position["id"] = pos_id
        self.positions.append(position)

        self.trade_log.update_trade_status(order["trade_id"], "filled")

        logger.info(
            f"PAPER FILL: {order['outcome']}@{fill_price:.4f} x{order['size']:.2f} "
            f"cost=${actual_cost:.2f} | balance=${self.balance:.2f}"
        )
        return {"status": "filled", "position_id": pos_id}

    def get_balance(self) -> float:
        return self.balance

    def get_open_positions(self) -> list[dict]:
        return [p for p in self.positions if p.get("status", "open") == "open"]

    def close_position(self, position_id: int, exit_price: float):
        for pos in self.positions:
            if pos.get("id") == position_id:
                pnl = (exit_price - pos["entry_price"]) * pos["size"]
                self.balance += pos["size"] * exit_price
                pos["status"] = "closed"
                self.trade_log.close_position(position_id, pnl)
                logger.info(f"PAPER CLOSE: pos#{position_id} pnl=${pnl:.2f} | balance=${self.balance:.2f}")
                return pnl
        return 0.0
