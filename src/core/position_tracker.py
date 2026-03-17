import logging
from src.storage.trade_log import TradeLog

logger = logging.getLogger("poly-trade")


class PositionTracker:
    def __init__(self, trade_log: TradeLog, paper_mode: bool = True):
        self.trade_log = trade_log
        self.paper_mode = paper_mode

    def get_open_positions(self) -> list[dict]:
        return self.trade_log.get_open_positions(paper_trade=self.paper_mode)

    def open_position(self, position: dict) -> int:
        return self.trade_log.save_position(position)

    def close_position(self, position_id: int, realized_pnl: float):
        self.trade_log.close_position(position_id, realized_pnl)
        logger.info(f"Position #{position_id} closed, PnL: ${realized_pnl:.2f}")

    def total_exposure(self) -> float:
        positions = self.get_open_positions()
        return sum(p.get("cost", 0) for p in positions)

    def count_open(self) -> int:
        return len(self.get_open_positions())
