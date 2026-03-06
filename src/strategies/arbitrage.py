import logging
from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings

logger = logging.getLogger("poly-trade")


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.min_spread = settings.arb_min_spread

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        signals = []
        for market in markets:
            arb_signals = self._check_arbitrage(market, clob_client)
            signals.extend(arb_signals)

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _check_arbitrage(self, market: dict, clob_client) -> list[TradeSignal]:
        tokens = market.get("clobTokenIds") or []
        outcomes = market.get("outcomes") or ["Yes", "No"]

        if len(tokens) != 2:
            return []

        try:
            price_yes = clob_client.get_price(tokens[0])
            price_no = clob_client.get_price(tokens[1])
        except Exception:
            return []

        ask_yes = price_yes["ask"]
        ask_no = price_no["ask"]

        # Arbitrage: if buying YES + NO costs less than $1.00, guaranteed profit
        total_cost = ask_yes + ask_no
        spread = 1.0 - total_cost

        if spread < self.min_spread:
            return []

        logger.info(
            f"ARB FOUND: {market.get('question', 'Unknown')[:60]} | "
            f"YES@{ask_yes:.3f} + NO@{ask_no:.3f} = {total_cost:.4f} | spread={spread:.4f}"
        )

        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", "Unknown")

        signals = [
            TradeSignal(
                market_id=market_id,
                token_id=tokens[0],
                market_question=question,
                side="BUY",
                outcome=outcomes[0] if outcomes else "Yes",
                price=ask_yes,
                confidence=0.95,
                strategy=self.name,
                expected_value=spread / 2,
                order_type="FOK",
            ),
            TradeSignal(
                market_id=market_id,
                token_id=tokens[1],
                market_question=question,
                side="BUY",
                outcome=outcomes[1] if len(outcomes) > 1 else "No",
                price=ask_no,
                confidence=0.95,
                strategy=self.name,
                expected_value=spread / 2,
                order_type="FOK",
            ),
        ]
        return signals
