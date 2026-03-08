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
        # Pre-filter to valid 2-outcome markets
        valid_markets = []
        all_token_ids = []
        for market in markets:
            tokens = market.get("clobTokenIds") or []
            if len(tokens) != 2:
                continue
            valid_markets.append(market)
            all_token_ids.extend(tokens[:2])

        if not valid_markets:
            return []

        # Batch-fetch all orderbooks in one call (instead of 2N sequential calls)
        price_map = clob_client.get_orderbooks_batch(all_token_ids) if all_token_ids else {}
        logger.info(f"arbitrage: checking {len(valid_markets)} markets (batch-fetched {len(price_map)} books)")

        signals = []
        for market in valid_markets:
            arb_signals = self._check_arbitrage(market, price_map)
            signals.extend(arb_signals)

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _check_arbitrage(self, market: dict, price_map: dict) -> list[TradeSignal]:
        tokens = market.get("clobTokenIds") or []
        outcomes = market.get("outcomes") or ["Yes", "No"]

        if len(tokens) != 2:
            return []

        price_yes = price_map.get(tokens[0])
        price_no = price_map.get(tokens[1])
        if price_yes is None or price_no is None:
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
