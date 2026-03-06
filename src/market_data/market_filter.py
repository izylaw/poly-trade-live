import logging
from datetime import datetime, timezone
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


class MarketFilter:
    def __init__(self, settings: Settings):
        self.settings = settings

    def filter_markets(self, markets: list[dict]) -> list[dict]:
        filtered = []
        for m in markets:
            if self._passes(m):
                filtered.append(m)
        logger.debug(f"Filtered {len(markets)} -> {len(filtered)} markets")
        return filtered[:self.settings.max_markets]

    def _passes(self, market: dict) -> bool:
        # Must be active and not closed
        if market.get("closed") or not market.get("active"):
            return False

        # Volume check
        volume = float(market.get("volume", 0) or 0)
        if volume < self.settings.min_volume_24h:
            return False

        # Liquidity check
        liquidity = float(market.get("liquidity", 0) or 0)
        if liquidity < self.settings.min_liquidity:
            return False

        # Spread check (if we have best bid/ask)
        best_bid = float(market.get("bestBid", 0) or 0)
        best_ask = float(market.get("bestAsk", 0) or 0)
        if best_bid > 0 and best_ask > 0:
            spread = best_ask - best_bid
            if spread > self.settings.max_spread:
                return False

        # Time to resolution check
        end_date = market.get("endDate") or market.get("end_date_iso")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                hours_remaining = (end_dt - now).total_seconds() / 3600
                if hours_remaining < self.settings.min_time_to_resolution_hours:
                    return False
            except (ValueError, TypeError):
                pass

        # Must have token IDs for trading
        tokens = market.get("clobTokenIds") or market.get("tokens", [])
        if not tokens:
            return False

        return True
