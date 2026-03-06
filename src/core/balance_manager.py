import logging
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


class BalanceManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._last_known_balance: float = settings.starting_capital

    def update(self, balance: float):
        self._last_known_balance = balance

    @property
    def balance(self) -> float:
        return self._last_known_balance

    @property
    def hard_floor(self) -> float:
        return self.settings.hard_floor

    def above_hard_floor(self, amount_to_spend: float = 0) -> bool:
        remaining = self._last_known_balance - amount_to_spend
        return remaining > self.hard_floor

    def available_for_trading(self) -> float:
        return max(self._last_known_balance - self.hard_floor, 0)

    def portfolio_exposure(self, open_positions: list[dict]) -> float:
        return sum(p.get("cost", 0) for p in open_positions)
