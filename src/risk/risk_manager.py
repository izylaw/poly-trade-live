import logging
import time as _time
from dataclasses import dataclass
from src.config.settings import Settings
from src.risk.kelly import half_kelly, calc_payout_ratio
from src.risk.circuit_breaker import CircuitBreaker

logger = logging.getLogger("poly-trade")


@dataclass
class TradeSignal:
    market_id: str
    token_id: str
    market_question: str
    side: str  # "BUY"
    outcome: str  # "YES" or "NO"
    price: float
    confidence: float
    strategy: str
    expected_value: float = 0.0
    order_type: str = "GTC"
    post_only: bool = False
    cancel_after_ts: float = 0.0
    arb_group: str = ""
    resolution_ts: float = 0.0
    slug: str = ""
    asset: str = ""


@dataclass
class ApprovedTrade:
    signal: TradeSignal
    size: float
    cost: float
    kelly_fraction: float


class RiskManager:
    STRATEGY_MIN_CONFIDENCE = {
        "btc_updown": 0.60,
        "safe_compounder": 0.78,
        "sports_daily": 0.55,
        "high_probability": 0.06,
        "llm_crypto": 0.55,
        "arbitrage": 0.01,
        "weather_temperature": 0.20,
    }

    STRATEGY_MAX_PER_MARKET = {
        "arbitrage": 2,
        "safe_compounder": 2,
        "sports_daily": 2,
    }

    def __init__(self, settings: Settings, circuit_breaker: CircuitBreaker):
        self.settings = settings
        self.cb = circuit_breaker
        # Overridable by aggression tuner
        self.max_single_trade_pct = settings.max_single_trade_pct
        self.min_confidence = 0.70

    def _get_min_confidence(self, strategy: str) -> float:
        override = self.STRATEGY_MIN_CONFIDENCE.get(strategy)
        if override is not None:
            return override
        return self.min_confidence

    def _get_max_positions(self, strategy: str) -> int | None:
        mapping = {
            "high_probability": self.settings.high_prob_max_positions,
            "safe_compounder": self.settings.safe_compounder_max_positions,
            "sports_daily": self.settings.sports_daily_max_positions,
            "btc_updown": self.settings.btc_updown_max_positions,
            "llm_crypto": self.settings.llm_max_positions,
            "weather_temperature": self.settings.weather_max_positions,
        }
        return mapping.get(strategy)

    def evaluate(self, signal: TradeSignal, balance: float,
                 open_positions: list[dict], portfolio_exposure: float) -> ApprovedTrade | None:
        # Circuit breaker check
        if self.cb.is_paused:
            remaining = self.cb.pause_remaining_seconds
            logger.debug(f"Circuit breaker active, {remaining:.0f}s remaining")
            return None

        # Hard floor check
        if balance <= self.settings.hard_floor:
            logger.warning(f"Balance ${balance:.2f} at/below hard floor ${self.settings.hard_floor:.2f}")
            return None

        # Confidence check
        if signal.confidence < self._get_min_confidence(signal.strategy):
            logger.debug(f"Signal confidence {signal.confidence:.2f} below min {self.min_confidence:.2f}")
            return None

        # Classify signal as long-term or short-term
        is_long_term = (signal.resolution_ts > 0 and
                        signal.resolution_ts - _time.time() > self.settings.long_term_threshold_days * 86400)

        if is_long_term:
            # Long-term positions go into a separate additive bucket
            long_term_count = sum(1 for p in open_positions if p.get("is_long_term"))
            if long_term_count >= self.settings.max_long_term_positions:
                logger.debug(f"Long-term position limit ({self.settings.max_long_term_positions}) reached")
                return None
        elif signal.strategy != "arbitrage":
            # Global cap for short-term non-arb positions
            non_arb = [p for p in open_positions
                       if p.get("strategy") != "arbitrage" and not p.get("is_long_term")]
            if len(non_arb) >= self.settings.max_open_positions:
                logger.debug(f"Max open positions ({self.settings.max_open_positions}) reached")
                return None

            # Per-strategy position limit
            strategy_max = self._get_max_positions(signal.strategy)
            if strategy_max is not None:
                strategy_count = sum(1 for p in non_arb
                                     if p.get("strategy") == signal.strategy)
                if strategy_count >= strategy_max:
                    logger.debug(f"Strategy {signal.strategy} at position limit ({strategy_max})")
                    return None

        # Per-market concentration check (strategy-aware)
        market_positions = [p for p in open_positions if p.get("market_id") == signal.market_id]
        max_for_market = self.STRATEGY_MAX_PER_MARKET.get(signal.strategy, self.settings.max_positions_per_market)
        if len(market_positions) >= max_for_market:
            logger.debug(f"Already have {len(market_positions)} position(s) on market {signal.market_id[:12]}")
            return None

        # Portfolio exposure check
        if portfolio_exposure >= balance * self.settings.max_portfolio_exposure_pct:
            logger.debug(f"Portfolio exposure ${portfolio_exposure:.2f} >= limit")
            return None

        # Daily loss check
        if not self.cb.check_daily_loss(balance):
            return None

        # Calculate position size via half-kelly
        payout_ratio = calc_payout_ratio(signal.price)
        if payout_ratio <= 0:
            return None

        available = balance - self.settings.hard_floor
        size = half_kelly(
            win_prob=signal.confidence,
            payout_ratio=payout_ratio,
            available_balance=available,
            min_trade=self.settings.min_trade_size,
            max_trade_pct=self.max_single_trade_pct,
        )

        if size <= 0:
            logger.debug(f"Kelly sizing returned 0 for {signal.market_question[:50]}")
            return None

        cost = size * signal.price

        # Ensure cost doesn't exceed available balance minus hard floor
        if balance - cost < self.settings.hard_floor:
            cost = balance - self.settings.hard_floor - 0.01
            size = cost / signal.price if signal.price > 0 else 0
            if size < self.settings.min_trade_size:
                return None

        kelly_f = (signal.confidence * payout_ratio - (1 - signal.confidence)) / payout_ratio

        logger.info(
            f"APPROVED: {signal.strategy} | {signal.outcome}@{signal.price:.3f} | "
            f"size={size:.2f} cost=${cost:.2f} | kelly={kelly_f:.3f} conf={signal.confidence:.2f}"
        )
        return ApprovedTrade(signal=signal, size=round(size, 2), cost=round(cost, 2), kelly_fraction=kelly_f)
