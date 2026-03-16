import time
import logging
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from src.utils.retry import retry

logger = logging.getLogger("poly-trade")

GAMMA_API_URL = "https://gamma-api.polymarket.com"
ASSET_NAMES = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "xrp"}


class GammaClient:
    def __init__(self):
        self._cache: dict[str, tuple[float, any]] = {}
        self._cache_ttl = 60.0

    def _get_cached(self, key: str, ttl: float | None = None):
        if key in self._cache:
            ts, data = self._cache[key]
            if time.time() - ts < (ttl if ttl is not None else self._cache_ttl):
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

    def get_all_active_events(self, max_pages: int = 30) -> list[dict]:
        """Paginate /events to fetch all active events (with nested markets)."""
        cache_key = f"all_active_events_{max_pages}"
        cached = self._get_cached(cache_key, ttl=45.0)
        if cached is not None:
            return cached

        # Fetch all pages in parallel
        page_results: dict[int, list] = {}

        def _fetch_page(page: int) -> tuple[int, list]:
            try:
                return page, self.get_active_events(limit=100, offset=page * 100)
            except Exception as e:
                logger.warning(f"Gamma page {page} fetch failed: {e}")
                return page, []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_page, p): p for p in range(max_pages)}
            for future in as_completed(futures):
                page_num, events = future.result()
                page_results[page_num] = events

        # Reassemble in page order, stopping at first empty page
        all_events = []
        for page in range(max_pages):
            events = page_results.get(page, [])
            if not events:
                break
            all_events.extend(events)

        logger.info(f"Fetched {len(all_events)} active events from Gamma (parallel pagination)")
        self._set_cache(cache_key, all_events)
        return all_events

    @staticmethod
    def extract_markets_from_events(events: list[dict]) -> list[dict]:
        """Flatten events into market dicts, attaching event metadata."""
        markets = []
        for event in events:
            title = event.get("title", "")
            slug = event.get("slug", "")
            tags = event.get("tags", [])
            for m in event.get("markets", []):
                m["_event_title"] = title
                m["_event_slug"] = slug
                m["_event_tags"] = tags
                markets.append(m)
        return markets

    def get_all_events_by_tag(self, tag: str, max_pages: int = 30) -> list[dict]:
        """Paginate /events by tag using parallel fetches (like get_all_active_events)."""
        cache_key = f"all_events_tag_{tag}_{max_pages}"
        cached = self._get_cached(cache_key, ttl=45.0)
        if cached is not None:
            return cached

        page_results: dict[int, list] = {}

        def _fetch_page(page: int) -> tuple[int, list]:
            try:
                return page, self.get_events_by_tag(tag, limit=100, offset=page * 100)
            except Exception as e:
                logger.warning(f"Gamma tag '{tag}' page {page} fetch failed: {e}")
                return page, []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(_fetch_page, p): p for p in range(max_pages)}
            for future in as_completed(futures):
                page_num, events = future.result()
                page_results[page_num] = events

        all_events = []
        for page in range(max_pages):
            events = page_results.get(page, [])
            if not events:
                break
            all_events.extend(events)

        logger.info(f"Fetched {len(all_events)} events for tag '{tag}' (parallel pagination)")
        self._set_cache(cache_key, all_events)
        return all_events

    @retry(max_attempts=3)
    def get_events_by_tag(self, tag: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch active events filtered by tag slug (e.g. 'sports', 'nba')."""
        cache_key = f"events_tag_{tag}_{limit}_{offset}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        resp = requests.get(
            f"{GAMMA_API_URL}/events",
            params={"limit": limit, "offset": offset, "active": True,
                    "closed": False, "tag": tag},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        self._set_cache(cache_key, data)
        return data

    # Sports league → Gamma series_id mapping (from /sports endpoint)
    SPORTS_SERIES = {
        "nba": "10345",
        "nfl": "10187",
        "mlb": "3",
        "nhl": "10346",
        "ufc": "10500",
        "mls": "10189",
        "epl": "10188",
        "soccer": "10188",  # alias
    }

    def get_sports_game_events(self, league: str, limit: int = 100, offset: int = 0) -> list[dict]:
        """Fetch active sports game events by league series_id."""
        series_id = self.SPORTS_SERIES.get(league.lower())
        if not series_id:
            return []

        cache_key = f"sports_games_{league}_{limit}_{offset}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/events",
                params={"limit": limit, "offset": offset, "active": True,
                        "closed": False, "series_id": series_id},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._set_cache(cache_key, data)
            return data
        except Exception as e:
            logger.warning(f"Gamma sports games '{league}' fetch failed: {e}")
            return []

    def get_all_sports_game_events(self, league: str, max_pages: int = 10) -> list[dict]:
        """Paginate sports game events for a league."""
        cache_key = f"all_sports_games_{league}_{max_pages}"
        cached = self._get_cached(cache_key, ttl=45.0)
        if cached is not None:
            return cached

        all_events = []
        for page in range(max_pages):
            events = self.get_sports_game_events(league, limit=100, offset=page * 100)
            if not events:
                break
            all_events.extend(events)

        if all_events:
            logger.info(f"Fetched {len(all_events)} game events for '{league}'")
        self._set_cache(cache_key, all_events)
        return all_events

    # Interval durations in seconds for slug construction
    INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400}

    def get_crypto_updown_markets(self, asset: str, interval: str) -> list[dict]:
        """Fetch crypto up/down markets by constructing event slugs.

        The Gamma API slug_contains search is unreliable for these restricted
        markets, so we construct candidate slugs from the current time and
        fetch each event directly.
        """
        interval_secs = self.INTERVAL_SECONDS.get(interval)
        if not interval_secs:
            return []

        now = int(time.time())
        base = (now // interval_secs) * interval_secs

        all_markets = []
        # Check a few past slots and upcoming slots
        for i in range(-2, 6):
            ts = base + i * interval_secs
            slug = f"{asset.lower()}-updown-{interval}-{ts}"
            markets = self._fetch_event_markets(slug, ts)
            all_markets.extend(markets)

        return all_markets

    def _fetch_event_markets(self, event_slug: str, start_ts: int) -> list[dict]:
        cache_key = f"updown_event_{event_slug}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/events",
                params={"slug": event_slug},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.debug(f"Gamma event lookup failed for {event_slug}: {e}")
            return []

        markets = []
        for event in events:
            if event.get("closed"):
                continue
            for m in event.get("markets", []):
                m["_event_slug"] = event_slug
                m["_start_ts"] = start_ts
                markets.append(m)

        self._set_cache(cache_key, markets)
        return markets

    @retry(max_attempts=3)
    def get_market(self, condition_id: str) -> dict | None:
        # Gamma /markets/{id} expects numeric ID, not conditionId hash.
        # Use query param for hex conditionIds (0x...).
        if condition_id.startswith("0x"):
            resp = requests.get(
                f"{GAMMA_API_URL}/markets",
                params={"condition_id": condition_id},
                timeout=15,
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            results = resp.json()
            return results[0] if results else None
        resp = requests.get(f"{GAMMA_API_URL}/markets/{condition_id}", timeout=15)
        if resp.status_code in (404, 422):
            return None
        resp.raise_for_status()
        return resp.json()

    def get_market_resolution(self, condition_id: str) -> dict | None:
        """Check if a market has resolved and return the winning outcome.

        Returns dict with 'resolved' bool and 'winning_outcome' str, or None on error.
        """
        market = self.get_market(condition_id)
        if market is None:
            return None

        closed = market.get("closed", False)
        if not closed:
            return {"resolved": False, "winning_outcome": None}

        # Parse outcomes and find the winner (resolution_price == "1")
        outcomes = market.get("outcomes", "[]")
        if isinstance(outcomes, str):
            try:
                import json
                outcomes = json.loads(outcomes)
            except (json.JSONDecodeError, TypeError):
                outcomes = []

        outcome_prices = market.get("outcomePrices", "[]")
        if isinstance(outcome_prices, str):
            try:
                import json
                outcome_prices = json.loads(outcome_prices)
            except (json.JSONDecodeError, TypeError):
                outcome_prices = []

        winning_outcome = None
        for i, outcome in enumerate(outcomes):
            if i < len(outcome_prices):
                try:
                    if float(outcome_prices[i]) == 1.0:
                        winning_outcome = outcome
                        break
                except (ValueError, TypeError):
                    continue

        return {"resolved": True, "winning_outcome": winning_outcome}

    def _fetch_slug_markets(self, slug: str, market_type: str, asset: str) -> list[dict]:
        """Fetch markets for an event by slug, tagging with market type and asset."""
        cache_key = f"slug_{slug}"
        cached = self._get_cached(cache_key, ttl=300)
        if cached is not None:
            return cached

        try:
            resp = requests.get(
                f"{GAMMA_API_URL}/events",
                params={"slug": slug},
                timeout=15,
            )
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.debug(f"Gamma slug lookup failed for {slug}: {e}")
            return []

        markets = []
        for event in events:
            if event.get("closed"):
                continue
            for m in event.get("markets", []):
                if m.get("closed"):
                    continue
                m["_event_slug"] = slug
                m["_market_type"] = market_type
                m["_asset"] = asset
                markets.append(m)

        self._set_cache(cache_key, markets)
        return markets

    def get_crypto_daily_markets(self, assets: list[str], lookahead_days: int = 5) -> list[dict]:
        """Fetch daily above/below, price range, and up/down markets for all assets."""
        all_markets = []
        today = datetime.now(timezone.utc).date()

        for asset in assets:
            coin = ASSET_NAMES.get(asset)
            if not coin:
                continue

            for offset in range(lookahead_days):
                day = today + timedelta(days=offset)
                month = day.strftime("%B").lower()
                day_num = day.day

                slugs = [
                    (f"{coin}-above-on-{month}-{day_num}", "above_below"),
                    (f"{coin}-price-on-{month}-{day_num}", "price_range"),
                    (f"{coin}-up-or-down-on-{month}-{day_num}", "daily_updown"),
                ]

                for slug, market_type in slugs:
                    markets = self._fetch_slug_markets(slug, market_type, asset)
                    all_markets.extend(markets)

        if all_markets:
            logger.info(f"Crypto daily discovery: {len(all_markets)} markets across {len(assets)} assets")
        return all_markets

    def get_crypto_weekly_markets(self) -> list[dict]:
        """Fetch weekly Bitcoin hit-price markets."""
        all_markets = []
        today = datetime.now(timezone.utc).date()
        monday = today - timedelta(days=today.weekday())

        for week_offset in range(2):
            week_monday = monday + timedelta(weeks=week_offset)
            week_sunday = week_monday + timedelta(days=6)

            month = week_monday.strftime("%B").lower()
            start = week_monday.day
            end = week_sunday.day

            slug = f"what-price-will-bitcoin-hit-{month}-{start}-{end}"
            markets = self._fetch_slug_markets(slug, "weekly_hit", "BTC")
            all_markets.extend(markets)

            # If week spans months, also try Sunday's month
            if week_sunday.month != week_monday.month:
                alt_month = week_sunday.strftime("%B").lower()
                alt_slug = f"what-price-will-bitcoin-hit-{alt_month}-{start}-{end}"
                alt_markets = self._fetch_slug_markets(alt_slug, "weekly_hit", "BTC")
                all_markets.extend(alt_markets)

        if all_markets:
            logger.info(f"Crypto weekly discovery: {len(all_markets)} markets")
        return all_markets

    def get_crypto_monthly_markets(self) -> list[dict]:
        """Fetch monthly hit-price markets for BTC, ETH, SOL."""
        all_markets = []
        now = datetime.now(timezone.utc)
        coins = {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana"}

        for month_offset in range(2):
            month = now.month + month_offset
            year = now.year
            if month > 12:
                month -= 12
                year += 1

            month_name = datetime(year, month, 1).strftime("%B").lower()

            for asset, coin in coins.items():
                slug = f"what-price-will-{coin}-hit-in-{month_name}-{year}"
                markets = self._fetch_slug_markets(slug, "monthly_hit", asset)
                all_markets.extend(markets)

        if all_markets:
            logger.info(f"Crypto monthly discovery: {len(all_markets)} markets")
        return all_markets
