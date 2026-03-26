import logging
import re
import json as _json
import math
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date, timezone
from src.strategies.base import Strategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.gamma_client import GammaClient
from src.market_data.weather_client import WeatherClient, CITY_REGISTRY

logger = logging.getLogger("poly-trade")


class WeatherTemperatureStrategy(Strategy):
    name = "weather_temperature"
    self_discovering = True

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.gamma = GammaClient()
        self.weather = WeatherClient(cache_ttl=300.0)
        self.min_edge = settings.weather_min_edge
        self.base_sigma = settings.weather_base_sigma
        self.sigma_per_day = settings.weather_sigma_per_day
        self.max_hours_to_resolution = settings.weather_max_hours_to_resolution
        self.maker_cushion = settings.weather_maker_cushion
        self.min_volume = settings.weather_min_volume
        self.min_liquidity = settings.weather_min_liquidity
        self._pending_predictions: list[dict] = []

    def analyze(self, markets: list[dict], clob_client) -> list[TradeSignal]:
        self._pending_predictions = []

        raw_markets = self._discover_weather_events()
        if not raw_markets:
            logger.debug("weather: no active weather events found")
            return []

        # Group sub-markets by event slug
        by_event: dict[str, list[dict]] = defaultdict(list)
        for m in raw_markets:
            slug = m.get("_event_slug", "")
            if slug:
                by_event[slug].append(m)

        logger.info(f"weather: discovered {len(by_event)} event(s) with {len(raw_markets)} sub-market(s)")

        signals = []
        for event_slug, event_markets in by_event.items():
            try:
                signal = self._analyze_event(event_slug, event_markets, clob_client)
                if signal:
                    signals.append(signal)
            except Exception as e:
                logger.warning(f"weather: error analyzing event {event_slug}: {e}")

        signals.sort(key=lambda s: s.expected_value, reverse=True)
        return signals

    def _analyze_event(self, event_slug: str, event_markets: list[dict],
                       clob_client) -> TradeSignal | None:
        """Analyze all sub-markets for a single weather event, return best signal."""
        parsed = self._parse_event_slug(event_slug)
        if not parsed:
            logger.debug(f"weather: skipping unparseable slug: {event_slug}")
            return None
        city_key, target_date = parsed

        # Try ensemble forecast first (31 GFS members), fall back to deterministic
        ensemble = self.weather.get_ensemble_highs(city_key, target_date)
        forecast = self.weather.get_forecast_high(city_key, target_date)

        if not ensemble and not forecast:
            logger.debug(f"weather: no forecast for {city_key} on {target_date}")
            return None

        use_ensemble = ensemble is not None and len(ensemble.get("members", [])) >= 10
        if not use_ensemble and ensemble is not None:
            n = len(ensemble.get("members", []))
            logger.info(f"weather: {city_key} ensemble returned only {n} members, using deterministic fallback")
        if not use_ensemble and not forecast:
            logger.info(f"weather: {city_key} {target_date} — no ensemble AND no forecast")
            return None
        if use_ensemble:
            members = ensemble["members"]
            unit = ensemble["unit"]
            horizon_days = ensemble["horizon_days"]
            forecast_high = sum(members) / len(members)
            logger.debug(
                f"weather: {city_key} {target_date} | ENSEMBLE {len(members)} members "
                f"mean={forecast_high:.1f} spread={max(members)-min(members):.1f} "
                f"horizon={horizon_days}d"
            )
        else:
            members = None
            forecast_high = forecast["high_temp"]
            unit = forecast["unit"]
            horizon_days = forecast["horizon_days"]
            logger.debug(
                f"weather: {city_key} {target_date} | DETERMINISTIC "
                f"forecast={forecast_high:.1f} horizon={horizon_days}d"
            )

        sigma = self.base_sigma + self.sigma_per_day * horizon_days

        # Parse and score each sub-market
        best_signal = None
        best_ev = 0.0
        best_edge_seen = -1.0  # track highest edge for diagnostics
        best_edge_bucket = ""
        n_parsed = 0

        for market in event_markets:
            question = market.get("question", "")
            condition_id = market.get("conditionId", market.get("condition_id", ""))

            # Parse token IDs and outcomes
            clob_tokens = market.get("clobTokenIds", [])
            if isinstance(clob_tokens, str):
                try:
                    clob_tokens = _json.loads(clob_tokens)
                except (ValueError, TypeError):
                    clob_tokens = []

            outcomes = market.get("outcomes", [])
            if isinstance(outcomes, str):
                try:
                    outcomes = _json.loads(outcomes)
                except (ValueError, TypeError):
                    outcomes = []

            if len(clob_tokens) < 2 or len(outcomes) < 2:
                continue

            # Token 0 is "Yes" outcome
            token_id = clob_tokens[0]

            bucket = self._parse_temperature_bucket(question)
            if not bucket:
                continue
            lower, upper = bucket

            if use_ensemble:
                model_prob = self._compute_ensemble_probability(lower, upper, members)
            else:
                model_prob = self._compute_bucket_probability(lower, upper, forecast_high, sigma)

            # Get market prices
            # Weather markets have thin CLOB books (asks at 0.999 are placeholders).
            # Gamma bestBid/bestAsk reflects actual market activity and is more reliable.
            best_ask = None
            best_bid = None

            # Priority 1: Gamma bestBid/bestAsk (reflects real market pricing)
            if market.get("bestAsk") is not None:
                try:
                    best_ask = float(market["bestAsk"])
                except (ValueError, TypeError):
                    pass
            if market.get("bestBid") is not None:
                try:
                    best_bid = float(market["bestBid"])
                except (ValueError, TypeError):
                    pass

            # Priority 2: outcomePrices (implied probability from last trades)
            if best_ask is None:
                outcome_prices = market.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = _json.loads(outcome_prices)
                    except (ValueError, TypeError):
                        outcome_prices = []
                if outcome_prices:
                    best_ask = float(outcome_prices[0])

            if best_ask is None:
                best_ask = 1.0
            if best_bid is None:
                best_bid = 0.0

            # Edge and EV
            edge = model_prob - best_ask
            ev = model_prob * (1.0 - best_ask) - (1.0 - model_prob) * best_ask

            # Bucket label for logging
            bucket_label = self._bucket_label(lower, upper, unit)
            n_parsed += 1
            if edge > best_edge_seen:
                best_edge_seen = edge
                best_edge_bucket = bucket_label

            # Parse resolution timestamp
            end_date = market.get("endDate", "")
            resolution_ts = 0.0
            if end_date:
                try:
                    resolution_ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    pass

            # Build prediction record
            pred = {
                "strategy": self.name,
                "asset": city_key,
                "interval": "daily",
                "market_id": condition_id,
                "token_id": token_id,
                "outcome": "Yes",
                "est_prob": model_prob,
                "bid_price": 0.0,
                "delta_pct": None,
                "window_progress": None,
                "momentum": None,
                "dynamic_vol": None,
                "resolution_ts": resolution_ts,
                "traded": False,
            }

            if edge < self.min_edge or ev <= 0:
                pred["skip_reason"] = "low_edge"
                self._pending_predictions.append(pred)
                continue

            # Maker bid: model_prob - cushion
            our_bid = round(model_prob - self.maker_cushion, 2)
            if our_bid <= best_bid:
                our_bid = round(best_bid + 0.01, 2)
            if our_bid <= 0.01 or our_bid >= 0.99:
                pred["skip_reason"] = "bid_out_of_range"
                self._pending_predictions.append(pred)
                continue

            pred["traded"] = True
            pred["bid_price"] = our_bid
            self._pending_predictions.append(pred)

            prob_src = "ensemble" if use_ensemble else "normal"
            logger.info(
                f"weather: {city_key} {bucket_label} | "
                f"forecast={forecast_high:.1f} model_prob={model_prob:.3f} ({prob_src}) | "
                f"ask={best_ask:.3f} edge={edge:+.3f} EV={ev:+.3f} | "
                f"bid=${our_bid:.2f}"
            )

            if ev > best_ev:
                best_ev = ev
                best_signal = TradeSignal(
                    market_id=condition_id,
                    token_id=token_id,
                    market_question=question,
                    side="BUY",
                    outcome="Yes",
                    price=our_bid,
                    confidence=model_prob,
                    strategy=self.name,
                    expected_value=ev,
                    order_type="GTC",
                    post_only=True,
                    cancel_after_ts=resolution_ts - 300 if resolution_ts else 0,
                    resolution_ts=resolution_ts,
                    slug=event_slug,
                    asset=city_key,
                )

        if best_signal is None and n_parsed > 0:
            prob_src = "ensemble" if use_ensemble else f"normal(σ={sigma:.1f})"
            logger.info(
                f"weather: {city_key} {target_date} — no signal | "
                f"best_edge={best_edge_seen:+.3f} at {best_edge_bucket} | "
                f"forecast={forecast_high:.1f} ({prob_src}) | "
                f"{n_parsed} buckets scored"
            )

        return best_signal

    def _discover_weather_events(self) -> list[dict]:
        """Find active weather temperature events from Gamma API."""
        try:
            events = self.gamma.get_all_events_by_tag("weather", max_pages=5)
        except Exception as e:
            logger.warning(f"weather: Gamma fetch failed: {e}")
            return []

        if not events:
            return []

        now = datetime.now(timezone.utc)
        markets = []

        for event in events:
            event_slug = event.get("slug", "")
            event_title = event.get("title", "")

            # Only temperature events
            if "temperature" not in event_title.lower():
                continue

            for market in event.get("markets", []):
                if market.get("closed"):
                    continue
                if not market.get("active", True):
                    continue

                # Time filter
                end_date = market.get("endDate", "")
                if end_date:
                    try:
                        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                        hours_to_resolution = (end_dt - now).total_seconds() / 3600
                        if hours_to_resolution < 0 or hours_to_resolution > self.max_hours_to_resolution:
                            continue
                    except (ValueError, TypeError):
                        continue

                # Volume/liquidity filter
                try:
                    volume = float(market.get("volume", 0) or 0)
                except (ValueError, TypeError):
                    volume = 0
                try:
                    liquidity = float(market.get("liquidity", 0) or 0)
                except (ValueError, TypeError):
                    liquidity = 0

                if volume < self.min_volume or liquidity < self.min_liquidity:
                    continue

                # Attach event metadata
                market["_event_slug"] = event_slug
                market["_event_title"] = event_title
                markets.append(market)

        logger.debug(f"weather: {len(markets)} sub-markets from {len(events)} events passed filters")
        return markets

    @staticmethod
    def _parse_event_slug(slug: str) -> tuple[str, date] | None:
        """Parse 'highest-temperature-in-{city}-on-{month}-{day}-{year}' into (city_key, date)."""
        match = re.search(r"highest-temperature-in-(.+?)-on-(\w+)-(\d+)-(\d{4})", slug)
        if not match:
            return None

        city_key = match.group(1)
        month_str = match.group(2)
        day_str = match.group(3)
        year_str = match.group(4)

        # Verify city is known
        if city_key not in CITY_REGISTRY:
            logger.debug(f"weather: unknown city in slug: {city_key}")
            return None

        try:
            target_date = datetime.strptime(f"{month_str} {day_str} {year_str}", "%B %d %Y").date()
        except ValueError:
            return None

        return city_key, target_date

    @staticmethod
    def _parse_temperature_bucket(question: str) -> tuple[float | None, float | None] | None:
        """Parse temperature range from market question.

        Returns (lower_bound, upper_bound) where None means unbounded.
        """
        # "X°F or below" / "X°C or below"
        m = re.search(r"(\d+)°[FC]\s+or\s+below", question)
        if m:
            return (None, float(m.group(1)))

        # "X°F or higher" / "X°C or higher"
        m = re.search(r"(\d+)°[FC]\s+or\s+higher", question)
        if m:
            return (float(m.group(1)), None)

        # "X-Y°F" / "X-Y°C"
        m = re.search(r"(\d+)-(\d+)°[FC]", question)
        if m:
            return (float(m.group(1)), float(m.group(2)))

        # Single degree: "X°F" (not followed by "or")
        m = re.search(r"(\d+)°[FC](?!\s+or)", question)
        if m:
            val = float(m.group(1))
            return (val, val)

        return None

    @staticmethod
    def _compute_ensemble_probability(lower: float | None, upper: float | None,
                                       members: list[float]) -> float:
        """Compute P(actual temp falls in bucket) by counting ensemble members.

        Each member is a full model run. The fraction of members in the bucket
        is a direct probability estimate — no distributional assumptions needed.
        """
        if not members:
            return 0.0

        count = 0
        for t in members:
            if lower is None and upper is not None:
                if t <= upper + 0.5:
                    count += 1
            elif lower is not None and upper is None:
                if t >= lower - 0.5:
                    count += 1
            elif lower is not None and upper is not None:
                if lower - 0.5 <= t <= upper + 0.5:
                    count += 1

        return count / len(members)

    @staticmethod
    def _compute_bucket_probability(lower: float | None, upper: float | None,
                                     forecast_high: float, sigma: float) -> float:
        """Compute P(actual temp falls in bucket) using normal distribution (fallback)."""
        if sigma <= 0:
            sigma = 0.1

        if lower is None and upper is not None:
            # "X or below" -> P(T <= upper + 0.5)
            return _normal_cdf(upper + 0.5, forecast_high, sigma)
        elif lower is not None and upper is None:
            # "X or higher" -> P(T >= lower - 0.5)
            return 1.0 - _normal_cdf(lower - 0.5, forecast_high, sigma)
        elif lower is not None and upper is not None:
            # Range [lower, upper] -> P(lower - 0.5 <= T <= upper + 0.5)
            return (_normal_cdf(upper + 0.5, forecast_high, sigma)
                    - _normal_cdf(lower - 0.5, forecast_high, sigma))
        return 0.0

    @staticmethod
    def _bucket_label(lower: float | None, upper: float | None, unit: str) -> str:
        sym = "°F" if unit == "fahrenheit" else "°C"
        if lower is None and upper is not None:
            return f"≤{upper:.0f}{sym}"
        elif lower is not None and upper is None:
            return f"≥{lower:.0f}{sym}"
        elif lower is not None and upper is not None:
            if lower == upper:
                return f"{lower:.0f}{sym}"
            return f"{lower:.0f}-{upper:.0f}{sym}"
        return "?"


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard normal CDF using math.erf (no scipy dependency)."""
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))
