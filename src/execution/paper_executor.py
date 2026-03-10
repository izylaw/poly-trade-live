import logging
import random
from datetime import datetime, timezone
from src.risk.risk_manager import ApprovedTrade
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")


class PaperExecutor:
    def __init__(self, starting_balance: float, trade_log: TradeLog, max_open_positions: int = 10):
        self.starting_balance = starting_balance
        self.trade_log = trade_log
        self.max_open_positions = max_open_positions

    def execute(self, trade: ApprovedTrade) -> dict:
        if trade.signal.post_only:
            return self._execute_maker(trade)
        return self._execute_taker(trade)

    def _execute_maker(self, trade: ApprovedTrade) -> dict:
        """Maker order: don't fill immediately, return pending."""
        actual_cost = trade.size * trade.signal.price

        balance = self.get_balance()
        if actual_cost > balance:
            logger.warning(f"Paper: insufficient balance ${balance:.2f} for cost ${actual_cost:.2f}")
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
            f"x{trade.size:.2f} | balance=${balance:.2f}"
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
            "strategy": trade.signal.strategy,
            "confidence": trade.signal.confidence,
        }

    def _execute_taker(self, trade: ApprovedTrade) -> dict:
        """Taker order: instant fill with slippage."""
        slippage = 0.001
        fill_price = trade.signal.price * (1 + slippage)
        actual_cost = trade.size * fill_price

        balance = self.get_balance()
        if actual_cost > balance:
            logger.warning(f"Paper: insufficient balance ${balance:.2f} for cost ${actual_cost:.2f}")
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
            "strategy": trade.signal.strategy,
            "entry_price": fill_price,
            "size": trade.size,
            "cost": round(actual_cost, 4),
            "trade_id": trade_id,
            "paper_trade": True,
        }
        pos_id = self.trade_log.save_position(position)

        logger.info(
            f"PAPER FILL: {trade.signal.outcome}@{fill_price:.4f} x{trade.size:.2f} "
            f"cost=${actual_cost:.2f} | balance=${self.get_balance():.2f}"
        )
        return {"status": "filled", "trade_id": trade_id, "position_id": pos_id, "fill_price": fill_price}

    def fill_order(self, order: dict) -> dict:
        """Called by order_manager when probabilistic fill triggers."""
        if len(self.trade_log.get_open_positions(paper_trade=True)) >= self.max_open_positions:
            logger.info(f"Paper: rejected fill — {self.max_open_positions} positions already open")
            return {"status": "rejected", "reason": "max_positions_reached"}

        fill_price = order["fill_price"]
        actual_cost = order["size"] * fill_price

        position = {
            "market_id": order["market_id"],
            "token_id": order["token_id"],
            "outcome": order["outcome"],
            "market_question": order.get("market_question", ""),
            "strategy": order.get("strategy", "unknown"),
            "entry_price": fill_price,
            "size": order["size"],
            "cost": round(actual_cost, 4),
            "trade_id": order["trade_id"],
            "paper_trade": True,
        }
        pos_id = self.trade_log.save_position(position)

        self.trade_log.update_trade_status(order["trade_id"], "filled")

        logger.info(
            f"PAPER FILL: {order['outcome']}@{fill_price:.4f} x{order['size']:.2f} "
            f"cost=${actual_cost:.2f} | balance=${self.get_balance():.2f}"
        )
        return {"status": "filled", "position_id": pos_id}

    def get_balance(self) -> float:
        return self.trade_log.compute_paper_balance(self.starting_balance)

    def get_open_positions(self) -> list[dict]:
        return self.trade_log.get_open_positions(paper_trade=True)

    def close_position(self, position_id: int, exit_price: float):
        pos = self.trade_log.get_position_by_id(position_id)
        if pos is None:
            return 0.0
        pnl = (exit_price - pos["entry_price"]) * pos["size"]
        self.trade_log.close_position(position_id, pnl)
        logger.info(f"PAPER CLOSE: pos#{position_id} pnl=${pnl:.2f} | balance=${self.get_balance():.2f}")
        return pnl
