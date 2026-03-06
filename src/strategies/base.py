from abc import ABC, abstractmethod
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings


class Strategy(ABC):
    name: str = "base"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = True

    @abstractmethod
    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        """Analyze markets and return trade signals sorted by expected value."""
        ...
