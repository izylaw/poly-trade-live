"""Safe Compounder strategy — capital-preserving compound growth via crypto up/down markets.

Innovations over btc_updown:
1. Dual-side quoting — bid on BOTH Up and Down for risk-free fills
2. Cross-asset momentum — BTC leads ETH/SOL (lead-lag)
3. Late-window sniping — only trade when progress > 0.65 and confidence > 0.78
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from src.strategies.base import Strategy
from src.strategies.crypto_utils import (
    estimate_outcome_probability,
    compute_price_delta,
    compute_momentum,
    get_smart_bid,
    clamp,
)
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.binance_client import BinanceClient
from src.market_data.gamma_client import GammaClient

logger = logging.getLogger("poly-trade")


class SafeCompounderStrategy(Strategy):
    name = "safe_compounder"
    self_discovering = True

    def __init__(self, settings: Settings, binance: BinanceClient | None = None):
        super().__init__(settings)
        self.binance = binance or BinanceClient()
        self.gamma = GammaClient()
        self.assets = settings.safe_compounder_assets
        self.intervals = settings.safe_compounder_intervals
        self.min_confidence = settings.safe_compounder_min_confidence
        self.min_edge = settings.safe_compounder_min_edge
        self.maker_edge_cushion = settings.safe_compounder_maker_edge_cushion
        self.min_window_progress = settings.safe_compounder_min_window_progress
        self.dual_side_max_combined = settings.safe_compounder_dual_side_max_combined
        self.cross_asset_boost_cap = settings.safe_compounder_cross_asset_boost_cap
        self.btc_5m_vol = settings.btc_updown_5m_vol
        self.logistic_k = settings.btc_updown_logistic_k
        self.momentum_weight = settings.btc_updown_momentum_weight
        self.min_bid = settings.btc_updown_min_ask
        self.max_bid = settings.btc_updown_max_ask
        self._pending_predictions: list[dict] = []
        self._btc_delta_cache: dict[str, float] = {}

    def _prefetch_asset_data(self, asset: str) -> dict:
        """Fetch all Binance data for an asset in one go."""
        price = self.binance.get_price(asset)
        klines_1m = self.binance.get_klines(asset, "1m", 30)
        atr = None
        try:
            atr = self.binance.compute_atr(asset, "5m", 14)
        except Exception:
            pass
        momentum = compute_momentum(self.binance, asset)
        return {
            "price": price,
            "klines_1m": klines_1m,
            "atr": atr,
            "momentum": momentum,
        }

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        self._pending_predictions = []
        self._btc_delta_cache = {}

        # Parallel prefetch all asset data
        if not self.assets:
            return []
        asset_data = {}
        with ThreadPoolExecutor(max_workers=len(self.assets)) as pool:
            futures = {asset: pool.submit(self._prefetch_asset_data, asset)
                       for asset in self.assets}
            for asset, future in futures.items():
                try:
                    asset_data[asset] = future.result()
                except Exception as e:
                    logger.warning(f"safe_compounder: prefetch failed for {asset}: {e}")

        signals = []
        for asset in self.assets:
            if asset not in asset_data:
                continue
            for interval in self.intervals:
                try:
                    sigs = self._analyze_asset_interval(
                        asset, interval, clob_client, asset_data[asset],
                    )
                    signals.extend(sigs)
                except Exception as e:
                    logger.warning(f"safe_compounder: error analyzing {asset} {interval}: {e}")

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _analyze_asset_interval(self, asset: str, interval: str, clob_client,
                                prefetched: dict) -> list[TradeSignal]:
        active_markets = self._discover_markets(asset, interval)
        if not active_markets:
            return []

        momentum = prefetched["momentum"]

        signals = []
        for market in active_markets:
            delta_info = compute_price_delta(
                self.binance, asset, market, self.btc_5m_vol, logger,
                prefetched=prefetched,
            )
            window_progress = delta_info["window_progress"]

            # Late-window filter: only trade when progress > threshold
            if not self._is_late_window(window_progress):
                logger.debug(
                    f"safe_compounder: {asset} {interval} progress={window_progress:.2f} "
                    f"< {self.min_window_progress} — skipping (not late window)"
                )
                continue

            # Cache BTC delta for cross-asset boost
            if asset == "BTC":
                self._btc_delta_cache[interval] = delta_info["delta_pct"]

            dynamic_vol = delta_info.get("dynamic_vol", self.btc_5m_vol)

            # Compute probabilities for both sides
            prob_up = estimate_outcome_probability(
                "Up", delta_info["delta_pct"], window_progress,
                momentum, dynamic_vol, self.logistic_k, self.momentum_weight,
            )
            prob_down = estimate_outcome_probability(
                "Down", delta_info["delta_pct"], window_progress,
                momentum, dynamic_vol, self.logistic_k, self.momentum_weight,
            )

            # Apply cross-asset boost
            boost = self._cross_asset_boost(asset, delta_info)
            if boost > 0:
                # Boost the favored side based on BTC direction
                btc_delta = self._get_btc_delta(interval)
                if btc_delta > 0:
                    prob_up = clamp(prob_up + boost, 0.05, 0.95)
                    prob_down = clamp(1.0 - prob_up, 0.05, 0.95)
                elif btc_delta < 0:
                    prob_down = clamp(prob_down + boost, 0.05, 0.95)
                    prob_up = clamp(1.0 - prob_down, 0.05, 0.95)
                logger.info(f"safe_compounder: CROSS_ASSET_BOOST {asset} boost={boost:.3f} btc_delta={btc_delta:+.4%}")

            # Try dual-side quoting first
            dual_signals = self._generate_dual_side_signals(
                market, asset, interval, prob_up, prob_down,
                delta_info, momentum, dynamic_vol, clob_client,
            )
            if dual_signals:
                signals.extend(dual_signals)
                continue  # don't also place directional on same market

            # Fall back to high-conviction directional
            directional = self._generate_directional_signal(
                market, asset, interval, prob_up, prob_down,
                delta_info, momentum, dynamic_vol, clob_client,
            )
            if directional:
                signals.append(directional)

        return signals

    def _is_late_window(self, progress: float) -> bool:
        return progress >= self.min_window_progress

    def _get_btc_delta(self, interval: str) -> float:
        cached = self._btc_delta_cache.get(interval)
        if cached is not None:
            return cached
        # Fetch fresh BTC delta if not cached
        try:
            price = self.binance.get_price("BTC")
            klines = self.binance.get_klines("BTC", interval="1m", limit=10)
            if klines:
                ref = klines[0]["open"]
                delta = (price - ref) / ref if ref > 0 else 0.0
                self._btc_delta_cache[interval] = delta
                return delta
        except Exception:
            pass
        return 0.0

    def _cross_asset_boost(self, target_asset: str, delta_info: dict) -> float:
        """Boost confidence in ETH/SOL using BTC's confirmed move."""
        if target_asset == "BTC":
            return 0.0

        btc_delta = self._get_btc_delta("5m")  # use 5m as primary
        target_delta = delta_info["delta_pct"]

        if btc_delta == 0:
            return 0.0

        # BTC moved but target hasn't followed yet
        lag_ratio = target_delta / btc_delta if btc_delta != 0 else 1.0
        if lag_ratio < 0.5:
            boost = min(abs(btc_delta) / self.btc_5m_vol * 0.1, self.cross_asset_boost_cap)
            return boost

        return 0.0

    def _generate_dual_side_signals(
        self, market: dict, asset: str, interval: str,
        prob_up: float, prob_down: float,
        delta_info: dict, momentum: float, dynamic_vol: float,
        clob_client,
    ) -> list[TradeSignal]:
        """Generate dual-side bid signals when combined cost < threshold."""
        outcomes = market.get("outcomes", [])
        tokens = market.get("clobTokenIds", [])
        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", f"Crypto {asset}")
        resolution_ts = delta_info.get("resolution_ts", 0)

        if len(outcomes) < 2 or len(tokens) < 2:
            return []

        # Both sides must have positive expected value
        if prob_up <= 0.52 or prob_down <= 0.52:
            return []

        # Get books for both sides
        book_up = None
        book_down = None
        try:
            book_up = clob_client.get_book(tokens[0])
        except Exception:
            pass
        try:
            book_down = clob_client.get_book(tokens[1])
        except Exception:
            pass

        bid_up = get_smart_bid(prob_up, book_up, self.maker_edge_cushion, self.min_edge)
        bid_down = get_smart_bid(prob_down, book_down, self.maker_edge_cushion, self.min_edge)

        combined = bid_up + bid_down

        # Log prediction for both sides regardless
        for i, (outcome, prob, bid) in enumerate([
            (outcomes[0], prob_up, bid_up),
            (outcomes[1], prob_down, bid_down),
        ]):
            self._pending_predictions.append({
                "strategy": self.name,
                "asset": asset,
                "interval": interval,
                "market_id": market_id,
                "token_id": tokens[i],
                "outcome": outcome,
                "est_prob": prob,
                "bid_price": bid,
                "delta_pct": delta_info.get("delta_pct"),
                "window_progress": delta_info.get("window_progress"),
                "momentum": momentum,
                "dynamic_vol": dynamic_vol,
                "resolution_ts": resolution_ts,
                "traded": False,
                "signal_type": "dual_side_candidate",
            })

        if combined >= self.dual_side_max_combined:
            logger.info(
                f"safe_compounder: DUAL_SIDE rejected {asset} {interval} | "
                f"bid_up={bid_up:.2f} + bid_down={bid_down:.2f} = {combined:.2f} "
                f">= {self.dual_side_max_combined}"
            )
            return []

        # Validate bids in tradeable range
        if bid_up < self.min_bid or bid_up > self.max_bid:
            return []
        if bid_down < self.min_bid or bid_down > self.max_bid:
            return []

        guaranteed_profit = 1.0 - combined
        logger.info(
            f"safe_compounder: DUAL_SIDE {asset} {interval} | "
            f"bid_up={bid_up:.2f} bid_down={bid_down:.2f} | "
            f"combined={combined:.2f} | guaranteed_profit={guaranteed_profit:.2f}"
        )

        # Mark predictions as traded
        self._pending_predictions[-2]["traded"] = True
        self._pending_predictions[-2]["signal_type"] = "dual_side"
        self._pending_predictions[-1]["traded"] = True
        self._pending_predictions[-1]["signal_type"] = "dual_side"

        signals = []
        for i, (outcome, prob, bid, token_id) in enumerate([
            (outcomes[0], prob_up, bid_up, tokens[0]),
            (outcomes[1], prob_down, bid_down, tokens[1]),
        ]):
            ev = prob * (1.0 - bid) - (1.0 - prob) * bid
            signals.append(TradeSignal(
                market_id=market_id,
                token_id=token_id,
                market_question=question,
                side="BUY",
                outcome=outcome,
                price=bid,
                confidence=prob,
                strategy=self.name,
                expected_value=ev,
                order_type="GTC",
                post_only=True,
                cancel_after_ts=resolution_ts - 30 if resolution_ts else 0,
                resolution_ts=resolution_ts,
            ))

        return signals

    def _generate_directional_signal(
        self, market: dict, asset: str, interval: str,
        prob_up: float, prob_down: float,
        delta_info: dict, momentum: float, dynamic_vol: float,
        clob_client,
    ) -> TradeSignal | None:
        """Generate a single high-conviction directional signal."""
        outcomes = market.get("outcomes", [])
        tokens = market.get("clobTokenIds", [])
        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", f"Crypto {asset}")
        resolution_ts = delta_info.get("resolution_ts", 0)

        best_signal = None
        best_ev = 0.0

        vol_ratio = dynamic_vol / self.btc_5m_vol if self.btc_5m_vol > 0 else 1.0
        effective_min_edge = self.min_edge * max(vol_ratio, 1.0)

        for i, (outcome, est_prob) in enumerate([(outcomes[0], prob_up), (outcomes[1], prob_down)]):
            if i >= len(tokens):
                continue

            token_id = tokens[i]

            book = None
            try:
                book = clob_client.get_book(token_id)
            except Exception:
                pass

            bid_price = get_smart_bid(est_prob, book, self.maker_edge_cushion, self.min_edge)

            pred = {
                "strategy": self.name,
                "asset": asset,
                "interval": interval,
                "market_id": market_id,
                "token_id": token_id,
                "outcome": outcome,
                "est_prob": est_prob,
                "bid_price": bid_price,
                "delta_pct": delta_info.get("delta_pct"),
                "window_progress": delta_info.get("window_progress"),
                "momentum": momentum,
                "dynamic_vol": dynamic_vol,
                "resolution_ts": resolution_ts,
                "traded": False,
                "signal_type": "directional",
            }

            if bid_price < self.min_bid or bid_price > self.max_bid:
                self._pending_predictions.append(pred)
                continue

            # High-conviction filter
            if est_prob < self.min_confidence:
                self._pending_predictions.append(pred)
                continue

            edge = est_prob - bid_price
            ev = est_prob * (1.0 - bid_price) - (1.0 - est_prob) * bid_price

            if edge < effective_min_edge or ev <= 0:
                self._pending_predictions.append(pred)
                continue

            pred["traded"] = True
            self._pending_predictions.append(pred)

            logger.info(
                f"safe_compounder: DIRECTIONAL {asset} {outcome} {interval} | "
                f"bid={bid_price:.2f} | prob={est_prob:.2f} | edge={edge:+.3f} | EV={ev:+.3f}"
            )

            if ev > best_ev:
                best_ev = ev
                best_signal = TradeSignal(
                    market_id=market_id,
                    token_id=token_id,
                    market_question=question,
                    side="BUY",
                    outcome=outcome,
                    price=bid_price,
                    confidence=est_prob,
                    strategy=self.name,
                    expected_value=ev,
                    order_type="GTC",
                    post_only=True,
                    cancel_after_ts=resolution_ts - 30 if resolution_ts else 0,
                    resolution_ts=resolution_ts,
                )

        return best_signal

    # --- Market discovery (reuses btc_updown pattern) ---

    def _discover_markets(self, asset: str, interval: str) -> list[dict]:
        interval_secs = GammaClient.INTERVAL_SECONDS.get(interval, 300)

        raw_markets = self.gamma.get_crypto_updown_markets(asset, interval)
        if not raw_markets:
            return []

        now = time.time()
        filtered = []
        for m in raw_markets:
            start_ts = m.get("_start_ts")
            if start_ts is None:
                continue
            resolution_ts = start_ts + interval_secs

            # Only include markets that haven't resolved yet
            if resolution_ts <= now:
                continue

            m["clobTokenIds"] = self._parse_json_field(m.get("clobTokenIds", []))
            m["outcomes"] = self._parse_json_field(m.get("outcomes", []))
            if len(m["clobTokenIds"]) >= 2 and len(m["outcomes"]) >= 2:
                m["_resolution_ts"] = resolution_ts
                filtered.append(m)

        if filtered:
            logger.info(f"safe_compounder: {asset} {interval}: {len(filtered)} active markets")

        return filtered

    @staticmethod
    def _parse_json_field(value):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return []
        return value if isinstance(value, list) else []
