import logging
import time
from datetime import datetime, timezone
from src.risk.risk_manager import ApprovedTrade
from src.storage.trade_log import TradeLog


def _is_long_term(resolution_ts: float, threshold_days: int) -> bool:
    return resolution_ts > 0 and resolution_ts - time.time() > threshold_days * 86400

logger = logging.getLogger("poly-trade")

# Realistic taker slippage on Polymarket's thin crypto books
TAKER_SLIPPAGE_BASE = 0.01   # 1% base slippage
TAKER_SLIPPAGE_NOISE = 0.005  # ±0.5% random variance


class PaperExecutor:
    def __init__(self, starting_balance: float, trade_log: TradeLog, max_open_positions: int = 10,
                 long_term_threshold_days: int = 7):
        self.starting_balance = starting_balance
        self.trade_log = trade_log
        self.max_open_positions = max_open_positions
        self.long_term_threshold_days = long_term_threshold_days
        self._reserved_cost: float = 0.0  # capital locked by pending orders

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

        # Reserve capital so subsequent orders can't double-spend
        self._reserved_cost += actual_cost

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
            "resolution_ts": trade.signal.resolution_ts,
        }
        trade_id = self.trade_log.log_trade(trade_record)

        logger.info(
            f"PENDING: maker bid {trade.signal.outcome}@{trade.signal.price:.4f} "
            f"x{trade.size:.2f} | balance=${balance:.2f} (reserved=${self._reserved_cost:.2f})"
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
            "slug": trade.signal.slug,
            "resolution_ts": trade.signal.resolution_ts,
            "asset": trade.signal.asset,
        }

    def _execute_taker(self, trade: ApprovedTrade) -> dict:
        """Taker order: instant fill with realistic slippage."""
        import random
        slippage = TAKER_SLIPPAGE_BASE + random.uniform(-TAKER_SLIPPAGE_NOISE, TAKER_SLIPPAGE_NOISE)
        slippage = max(slippage, 0.002)  # at least 0.2%
        fill_price = min(trade.signal.price * (1 + slippage), 0.99)
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
            "resolution_ts": trade.signal.resolution_ts,
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
            "resolution_ts": trade.signal.resolution_ts,
            "is_long_term": 1 if _is_long_term(trade.signal.resolution_ts, self.long_term_threshold_days) else 0,
            "slug": trade.signal.slug,
        }
        pos_id = self.trade_log.save_position(position)

        logger.info(
            f"PAPER FILL: {trade.signal.outcome}@{fill_price:.4f} (slippage={slippage:.3%}) "
            f"x{trade.size:.2f} cost=${actual_cost:.2f} | balance=${self.get_balance():.2f}"
        )
        return {"status": "filled", "trade_id": trade_id, "position_id": pos_id, "fill_price": fill_price}

    def fill_order(self, order: dict) -> dict:
        """Called by order_manager when market-price fill triggers."""
        open_positions = self.trade_log.get_open_positions(paper_trade=True)
        is_arb = order.get("strategy") == "arbitrage"
        if not is_arb:
            non_arb = [p for p in open_positions if p.get("strategy") != "arbitrage"]
            if len(non_arb) >= self.max_open_positions:
                logger.info(f"Paper: rejected fill — {self.max_open_positions} positions already open")
                return {"status": "rejected", "reason": "max_positions_reached"}

        fill_price = order["fill_price"]
        actual_cost = order["size"] * fill_price

        resolution_ts = order.get("resolution_ts", 0)
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
            "resolution_ts": resolution_ts,
            "is_long_term": 1 if _is_long_term(resolution_ts, self.long_term_threshold_days) else 0,
            "slug": order.get("slug", ""),
        }
        pos_id = self.trade_log.save_position(position)

        self.trade_log.update_trade_status(order["trade_id"], "filled")

        # Release reserved capital (position cost is now tracked in DB)
        self._reserved_cost = max(0.0, self._reserved_cost - actual_cost)

        logger.info(
            f"PAPER FILL: {order['outcome']}@{fill_price:.4f} x{order['size']:.2f} "
            f"cost=${actual_cost:.2f} | balance=${self.get_balance():.2f}"
        )
        return {"status": "filled", "position_id": pos_id}

    def sell_position(self, position: dict, sell_price: float) -> dict:
        """Simulate selling a position at the given price with slippage."""
        import random
        slippage = TAKER_SLIPPAGE_BASE + random.uniform(-TAKER_SLIPPAGE_NOISE, TAKER_SLIPPAGE_NOISE)
        slippage = max(slippage, 0.002)
        actual_sell_price = max(sell_price * (1 - slippage), 0.01)

        pnl = (actual_sell_price - position["entry_price"]) * position["size"]
        self.trade_log.close_position(position["id"], pnl)

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": position["market_id"],
            "market_question": position.get("market_question", ""),
            "token_id": position["token_id"],
            "side": "SELL",
            "outcome": position["outcome"],
            "price": sell_price,
            "size": position["size"],
            "cost": 0,
            "strategy": position.get("strategy", "unknown"),
            "confidence": 0,
            "kelly_fraction": 0,
            "order_type": "FOK",
            "status": "filled",
            "fill_price": actual_sell_price,
            "pnl": pnl,
            "paper_trade": True,
            "resolution_ts": position.get("resolution_ts", 0),
            "notes": "take_profit_sell",
        }
        self.trade_log.log_trade(trade_record)

        logger.info(
            f"PAPER TAKE-PROFIT SELL: pos#{position['id']} {position['outcome']} "
            f"entry=${position['entry_price']:.4f} exit=${actual_sell_price:.4f} "
            f"pnl=${pnl:+.2f} | balance=${self.get_balance():.2f}"
        )
        return {"status": "filled", "pnl": pnl, "position_id": position["id"]}

    def release_reserved(self, cost: float):
        """Release reserved capital when an order is cancelled."""
        self._reserved_cost = max(0.0, self._reserved_cost - cost)

    def get_balance(self) -> float:
        """Available balance = DB balance - reserved capital for pending orders."""
        db_balance = self.trade_log.compute_paper_balance(self.starting_balance)
        return db_balance - self._reserved_cost

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
