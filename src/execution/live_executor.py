import logging
import time
from datetime import datetime, timezone
from src.risk.risk_manager import ApprovedTrade
from src.market_data.clob_client import PolymarketClobClient
from src.storage.trade_log import TradeLog


def _is_long_term(resolution_ts: float, threshold_days: int) -> bool:
    return resolution_ts > 0 and resolution_ts - time.time() > threshold_days * 86400

logger = logging.getLogger("poly-trade")


class LiveExecutor:
    def __init__(self, clob_client: PolymarketClobClient, trade_log: TradeLog, max_open_positions: int = 10,
                 long_term_threshold_days: int = 7):
        self.clob = clob_client
        self.trade_log = trade_log
        self.max_open_positions = max_open_positions
        self.long_term_threshold_days = long_term_threshold_days

    def execute(self, trade: ApprovedTrade) -> dict:
        if trade.signal.strategy != "arbitrage":
            non_arb = [p for p in self.get_open_positions() if p.get("strategy") != "arbitrage"]
            if len(non_arb) >= self.max_open_positions:
                logger.info(f"Live: rejected order — {self.max_open_positions} positions already open")
                return {"status": "rejected", "reason": "max_positions_reached"}

        try:
            result = self.clob.post_order(
                token_id=trade.signal.token_id,
                side=trade.signal.side,
                price=trade.signal.price,
                size=trade.size,
                order_type=trade.signal.order_type,
                post_only=trade.signal.post_only,
            )
        except Exception as e:
            logger.error(f"Live order failed: {e}")
            return {"status": "error", "reason": str(e)}

        order_id = result.get("orderID", result.get("id", "unknown"))
        status = "pending" if trade.signal.order_type == "GTC" else "filled"

        trade_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_id": trade.signal.market_id,
            "market_question": trade.signal.market_question,
            "token_id": trade.signal.token_id,
            "side": trade.signal.side,
            "outcome": trade.signal.outcome,
            "price": trade.signal.price,
            "size": trade.size,
            "cost": round(trade.cost, 4),
            "strategy": trade.signal.strategy,
            "confidence": trade.signal.confidence,
            "kelly_fraction": trade.kelly_fraction,
            "order_type": trade.signal.order_type,
            "status": status,
            "fill_price": trade.signal.price,
            "paper_trade": False,
            "resolution_ts": trade.signal.resolution_ts,
            "notes": f"order_id={order_id}",
        }
        trade_id = self.trade_log.log_trade(trade_record)

        if status == "filled":
            position = {
                "market_id": trade.signal.market_id,
                "token_id": trade.signal.token_id,
                "outcome": trade.signal.outcome,
                "market_question": trade.signal.market_question,
                "strategy": trade.signal.strategy,
                "entry_price": trade.signal.price,
                "size": trade.size,
                "cost": round(trade.cost, 4),
                "paper_trade": False,
                "resolution_ts": trade.signal.resolution_ts,
                "is_long_term": 1 if _is_long_term(trade.signal.resolution_ts, self.long_term_threshold_days) else 0,
                "slug": trade.signal.slug,
            }
            pos_id = self.trade_log.save_position(position)
        else:
            pos_id = None

        logger.info(f"LIVE ORDER: {order_id} status={status} | {trade.signal.outcome}@{trade.signal.price:.4f}")
        result = {"status": status, "trade_id": trade_id, "position_id": pos_id, "order_id": order_id}
        if status == "pending":
            result["market_id"] = trade.signal.market_id
            result["token_id"] = trade.signal.token_id
            result["outcome"] = trade.signal.outcome
            result["market_question"] = trade.signal.market_question
            result["strategy"] = trade.signal.strategy
            result["entry_price"] = trade.signal.price
            result["size"] = trade.size
            result["cost"] = round(trade.cost, 4)
            result["resolution_ts"] = trade.signal.resolution_ts
            result["slug"] = trade.signal.slug
            result["asset"] = trade.signal.asset
        return result

    def sell_position(self, position: dict, sell_price: float) -> dict:
        """Sell an open position on the CLOB to take profit."""
        try:
            sell_result = self.clob.post_order(
                token_id=position["token_id"],
                side="SELL",
                price=sell_price,
                size=position["size"],
                order_type="FOK",
            )
        except Exception as e:
            logger.error(f"Live sell failed for pos#{position['id']}: {e}")
            return {"status": "error", "reason": str(e)}

        order_id = sell_result.get("orderID", sell_result.get("id", "unknown")) if isinstance(sell_result, dict) else "unknown"
        pnl = (sell_price - position["entry_price"]) * position["size"]
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
            "fill_price": sell_price,
            "pnl": pnl,
            "paper_trade": False,
            "resolution_ts": position.get("resolution_ts", 0),
            "notes": f"take_profit_sell order_id={order_id}",
        }
        self.trade_log.log_trade(trade_record)

        logger.info(
            f"LIVE TAKE-PROFIT SELL: pos#{position['id']} {position['outcome']} "
            f"entry=${position['entry_price']:.4f} exit=${sell_price:.4f} "
            f"pnl=${pnl:+.2f}"
        )
        return {"status": "filled", "pnl": pnl, "position_id": position["id"]}

    def get_balance(self) -> float:
        return self.clob.get_balance()

    def get_open_positions(self) -> list[dict]:
        return self.trade_log.get_open_positions(paper_trade=False)
