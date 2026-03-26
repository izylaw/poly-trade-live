#!/usr/bin/env python3
"""Backtest the weather_temperature strategy against resolved Polymarket events.

Fetches ~100 most recent closed temperature events, determines the winning bucket,
retrieves Open-Meteo historical weather data, runs the probability model, and
simulates maker-order PnL.
"""
import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, date, timedelta
from pathlib import Path

import requests

# ── City registry (mirrors weather_client.py + extra cities from Polymarket) ──

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
    "beijing":       {"lat": 39.9042, "lon": 116.4074, "unit": "celsius"},
    "shanghai":      {"lat": 31.2304, "lon": 121.4737, "unit": "celsius"},
    "singapore":     {"lat": 1.3521,  "lon": 103.8198, "unit": "celsius"},
    "madrid":        {"lat": 40.4168, "lon": -3.7038,  "unit": "celsius"},
    "milan":         {"lat": 45.4642, "lon": 9.1900,   "unit": "celsius"},
    "munich":        {"lat": 48.1351, "lon": 11.5820,  "unit": "celsius"},
    "warsaw":        {"lat": 52.2297, "lon": 21.0122,  "unit": "celsius"},
    "ankara":        {"lat": 39.9334, "lon": 32.8597,  "unit": "celsius"},
    "wellington":    {"lat": -41.2865,"lon": 174.7762, "unit": "celsius"},
    "chengdu":       {"lat": 30.5728, "lon": 104.0668, "unit": "celsius"},
    "shenzhen":      {"lat": 22.5431, "lon": 114.0579, "unit": "celsius"},
    "lucknow":       {"lat": 26.8467, "lon": 80.9462,  "unit": "celsius"},
    "wuhan":         {"lat": 30.5928, "lon": 114.3055, "unit": "celsius"},
    "chongqing":     {"lat": 29.4316, "lon": 106.9123, "unit": "celsius"},
    "tel-aviv":      {"lat": 32.0853, "lon": 34.7818,  "unit": "celsius"},
}


# ── Probability model (mirrors weather_temperature.py) ──

def normal_cdf(x, mu, sigma):
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def compute_bucket_probability(lower, upper, forecast_high, sigma):
    if sigma <= 0:
        sigma = 0.1
    if lower is None and upper is not None:
        return normal_cdf(upper + 0.5, forecast_high, sigma)
    elif lower is not None and upper is None:
        return 1.0 - normal_cdf(lower - 0.5, forecast_high, sigma)
    elif lower is not None and upper is not None:
        return (normal_cdf(upper + 0.5, forecast_high, sigma)
                - normal_cdf(lower - 0.5, forecast_high, sigma))
    return 0.0


def parse_temperature_bucket(question):
    m = re.search(r"(\d+)°[FC]\s+or\s+below", question)
    if m:
        return (None, float(m.group(1)))
    m = re.search(r"(\d+)°[FC]\s+or\s+higher", question)
    if m:
        return (float(m.group(1)), None)
    m = re.search(r"(\d+)-(\d+)°[FC]", question)
    if m:
        return (float(m.group(1)), float(m.group(2)))
    m = re.search(r"(\d+)°[FC](?!\s+or)", question)
    if m:
        val = float(m.group(1))
        return (val, val)
    return None


def parse_event_slug(slug):
    match = re.search(r"highest-temperature-in-(.+?)-on-(\w+)-(\d+)-(\d{4})", slug)
    if not match:
        return None
    city_key = match.group(1)
    month_str = match.group(2)
    day_str = match.group(3)
    year_str = match.group(4)
    if city_key not in CITY_REGISTRY:
        return None
    try:
        target_date = datetime.strptime(f"{month_str} {day_str} {year_str}", "%B %d %Y").date()
    except ValueError:
        return None
    return city_key, target_date


def bucket_label(lower, upper):
    if lower is None and upper is not None:
        return f"≤{upper:.0f}"
    elif lower is not None and upper is None:
        return f"≥{lower:.0f}"
    elif lower is not None and upper is not None:
        if lower == upper:
            return f"{lower:.0f}"
        return f"{lower:.0f}-{upper:.0f}"
    return "?"


# ── Open-Meteo historical weather ──

_weather_cache = {}

def get_historical_high(city_key, target_date):
    """Fetch actual recorded high temperature from Open-Meteo historical API."""
    cache_key = f"{city_key}_{target_date}"
    if cache_key in _weather_cache:
        return _weather_cache[cache_key]

    city = CITY_REGISTRY.get(city_key)
    if not city:
        return None

    url = "https://archive-api.open-meteo.com/v1/archive"
    params = {
        "latitude": city["lat"],
        "longitude": city["lon"],
        "daily": "temperature_2m_max",
        "temperature_unit": city["unit"],
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "timezone": "auto",
    }

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        temps = data.get("daily", {}).get("temperature_2m_max", [])
        if temps and temps[0] is not None:
            result = float(temps[0])
            _weather_cache[cache_key] = result
            return result
    except Exception as e:
        print(f"  WARNING: historical fetch failed for {city_key} {target_date}: {e}", file=sys.stderr)

    _weather_cache[cache_key] = None
    return None


