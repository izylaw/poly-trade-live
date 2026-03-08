import logging
from datetime import datetime, timezone
from src.risk.risk_manager import ApprovedTrade
from src.market_data.clob_client import PolymarketClobClient
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")


class LiveExecutor:
    def __init__(self, clob_client: PolymarketClobClient, trade_log: TradeLog, max_open_positions: int = 10):
        self.clob = clob_client
        self.trade_log = trade_log
        self.max_open_positions = max_open_positions

    def execute(self, trade: ApprovedTrade) -> dict:
        if len(self.get_open_positions()) >= self.max_open_positions:
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
            "notes": f"order_id={order_id}",
        }
        trade_id = self.trade_log.log_trade(trade_record)

        if status == "filled":
            position = {
                "market_id": trade.signal.market_id,
                "token_id": trade.signal.token_id,
                "outcome": trade.signal.outcome,
                "market_question": trade.signal.market_question,
                "entry_price": trade.signal.price,
                "size": trade.size,
                "cost": round(trade.cost, 4),
            }
            pos_id = self.trade_log.save_position(position)
        else:
            pos_id = None

        logger.info(f"LIVE ORDER: {order_id} status={status} | {trade.signal.outcome}@{trade.signal.price:.4f}")
        result = {"status": status, "trade_id": trade_id, "position_id": pos_id, "order_id": order_id}
        if status == "pending":
            result["market_id"] = trade.signal.market_id
        return result

    def get_balance(self) -> float:
        return self.clob.get_balance()

    def get_open_positions(self) -> list[dict]:
        return self.trade_log.get_open_positions()
