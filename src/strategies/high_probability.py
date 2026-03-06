import logging
from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


class HighProbabilityStrategy(Strategy):
    name = "high_probability"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.min_price = settings.high_prob_min_price
        self.max_price = settings.high_prob_max_price

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        signals = []
        for market in markets:
            signal = self._evaluate_market(market, clob_client)
            if signal:
                signals.append(signal)

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _evaluate_market(self, market: dict, clob_client) -> TradeSignal | None:
        tokens = market.get("clobTokenIds") or []
        outcomes = market.get("outcomes") or ["Yes", "No"]

        if len(tokens) < 2:
            return None

        # Check each outcome for high probability
        for i, token_id in enumerate(tokens):
            outcome_name = outcomes[i] if i < len(outcomes) else f"Outcome_{i}"
            price_data = clob_client.get_price(token_id)
            if price_data is None:
                continue

            ask_price = price_data["ask"]

            # We want to BUY outcomes priced between min_price and max_price
            # These are near-certain outcomes with small but reliable profit
            if self.min_price <= ask_price <= self.max_price:
                confidence = self._score_confidence(market, ask_price)
                profit_margin = 1.0 - ask_price
                expected_value = confidence * profit_margin

                if expected_value > 0:
                    return TradeSignal(
                        market_id=market.get("conditionId", market.get("condition_id", "")),
                        token_id=token_id,
                        market_question=market.get("question", "Unknown"),
                        side="BUY",
                        outcome=outcome_name,
                        price=ask_price,
                        confidence=confidence,
                        strategy=self.name,
                        expected_value=expected_value,
                        order_type="GTC",
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