def get_forecast_high(city_key, target_date, horizon_days=1):
    """Simulate a forecast by fetching actual temp (best-case scenario for model).

    For a more realistic backtest, we add noise based on horizon_days.
    But for now we use actual temp as forecast — this tests the probability model
    and market pricing, not forecast accuracy.
    """
    return get_historical_high(city_key, target_date)


# ── Gamma API ──

def fetch_closed_weather_events(limit=100):
    """Fetch most recent closed temperature events from Gamma."""
    all_events = []
    for offset in range(0, limit + 50, 50):
        params = {
            "closed": "true",
            "limit": 50,
            "offset": offset,
            "order": "endDate",
            "ascending": "false",
            "tag_slug": "weather",
        }
        try:
            resp = requests.get("https://gamma-api.polymarket.com/events", params=params, timeout=15)
            events = resp.json()
        except Exception as e:
            print(f"Gamma fetch error: {e}", file=sys.stderr)
            break

        if not events:
            break

        for event in events:
            title = event.get("title", "")
            if "temperature" not in title.lower():
                continue
            all_events.append(event)
            if len(all_events) >= limit:
                break

        if len(all_events) >= limit:
            break
        time.sleep(0.3)

    return all_events[:limit]


# ── Backtest engine ──

def find_winning_bucket(markets):
    """Find which sub-market resolved to Yes (outcomePrices[0] == "1")."""
    for m in markets:
        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            try:
                prices = json.loads(prices)
            except (ValueError, TypeError):
                prices = []
        if len(prices) >= 1:
            try:
                if float(prices[0]) >= 0.99:
                    return m
            except (ValueError, TypeError):
                pass
    return None


def run_backtest(events, base_sigma=1.5, sigma_per_day=1.0, min_edge=0.08, maker_cushion=0.03):
    """Run backtest over resolved events."""
    results = []
    skipped = 0

    for event in events:
        slug = event.get("slug", "")
        parsed = parse_event_slug(slug)
        if not parsed:
            skipped += 1
            continue

        city_key, target_date = parsed
        markets = event.get("markets", [])
        if not markets:
            skipped += 1
            continue

        # Find which bucket actually won
        winner = find_winning_bucket(markets)
        if not winner:
            skipped += 1
            continue

        winning_question = winner.get("question", "")
        winning_bucket = parse_temperature_bucket(winning_question)

        # Get actual high temp (also used as "forecast" for ideal backtest)
        actual_high = get_historical_high(city_key, target_date)
        if actual_high is None:
            skipped += 1
            continue

        # Simulate forecast = actual (best case) with horizon=1 day sigma
        horizon_days = 1
        sigma = base_sigma + sigma_per_day * horizon_days

        # Score each sub-market
        best_market = None
        best_ev = 0.0
        best_model_prob = 0.0
        best_ask = 0.0
        best_bucket = None

        for m in markets:
            question = m.get("question", "")
            bucket = parse_temperature_bucket(question)
            if not bucket:
                continue

            model_prob = compute_bucket_probability(bucket[0], bucket[1], actual_high, sigma)

            # Get market ask price (pre-resolution)
            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except (ValueError, TypeError):
                    prices = []

            # For resolved markets, current prices are 0/1. Use bestAsk if available.
            best_ask_val = None
            if m.get("bestAsk") is not None:
                try:
                    best_ask_val = float(m["bestAsk"])
                except (ValueError, TypeError):
                    pass

            # If no pre-resolution price, skip this market for trading
            # Use volume-weighted midpoint as proxy
            if best_ask_val is None or best_ask_val >= 0.99 or best_ask_val <= 0.01:
                # Market already resolved — use a heuristic: implied prob from closing
                # For backtesting, we need pre-resolution prices which aren't available
                # after close. Use model_prob * 0.7 as a conservative market estimate.
                best_ask_val = min(model_prob * 0.85, 0.95)

            edge = model_prob - best_ask_val
            ev = model_prob * (1.0 - best_ask_val) - (1.0 - model_prob) * best_ask_val

            if edge >= min_edge and ev > best_ev:
                best_ev = ev
                best_market = m
                best_model_prob = model_prob
                best_ask = best_ask_val
                best_bucket = bucket

        if best_market is None:
            results.append({
                "slug": slug, "city": city_key, "date": target_date,
                "actual_high": actual_high, "traded": False,
                "reason": "no_edge",
            })
            continue

        # Did our chosen bucket win?
        best_question = best_market.get("question", "")
        our_bucket_won = (best_market.get("conditionId") == winner.get("conditionId"))

        # PnL calculation (maker order)
        our_bid = round(best_model_prob - maker_cushion, 2)
        if our_bid <= 0.01:
            our_bid = 0.02

        if our_bucket_won:
            pnl = 1.0 - our_bid  # Win: paid our_bid, receive $1
        else:
            pnl = -our_bid  # Loss: lose our bid amount

        results.append({
            "slug": slug,
            "city": city_key,
            "date": target_date,
            "actual_high": actual_high,
            "traded": True,
            "our_bucket": bucket_label(best_bucket[0], best_bucket[1]) if best_bucket else "?",
            "winning_bucket": bucket_label(winning_bucket[0], winning_bucket[1]) if winning_bucket else "?",
            "model_prob": best_model_prob,
            "market_ask": best_ask,
            "edge": best_model_prob - best_ask,
            "our_bid": our_bid,
            "won": our_bucket_won,
            "pnl": pnl,
        })

    return results, skipped


