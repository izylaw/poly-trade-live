import time
import logging
import requests
from src.utils.retry import retry

logger = logging.getLogger("poly-trade")

GAMMA_API_URL = "https://gamma-api.polymarket.com"


class GammaClient:
    def __init__(self):
        self._cache: dict[str, tuple[float, any]] = {}
        self._cache_ttl = 60.0

    def _get_cached(self, key: str):
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < self._cache_ttl:
                return data
        return None

    def _set_cache(self, key: str, data):
        self._cache[key] = (time.time(), data)

    @retry(max_attempts=3)
    def get_active_events(self, limit: int = 100, offset: int = 0) -> list[dict]:
        cached = self._get_cached(f"events_{limit}_{offset}")
        if cached is not None:
            return cached

        resp = requests.get(
            f"{GAMMA_API_URL}/events",
            params={"limit": limit, "offset": offset, "active": True, "closed": False},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._set_cache(f"events_{limit}_{offset}", data)
        return data

    @retry(max_attempts=3)
    def get_markets(self, limit: int = 100, offset: int = 0) -> list[dict]:
        cached = self._get_cached(f"markets_{limit}_{offset}")
        if cached is not None:
            return cached

        resp = requests.get(
            f"{GAMMA_API_URL}/markets",
            params={"limit": limit, "offset": offset, "active": True, "closed": False},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._set_cache(f"markets_{limit}_{offset}", data)
        return data

    def get_all_active_markets(self, max_pages: int = 5) -> list[dict]:
        all_markets = []
        for page in range(max_pages):
            markets = self.get_markets(limit=100, offset=page * 100)
            if not markets:
                break
            all_markets.extend(markets)
        logger.info(f"Fetched {len(all_markets)} active markets from Gamma")
        return all_markets

    @retry(max_attempts=3)
    def get_market(self, condition_id: str) -> dict | None:
        resp = requests.get(f"{GAMMA_API_URL}/markets/{condition_id}", timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
