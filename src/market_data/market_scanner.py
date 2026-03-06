import json
import logging
from src.market_data.gamma_client import GammaClient
from src.market_data.market_filter import MarketFilter
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


def _parse_json_field(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return []


def normalize_market(market: dict) -> dict:
    """Parse JSON-encoded fields from Gamma API into proper Python lists."""
    market["clobTokenIds"] = _parse_json_field(market.get("clobTokenIds"))
    market["outcomes"] = _parse_json_field(market.get("outcomes")) or ["Yes", "No"]
    return market


class MarketScanner:
    def __init__(self, settings: Settings, gamma_client: GammaClient, market_filter: MarketFilter):
        self.settings = settings
        self.gamma = gamma_client
        self.filter = market_filter

    def scan(self) -> list[dict]:
        raw_markets = self.gamma.get_all_active_markets()
        normalized = [normalize_market(m) for m in raw_markets]
        filtered = self.filter.filter_markets(normalized)
        logger.info(f"Scanner found {len(filtered)} tradeable markets")
        return filtered

    def get_market_details(self, condition_id: str) -> dict | None:
        m = self.gamma.get_market(condition_id)
        return normalize_market(m) if m else None
