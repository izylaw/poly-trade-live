import logging
import time
import threading
import requests

logger = logging.getLogger("poly-trade")

CITY_REGISTRY = {
    "new-york-city": {"lat": 40.7128, "lon": -74.0060, "unit": "fahrenheit"},
    "new-york":      {"lat": 40.7128, "lon": -74.0060, "unit": "fahrenheit"},
    "nyc":           {"lat": 40.7128, "lon": -74.0060, "unit": "fahrenheit"},
    "atlanta":       {"lat": 33.7490, "lon": -84.3880, "unit": "fahrenheit"},
    "chicago":       {"lat": 41.8781, "lon": -87.6298, "unit": "fahrenheit"},
    "los-angeles":   {"lat": 34.0522, "lon": -118.2437, "unit": "fahrenheit"},
    "miami":         {"lat": 25.7617, "lon": -80.1918, "unit": "fahrenheit"},
    "dallas":        {"lat": 32.7767, "lon": -96.7970, "unit": "fahrenheit"},
    "denver":        {"lat": 39.7392, "lon": -104.9903, "unit": "fahrenheit"},
    "seattle":       {"lat": 47.6062, "lon": -122.3321, "unit": "fahrenheit"},
    "washington":    {"lat": 38.9072, "lon": -77.0369, "unit": "fahrenheit"},
    "seoul":         {"lat": 37.5665, "lon": 126.9780, "unit": "celsius"},
    "london":        {"lat": 51.5074, "lon": -0.1278,  "unit": "celsius"},
    "tokyo":         {"lat": 35.6762, "lon": 139.6503, "unit": "celsius"},
    "paris":         {"lat": 48.8566, "lon": 2.3522,   "unit": "celsius"},
}


class WeatherClient:
    """Fetch weather forecasts from Open-Meteo API.

    Primary: ensemble endpoint (31-member GFS ensemble) for probability estimation.
    Fallback: deterministic forecast endpoint for point estimate + normal CDF.
    """

    ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, cache_ttl: float = 300.0):
        self._cache: dict[str, tuple[float, any]] = {}
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()

    def get_ensemble_highs(self, city_key: str, target_date) -> dict | None:
        """Return {"members": list[float], "unit": str, "horizon_days": int} or None.

        Uses the 31-member GFS ensemble for probability estimation.
        """
        city = CITY_REGISTRY.get(city_key)
        if not city:
            logger.warning(f"weather: unknown city '{city_key}'")
            return None

        from datetime import date as date_type
        today = date_type.today()
        horizon_days = (target_date - today).days

        cache_key = f"ensemble_{city_key}_{target_date.isoformat()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        members = self._fetch_ensemble(city["lat"], city["lon"], city["unit"], target_date)
        if members is not None and len(members) >= 5:
            result = {
                "members": members,
                "unit": city["unit"],
                "horizon_days": max(horizon_days, 0),
            }
            self._set_cache(cache_key, result)
            return result

        return None

    def get_forecast_high(self, city_key: str, target_date) -> dict | None:
        """Return {"high_temp": float, "unit": str, "horizon_days": int} or None.

        Deterministic forecast fallback.
        """
        city = CITY_REGISTRY.get(city_key)
        if not city:
            logger.warning(f"weather: unknown city '{city_key}'")
            return None

        from datetime import date as date_type
        today = date_type.today()
        horizon_days = (target_date - today).days

        cache_key = f"forecast_{city_key}_{target_date.isoformat()}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        high_temp = self._fetch_deterministic(city["lat"], city["lon"], city["unit"], target_date)
        if high_temp is None:
            return None

        result = {
            "high_temp": high_temp,
            "unit": city["unit"],
            "horizon_days": max(horizon_days, 0),
        }
        self._set_cache(cache_key, result)
        return result

    def _fetch_ensemble(self, lat: float, lon: float, unit: str, target_date) -> list[float] | None:
        """Fetch daily max temperature from all GFS ensemble members."""
        from datetime import date as date_type
        today = date_type.today()
        forecast_days = (target_date - today).days + 1
        if forecast_days < 1 or forecast_days > 16:
            return None

        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": unit,
            "forecast_days": min(forecast_days, 16),
            "timezone": "auto",
            "models": "gfs_seamless",
        }

        try:
            resp = requests.get(self.ENSEMBLE_URL, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"weather: ensemble fetch failed: {e}")
            return None

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        target_str = target_date.isoformat()

        try:
            day_idx = dates.index(target_str)
        except ValueError:
            logger.debug(f"weather: target date {target_str} not in ensemble response")
            return None

        # Collect all ensemble member values for this date
        members = []
        for key, values in daily.items():
            if key.startswith("temperature_2m_max") and isinstance(values, list):
                if day_idx < len(values) and values[day_idx] is not None:
                    members.append(float(values[day_idx]))

        if members:
            logger.debug(
                f"weather: ensemble {len(members)} members | "
                f"mean={sum(members)/len(members):.1f} "
                f"min={min(members):.1f} max={max(members):.1f}"
            )
        return members if members else None

    def _fetch_deterministic(self, lat: float, lon: float, unit: str, target_date) -> float | None:
        """Fetch single-model daily max temperature (fallback)."""
        from datetime import date as date_type
        today = date_type.today()
        forecast_days = (target_date - today).days + 1
        if forecast_days < 1 or forecast_days > 16:
            return None

        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": unit,
            "forecast_days": min(forecast_days, 16),
            "timezone": "auto",
        }

        try:
            resp = requests.get(self.FORECAST_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"weather: deterministic fetch failed: {e}")
            return None

        dates = data.get("daily", {}).get("time", [])
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        target_str = target_date.isoformat()

        for i, d in enumerate(dates):
            if d == target_str and i < len(temps) and temps[i] is not None:
                return float(temps[i])

        return None

    def _get_cached(self, key: str):
        with self._lock:
            entry = self._cache.get(key)
            if entry and time.time() - entry[0] < self._cache_ttl:
                return entry[1]
            return None

    def _set_cache(self, key: str, data):
        with self._lock:
            self._cache[key] = (time.time(), data)
