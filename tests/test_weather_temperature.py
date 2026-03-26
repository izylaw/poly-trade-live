"""Tests for weather_temperature strategy."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import math
import pytest


def _tomorrow_noon():
    """Return ISO 8601 string for tomorrow at noon UTC (always in the future, within 72h)."""
    return (datetime.now(timezone.utc) + timedelta(hours=24)).strftime("%Y-%m-%dT12:00:00Z")


class TestParseEventSlug:
    def test_atlanta_slug(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_event_slug(
            "highest-temperature-in-atlanta-on-march-26-2026"
        )
        assert result == ("atlanta", date(2026, 3, 26))

    def test_nyc_slug(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_event_slug(
            "highest-temperature-in-new-york-city-on-april-1-2026"
        )
        assert result == ("new-york-city", date(2026, 4, 1))

    def test_seoul_slug(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_event_slug(
            "highest-temperature-in-seoul-on-march-27-2026"
        )
        assert result == ("seoul", date(2026, 3, 27))

    def test_unknown_city_returns_none(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_event_slug(
            "highest-temperature-in-mars-colony-on-march-26-2026"
        )
        assert result is None

    def test_unrelated_slug_returns_none(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_event_slug(
            "will-btc-go-up-or-down"
        )
        assert result is None


class TestParseTemperatureBucket:
    def test_or_below(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_temperature_bucket(
            "Will the highest temperature in Atlanta be 71°F or below on March 26?"
        )
        assert result == (None, 71.0)

    def test_range(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_temperature_bucket(
            "Will the highest temperature in Atlanta be 72-73°F on March 26?"
        )
        assert result == (72.0, 73.0)

    def test_or_higher(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_temperature_bucket(
            "Will the highest temperature in Atlanta be 90°F or higher on March 26?"
        )
        assert result == (90.0, None)

    def test_celsius_or_below(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_temperature_bucket(
            "Will the highest temperature in Seoul be 6°C or below on March 26?"
        )
        assert result == (None, 6.0)

    def test_celsius_range(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_temperature_bucket(
            "Will the highest temperature in Seoul be 7°C on March 26?"
        )
        assert result == (7.0, 7.0)

    def test_unparseable_returns_none(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        result = WeatherTemperatureStrategy._parse_temperature_bucket(
            "Who will win the NBA game?"
        )
        assert result is None


class TestBucketProbability:
    def test_peak_bucket_has_highest_prob(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        # Forecast 80°F, sigma 2.5. Bucket 80-81 should be highest.
        p_peak = WeatherTemperatureStrategy._compute_bucket_probability(80, 81, 80.0, 2.5)
        p_low = WeatherTemperatureStrategy._compute_bucket_probability(74, 75, 80.0, 2.5)
        p_high = WeatherTemperatureStrategy._compute_bucket_probability(86, 87, 80.0, 2.5)
        assert p_peak > p_low
        assert p_peak > p_high

    def test_tail_low_small_prob(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        # Forecast 80°F. "71 or below" should be very unlikely.
        p = WeatherTemperatureStrategy._compute_bucket_probability(None, 71, 80.0, 2.5)
        assert p < 0.01

    def test_tail_high_small_prob(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        # Forecast 80°F. "90 or higher" should be very unlikely.
        p = WeatherTemperatureStrategy._compute_bucket_probability(90, None, 80.0, 2.5)
        assert p < 0.01

    def test_probabilities_sum_to_one(self):
        """Full set of buckets should sum to ~1.0."""
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        compute = WeatherTemperatureStrategy._compute_bucket_probability
        forecast = 80.0
        sigma = 2.5

        # Simulate Atlanta-style buckets
        buckets = [
            (None, 71),   # ≤71
            (72, 73), (74, 75), (76, 77), (78, 79),
            (80, 81), (82, 83), (84, 85), (86, 87), (88, 89),
            (90, None),   # ≥90
        ]
        total = sum(compute(lo, hi, forecast, sigma) for lo, hi in buckets)
        assert abs(total - 1.0) < 0.01, f"Bucket probs sum to {total}, expected ~1.0"

    def test_single_degree_bucket(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        # Seoul-style: single degree "7°C"
        p = WeatherTemperatureStrategy._compute_bucket_probability(7, 7, 7.0, 1.5)
        assert 0.2 < p < 0.6  # peak bucket around forecast


class TestNormalCDF:
    def test_midpoint(self):
        from src.strategies.weather_temperature import _normal_cdf
        assert abs(_normal_cdf(0, 0, 1) - 0.5) < 0.001

    def test_two_sigma(self):
        from src.strategies.weather_temperature import _normal_cdf
        assert abs(_normal_cdf(1.96, 0, 1) - 0.975) < 0.01

    def test_negative(self):
        from src.strategies.weather_temperature import _normal_cdf
        assert abs(_normal_cdf(-1.96, 0, 1) - 0.025) < 0.01


class TestEnsembleProbability:
    def test_all_members_in_bucket(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        # All 31 members at 80°F, bucket 78-81 should be 100%
        members = [80.0] * 31
        p = WeatherTemperatureStrategy._compute_ensemble_probability(78, 81, members)
        assert p == 1.0

    def test_no_members_in_bucket(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        members = [80.0] * 31
        p = WeatherTemperatureStrategy._compute_ensemble_probability(90, 95, members)
        assert p == 0.0

    def test_partial_members(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        # 10 members at 79, 11 at 80, 10 at 81
        members = [79.0]*10 + [80.0]*11 + [81.0]*10
        p = WeatherTemperatureStrategy._compute_ensemble_probability(80, 81, members)
        # 80 and 81 are in [79.5, 81.5], so 11+10=21 out of 31
        assert abs(p - 21/31) < 0.01

    def test_tail_high(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        members = [78.0]*5 + [80.0]*20 + [85.0]*6
        p = WeatherTemperatureStrategy._compute_ensemble_probability(82, None, members)
        # Only the 6 members at 85 are >= 81.5
        assert abs(p - 6/31) < 0.01

    def test_tail_low(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        members = [70.0]*4 + [75.0]*27
        p = WeatherTemperatureStrategy._compute_ensemble_probability(None, 72, members)
        # Only 4 members at 70 are <= 72.5
        assert abs(p - 4/31) < 0.01

    def test_empty_members(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        p = WeatherTemperatureStrategy._compute_ensemble_probability(80, 81, [])
        assert p == 0.0


class TestWeatherClient:
    def test_get_forecast_high_caches(self):
        from src.market_data.weather_client import WeatherClient
        client = WeatherClient(cache_ttl=60.0)

        with patch("src.market_data.weather_client.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "daily": {
                    "time": ["2026-03-26"],
                    "temperature_2m_max": [80.5],
                }
            }
            mock_get.return_value = mock_resp

            result1 = client.get_forecast_high("atlanta", date(2026, 3, 26))
            result2 = client.get_forecast_high("atlanta", date(2026, 3, 26))

            assert result1["high_temp"] == 80.5
            assert result2["high_temp"] == 80.5
            assert mock_get.call_count == 1  # cached on second call

    def test_unknown_city_returns_none(self):
        from src.market_data.weather_client import WeatherClient
        client = WeatherClient()
        result = client.get_forecast_high("mars-colony", date(2026, 3, 26))
        assert result is None


class TestAnalyzeIntegration:
    def _make_strategy(self):
        from src.strategies.weather_temperature import WeatherTemperatureStrategy
        settings = MagicMock()
        settings.weather_min_edge = 0.08
        settings.weather_base_sigma = 1.5
        settings.weather_sigma_per_day = 1.0
        settings.weather_max_hours_to_resolution = 72
        settings.weather_maker_cushion = 0.03
        settings.weather_min_volume = 0.0
        settings.weather_min_liquidity = 0.0

        strategy = WeatherTemperatureStrategy.__new__(WeatherTemperatureStrategy)
        strategy.settings = settings
        strategy.gamma = MagicMock()
        strategy.weather = MagicMock()
        strategy.min_edge = 0.08
        strategy.base_sigma = 1.5
        strategy.sigma_per_day = 1.0
        strategy.max_hours_to_resolution = 72
        strategy.maker_cushion = 0.03
        strategy.min_volume = 0.0
        strategy.min_liquidity = 0.0
        strategy._pending_predictions = []
        strategy.enabled = True
        return strategy

    def test_generates_signal_for_mispriced_bucket(self):
        strategy = self._make_strategy()

        # Mock Gamma events
        strategy.gamma.get_all_events_by_tag.return_value = [{
            "slug": "highest-temperature-in-atlanta-on-march-26-2026",
            "title": "Highest temperature in Atlanta on March 26?",
            "markets": [
                {
                    "question": "Will the highest temperature in Atlanta be 78-79°F on March 26?",
                    "conditionId": "0xabc",
                    "clobTokenIds": '["token_yes", "token_no"]',
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.10", "0.90"]',
                    "endDate": _tomorrow_noon(),
                    "active": True,
                    "closed": False,
                    "volume": "1000",
                    "liquidity": "500",
                },
                {
                    "question": "Will the highest temperature in Atlanta be 80-81°F on March 26?",
                    "conditionId": "0xdef",
                    "clobTokenIds": '["token_peak_yes", "token_peak_no"]',
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.15", "0.85"]',
                    "endDate": _tomorrow_noon(),
                    "active": True,
                    "closed": False,
                    "volume": "1000",
                    "liquidity": "500",
                },
            ],
        }]

        # Mock ensemble forecast: 31 members clustered around 80°F
        strategy.weather.get_ensemble_highs.return_value = {
            "members": [79.0]*5 + [80.0]*15 + [81.0]*8 + [82.0]*3,
            "unit": "fahrenheit",
            "horizon_days": 0,
        }
        strategy.weather.get_forecast_high.return_value = {
            "high_temp": 80.0,
            "unit": "fahrenheit",
            "horizon_days": 0,
        }

        clob = MagicMock()
        # CLOB returns realistic orderbook prices (mispriced: ask much lower than model prob)
        def mock_price(tid):
            if tid == "token_yes":
                return {"bid": 0.08, "ask": 0.10}
            if tid == "token_peak_yes":
                return {"bid": 0.13, "ask": 0.15}
            return None
        clob.get_price.side_effect = mock_price

        signals = strategy.analyze([], clob)

        # Should generate a signal — 80-81°F bucket has ~74% ensemble prob vs ask 0.15
        assert len(signals) == 1
        assert signals[0].strategy == "weather_temperature"
        assert signals[0].asset == "atlanta"
        assert signals[0].market_id == "0xdef"  # peak bucket

    def test_no_signal_when_no_edge(self):
        strategy = self._make_strategy()

        strategy.gamma.get_all_events_by_tag.return_value = [{
            "slug": "highest-temperature-in-atlanta-on-march-26-2026",
            "title": "Highest temperature in Atlanta on March 26?",
            "markets": [{
                "question": "Will the highest temperature in Atlanta be 80-81°F on March 26?",
                "conditionId": "0xdef",
                "clobTokenIds": '["token_yes", "token_no"]',
                "outcomes": '["Yes", "No"]',
                "outcomePrices": '["0.95", "0.05"]',  # fairly priced (ensemble=100%)
                "endDate": "2026-03-26T12:00:00Z",
                "active": True,
                "closed": False,
                "volume": "1000",
                "liquidity": "500",
            }],
        }]

        strategy.weather.get_ensemble_highs.return_value = {
            "members": [80.0] * 31,
            "unit": "fahrenheit",
            "horizon_days": 0,
        }
        strategy.weather.get_forecast_high.return_value = {
            "high_temp": 80.0,
            "unit": "fahrenheit",
            "horizon_days": 0,
        }

        clob = MagicMock()
        # CLOB ask is 0.95 — no edge when ensemble says 100%
        clob.get_price.return_value = {"bid": 0.94, "ask": 0.95}

        signals = strategy.analyze([], clob)
        assert len(signals) == 0  # no edge (1.0 - 0.95 = 0.05 < min_edge 0.08)

    def test_predictions_logged_for_all_buckets(self):
        strategy = self._make_strategy()

        strategy.gamma.get_all_events_by_tag.return_value = [{
            "slug": "highest-temperature-in-atlanta-on-march-26-2026",
            "title": "Highest temperature in Atlanta on March 26?",
            "markets": [
                {
                    "question": f"Will the highest temperature in Atlanta be {lo}-{lo+1}°F on March 26?",
                    "conditionId": f"0x{lo}",
                    "clobTokenIds": f'["tok_{lo}_yes", "tok_{lo}_no"]',
                    "outcomes": '["Yes", "No"]',
                    "outcomePrices": '["0.10", "0.90"]',
                    "endDate": _tomorrow_noon(),
                    "active": True,
                    "closed": False,
                    "volume": "1000",
                    "liquidity": "500",
                }
                for lo in range(76, 86, 2)
            ],
        }]

        strategy.weather.get_ensemble_highs.return_value = {
            "members": [80.0] * 31,
            "unit": "fahrenheit",
            "horizon_days": 0,
        }
        strategy.weather.get_forecast_high.return_value = {
            "high_temp": 80.0, "unit": "fahrenheit", "horizon_days": 0,
        }

        clob = MagicMock()
        clob.get_price.return_value = {"bid": 0.08, "ask": 0.10}

        strategy.analyze([], clob)
        assert len(strategy._pending_predictions) == 5  # one per sub-market
