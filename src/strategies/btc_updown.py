import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor
from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.binance_client import BinanceClient
from src.market_data.gamma_client import GammaClient
from src.market_data.crypto_discovery import discover_crypto_markets, INTERVAL_WINDOWS

logger = logging.getLogger("poly-trade")


class BtcUpdownStrategy(Strategy):
    name = "btc_updown"
    self_discovering = True

    def __init__(self, settings: Settings, binance: BinanceClient | None = None):
        super().__init__(settings)
        self.binance = binance or BinanceClient()
        self.gamma = GammaClient()
        self.assets = settings.btc_updown_assets
        self.intervals = settings.btc_updown_intervals
        self.min_edge = settings.btc_updown_min_edge
        self.btc_5m_vol = settings.btc_updown_5m_vol
        self.logistic_k = settings.btc_updown_logistic_k
        self.momentum_weight = settings.btc_updown_momentum_weight
        self.min_ask = settings.btc_updown_min_ask
        self.max_ask = settings.btc_updown_max_ask
        self.taker_fee_rate = settings.btc_updown_taker_fee_rate
        self.maker_edge_cushion = settings.btc_updown_maker_edge_cushion
        self.min_confidence = settings.btc_updown_min_confidence
        self.fok_threshold_secs = settings.btc_updown_fok_threshold_secs
        self._pending_predictions: list[dict] = []

    def _prefetch_asset_data(self, asset: str) -> dict:
        """Fetch all Binance data for an asset in one go."""
        price = self.binance.get_price(asset)
        klines_1m = self.binance.get_klines(asset, "1m", 30)
        atr = None
        try:
            atr = self.binance.compute_atr(asset, "5m", 14)
        except Exception:
            pass
        momentum = self._compute_momentum_confirmation(asset)
        return {
            "price": price,
            "klines_1m": klines_1m,
            "atr": atr,
            "momentum": momentum,
        }

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        self._pending_predictions = []

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
                    logger.warning(f"btc_updown: prefetch failed for {asset}: {e}")

        if asset_data:
            assets_str = ", ".join(asset_data.keys())
            logger.debug(f"btc_updown: fetched data for {len(asset_data)} assets ({assets_str})")

        signals = []
        for asset in self.assets:
            if asset not in asset_data:
                continue
            for interval in self.intervals:
                try:
                    asset_signals = self._analyze_asset_interval(
                        asset, interval, clob_client, asset_data[asset],
                    )
                    signals.extend(asset_signals)
                except Exception as e:
                    logger.warning(f"btc_updown: error analyzing {asset} {interval}: {e}")
        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _analyze_asset_interval(self, asset: str, interval: str, clob_client,
                                prefetched: dict) -> list[TradeSignal]:
        active_markets = self._discover_markets(asset, interval)
        if not active_markets:
            logger.debug(f"btc_updown: no active {asset} {interval} markets found")
            return []

        # Compute delta once per asset-interval using prefetched data
        price = prefetched["price"]
        klines = prefetched["klines_1m"]
        atr = prefetched["atr"]
        atr_vol = atr / price if atr and price > 0 else self.btc_5m_vol
        momentum = prefetched["momentum"]

        signals = []
        for market in active_markets:
            delta_info = self._compute_delta_from_prefetched(price, klines, atr_vol, market)
            signal, predictions = self._evaluate_both_sides(
                market, asset, delta_info, momentum, clob_client, interval=interval,
            )
            self._pending_predictions.extend(predictions)
            if signal:
                signals.append(signal)
        return sorted(signals, key=lambda s: s.expected_value, reverse=True)

    def _compute_delta_from_prefetched(self, current_price: float, klines: list[dict],
                                       dynamic_vol: float, market: dict) -> dict:
        """Compute price delta using pre-fetched data (no Binance calls)."""
        start_ts = market.get("_start_ts", 0)
        resolution_ts = market.get("_resolution_ts", 0)

        reference_price = current_price  # fallback: no delta
        if klines:
            start_ts_ms = start_ts * 1000
            for k in klines:
                if k["open_time"] <= start_ts_ms <= k["close_time"]:
                    reference_price = k["open"]
                    break
            else:
                if klines[0]["open_time"] > start_ts_ms:
                    reference_price = klines[0]["open"]

        now = time.time()
        total_window = resolution_ts - start_ts if resolution_ts > start_ts else 300
        elapsed = now - start_ts
        window_progress = _clamp(elapsed / total_window, 0.0, 1.0)

        delta_pct = (current_price - reference_price) / reference_price if reference_price > 0 else 0.0

        logger.debug(
            f"btc_updown: delta={delta_pct:+.4%} | ref=${reference_price:,.2f} | "
            f"now=${current_price:,.2f} | progress={window_progress:.2f} | "
            f"vol={dynamic_vol:.4f} vs {self.btc_5m_vol:.4f} (baseline)"
        )

        return {
            "current_price": current_price,
            "reference_price": reference_price,
            "delta_pct": delta_pct,
            "time_remaining": resolution_ts - now,
            "window_progress": window_progress,
            "dynamic_vol": dynamic_vol,
            "resolution_ts": resolution_ts,
        }

    def _compute_momentum_confirmation(self, asset: str) -> float:
        try:
            trade_flow = self._calc_trade_flow(asset)
        except Exception:
            trade_flow = 0.0

        try:
            ob_imbalance = self._calc_orderbook_imbalance(asset)
        except Exception:
            ob_imbalance = 0.0

        return trade_flow * 0.6 + ob_imbalance * 0.4

    def _evaluate_both_sides(self, market: dict, asset: str, delta_info: dict,
                             momentum: float, clob_client,
                             interval: str = "5m") -> tuple[TradeSignal | None, list[dict]]:
        outcomes = market.get("outcomes", [])
        tokens = market.get("clobTokenIds", [])
        market_id = market.get("conditionId", market.get("condition_id", ""))
        question = market.get("question", f"Crypto {asset}")

        best_signal = None
        best_ev = 0.0
        predictions = []

        dynamic_vol = delta_info.get("dynamic_vol", self.btc_5m_vol)

        # Volatility regime scaling: require more edge when vol is high (capped at 2x)
        vol_ratio = dynamic_vol / self.btc_5m_vol if self.btc_5m_vol > 0 else 1.0
        vol_ratio = min(max(vol_ratio, 1.0), 2.0)
        effective_min_edge = self.min_edge * vol_ratio

        resolution_ts = delta_info.get("resolution_ts", 0)

        # Extract Gamma prices for fallback when CLOB book is empty
        gamma_prices = self._extract_gamma_prices(market)
        window_progress = delta_info.get("window_progress", 0.0)

        for i, outcome in enumerate(outcomes):
            if i >= len(tokens):
                continue

            token_id = tokens[i]

            # Fetch full book for smart bid placement
            book = None
            try:
                book = clob_client.get_book(token_id)
            except Exception:
                pass

            # Extract prices for logging
            price_data = clob_client.get_price(token_id)
            best_bid = price_data["bid"] if price_data else 0.0
            best_ask = price_data["ask"] if price_data else 1.0

            # Gamma price fallback: when CLOB book is empty (spread > 0.90),
            # use Gamma's bestBid/bestAsk for pricing
            clob_spread = best_ask - best_bid
            gamma_bid = gamma_prices.get(f"bid_{i}", 0.0)
            gamma_ask = gamma_prices.get(f"ask_{i}", 1.0)
            use_gamma_prices = clob_spread > 0.90 and gamma_bid > 0.01

            if use_gamma_prices:
                logger.debug(
                    f"btc_updown: CLOB empty for {asset} {outcome} "
                    f"[{best_bid:.2f}/{best_ask:.2f}], using Gamma "
                    f"[{gamma_bid:.2f}/{gamma_ask:.2f}]"
                )

            # Use Gamma bid or CLOB bid as market reference price
            market_ref_bid = gamma_bid if use_gamma_prices else best_bid

            # Compute unblended model probability for disagreement check
            model_prob = self._compute_model_probability(
                outcome, delta_info["delta_pct"], delta_info["window_progress"],
                momentum, dynamic_vol,
            )

            # Market disagreement check: skip when unblended model and market diverge >25 points
            if market_ref_bid > 0.02 and abs(model_prob - market_ref_bid) > 0.25:
                logger.debug(
                    f"btc_updown: skip {asset} {outcome} — market disagreement "
                    f"model_prob={model_prob:.2f} vs market={market_ref_bid:.2f}"
                )
                pred = {
                    "strategy": self.name, "asset": asset, "interval": interval,
                    "market_id": market_id, "token_id": token_id, "outcome": outcome,
                    "est_prob": model_prob, "bid_price": 0.0,
                    "delta_pct": delta_info.get("delta_pct"),
                    "window_progress": delta_info.get("window_progress"),
                    "momentum": momentum, "dynamic_vol": dynamic_vol,
                    "resolution_ts": resolution_ts, "traded": False,
                    "skip_reason": "market_disagreement",
                }
                predictions.append(pred)
                continue

            # Blend model with market for final estimate
            est_prob = self._estimate_outcome_probability(
                outcome, delta_info["delta_pct"], delta_info["window_progress"],
                momentum, dynamic_vol, market_bid=market_ref_bid,
            )

            # Minimum confidence filter — skip low-quality signals early
            if est_prob < self.min_confidence:
                pred = {
                    "strategy": self.name, "asset": asset, "interval": interval,
                    "market_id": market_id, "token_id": token_id, "outcome": outcome,
                    "est_prob": est_prob, "bid_price": 0.0,
                    "delta_pct": delta_info.get("delta_pct"),
                    "window_progress": delta_info.get("window_progress"),
                    "momentum": momentum, "dynamic_vol": dynamic_vol,
                    "resolution_ts": resolution_ts, "traded": False,
                    "skip_reason": "low_confidence",
                }
                predictions.append(pred)
                continue

            # Smart bid placement using book depth + Gamma fallback
            bid_price = self._get_smart_bid(
                est_prob, book,
                gamma_bid=gamma_bid if use_gamma_prices else None,
                gamma_ask=gamma_ask if use_gamma_prices else None,
            )

            # Build prediction record for every candidate
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
            }

            time_remaining = delta_info.get("time_remaining", float("inf"))
            use_fok = time_remaining < self.fok_threshold_secs

            # FOK taker for short-lived markets OR late-window strong signal
            should_take = (
                (use_fok and use_gamma_prices and gamma_ask < est_prob - 0.01)
                or (use_gamma_prices and window_progress > 0.70
                    and est_prob > 0.65 and gamma_ask < est_prob - 0.01)
            )
            if should_take:
                taker_price = gamma_ask
                taker_ev = est_prob * (1.0 - taker_price) * (1.0 - self.taker_fee_rate) \
                    - (1.0 - est_prob) * taker_price
                if taker_ev > 0 and self.min_ask <= taker_price <= self.max_ask:
                    taker_edge = est_prob - taker_price
                    order_type = "FOK" if use_fok else "GTC"
                    logger.info(
                        f"btc_updown: TAKER({order_type}) {asset} {outcome} | "
                        f"ask=${taker_price:.2f} | est_prob={est_prob:.2f} | "
                        f"edge={taker_edge:+.3f} | EV={taker_ev:+.3f} "
                        f"(after {self.taker_fee_rate:.2%} fee) | "
                        f"progress={window_progress:.2f} | "
                        f"time_remaining={time_remaining:.0f}s"
                    )
                    pred["traded"] = True
                    pred["bid_price"] = taker_price
                    predictions.append(pred)
                    if taker_ev > best_ev:
                        best_ev = taker_ev
                        best_signal = TradeSignal(
                            market_id=market_id,
                            token_id=token_id,
                            market_question=question,
                            side="BUY",
                            outcome=outcome,
                            price=taker_price,
                            confidence=est_prob,
                            strategy=self.name,
                            expected_value=taker_ev,
                            order_type=order_type,
                            post_only=False,
                            cancel_after_ts=resolution_ts - 30 if resolution_ts else 0,
                            resolution_ts=resolution_ts,
                            slug=market.get("_event_slug", ""),
                            asset=asset,
                        )
                    continue

            # Validate bid is in tradeable range
            if bid_price < self.min_ask or bid_price > self.max_ask:
                logger.debug(
                    f"btc_updown: skip {asset} {outcome} | bid=${bid_price:.2f} out of range "
                    f"[{self.min_ask}, {self.max_ask}] (est_prob={est_prob:.2f})"
                )
                predictions.append(pred)
                continue

            # EV as maker (zero fee) — round edge to avoid float comparison issues
            edge = round(est_prob - bid_price, 4)
            ev = est_prob * (1.0 - bid_price) - (1.0 - est_prob) * bid_price

            logger.debug(
                f"btc_updown: MAKER BID {asset} {outcome} | bid=${bid_price:.2f} | "
                f"est_prob={est_prob:.2f} | edge={edge:+.3f} | EV={ev:+.3f} | "
                f"book=[{best_bid:.2f}/{best_ask:.2f}] | "
                f"eff_min_edge={effective_min_edge:.3f}"
            )

            outcome_min_edge = effective_min_edge
            if edge < outcome_min_edge or ev <= 0:
                logger.debug(
                    f"btc_updown: skip {asset} {outcome} | edge={edge:+.3f} "
                    f"(below min {outcome_min_edge:.3f})"
                )
                predictions.append(pred)
                continue

            pred["traded"] = True
            predictions.append(pred)

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
                    slug=market.get("_event_slug", ""),
                    asset=asset,
                )

        return best_signal, predictions

    def _get_smart_bid(self, est_prob: float, book, tick_size: float = 0.01,
                       gamma_bid: float | None = None,
                       gamma_ask: float | None = None) -> float:
        """Place bid intelligently based on book state and Gamma fallback."""
        # Dynamic cushion: tighten for high-confidence signals
        if est_prob > 0.65:
            cushion = 0.02
        else:
            cushion = self.maker_edge_cushion
        fair_bid = round(est_prob - cushion, 2)

        # When Gamma prices available (CLOB empty), bid near Gamma best bid
        if gamma_bid is not None and gamma_bid > 0.01:
            # Bid at Gamma bid level or our fair_bid, whichever is lower (more conservative)
            gamma_based_bid = round(gamma_bid, 2)
            bid = min(fair_bid, gamma_based_bid)
            return max(bid, 0.01)

        if book is None or not hasattr(book, 'asks') or not book.asks:
            return fair_bid

        best_ask = float(book.asks[0].price)

        # If best ask is below our fair value, undercut it by one tick
        if best_ask < fair_bid:
            undercut_bid = round(best_ask - tick_size, 2)
            if est_prob - undercut_bid >= self.min_edge:
                return undercut_bid

        return fair_bid

    @staticmethod
    def _extract_gamma_prices(market: dict) -> dict:
        """Extract bestBid/bestAsk per outcome index from Gamma market data."""
        import json as _json
        prices = {}
        # Gamma provides bestBid/bestAsk at market level (for outcome 0)
        # and outcomePrices as a JSON list
        best_bid = market.get("bestBid")
        best_ask = market.get("bestAsk")
        if best_bid is not None:
            try:
                prices["bid_0"] = float(best_bid)
            except (ValueError, TypeError):
                pass
        if best_ask is not None:
            try:
                prices["ask_0"] = float(best_ask)
            except (ValueError, TypeError):
                pass

        # outcomePrices gives the last trade price per outcome
        outcome_prices = market.get("outcomePrices")
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = _json.loads(outcome_prices)
            except (ValueError, TypeError):
                outcome_prices = []
        if isinstance(outcome_prices, list):
            for idx, p in enumerate(outcome_prices):
                try:
                    price = float(p)
                    if f"bid_{idx}" not in prices:
                        prices[f"bid_{idx}"] = max(price - 0.01, 0.01)
                    if f"ask_{idx}" not in prices:
                        prices[f"ask_{idx}"] = min(price + 0.01, 0.99)
                except (ValueError, TypeError):
                    continue

        # For binary markets, derive outcome 1 from outcome 0
        if "bid_0" in prices and "bid_1" not in prices:
            prices["bid_1"] = round(max(1.0 - prices.get("ask_0", 1.0), 0.01), 2)
            prices["ask_1"] = round(min(1.0 - prices.get("bid_0", 0.0), 0.99), 2)

        return prices

    def _compute_model_probability(self, outcome: str, delta_pct: float,
                                     window_progress: float, momentum: float,
                                     dynamic_vol: float | None = None) -> float:
        """Pure model probability without market anchoring."""
        vol = dynamic_vol if dynamic_vol is not None else self.btc_5m_vol
        directional_delta = delta_pct if outcome.lower() == "up" else -delta_pct
        normalized = directional_delta / vol if vol > 0 else 0.0

        # Linear time factor capped at 0.85 to avoid over-amplification near window end
        time_factor = min(window_progress, 0.85)

        # Dampen momentum weight (0.10 instead of full self.momentum_weight)
        effective_momentum_weight = min(self.momentum_weight, 0.10)
        momentum_direction = 1.0 if outcome.lower() == "up" else -1.0
        z = normalized * time_factor + momentum_direction * momentum * effective_momentum_weight * time_factor

        model_prob = 1.0 / (1.0 + math.exp(-self.logistic_k * z))
        return _clamp(model_prob, 0.05, 0.95)

    def _estimate_outcome_probability(self, outcome: str, delta_pct: float,
                                       window_progress: float, momentum: float,
                                       dynamic_vol: float | None = None,
                                       market_bid: float = 0.0) -> float:
        model_prob = self._compute_model_probability(
            outcome, delta_pct, window_progress, momentum, dynamic_vol,
        )

        # Anchor to market price: blend 70% model + 30% market
        if market_bid > 0.02:
            blended = 0.70 * model_prob + 0.30 * market_bid
            return _clamp(blended, 0.05, 0.95)

        return model_prob

    # --- Market discovery ---

    def _discover_markets(self, asset: str, interval: str) -> list[dict]:
        return discover_crypto_markets(
            self.gamma, asset, interval, strategy_name=self.name,
        )

    # --- Binance data helpers (kept from v1) ---

    def _calc_trade_flow(self, asset: str) -> float:
        trades = self.binance.get_recent_trades(asset, limit=500)
        if not trades:
            return 0.0

        buy_volume = 0.0
        sell_volume = 0.0
        for t in trades:
            qty = float(t["qty"])
            if t["isBuyerMaker"]:
                sell_volume += qty
            else:
                buy_volume += qty

        total = buy_volume + sell_volume
        if total == 0:
            return 0.0

        ratio = buy_volume / total
        return _clamp((ratio - 0.5) * 2, -1.0, 1.0)

    def _calc_orderbook_imbalance(self, asset: str) -> float:
        book = self.binance.get_orderbook(asset, limit=20)
        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return 0.0

        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2
        threshold = mid * 0.001

        bid_vol = sum(float(b[1]) for b in bids if mid - float(b[0]) <= threshold)
        ask_vol = sum(float(a[1]) for a in asks if float(a[0]) - mid <= threshold)

        total = bid_vol + ask_vol
        if total == 0:
            return 0.0

        imbalance = (bid_vol - ask_vol) / total
        return _clamp(imbalance * 2, -1.0, 1.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
