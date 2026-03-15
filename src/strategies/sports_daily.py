"""Sports Daily strategy — trade daily sports events using market microstructure.

Edge sources (no external odds feed required):
1. Spread capture — maker bids in the middle of wide spreads
2. Book imbalance — follow informed flow when order book is skewed
3. Favorite value — exploit favorite-longshot bias on heavy favorites
"""

import json
import logging
import re
import time
from datetime import datetime, timezone

from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.gamma_client import GammaClient
from src.market_data.clob_client import PolymarketClobClient

logger = logging.getLogger("poly-trade")

# Keywords for identifying sports events from event titles or market questions
SPORT_KEYWORDS = {
    "nba", "nfl", "mlb", "nhl", "mma", "ufc", "soccer", "football",
    "tennis", "boxing", "epl", "premier league", "champions league",
    "serie a", "la liga", "bundesliga", "ligue 1", "cricket", "rugby",
    "f1", "formula 1", "nascar", "pga", "golf", "ncaa", "college",
    "wnba", "mls", "copa", "world cup", "afl", "ipl",
}

# Patterns that indicate a sports game/match question
GAME_PATTERNS = [
    r"\bwin\b.*\bgame\b",
    r"\bbeat\b",
    r"\bvs\.?\b",
    r"\bdefeat\b",
    r"\bwin\b.*\bmatch\b",
    r"\bwin\b.*\bseries\b",
    r"\bwin\b.*\btournament\b",
    r"\bover\b.*\bunder\b",
    r"\bmoneyline\b",
    r"\bspread\b",
]


