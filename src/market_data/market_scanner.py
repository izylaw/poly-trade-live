import json
import logging
import time
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
    def __init__(self, settings: Settings, gamma_client: GammaClient,
                 market_filter: MarketFilter, clob_client=None):
        self.settings = settings
        self.gamma = gamma_client
        self.filter = market_filter
        self.clob_client = clob_client
        self._clob_ids: set[str] | None = None
        self._clob_ids_ts: float = 0.0

    def scan(self) -> list[dict]:
        # 1. Fetch all events via full pagination
        events = self.gamma.get_all_active_events(
            max_pages=self.settings.scanner_max_event_pages
        )

        # 2. Flatten events into markets
        raw_markets = GammaClient.extract_markets_from_events(events)

        # 3. Normalize JSON fields
        normalized = [normalize_market(m) for m in raw_markets]

        # 4. Apply volume/liquidity/spread filters
        filtered = self.filter.filter_markets(normalized)

        # 5. Apply CLOB tradeability filter
        if self.settings.scanner_clob_cross_ref and self.clob_client is not None:
            filtered = self._apply_tradeability_filter(filtered)

        logger.info(
            f"Scanner found {len(filtered)} tradeable markets "
            f"(from {len(raw_markets)} raw, {len(events)} events)"
        )
        return filtered

    def _apply_tradeability_filter(self, markets: list[dict]) -> list[dict]:
        """Filter out markets not present in the CLOB tradeability index."""
        clob_ids = self._get_clob_ids()
        if clob_ids is None:
            return markets  # graceful fallback

        tradeable = [
            m for m in markets
            if (m.get("conditionId") or m.get("condition_id", "")) in clob_ids
        ]
        removed = len(markets) - len(tradeable)
        if removed:
            logger.debug(f"CLOB filter removed {removed} non-tradeable markets")
        return tradeable

    def _get_clob_ids(self) -> set[str] | None:
        """Return cached CLOB condition_id set, refreshing if stale."""
        now = time.time()
        ttl = self.settings.scanner_clob_ttl
        if self._clob_ids is not None and (now - self._clob_ids_ts) < ttl:
            return self._clob_ids
        try:
            self._clob_ids = self.clob_client.get_all_tradeable_condition_ids()
            self._clob_ids_ts = now
        except Exception as e:
            logger.warning(f"CLOB tradeability fetch failed, skipping filter: {e}")
            return None
        return self._clob_ids

    def get_market_details(self, condition_id: str) -> dict | None:
        m = self.gamma.get_market(condition_id)
        return normalize_market(m) if m else None
