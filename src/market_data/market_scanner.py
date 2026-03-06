import logging
from src.market_data.gamma_client import GammaClient
from src.market_data.market_filter import MarketFilter
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


class MarketScanner:
    def __init__(self, settings: Settings, gamma_client: GammaClient, market_filter: MarketFilter):
        self.settings = settings
        self.gamma = gamma_client
        self.filter = market_filter

    def scan(self) -> list[dict]:
        raw_markets = self.gamma.get_all_active_markets()
        filtered = self.filter.filter_markets(raw_markets)
        logger.info(f"Scanner found {len(filtered)} tradeable markets")
        return filtered

    def get_market_details(self, condition_id: str) -> dict | None:
        return self.gamma.get_market(condition_id)
