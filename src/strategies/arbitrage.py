import json
import logging
import re
from collections import defaultdict
from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.gamma_client import GammaClient

logger = logging.getLogger("poly-trade")


def _gamma_prices(market: dict) -> tuple[float, float] | None:
    """Extract YES/NO prices from Gamma outcomePrices field (no API call)."""
    raw = market.get("outcomePrices", "")
    if isinstance(raw, str):
        try:
            prices = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
    elif isinstance(raw, list):
        prices = raw
    else:
        return None
    if len(prices) >= 2:
        try:
            return float(prices[0]), float(prices[1])
        except (ValueError, TypeError):
            return None
    return None


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.min_spread = settings.arb_min_spread
        self.fee_rate = settings.arb_fee_rate
        self.min_event_markets = settings.arb_min_event_markets
        self.min_event_spread = settings.arb_min_event_spread
        self.max_event_legs = settings.arb_max_event_legs
        self.mono_min_spread = settings.arb_mono_min_spread
        self.gamma = GammaClient()

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        valid_markets = []
        for market in markets:
            tokens = market.get("clobTokenIds") or []
            if len(tokens) == 2:
                valid_markets.append(market)

        if not valid_markets:
            return []

        # --- Pass 1: pre-screen using free Gamma prices ---
        net_payout = 1.0 - self.fee_rate
        single_candidates = []
        for market in valid_markets:
            gp = _gamma_prices(market)
            if gp is None:
                single_candidates.append(market)  # no data → include conservatively
                continue
            yes_p, no_p = gp
            # Loose check: if sum is already well above net payout, skip
            if yes_p + no_p < net_payout + 0.02:
                single_candidates.append(market)

        # Group all markets by event slug for multi-outcome / monotonicity
        events: dict[str, list[dict]] = defaultdict(list)
        for market in valid_markets:
            slug = market.get("_event_slug", "")
            if slug:
                events[slug].append(market)

        # Classify events
        above_below_events: dict[str, list[dict]] = {}
        multi_outcome_events: dict[str, list[dict]] = {}
        for slug, mkts in events.items():
            if self._is_above_below(slug, mkts):
                above_below_events[slug] = mkts
            elif len(mkts) >= self.min_event_markets:
                multi_outcome_events[slug] = mkts

        # Pre-screen multi-outcome using Gamma prices
        multi_candidates: dict[str, list[dict]] = {}
        for slug, event_markets in multi_outcome_events.items():
            if len(event_markets) > self.max_event_legs:
                continue
            total = 0.0
            has_all_prices = True
            for m in event_markets:
                gp = _gamma_prices(m)
                if gp is None:
                    has_all_prices = False
                    break
                total += gp[0]  # YES price
            if not has_all_prices or total < net_payout + 0.02:
                multi_candidates[slug] = event_markets

        # 3. Monotonicity: merge scanner above/below with self-discovered (before CLOB fetch)
        discovered_events = self._discover_above_below_markets()
        for slug, mkts in discovered_events.items():
            if slug not in above_below_events:
                above_below_events[slug] = mkts
            else:
                existing_ids = {m.get("conditionId") for m in above_below_events[slug]}
                for m in mkts:
                    if m.get("conditionId") not in existing_ids:
                        above_below_events[slug].append(m)

        # Collect ALL token IDs for a single CLOB batch fetch
        needed_token_ids = set()
        for m in single_candidates:
            tokens = m.get("clobTokenIds") or []
            needed_token_ids.update(tokens[:2])
        for mkts in multi_candidates.values():
            for m in mkts:
                tokens = m.get("clobTokenIds") or []
                needed_token_ids.add(tokens[0])  # only YES token needed
        for mkts in above_below_events.values():
            for m in mkts:
                tokens = m.get("clobTokenIds") or []
                needed_token_ids.update(tokens[:2])  # both YES and NO needed

        logger.info(
            f"arbitrage: pre-screened {len(valid_markets)} markets → "
            f"{len(single_candidates)} single, "
            f"{len(multi_candidates)} multi-outcome, "
            f"{len(above_below_events)} above/below | "
            f"CLOB-fetching {len(needed_token_ids)} tokens"
        )

        # --- Pass 2: single CLOB batch fetch for everything ---
        price_map = {}
        if needed_token_ids:
            price_map = clob_client.get_orderbooks_batch(list(needed_token_ids))

        signals = []

        # 1. Single-market arb
        for market in single_candidates:
            arb_signals = self._check_single_market_arb(market, price_map)
            signals.extend(arb_signals)

        # 2. Multi-outcome arb
        if multi_candidates:
            logger.info(f"arbitrage: checking {len(multi_candidates)} multi-outcome events")
            for slug, event_markets in multi_candidates.items():
                event_signals = self._check_event_arb(slug, event_markets, price_map)
                signals.extend(event_signals)

        # 3. Monotonicity arb
        if above_below_events:
            logger.info(f"arbitrage: checking {len(above_below_events)} above/below events for monotonicity")
            for slug, event_markets in above_below_events.items():
                mono_signals = self._check_monotonicity_arb(slug, event_markets, price_map)
                signals.extend(mono_signals)

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    @staticmethod
    def _is_above_below(slug: str, markets: list[dict]) -> bool:
        slug_lower = slug.lower()
        if "above" in slug_lower:
            return True
        for m in markets:
            q = (m.get("question") or "").lower()
            if "above" in q:
                return True
        return False

    def _discover_above_below_markets(self) -> dict[str, list[dict]]:
        """Self-discover above/below crypto markets via Gamma slug lookup."""
        try:
            daily_markets = self.gamma.get_crypto_daily_markets(
                assets=["BTC", "ETH", "SOL"], lookahead_days=3,
            )
        except Exception as e:
            logger.warning(f"arbitrage: crypto daily discovery failed: {e}")
            return {}

        # Filter to above_below type only and normalize
        events: dict[str, list[dict]] = defaultdict(list)
        for m in daily_markets:
            if m.get("_market_type") != "above_below":
                continue
            if m.get("closed"):
                continue
            # Normalize JSON fields
            if isinstance(m.get("clobTokenIds"), str):
                try:
                    m["clobTokenIds"] = json.loads(m["clobTokenIds"])
                except (json.JSONDecodeError, TypeError):
                    continue
            if isinstance(m.get("outcomes"), str):
                try:
                    m["outcomes"] = json.loads(m["outcomes"])
                except (json.JSONDecodeError, TypeError):
                    m["outcomes"] = ["Yes", "No"]
            tokens = m.get("clobTokenIds") or []
            if len(tokens) != 2:
                continue
            slug = m.get("_event_slug", "")
            if slug:
                events[slug].append(m)

        if events:
            total = sum(len(mkts) for mkts in events.values())
            logger.info(f"arbitrage: discovered {total} above/below markets across {len(events)} events")
        return dict(events)

    def _check_single_market_arb(self, market: dict, price_map: dict) -> list[TradeSignal]:
        """Buy YES + NO in same market for < $1.00 minus fees."""
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

        total_cost = ask_yes + ask_no
        # Profit after fees: payout($1.00) - fees - cost
        spread = (1.0 - self.fee_rate) - total_cost

        if spread < self.min_spread:
            return []

        logger.info(
            f"ARB FOUND (single): {market.get('question', 'Unknown')[:60]} | "
            f"YES@{ask_yes:.3f} + NO@{ask_no:.3f} = {total_cost:.4f} | spread={spread:.4f}"
        )

        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", "Unknown")
        arb_group = f"single:{market_id}"

        return [
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
                arb_group=arb_group,
                slug=market.get("_event_slug", ""),
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
                arb_group=arb_group,
                slug=market.get("_event_slug", ""),
            ),
        ]

    def _check_event_arb(self, slug: str, event_markets: list[dict], price_map: dict) -> list[TradeSignal]:
        """Multi-outcome event arb: buy YES on all outcomes for < $1.00 minus fees."""
        if len(event_markets) > self.max_event_legs:
            return []

        market_asks: list[tuple[dict, float]] = []
        for market in event_markets:
            tokens = market.get("clobTokenIds") or []
            if len(tokens) < 1:
                continue
            yes_price = price_map.get(tokens[0])
            if yes_price is None:
                continue
            ask = yes_price["ask"]
            if ask >= 1.0 or ask <= 0:
                return []
            market_asks.append((market, ask))

        if len(market_asks) != len(event_markets):
            return []

        total_cost = sum(ask for _, ask in market_asks)
        spread = (1.0 - self.fee_rate) - total_cost

        if spread < self.min_event_spread:
            return []

        event_title = event_markets[0].get("_event_title", slug)
        logger.info(
            f"ARB FOUND (multi-outcome): {event_title[:60]} | "
            f"{len(market_asks)} outcomes | total_ask={total_cost:.4f} | spread={spread:.4f}"
        )

        signals = []
        per_market_ev = spread / len(market_asks)
        arb_group = f"multi:{slug}"
        for market, ask in market_asks:
            market_id = market.get("conditionId", market.get("condition_id", ""))
            tokens = market.get("clobTokenIds") or []
            question = market.get("question", "Unknown")
            outcomes = market.get("outcomes") or ["Yes", "No"]

            signals.append(TradeSignal(
                market_id=market_id,
                token_id=tokens[0],
                market_question=f"[EVENT ARB] {question}",
                side="BUY",
                outcome=outcomes[0] if outcomes else "Yes",
                price=ask,
                confidence=0.95,
                strategy=self.name,
                expected_value=per_market_ev,
                order_type="FOK",
                arb_group=arb_group,
                slug=slug,
            ))

        return signals

    @staticmethod
    def _parse_strike(question: str) -> float | None:
        """Extract dollar strike price from question string."""
        match = re.search(r'\$([\d,]+)', question)
        if not match:
            return None
        return float(match.group(1).replace(",", ""))

    def _check_monotonicity_arb(self, slug: str, event_markets: list[dict], price_map: dict) -> list[TradeSignal]:
        """Cross-market monotonicity arb for above/below crypto events.

        "Above $X" YES prices must decrease as strike increases.
        When violated: buy cheap lower-strike YES + buy expensive higher-strike NO.
        """
        # Parse strikes and collect prices
        strike_markets: list[tuple[float, dict, float, float]] = []  # (strike, market, yes_ask, no_ask)
        for market in event_markets:
            question = market.get("question", "")
            strike = self._parse_strike(question)
            if strike is None:
                continue

            tokens = market.get("clobTokenIds") or []
            if len(tokens) != 2:
                continue

            yes_price = price_map.get(tokens[0])
            no_price = price_map.get(tokens[1])
            if yes_price is None or no_price is None:
                continue

            yes_ask = yes_price["ask"]
            no_ask = no_price["ask"]
            if yes_ask <= 0 or no_ask <= 0:
                continue

            strike_markets.append((strike, market, yes_ask, no_ask))

        if len(strike_markets) < 2:
            return []

        # Sort by strike ascending
        strike_markets.sort(key=lambda x: x[0])

        signals = []
        # Check monotonicity: YES prices should DECREASE as strike increases
        for i in range(len(strike_markets) - 1):
            strike_low, mkt_low, yes_ask_low, _ = strike_markets[i]
            strike_high, mkt_high, _, no_ask_high = strike_markets[i + 1]

            # Violation: lower strike has CHEAPER YES than higher strike
            # i.e. yes_ask_low < yes_ask_high (should be the opposite)
            _, _, yes_ask_high, _ = strike_markets[i + 1]
            if yes_ask_low >= yes_ask_high:
                continue  # Monotonicity holds

            # Found violation: buy lower-strike YES (cheap) + higher-strike NO
            # Payout: guaranteed $1.00 in all scenarios
            # Cost: yes_ask_low + no_ask_high + 2*fee
            cost = yes_ask_low + no_ask_high
            spread = 1.0 - cost - 2 * self.fee_rate

            if spread < self.mono_min_spread:
                continue

            logger.info(
                f"ARB FOUND (monotonicity): {slug[:40]} | "
                f"${strike_low:,.0f} YES@{yes_ask_low:.3f} + ${strike_high:,.0f} NO@{no_ask_high:.3f} | "
                f"cost={cost:.4f} | spread={spread:.4f}"
            )

            arb_group = f"mono:{slug}:{strike_low:.0f}-{strike_high:.0f}"

            # Buy lower-strike YES
            tokens_low = mkt_low.get("clobTokenIds") or []
            market_id_low = mkt_low.get("conditionId", mkt_low.get("condition_id", ""))
            question_low = mkt_low.get("question", "Unknown")
            outcomes_low = mkt_low.get("outcomes") or ["Yes", "No"]

            signals.append(TradeSignal(
                market_id=market_id_low,
                token_id=tokens_low[0],
                market_question=f"[MONO ARB] {question_low}",
                side="BUY",
                outcome=outcomes_low[0] if outcomes_low else "Yes",
                price=yes_ask_low,
                confidence=0.95,
                strategy=self.name,
                expected_value=spread / 2,
                order_type="FOK",
                arb_group=arb_group,
                slug=slug,
            ))

            # Buy higher-strike NO
            tokens_high = mkt_high.get("clobTokenIds") or []
            market_id_high = mkt_high.get("conditionId", mkt_high.get("condition_id", ""))
            question_high = mkt_high.get("question", "Unknown")
            outcomes_high = mkt_high.get("outcomes") or ["Yes", "No"]

            signals.append(TradeSignal(
                market_id=market_id_high,
                token_id=tokens_high[1],
                market_question=f"[MONO ARB] {question_high}",
                side="BUY",
                outcome=outcomes_high[1] if len(outcomes_high) > 1 else "No",
                price=no_ask_high,
                confidence=0.95,
                strategy=self.name,
                expected_value=spread / 2,
                order_type="FOK",
                arb_group=arb_group,
                slug=slug,
            ))

        return signals