class SportsDailyStrategy(Strategy):
    name = "sports_daily"
    self_discovering = True

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.gamma = GammaClient()
        self.min_volume = settings.sports_daily_min_volume
        self.min_liquidity = settings.sports_daily_min_liquidity
        self.min_spread = settings.sports_daily_min_spread
        self.max_hours_to_resolution = settings.sports_daily_max_hours_to_resolution
        self.favorite_min_prob = settings.sports_daily_favorite_min_prob
        self.favorite_max_prob = settings.sports_daily_favorite_max_prob
        self.imbalance_threshold = settings.sports_daily_imbalance_threshold
        self.maker_cushion = settings.sports_daily_maker_cushion
        self.min_edge = settings.sports_daily_min_edge
        self.max_spread = settings.sports_daily_max_spread
        self.min_book_depth = settings.sports_daily_min_book_depth
        self.tags_to_search = settings.sports_daily_tags
        self._pending_predictions: list[dict] = []

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        self._pending_predictions = []
        signals = []

        sports_markets = self._discover_sports_markets()
        if not sports_markets:
            logger.debug("sports_daily: no sports markets found")
            return []

        logger.info(f"sports_daily: evaluating {len(sports_markets)} sports markets")

        # Batch-fetch all orderbooks upfront
        all_token_ids = []
        for market in sports_markets:
            tokens = market.get("clobTokenIds", [])
            all_token_ids.extend(tokens[:2])

        book_cache = {}
        if all_token_ids:
            book_cache = clob_client.get_books_batch(all_token_ids)

        for market in sports_markets:
            try:
                market_signals = self._analyze_market(market, clob_client, book_cache)
                signals.extend(market_signals)
            except Exception as e:
                q = market.get("question", "?")[:60]
                logger.warning(f"sports_daily: error analyzing '{q}': {e}")

        if not signals and sports_markets:
            logger.info(
                f"sports_daily: 0 signals from {len(sports_markets)} markets "
                f"(check book depth and spread filters)"
            )

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    # --- Market discovery ---

    def _discover_sports_markets(self) -> list[dict]:
        """Find sports markets resolving within max_hours_to_resolution."""
        all_markets = []

        # Source 1: tag-based search for each configured tag (parallel pagination)
        for tag in self.tags_to_search:
            try:
                events = self.gamma.get_all_events_by_tag(tag)
                for event in events:
                    title = event.get("title", "")
                    slug = event.get("slug", "")
                    tags = event.get("tags", [])
                    if not self._is_sports_event(title, slug, tags):
                        continue
                    for m in event.get("markets", []):
                        m["_event_title"] = title
                        m["_event_slug"] = slug
                        all_markets.append(m)
            except Exception as e:
                logger.debug(f"sports_daily: tag search '{tag}' failed: {e}")

        # Source 2: scan all active events and filter by keywords (catches
        # sports events not tagged with any of the configured tags)
        try:
            for page in range(30):
                events = self.gamma.get_active_events(limit=100, offset=page * 100)
                if not events:
                    break
                for event in events:
                    title = event.get("title", "")
                    slug = event.get("slug", "")
                    tags = event.get("tags", [])
                    if self._is_sports_event(title, slug, tags):
                        for m in event.get("markets", []):
                            m["_event_title"] = title
                            m["_event_slug"] = slug
                            all_markets.append(m)
        except Exception as e:
            logger.warning(f"sports_daily: event scan failed: {e}")

        # Deduplicate by conditionId
        seen = set()
        unique = []
        for m in all_markets:
            cid = m.get("conditionId") or m.get("condition_id", "")
            if cid and cid not in seen:
                seen.add(cid)
                unique.append(m)

        # Filter: resolving within time window, sufficient volume/liquidity
        now = datetime.now(timezone.utc)
        filtered = []
        for m in unique:
            if m.get("closed") or not m.get("active", True):
                continue

            # Check resolution time
            end_date = m.get("endDate") or m.get("end_date_iso")
            if not end_date:
                continue
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                hours_remaining = (end_dt - now).total_seconds() / 3600
                if hours_remaining <= 0 or hours_remaining > self.max_hours_to_resolution:
                    continue
                m["_hours_remaining"] = hours_remaining
            except (ValueError, TypeError):
                continue

            # Volume/liquidity checks
            volume = float(m.get("volume", 0) or 0)
            liquidity = float(m.get("liquidity", 0) or 0)
            if volume < self.min_volume or liquidity < self.min_liquidity:
                continue

            # Parse JSON-encoded fields
            m["clobTokenIds"] = self._parse_json_field(m.get("clobTokenIds", []))
            m["outcomes"] = self._parse_json_field(m.get("outcomes", []))
            if len(m["clobTokenIds"]) < 2 or len(m["outcomes"]) < 2:
                continue

            filtered.append(m)

        if filtered:
            logger.info(f"sports_daily: {len(filtered)} markets pass filters (of {len(unique)} unique)")

        return filtered

    @staticmethod
    def _is_sports_event(title: str, slug: str, tags) -> bool:
        """Check if an event is sports-related by keywords and tags."""
        text = f"{title} {slug}".lower()

        # Check keywords
        for keyword in SPORT_KEYWORDS:
            if keyword in text:
                return True

        # Check tags (can be list of strings or list of dicts)
        if isinstance(tags, list):
            for tag in tags:
                label = tag.get("label", tag) if isinstance(tag, dict) else str(tag)
                if label.lower() in SPORT_KEYWORDS or "sport" in label.lower():
                    return True

        # Check game patterns in title
        for pattern in GAME_PATTERNS:
            if re.search(pattern, title, re.IGNORECASE):
                return True

        return False

    # --- Market analysis ---

    def _analyze_market(self, market: dict, clob_client, book_cache: dict | None = None) -> list[TradeSignal]:
        """Analyze a single sports market for all signal types."""
        outcomes = market.get("outcomes", [])
        tokens = market.get("clobTokenIds", [])
        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", market.get("_event_title", ""))

        if len(outcomes) < 2 or len(tokens) < 2:
            return []

        # Look up books from cache (batch-fetched) or fall back to individual fetch
        books = {}
        prices = {}
        for i, token_id in enumerate(tokens[:2]):
            if book_cache is not None and token_id in book_cache:
                books[i] = book_cache[token_id]
            else:
                try:
                    books[i] = clob_client.get_book(token_id)
                except Exception:
                    books[i] = None
            prices[i] = PolymarketClobClient.extract_price(books.get(i))

        # Extract bid/ask for the primary outcome (index 0)
        bid_0 = prices[0]["bid"] if prices.get(0) else 0.0
        ask_0 = prices[0]["ask"] if prices.get(0) else 1.0
        bid_1 = prices[1]["bid"] if prices.get(1) else 0.0
        ask_1 = prices[1]["ask"] if prices.get(1) else 1.0

        mid_0 = (bid_0 + ask_0) / 2 if bid_0 > 0 and ask_0 < 1.0 else bid_0 or ask_0
        mid_1 = (bid_1 + ask_1) / 2 if bid_1 > 0 and ask_1 < 1.0 else bid_1 or ask_1

        spread_0 = ask_0 - bid_0 if bid_0 > 0 and ask_0 < 1.0 else 0.0
        spread_1 = ask_1 - bid_1 if bid_1 > 0 and ask_1 < 1.0 else 0.0

        # Book imbalance for primary outcome
        imbalance_0 = self._compute_book_imbalance(books.get(0))
        imbalance_1 = self._compute_book_imbalance(books.get(1))

        volume = float(market.get("volume", 0) or 0)
        liquidity = float(market.get("liquidity", 0) or 0)
        hours_remaining = market.get("_hours_remaining", 24.0)

        signals = []
        for i, outcome in enumerate(outcomes[:2]):
            token_id = tokens[i]
            bid = bid_0 if i == 0 else bid_1
            ask = ask_0 if i == 0 else ask_1
            mid = mid_0 if i == 0 else mid_1
            spread = spread_0 if i == 0 else spread_1
            imbalance = imbalance_0 if i == 0 else imbalance_1
            book = books.get(i)

            # Gate: skip empty/illiquid books and excessively wide spreads
            if spread > self.max_spread:
                logger.debug(
                    f"sports_daily: skip {outcome} '{question[:40]}' spread={spread:.2f} > max={self.max_spread}"
                )
                continue
            if not self._is_book_liquid(book, self.min_book_depth):
                logger.debug(
                    f"sports_daily: skip {outcome} '{question[:40]}' book too thin"
                )
                continue

            pred = {
                "strategy": self.name,
                "market_id": market_id,
                "token_id": token_id,
                "outcome": outcome,
                "est_prob": mid,
                "bid_price": 0.0,
                "spread": spread,
                "imbalance": imbalance,
                "hours_remaining": hours_remaining,
                "traded": False,
                "signal_type": None,
            }

            # --- Signal 1: Spread capture ---
            sig = self._spread_capture_signal(
                market_id, token_id, question, outcome,
                bid, ask, mid, spread, book, hours_remaining,
                volume=volume, liquidity=liquidity,
            )
            if sig:
                pred["traded"] = True
                pred["signal_type"] = "spread_capture"
                pred["bid_price"] = sig.price
                self._pending_predictions.append(pred)
                signals.append(sig)
                continue

            # --- Signal 2: Book imbalance momentum ---
            sig = self._book_imbalance_signal(
                market_id, token_id, question, outcome,
                bid, ask, mid, imbalance, book, hours_remaining,
                volume=volume, liquidity=liquidity,
            )
            if sig:
                pred["traded"] = True
                pred["signal_type"] = "book_imbalance"
                pred["bid_price"] = sig.price
                self._pending_predictions.append(pred)
                signals.append(sig)
                continue

            # --- Signal 3: Favorite value ---
            sig = self._favorite_value_signal(
                market_id, token_id, question, outcome,
                bid, ask, mid, book, hours_remaining,
            )
            if sig:
                pred["traded"] = True
                pred["signal_type"] = "favorite_value"
                pred["bid_price"] = sig.price
                self._pending_predictions.append(pred)
                signals.append(sig)
                continue

            self._pending_predictions.append(pred)

        return signals

    def _spread_capture_signal(
        self, market_id, token_id, question, outcome,
        bid, ask, mid, spread, book, hours_remaining,
        volume=0.0, liquidity=0.0,
    ) -> TradeSignal | None:
        """Generate signal when spread is wide enough to capture."""
        if spread < self.min_spread:
            return None

        # Place bid slightly above best bid (midpoint - cushion)
        our_bid = round(mid - self.maker_cushion, 2)

        # Must be above current best bid
        if our_bid <= bid:
            our_bid = round(bid + 0.01, 2)

        # Must still have edge vs midpoint
        edge = mid - our_bid
        if edge < self.min_edge:
            return None

        # Confidence: midpoint + bonuses for volume, liquidity, spread quality
        vol_bonus = min(volume / 20000, 1.0) * 0.05
        liq_bonus = min(liquidity / 10000, 1.0) * 0.03
        spread_quality = max(0, 1.0 - spread / 0.20) * 0.02
        confidence = min(mid + vol_bonus + liq_bonus + spread_quality, 0.95)
        ev = confidence * (1.0 - our_bid) - (1.0 - confidence) * our_bid

        if ev <= 0:
            return None

        # Closer to game time = higher fill probability = better
        time_bonus = max(0, 1.0 - hours_remaining / self.max_hours_to_resolution)

        logger.info(
            f"sports_daily: SPREAD_CAPTURE {outcome} '{question[:50]}' | "
            f"bid={our_bid:.2f} spread={spread:.2f} mid={mid:.2f} edge={edge:.3f} "
            f"EV={ev:.3f} hrs={hours_remaining:.1f}"
        )

        # Estimate end-of-game resolution timestamp
        cancel_ts = time.time() + hours_remaining * 3600 - 300  # cancel 5min before resolution

        return TradeSignal(
            market_id=market_id,
            token_id=token_id,
            market_question=question,
            side="BUY",
            outcome=outcome,
            price=our_bid,
            confidence=confidence,
            strategy=self.name,
            expected_value=ev * (1 + time_bonus * 0.2),  # slight boost for near-game
            order_type="GTC",
            post_only=True,
            cancel_after_ts=cancel_ts,
            resolution_ts=cancel_ts + 300,  # cancel_ts is 5min before resolution
        )

    def _book_imbalance_signal(
        self, market_id, token_id, question, outcome,
        bid, ask, mid, imbalance, book, hours_remaining,
        volume=0.0, liquidity=0.0,
    ) -> TradeSignal | None:
        """Generate signal when order book is heavily skewed (informed flow)."""
        if abs(imbalance) < self.imbalance_threshold:
            return None

        # imbalance > 0 means more bids (bullish for this outcome)
        # imbalance < 0 means more asks (bearish)
        if imbalance <= 0:
            return None  # only follow positive imbalance (bid side heavier)

        # Adjusted probability: midpoint + boost from imbalance + vol/liq bonuses
        boost = imbalance * 0.10  # up to 10% probability boost
        vol_bonus = min(volume / 20000, 1.0) * 0.03
        liq_bonus = min(liquidity / 10000, 1.0) * 0.02
        adjusted_prob = min(mid + boost + vol_bonus + liq_bonus, 0.95)

        our_bid = round(adjusted_prob - self.maker_cushion, 2)
        if our_bid <= bid:
            our_bid = round(bid + 0.01, 2)

        edge = adjusted_prob - our_bid
        if edge < self.min_edge:
            return None

        ev = adjusted_prob * (1.0 - our_bid) - (1.0 - adjusted_prob) * our_bid
        if ev <= 0:
            return None

        logger.info(
            f"sports_daily: BOOK_IMBALANCE {outcome} '{question[:50]}' | "
            f"bid={our_bid:.2f} imbalance={imbalance:+.2f} adj_prob={adjusted_prob:.2f} "
            f"EV={ev:.3f}"
        )

        cancel_ts = time.time() + hours_remaining * 3600 - 300

        return TradeSignal(
            market_id=market_id,
            token_id=token_id,
            market_question=question,
            side="BUY",
            outcome=outcome,
            price=our_bid,
            confidence=adjusted_prob,
            strategy=self.name,
            expected_value=ev,
            order_type="GTC",
            post_only=True,
            cancel_after_ts=cancel_ts,
            resolution_ts=cancel_ts + 300,
        )

    def _favorite_value_signal(
        self, market_id, token_id, question, outcome,
        bid, ask, mid, book, hours_remaining,
    ) -> TradeSignal | None:
        """Back heavy favorites exploiting the favorite-longshot bias.

        Sports bettors systematically over-bet underdogs and under-bet favorites.
        When the implied probability is high (0.82-0.95), the true probability
        is typically even higher.
        """
        if mid < self.favorite_min_prob or mid > self.favorite_max_prob:
            return None

        # Favorite-longshot bias: true prob is roughly mid + bias_boost
        # The bias is stronger for higher favorites
        bias_boost = (mid - 0.50) * 0.06  # ~3% boost at mid=0.85
        adjusted_prob = min(mid + bias_boost, 0.96)

        our_bid = round(adjusted_prob - self.maker_cushion, 2)
        if our_bid <= bid:
            our_bid = round(bid + 0.01, 2)

        # Must stay below the ask
        if our_bid >= ask and ask < 1.0:
            return None

        edge = adjusted_prob - our_bid
        if edge < self.min_edge:
            return None

        ev = adjusted_prob * (1.0 - our_bid) - (1.0 - adjusted_prob) * our_bid
        if ev <= 0:
            return None

        logger.info(
            f"sports_daily: FAVORITE_VALUE {outcome} '{question[:50]}' | "
            f"bid={our_bid:.2f} mid={mid:.2f} adj_prob={adjusted_prob:.2f} "
            f"bias_boost={bias_boost:.3f} EV={ev:.3f}"
        )

        cancel_ts = time.time() + hours_remaining * 3600 - 300

        return TradeSignal(
            market_id=market_id,
            token_id=token_id,
            market_question=question,
            side="BUY",
            outcome=outcome,
            price=our_bid,
            confidence=adjusted_prob,
            strategy=self.name,
            expected_value=ev,
            order_type="GTC",
            post_only=True,
            cancel_after_ts=cancel_ts,
            resolution_ts=cancel_ts + 300,
        )

    # --- Helpers ---

    @staticmethod
    def _is_book_liquid(book, min_depth: float) -> bool:
        """Check if both sides of the book have sufficient dollar depth."""
        if book is None:
            return False

        bid_depth = 0.0
        ask_depth = 0.0
        if hasattr(book, 'bids') and book.bids:
            for level in book.bids[:5]:
                try:
                    bid_depth += float(level.size) * float(level.price)
                except (AttributeError, ValueError):
                    pass
        if hasattr(book, 'asks') and book.asks:
            for level in book.asks[:5]:
                try:
                    ask_depth += float(level.size) * float(level.price)
                except (AttributeError, ValueError):
                    pass

        return bid_depth >= min_depth and ask_depth >= min_depth

    @staticmethod
    def _compute_book_imbalance(book) -> float:
        """Compute order book imbalance: +1 = all bids, -1 = all asks."""
        if book is None:
            return 0.0

        bid_depth = 0.0
        ask_depth = 0.0
        if hasattr(book, 'bids') and book.bids:
            for level in book.bids[:10]:
                try:
                    bid_depth += float(level.size)
                except (AttributeError, ValueError):
                    pass
        if hasattr(book, 'asks') and book.asks:
            for level in book.asks[:10]:
                try:
                    ask_depth += float(level.size)
                except (AttributeError, ValueError):
                    pass

        total = bid_depth + ask_depth
        if total == 0:
            return 0.0

        return (bid_depth - ask_depth) / total

    @staticmethod
    def _parse_json_field(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
        return value if isinstance(value, list) else []