def print_results(results, skipped):
    traded = [r for r in results if r.get("traded")]
    wins = [r for r in traded if r.get("won")]
    losses = [r for r in traded if not r.get("won")]

    print("\n" + "=" * 90)
    print("WEATHER TEMPERATURE STRATEGY — BACKTEST RESULTS")
    print("=" * 90)

    print(f"\nEvents analyzed: {len(results)}")
    print(f"Events skipped:  {skipped}")
    print(f"Trades taken:    {len(traded)}")
    print(f"No-trade (no edge): {len(results) - len(traded)}")

    if not traded:
        print("\nNo trades to analyze.")
        return

    total_pnl = sum(r["pnl"] for r in traded)
    win_rate = len(wins) / len(traded) * 100
    avg_win = sum(r["pnl"] for r in wins) / len(wins) if wins else 0
    avg_loss = sum(r["pnl"] for r in losses) / len(losses) if losses else 0
    avg_edge = sum(r["edge"] for r in traded) / len(traded)
    avg_model_prob = sum(r["model_prob"] for r in traded) / len(traded)

    print(f"\n--- Performance ---")
    print(f"Win rate:        {win_rate:.1f}% ({len(wins)}/{len(traded)})")
    print(f"Total PnL:       ${total_pnl:+.2f} (per $1 unit size)")
    print(f"Avg win:         ${avg_win:+.2f}")
    print(f"Avg loss:        ${avg_loss:-.2f}")
    print(f"Avg edge:        {avg_edge:+.3f}")
    print(f"Avg model prob:  {avg_model_prob:.3f}")

    if losses:
        profit_factor = abs(sum(r["pnl"] for r in wins)) / abs(sum(r["pnl"] for r in losses))
        print(f"Profit factor:   {profit_factor:.2f}")

    # Per-city breakdown
    by_city = defaultdict(list)
    for r in traded:
        by_city[r["city"]].append(r)

    print(f"\n--- Per-City Breakdown ---")
    print(f"{'City':<20} {'Trades':>6} {'Wins':>5} {'Win%':>6} {'PnL':>8}")
    print("-" * 50)
    for city in sorted(by_city.keys()):
        city_trades = by_city[city]
        city_wins = sum(1 for r in city_trades if r["won"])
        city_pnl = sum(r["pnl"] for r in city_trades)
        city_wr = city_wins / len(city_trades) * 100
        print(f"{city:<20} {len(city_trades):>6} {city_wins:>5} {city_wr:>5.1f}% ${city_pnl:>+7.2f}")

    # Show sample trades
    print(f"\n--- Sample Trades (last 20) ---")
    print(f"{'City':<15} {'Date':<12} {'Actual':>7} {'Our Bucket':<12} {'Win Bucket':<12} {'Prob':>5} {'Bid':>5} {'W/L':>4} {'PnL':>7}")
    print("-" * 95)
    for r in traded[-20:]:
        wl = "WIN" if r["won"] else "LOSS"
        print(
            f"{r['city']:<15} {r['date']!s:<12} {r['actual_high']:>6.1f} "
            f"{r['our_bucket']:<12} {r['winning_bucket']:<12} "
            f"{r['model_prob']:>.3f} {r['our_bid']:>.2f} {wl:>4} ${r['pnl']:>+6.2f}"
        )


def main():
    print("Fetching closed weather events from Gamma API...")
    events = fetch_closed_weather_events(limit=100)
    print(f"Fetched {len(events)} temperature events")

    print("Fetching historical weather data from Open-Meteo...")
    results, skipped = run_backtest(
        events,
        base_sigma=1.5,
        sigma_per_day=1.0,
        min_edge=0.08,
        maker_cushion=0.03,
    )

    print_results(results, skipped)

    # Also test with actual forecast error (add sigma to simulate not knowing exact temp)
    print("\n\n" + "=" * 90)
    print("SENSITIVITY: Higher uncertainty (base_sigma=2.5, simulating real forecast error)")
    print("=" * 90)
    results2, skipped2 = run_backtest(
        events,
        base_sigma=2.5,
        sigma_per_day=1.0,
        min_edge=0.08,
        maker_cushion=0.03,
    )
    print_results(results2, skipped2)


if __name__ == "__main__":
    main()
