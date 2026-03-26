import logging
from src.config.settings import Settings
from src.risk.risk_manager import ApprovedTrade
from src.execution.paper_executor import PaperExecutor
from src.execution.live_executor import LiveExecutor

logger = logging.getLogger("poly-trade")


class Executor:
    def __init__(self, settings: Settings, paper: PaperExecutor | None = None, live: LiveExecutor | None = None):
        self.settings = settings
        self.paper = paper
        self.live = live

    def execute(self, trade: ApprovedTrade) -> dict:
        if self.settings.paper_trading:
            if self.paper is None:
                raise RuntimeError("Paper executor not initialized")
            return self.paper.execute(trade)
        else:
            if self.live is None:
                raise RuntimeError("Live executor not initialized")
            return self.live.execute(trade)

    def get_balance(self) -> float:
        if self.settings.paper_trading:
            return self.paper.get_balance()
        return self.live.get_balance()

    def get_open_positions(self) -> list[dict]:
        if self.settings.paper_trading:
            return self.paper.get_open_positions()
        return self.live.get_open_positions()

    def sell_position(self, position: dict, sell_price: float) -> dict:
        if self.settings.paper_trading:
            if self.paper is None:
                raise RuntimeError("Paper executor not initialized")
            return self.paper.sell_position(position, sell_price)
        else:
            if self.live is None:
                raise RuntimeError("Live executor not initialized")
            return self.live.sell_position(position, sell_price)

    @property
    def mode(self) -> str:
        return "paper" if self.settings.paper_trading else "live"
