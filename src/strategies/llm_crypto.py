"""LLM-powered crypto up/down strategy.

Sends Binance price data + Polymarket orderbook data to Claude for
probability estimation. Runs alongside btc_updown/safe_compounder
without changing their logic.
"""

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from src.strategies.base import Strategy
from src.strategies.crypto_utils import compute_price_delta, compute_momentum
from src.market_data.crypto_discovery import discover_crypto_markets, parse_json_field
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.binance_client import BinanceClient
from src.market_data.gamma_client import GammaClient
from src.llm.client import LLMClient
from src.llm.prompts import SYSTEM_PROMPT, build_crypto_prompt

logger = logging.getLogger("poly-trade")

CONFIDENCE_SCALING = {
    "low": 0.85,
    "medium": 0.92,
    "high": 1.0,
}


class LLMCryptoStrategy(Strategy):
    name = "llm_crypto"
    self_discovering = True

    def __init__(self, settings: Settings, binance: BinanceClient | None = None):
        super().__init__(settings)
        self.binance = binance or BinanceClient()
        self.gamma = GammaClient()
        self.llm = LLMClient(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout=settings.llm_timeout,
            api_key=settings.llm_api_key,
            context_size=settings.llm_context_size,
        )
        self.assets = settings.btc_updown_assets
        self.llm_intervals = settings.llm_intervals
        self.min_edge = settings.llm_min_edge
        self.batch_size = settings.llm_batch_size
        self.cache_ttl = settings.llm_cache_ttl
        self.daily_cache_ttl = settings.llm_daily_cache_ttl
        self.daily_lookahead = settings.llm_daily_lookahead_days
        self.run_every_n = settings.llm_run_every_n_cycles
        self.max_tokens = settings.llm_max_tokens
        self.maker_edge_cushion = settings.llm_maker_edge_cushion
        self.btc_5m_vol = settings.btc_updown_5m_vol
        self.min_ask = settings.btc_updown_min_ask
        self.max_ask = settings.btc_updown_max_ask
        self._cycle_counter = 0
        self._cache: dict[str, tuple[float, dict]] = {}  # conditionId -> (timestamp, assessment)

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

        dynamic_vol = self.btc_5m_vol
        if atr is not None and price > 0:
            dynamic_vol = atr / price

        klines_15m = []
        klines_1h = []
        try:
            klines_15m = self.binance.get_klines(asset, interval="15m", limit=5)
        except Exception:
            pass
        try:
            klines_1h = self.binance.get_klines(asset, interval="1h", limit=5)
        except Exception:
            pass

        return {
            "price": price,
            "klines_1m": klines_1m,
            "atr": atr,
            "momentum": momentum,
            "dynamic_vol": dynamic_vol,
            "klines_15m": klines_15m,
            "klines_1h": klines_1h,
        }

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        self._cycle_counter += 1

        if self._cycle_counter % self.run_every_n != 0:
            logger.debug(
                f"llm_crypto: skipping cycle {self._cycle_counter} "
                f"(runs every {self.run_every_n})"
            )
            return []

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
                    logger.warning(f"llm_crypto: prefetch failed for {asset}: {e}")

        # Source 1: Up/Down interval markets (1h, 4h)
        all_markets_data = []
        for asset in self.assets:
            if asset not in asset_data:
                continue
            for interval in self.llm_intervals:
                try:
                    market_data = self._gather_market_data(
                        asset, interval, clob_client, asset_data[asset],
                    )
                    all_markets_data.extend(market_data)
                except Exception as e:
                    logger.warning(f"llm_crypto: error gathering {asset} {interval}: {e}")

        # Source 2: Daily markets (above/below, price range, daily up/down)
        try:
            daily_markets = self.gamma.get_crypto_daily_markets(
                self.assets, self.daily_lookahead,
            )
            all_markets_data.extend(
                self._gather_general_market_data(daily_markets, clob_client, asset_data)
            )
        except Exception as e:
            logger.warning(f"llm_crypto: error gathering daily markets: {e}")

        # Source 3: Weekly hit price
        try:
            weekly_markets = self.gamma.get_crypto_weekly_markets()
            all_markets_data.extend(
                self._gather_general_market_data(weekly_markets, clob_client, asset_data)
            )
        except Exception as e:
            logger.warning(f"llm_crypto: error gathering weekly markets: {e}")

        # Source 4: Monthly hit price
        try:
            monthly_markets = self.gamma.get_crypto_monthly_markets()
            all_markets_data.extend(
                self._gather_general_market_data(monthly_markets, clob_client, asset_data)
            )
        except Exception as e:
            logger.warning(f"llm_crypto: error gathering monthly markets: {e}")

        if not all_markets_data:
            logger.debug("llm_crypto: no uncached markets to analyze")
            return []

        # Process in batches
        signals = []
        batches_processed = 0
        for i in range(0, len(all_markets_data), self.batch_size):
            if batches_processed >= 2:
                break
            batch = all_markets_data[i:i + self.batch_size]
            try:
                batch_signals = self._process_batch(batch, clob_client)
                signals.extend(batch_signals)
                batches_processed += 1
            except Exception as e:
                logger.warning(f"llm_crypto: LLM batch {batches_processed} failed: {e}")

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _gather_market_data(self, asset: str, interval: str, clob_client,
                            prefetched: dict) -> list[dict]:
        """Discover markets and gather data for uncached ones."""
        active_markets = discover_crypto_markets(
            self.gamma, asset, interval, strategy_name=self.name,
        )
        if not active_markets:
            return []

        result = []
        now = time.time()
        momentum = prefetched["momentum"]
        klines = prefetched.get("klines_1m", [])[:5]

        for market in active_markets:
            condition_id = market.get("conditionId", market.get("condition_id", ""))
            if not condition_id:
                continue

            # Cache check
            if condition_id in self._cache:
                cached_ts, _ = self._cache[condition_id]
                if now - cached_ts < self.cache_ttl:
                    logger.debug(f"llm_crypto: cache hit for {condition_id[:12]}")
                    continue

            # Gather Binance data using prefetched
            delta_info = compute_price_delta(
                self.binance, asset, market, self.btc_5m_vol, logger,
                prefetched=prefetched,
            )

            # Gather CLOB prices
            tokens = market.get("clobTokenIds", [])
            outcomes = market.get("outcomes", [])
            yes_price, no_price = 0.5, 0.5
            best_bid, best_ask = 0.0, 1.0
            if len(tokens) >= 2:
                for idx, token_id in enumerate(tokens[:2]):
                    price_data = clob_client.get_price(token_id)
                    if price_data and idx == 0:
                        yes_price = price_data.get("ask", 0.5)
                        best_bid = price_data.get("bid", 0.0)
                        best_ask = price_data.get("ask", 1.0)
                    elif price_data and idx == 1:
                        no_price = price_data.get("ask", 0.5)

            result.append({
                "market_id": condition_id,
                "asset": asset,
                "interval": interval,
                "market_type": "updown",
                "kline_interval": "1m",
                "question": market.get("question", f"Crypto {asset}"),
                "current_price": delta_info["current_price"],
                "reference_price": delta_info["reference_price"],
                "delta_pct": delta_info["delta_pct"],
                "window_progress": delta_info["window_progress"],
                "time_remaining": delta_info["time_remaining"],
                "dynamic_vol": delta_info["dynamic_vol"],
                "resolution_ts": delta_info["resolution_ts"],
                "momentum": momentum,
                "yes_price": yes_price,
                "no_price": no_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "recent_klines": klines,
                # Keep market metadata for signal generation
                "_market": market,
                "_tokens": tokens,
                "_outcomes": outcomes,
            })

        return result

    def _gather_general_market_data(self, markets: list[dict], clob_client,
                                    prefetched_by_asset: dict[str, dict]) -> list[dict]:
        """Gather data for non-updown markets (daily, weekly, monthly).

        Uses pre-fetched Binance data from analyze(), caps markets at
        batch_size * 2 to limit CLOB calls.
        """
        from datetime import datetime as _dt

        now = time.time()

        # Phase 1: filter out cached/invalid markets
        uncached = []
        for market in markets:
            condition_id = market.get("conditionId", market.get("condition_id", ""))
            if not condition_id:
                continue

            if condition_id in self._cache:
                cached_ts, _ = self._cache[condition_id]
                if now - cached_ts < self.daily_cache_ttl:
                    continue

            tokens = parse_json_field(market.get("clobTokenIds", []))
            outcomes = parse_json_field(market.get("outcomes", []))
            if len(tokens) < 2 or len(outcomes) < 2:
                continue

            uncached.append((market, condition_id, tokens, outcomes))

        if not uncached:
            return []

        # Cap at max processable (batch_size * 2 batches)
        max_to_gather = self.batch_size * 2
        uncached = uncached[:max_to_gather]

        logger.info(
            f"llm_crypto: gathering data for {len(uncached)} general markets "
            f"(of {len(markets)} discovered)"
        )

        # Phase 2: build market data using pre-fetched Binance data + per-market CLOB
        result = []
        for market, condition_id, tokens, outcomes in uncached:
            asset = market.get("_asset", "BTC")
            if asset not in prefetched_by_asset:
                continue

            ad = prefetched_by_asset[asset]
            market_type = market.get("_market_type", "")

            # Resolution time from endDate
            end_date = market.get("endDate", "")
            resolution_ts = 0
            if end_date:
                try:
                    dt = _dt.fromisoformat(end_date.replace("Z", "+00:00"))
                    resolution_ts = dt.timestamp()
                except (ValueError, AttributeError):
                    pass

            time_remaining = resolution_ts - now if resolution_ts else 0

            # CLOB prices (per-market — each has unique tokens)
            yes_price, no_price = 0.5, 0.5
            best_bid, best_ask = 0.0, 1.0
            for idx, token_id in enumerate(tokens[:2]):
                try:
                    price_data = clob_client.get_price(token_id)
                except Exception:
                    price_data = None
                if price_data and idx == 0:
                    yes_price = price_data.get("ask", 0.5)
                    best_bid = price_data.get("bid", 0.0)
                    best_ask = price_data.get("ask", 1.0)
                elif price_data and idx == 1:
                    no_price = price_data.get("ask", 0.5)

            kline_interval = "15m" if market_type in (
                "above_below", "price_range", "daily_updown",
            ) else "1h"
            klines = ad.get(f"klines_{kline_interval}", [])

            current_price = ad["price"]

            result.append({
                "market_id": condition_id,
                "asset": asset,
                "interval": market_type,
                "market_type": market_type,
                "kline_interval": kline_interval,
                "question": market.get("question", ""),
                "current_price": current_price,
                "reference_price": current_price,
                "delta_pct": 0.0,
                "window_progress": 0.0,
                "time_remaining": time_remaining,
                "dynamic_vol": ad["dynamic_vol"],
                "resolution_ts": resolution_ts,
                "momentum": ad["momentum"],
                "yes_price": yes_price,
                "no_price": no_price,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "recent_klines": klines,
                "_market": market,
                "_tokens": tokens,
                "_outcomes": outcomes,
            })

        return result

    def _process_batch(self, batch: list[dict], clob_client) -> list[TradeSignal]:
        """Send a batch to the LLM and convert assessments to signals."""
        prompt = build_crypto_prompt(batch)
        response = self.llm.complete(
            system=SYSTEM_PROMPT,
            user=prompt,
            max_tokens=self.max_tokens,
        )

        assessments = self._parse_llm_response(response["content"])
        if not assessments:
            return []

        # Index batch by market_id for lookup
        batch_by_id = {m["market_id"]: m for m in batch}

        signals = []
        now = time.time()
        for assessment in assessments:
            market_id = assessment.get("market_id", "")
            action = assessment.get("action", "SKIP")
            est_prob = assessment.get("estimated_probability", 0.0)
            confidence_level = assessment.get("confidence_level", "low")
            reasoning = assessment.get("reasoning", "")

            if action == "SKIP" or market_id not in batch_by_id:
                continue

            market_data = batch_by_id[market_id]

            # Cache the assessment
            self._cache[market_id] = (now, assessment)

            # Apply confidence scaling
            scale = CONFIDENCE_SCALING.get(confidence_level, 0.85)
            scaled_prob = est_prob * scale

            # Determine which token to buy
            tokens = market_data["_tokens"]
            outcomes = market_data["_outcomes"]
            if len(tokens) < 2 or len(outcomes) < 2:
                continue

            if action in ("BUY_UP", "BUY_YES"):
                token_idx = 0
            elif action in ("BUY_DOWN", "BUY_NO"):
                token_idx = 1
            else:
                continue

            token_id = tokens[token_idx]
            outcome = outcomes[token_idx]

            # Get book for smart bid
            book = None
            try:
                book = clob_client.get_book(token_id)
            except Exception:
                pass

            bid_price = self._get_smart_bid(scaled_prob, book)

            # Validate bid range
            if bid_price < self.min_ask or bid_price > self.max_ask:
                logger.info(
                    f"llm_crypto: skip {market_data['asset']} {outcome} | "
                    f"bid={bid_price:.2f} out of range"
                )
                continue

            edge = scaled_prob - bid_price
            ev = scaled_prob * (1.0 - bid_price) - (1.0 - scaled_prob) * bid_price

            if edge < self.min_edge or ev <= 0:
                logger.info(
                    f"llm_crypto: skip {market_data['asset']} {outcome} | "
                    f"edge={edge:+.3f} < min {self.min_edge}"
                )
                continue

            resolution_ts = market_data["resolution_ts"]

            logger.info(
                f"llm_crypto: SIGNAL {market_data['asset']} {outcome} | "
                f"bid={bid_price:.2f} | est_prob={scaled_prob:.2f} | "
                f"edge={edge:+.3f} | EV={ev:+.3f} | "
                f"confidence={confidence_level} | {reasoning}"
            )

            signals.append(TradeSignal(
                market_id=market_id,
                token_id=token_id,
                market_question=market_data["question"],
                side="BUY",
                outcome=outcome,
                price=bid_price,
                confidence=scaled_prob,
                strategy=self.name,
                expected_value=ev,
                order_type="GTC",
                post_only=True,
                cancel_after_ts=resolution_ts - 30 if resolution_ts else 0,
                resolution_ts=resolution_ts,
            ))

        return signals

    def _get_smart_bid(self, est_prob: float, book, tick_size: float = 0.01) -> float:
        """Place bid intelligently based on book state."""
        fair_bid = round(est_prob - self.maker_edge_cushion, 2)

        if book is None or not hasattr(book, 'asks') or not book.asks:
            return fair_bid

        best_ask = float(book.asks[0].price)

        if best_ask < fair_bid:
            undercut_bid = round(best_ask - tick_size, 2)
            if est_prob - undercut_bid >= self.min_edge:
                return undercut_bid

        return fair_bid

    @staticmethod
    def _parse_llm_response(content: str) -> list[dict]:
        """Parse JSON array from LLM response, handling edge cases."""
        if not content or not content.strip():
            return []
        try:
            result = json.loads(content)
            if isinstance(result, list):
                return result
            return []
        except (json.JSONDecodeError, TypeError):
            logger.warning(f"llm_crypto: failed to parse LLM response: {content[:200]}")
            return []
