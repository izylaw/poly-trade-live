import json
import logging
import time
from datetime import datetime
from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


def _parse_resolution_ts(market: dict) -> float:
    """Extract resolution timestamp from Gamma market data."""
    end_date = market.get("endDate", "")
    if end_date:
        try:
            dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            pass
    return 0.0


def _parse_outcome_prices(market: dict) -> list[float]:
    """Extract outcome prices from Gamma market data."""
    raw = market.get("outcomePrices", "[]")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, list):
        prices = []
        for p in raw:
            try:
                prices.append(float(p))
            except (ValueError, TypeError):
                prices.append(0.0)
        return prices
    return []


class HighProbabilityStrategy(Strategy):
    name = "high_probability"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.min_price = settings.high_prob_min_price
        self.max_price = settings.high_prob_max_price
        self.longshot_threshold = settings.high_prob_longshot_threshold
        self.longshot_min_price = settings.high_prob_longshot_min_price
        self.longshot_conf_multiplier = settings.high_prob_longshot_conf_multiplier
        self.maker_ttl_hours = settings.high_prob_maker_ttl_hours

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        # Pre-filter using Gamma price data to avoid hitting CLOB for every market
        candidates = self._pre_filter(markets)
        logger.info(f"high_probability: {len(candidates)} candidates from {len(markets)} markets")

        # Phase 1: Score using Gamma prices (fast, no CLOB calls)
        gamma_signals = []
        for market, idx, gamma_price in candidates:
            signal = self._evaluate_from_gamma(market, idx, gamma_price)
            if signal:
                gamma_signals.append(signal)

        if not gamma_signals:
            logger.info(f"high_probability: 0 gamma-scored signals from {len(candidates)} candidates")
            return []

        # Phase 2: Verify only the top candidates with actual CLOB orderbooks
        gamma_signals.sort(key=lambda s: s.expected_value, reverse=True)
        top = gamma_signals[:20]

        token_ids = [s.token_id for s in top]
        price_map = clob_client.get_orderbooks_batch(token_ids) if token_ids else {}

        cancel_ts = time.time() + self.maker_ttl_hours * 3600

        verified = []
        clob_miss = 0
        clob_out_of_range = 0
        for signal in top:
            price_data = price_map.get(signal.token_id)
            if price_data is None:
                clob_miss += 1
                continue

            ask = price_data["ask"]
            bid = price_data["bid"]
            is_longshot = signal.price <= self.longshot_threshold

            if is_longshot:
                # Long-shots: always maker bids, never taker (spread is proportionally too big)
                # Skip empty books — bid <= 0.01 means no real market-maker is present
                if bid <= 0.01:
                    clob_out_of_range += 1
                    continue
                maker_price = round(bid + 0.01, 2)
                if maker_price < self.longshot_min_price:
                    maker_price = self.longshot_min_price
                if maker_price > self.longshot_threshold:
                    clob_out_of_range += 1
                    continue
                confidence = self._score_confidence_with_liquidity(signal, price_data)
                ev = confidence * (1.0 - maker_price)
                verified.append(TradeSignal(
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    market_question=signal.market_question,
                    side="BUY",
                    outcome=signal.outcome,
                    price=maker_price,
                    confidence=confidence,
                    strategy=self.name,
                    expected_value=ev,
                    order_type="GTC",
                    post_only=True,
                    cancel_after_ts=cancel_ts,
                    resolution_ts=signal.resolution_ts,
                    slug=signal.slug,
                ))
            elif self.min_price <= ask <= self.max_price:
                # Taker: buy at the ask
                confidence = self._score_confidence_with_liquidity(signal, price_data)
                ev = confidence * (1.0 - ask)
                verified.append(TradeSignal(
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    market_question=signal.market_question,
                    side="BUY",
                    outcome=signal.outcome,
                    price=ask,
                    confidence=confidence,
                    strategy=self.name,
                    expected_value=ev,
                    order_type="GTC",
                    resolution_ts=signal.resolution_ts,
                    slug=signal.slug,
                ))
            elif ask > self.max_price and self.min_price <= bid + 0.01 <= self.max_price:
                # Ask above max (including no asks at 1.0) — place maker bid just above best bid
                maker_price = round(bid + 0.01, 2)
                confidence = self._score_confidence_with_liquidity(signal, price_data)
                ev = confidence * (1.0 - maker_price)
                verified.append(TradeSignal(
                    market_id=signal.market_id,
                    token_id=signal.token_id,
                    market_question=signal.market_question,
                    side="BUY",
                    outcome=signal.outcome,
                    price=maker_price,
                    confidence=confidence,
                    strategy=self.name,
                    expected_value=ev,
                    order_type="GTC",
                    post_only=True,
                    cancel_after_ts=cancel_ts,
                    resolution_ts=signal.resolution_ts,
                    slug=signal.slug,
                ))
            else:
                clob_out_of_range += 1

        logger.info(
            f"high_probability: {len(gamma_signals)} gamma signals -> "
            f"top {len(top)} checked -> {len(verified)} verified "
            f"(miss={clob_miss}, out_of_range={clob_out_of_range})"
        )
        return verified

    def _pre_filter(self, markets: list[dict]) -> list[tuple[dict, int, float]]:
        """Use Gamma outcomePrices to find markets likely in our price range.

        Returns list of (market, outcome_index, gamma_price) tuples.
        """
        candidates = []
        # Widen the filter slightly vs our actual range to account for
        # Gamma prices being slightly stale vs live CLOB orderbook
        margin = 0.03
        low = self.min_price - margin
        high = self.max_price + margin

        for market in markets:
            tokens = market.get("clobTokenIds") or []
            if len(tokens) < 2:
                continue

            outcome_prices = _parse_outcome_prices(market)
            if not outcome_prices:
                continue

            for i, price in enumerate(outcome_prices):
                if i >= len(tokens):
                    continue
                if low <= price <= high:
                    candidates.append((market, i, price))
                elif self.longshot_min_price <= price <= self.longshot_threshold:
                    candidates.append((market, i, price))

        return candidates

    def _evaluate_from_gamma(self, market: dict, idx: int, gamma_price: float) -> TradeSignal | None:
        """Score a candidate using Gamma mid-market price (no CLOB call)."""
        tokens = market.get("clobTokenIds") or []
        outcomes = market.get("outcomes") or ["Yes", "No"]
        token_id = tokens[idx]
        outcome_name = outcomes[idx] if idx < len(outcomes) else f"Outcome_{idx}"

        is_longshot = gamma_price <= self.longshot_threshold

        if is_longshot:
            if not (self.longshot_min_price <= gamma_price <= self.longshot_threshold):
                return None
            confidence = self._score_longshot_confidence(market, gamma_price)
        else:
            if not (self.min_price <= gamma_price <= self.max_price):
                return None
            confidence = self._score_confidence(market, gamma_price)

        profit_margin = 1.0 - gamma_price
        expected_value = confidence * profit_margin

        if expected_value > 0:
            return TradeSignal(
                market_id=market.get("conditionId", market.get("condition_id", "")),
                token_id=token_id,
                market_question=market.get("question", "Unknown"),
                side="BUY",
                outcome=outcome_name,
                price=gamma_price,
                confidence=confidence,
                strategy=self.name,
                expected_value=expected_value,
                order_type="GTC",
                resolution_ts=_parse_resolution_ts(market),
                slug=market.get("_event_slug", ""),
            )
        return None

    def _score_confidence(self, market: dict, price: float) -> float:
        # Base confidence from price level (higher price = more certain)
        price_score = price  # 0.92 -> 0.92 confidence base

        # Volume bonus (higher volume = more reliable signal)
        volume = float(market.get("volume", 0) or 0)
        volume_score = min(volume / 10000, 1.0) * 0.05  # up to 5% bonus

        # Liquidity bonus
        liquidity = float(market.get("liquidity", 0) or 0)
        liq_score = min(liquidity / 5000, 1.0) * 0.03  # up to 3% bonus

        confidence = min(price_score + volume_score + liq_score, 0.99)
        return round(confidence, 4)

    def _score_longshot_confidence(self, market: dict, price: float) -> float:
        """Confidence model for long-shot outcomes (price 0.02-0.20)."""
        base = price * self.longshot_conf_multiplier  # e.g., 0.05 * 2.0 = 0.10

        # Volume bonus (up to 0.08)
        volume = float(market.get("volume", 0) or 0)
        volume_bonus = min(volume / 10000, 1.0) * 0.08

        # Liquidity bonus (up to 0.05)
        liquidity = float(market.get("liquidity", 0) or 0)
        liq_bonus = min(liquidity / 5000, 1.0) * 0.05

        confidence = min(base + volume_bonus + liq_bonus, 0.40)
        return round(confidence, 4)

    def _score_confidence_with_liquidity(self, gamma_signal: TradeSignal, price_data: dict) -> float:
        """Refine confidence using CLOB bid-ask spread as liquidity signal."""
        base = gamma_signal.confidence
        bid = price_data["bid"]
        ask = price_data["ask"]
        spread = ask - bid if ask < 1.0 else 0.10  # assume wide spread if no asks

        is_longshot = gamma_signal.price <= self.longshot_threshold
        if is_longshot:
            # Scale spread adjustment for long-shots to avoid huge relative swings
            # A 0.03 spread on a 0.05 price is normal, not alarming
            spread_adj = max(-0.01, min(0.01, 0.01 - spread * 0.5))
        else:
            # Tight spread = more confidence, wide spread = less
            spread_adj = max(-0.03, min(0.02, 0.02 - spread))

        return round(min(base + spread_adj, 0.99), 4)
