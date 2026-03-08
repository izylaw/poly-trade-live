import logging
import math
import time
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

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.binance = BinanceClient()
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
        self._pending_predictions: list[dict] = []

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        self._pending_predictions = []
        signals = []
        for asset in self.assets:
            for interval in self.intervals:
                try:
                    asset_signals = self._analyze_asset_interval(asset, interval, clob_client)
                    signals.extend(asset_signals)
                except Exception as e:
                    logger.warning(f"btc_updown: error analyzing {asset} {interval}: {e}")
        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _analyze_asset_interval(self, asset: str, interval: str, clob_client) -> list[TradeSignal]:
        active_markets = self._discover_markets(asset, interval)
        if not active_markets:
            logger.debug(f"btc_updown: no active {asset} {interval} markets found")
            return []

        signals = []
        for market in active_markets:
            delta_info = self._compute_price_delta(asset, market)
            momentum = self._compute_momentum_confirmation(asset)
            signal, predictions = self._evaluate_both_sides(
                market, asset, delta_info, momentum, clob_client, interval=interval,
            )
            self._pending_predictions.extend(predictions)
            if signal:
                signals.append(signal)
        return sorted(signals, key=lambda s: s.expected_value, reverse=True)

    def _compute_price_delta(self, asset: str, market: dict) -> dict:
        current_price = self.binance.get_price(asset)
        start_ts = market.get("_start_ts", 0)
        resolution_ts = market.get("_resolution_ts", 0)

        # Get 1-min klines to find reference price at start_ts
        try:
            klines = self.binance.get_klines(asset, interval="1m", limit=30)
        except Exception as e:
            logger.warning(f"btc_updown: failed to fetch klines for {asset}: {e}")
            klines = []

        reference_price = current_price  # fallback: no delta
        if klines:
            # Find the kline whose open_time covers start_ts
            start_ts_ms = start_ts * 1000
            for k in klines:
                if k["open_time"] <= start_ts_ms <= k["close_time"]:
                    reference_price = k["open"]
                    break
            else:
                # If start_ts is before all klines, use earliest kline open
                if klines[0]["open_time"] > start_ts_ms:
                    reference_price = klines[0]["open"]

        now = time.time()
        total_window = resolution_ts - start_ts if resolution_ts > start_ts else 300
        elapsed = now - start_ts
        window_progress = _clamp(elapsed / total_window, 0.0, 1.5)

        delta_pct = (current_price - reference_price) / reference_price if reference_price > 0 else 0.0

        # Dynamic ATR-based volatility
        dynamic_vol = self.btc_5m_vol  # fallback
        try:
            atr = self.binance.compute_atr(asset, "5m", 14)
            if atr is not None and current_price > 0:
                dynamic_vol = atr / current_price
        except Exception as e:
            logger.warning(f"btc_updown: ATR failed for {asset}, using default vol: {e}")

        logger.info(
            f"btc_updown: {asset} delta={delta_pct:+.4%} | ref=${reference_price:,.2f} | "
            f"now=${current_price:,.2f} | progress={window_progress:.2f} | "
            f"vol={dynamic_vol:.4f} ({'dynamic' if dynamic_vol != self.btc_5m_vol else 'default'}) "
            f"vs {self.btc_5m_vol:.4f} (baseline)"
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

        # Volatility regime scaling: require more edge when vol is high
        vol_ratio = dynamic_vol / self.btc_5m_vol if self.btc_5m_vol > 0 else 1.0
        effective_min_edge = self.min_edge * max(vol_ratio, 1.0)  # only widen, never shrink

        resolution_ts = delta_info.get("resolution_ts", 0)

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

            est_prob = self._estimate_outcome_probability(
                outcome, delta_info["delta_pct"], delta_info["window_progress"],
                momentum, dynamic_vol
            )

            # Smart bid placement using book depth
            bid_price = self._get_smart_bid(est_prob, book)

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

            # Validate bid is in tradeable range
            if bid_price < self.min_ask or bid_price > self.max_ask:
                logger.info(
                    f"btc_updown: skip {asset} {outcome} | bid=${bid_price:.2f} out of range "
                    f"[{self.min_ask}, {self.max_ask}] (est_prob={est_prob:.2f})"
                )
                predictions.append(pred)
                continue

            # EV as maker (zero fee)
            edge = est_prob - bid_price
            ev = est_prob * (1.0 - bid_price) - (1.0 - est_prob) * bid_price

            logger.info(
                f"btc_updown: MAKER BID {asset} {outcome} | bid=${bid_price:.2f} | "
                f"est_prob={est_prob:.2f} | edge={edge:+.3f} | EV={ev:+.3f} | "
                f"book=[{best_bid:.2f}/{best_ask:.2f}] | "
                f"eff_min_edge={effective_min_edge:.3f}"
            )

            if edge < effective_min_edge or ev <= 0:
                logger.info(
                    f"btc_updown: skip {asset} {outcome} | edge={edge:+.3f} "
                    f"(below min {effective_min_edge:.3f})"
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
                )

        return best_signal, predictions

    def _get_smart_bid(self, est_prob: float, book, tick_size: float = 0.01) -> float:
        """Place bid intelligently based on book state."""
        fair_bid = round(est_prob - self.maker_edge_cushion, 2)

        if book is None or not hasattr(book, 'asks') or not book.asks:
            return fair_bid

        best_ask = float(book.asks[0].price)

        # If best ask is below our fair value, undercut it by one tick
        if best_ask < fair_bid:
            undercut_bid = round(best_ask - tick_size, 2)
            if est_prob - undercut_bid >= self.min_edge:
                return undercut_bid

        return fair_bid

    def _estimate_outcome_probability(self, outcome: str, delta_pct: float,
                                       window_progress: float, momentum: float,
                                       dynamic_vol: float | None = None) -> float:
        vol = dynamic_vol if dynamic_vol is not None else self.btc_5m_vol
        directional_delta = delta_pct if outcome.lower() == "up" else -delta_pct
        normalized = directional_delta / vol if vol > 0 else 0.0
        time_factor = math.sqrt(max(window_progress, 0.0))

        momentum_direction = 1.0 if outcome.lower() == "up" else -1.0
        z = normalized * time_factor + momentum_direction * momentum * self.momentum_weight * time_factor

        prob = 1.0 / (1.0 + math.exp(-self.logistic_k * z))
        return _clamp(prob, 0.05, 0.95)

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
