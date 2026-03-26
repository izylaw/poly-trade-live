#!/usr/bin/env python3
"""Deep analysis of weather_temperature strategy backtest.

Fetches 200 closed events, analyzes:
1. Tail vs range bucket performance
2. Per-city sigma calibration
3. Edge threshold sensitivity
4. Model calibration (predicted vs actual win rate)
5. Bucket width analysis
6. Forecast error simulation
"""
import json
import math
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

import requests

# ── City registry ──

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
    "hong-kong":     {"lat": 22.3193, "lon": 114.1694, "unit": "celsius"},
    "taipei":        {"lat": 25.0330, "lon": 121.5654, "unit": "celsius"},
    "mumbai":        {"lat": 19.0760, "lon": 72.8777,  "unit": "celsius"},
    "bangalore":     {"lat": 12.9716, "lon": 77.5946,  "unit": "celsius"},
    "sydney":        {"lat": -33.8688,"lon": 151.2093, "unit": "celsius"},
    "melbourne":     {"lat": -37.8136,"lon": 144.9631, "unit": "celsius"},
    "toronto":       {"lat": 43.6532, "lon": -79.3832, "unit": "celsius"},
    "mexico-city":   {"lat": 19.4326, "lon": -99.1332, "unit": "celsius"},
    "sao-paulo":     {"lat": -23.5505,"lon": -46.6333, "unit": "celsius"},
    "buenos-aires":  {"lat": -34.6037,"lon": -58.3816, "unit": "celsius"},
    "lagos":         {"lat": 6.5244,  "lon": 3.3792,   "unit": "celsius"},
    "nairobi":       {"lat": -1.2921, "lon": 36.8219,  "unit": "celsius"},
    "johannesburg":  {"lat": -26.2041,"lon": 28.0473,  "unit": "celsius"},
    "cairo":         {"lat": 30.0444, "lon": 31.2357,  "unit": "celsius"},
    "istanbul":      {"lat": 41.0082, "lon": 28.9784,  "unit": "celsius"},
    "riyadh":        {"lat": 24.7136, "lon": 46.6753,  "unit": "celsius"},
    "dubai":         {"lat": 25.2048, "lon": 55.2708,  "unit": "celsius"},
    "bangkok":       {"lat": 13.7563, "lon": 100.5018, "unit": "celsius"},
    "hanoi":         {"lat": 21.0285, "lon": 105.8542, "unit": "celsius"},
    "jakarta":       {"lat": -6.2088, "lon": 106.8456, "unit": "celsius"},
    "berlin":        {"lat": 52.5200, "lon": 13.4050,  "unit": "celsius"},
    "rome":          {"lat": 41.9028, "lon": 12.4964,  "unit": "celsius"},
    "vienna":        {"lat": 48.2082, "lon": 16.3738,  "unit": "celsius"},
    "amsterdam":     {"lat": 52.3676, "lon": 4.9041,   "unit": "celsius"},
    "stockholm":     {"lat": 59.3293, "lon": 18.0686,  "unit": "celsius"},
    "oslo":          {"lat": 59.9139, "lon": 10.7522,  "unit": "celsius"},
    "copenhagen":    {"lat": 55.6761, "lon": 12.5683,  "unit": "celsius"},
    "lisbon":        {"lat": 38.7223, "lon": -9.1393,  "unit": "celsius"},
    "zurich":        {"lat": 47.3769, "lon": 8.5417,   "unit": "celsius"},
    "new-delhi":     {"lat": 28.6139, "lon": 77.2090,  "unit": "celsius"},
    "kolkata":       {"lat": 22.5726, "lon": 88.3639,  "unit": "celsius"},
    "kuala-lumpur":  {"lat": 3.1390,  "lon": 101.6869, "unit": "celsius"},
    "manila":        {"lat": 14.5995, "lon": 120.9842, "unit": "celsius"},
    "lima":          {"lat": -12.0464,"lon": -77.0428, "unit": "celsius"},
    "bogota":        {"lat": 4.7110,  "lon": -74.0721, "unit": "celsius"},
    "santiago":      {"lat": -33.4489,"lon": -70.6693, "unit": "celsius"},
    "accra":         {"lat": 5.6037,  "lon": -0.1870,  "unit": "celsius"},
    "dar-es-salaam": {"lat": -6.7924, "lon": 39.2083,  "unit": "celsius"},
    "kinshasa":      {"lat": -4.4419, "lon": 15.2663,  "unit": "celsius"},
    "casablanca":    {"lat": 33.5731, "lon": -7.5898,  "unit": "celsius"},
    "algiers":       {"lat": 36.7538, "lon": 3.0588,   "unit": "celsius"},
    "tunis":         {"lat": 36.8065, "lon": 10.1815,  "unit": "celsius"},
    "athens":        {"lat": 37.9838, "lon": 23.7275,  "unit": "celsius"},
    "bucharest":     {"lat": 44.4268, "lon": 26.1025,  "unit": "celsius"},
    "prague":        {"lat": 50.0755, "lon": 14.4378,  "unit": "celsius"},
    "budapest":      {"lat": 47.4979, "lon": 19.0402,  "unit": "celsius"},
    "helsinki":      {"lat": 60.1699, "lon": 24.9384,  "unit": "celsius"},
}


# ── Model ──

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


def bucket_type(lower, upper):
    if lower is None:
        return "tail_low"
    elif upper is None:
        return "tail_high"
    elif lower == upper:
        return "single"
    else:
        return "range"


def bucket_width(lower, upper):
    """Width in degrees. Tail buckets get width=5 as estimate."""
    if lower is None or upper is None:
        return 5.0
    return upper - lower + 1


# ── Weather data ──

_weather_cache = {}


def get_historical_high(city_key, target_date):
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


# ── Gamma API ──

def fetch_closed_weather_events(limit=200):
    all_events = []
    for offset in range(0, limit + 100, 50):
        params = {
            "closed": "true", "limit": 50, "offset": offset,
            "order": "endDate", "ascending": "false", "tag_slug": "weather",
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


def find_winning_bucket(markets):
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


# ── Analysis engine ──

def analyze_all_events(events, sigma):
    """Score ALL buckets across all events. Returns list of scored records."""
    records = []
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

        winner = find_winning_bucket(markets)
        if not winner:
            skipped += 1
            continue

        winning_cid = winner.get("conditionId", "")
        winning_bucket = parse_temperature_bucket(winner.get("question", ""))

        actual_high = get_historical_high(city_key, target_date)
        if actual_high is None:
            skipped += 1
            continue

        city_info = CITY_REGISTRY.get(city_key, {})
        unit = city_info.get("unit", "celsius")

        for m in markets:
            question = m.get("question", "")
            cid = m.get("conditionId", m.get("condition_id", ""))
            bucket = parse_temperature_bucket(question)
            if not bucket:
                continue

            lower, upper = bucket
            model_prob = compute_bucket_probability(lower, upper, actual_high, sigma)
            is_winner = (cid == winning_cid)

            # Estimate market ask (post-resolution prices are 0/1, so we estimate)
            market_ask_est = min(model_prob * 0.85, 0.95) if model_prob > 0.01 else 0.01

            edge = model_prob - market_ask_est
            btype = bucket_type(lower, upper)
            bwidth = bucket_width(lower, upper)
            blabel = bucket_label(lower, upper)

            # Distance from forecast to bucket center
            if lower is not None and upper is not None:
                center = (lower + upper) / 2
            elif lower is not None:
                center = lower + 2.5  # estimate tail center
            elif upper is not None:
                center = upper - 2.5
            else:
                center = actual_high
            dist_from_forecast = abs(actual_high - center)

            records.append({
                "slug": slug,
                "city": city_key,
                "date": target_date,
                "unit": unit,
                "actual_high": actual_high,
                "bucket": blabel,
                "bucket_type": btype,
                "bucket_width": bwidth,
                "lower": lower,
                "upper": upper,
                "model_prob": model_prob,
                "market_ask_est": market_ask_est,
                "edge": edge,
                "is_winner": is_winner,
                "dist_from_forecast": dist_from_forecast,
            })

    return records, skipped


def simulate_trades(records, min_edge, maker_cushion, filters=None):
    """Simulate trades: pick best-EV bucket per event, compute PnL."""
    # Group by event slug
    by_event = defaultdict(list)
    for r in records:
        by_event[r["slug"]].append(r)

    trades = []
    for slug, buckets in by_event.items():
        # Apply optional filters
        candidates = buckets
        if filters:
            candidates = [b for b in candidates if all(f(b) for f in filters)]

        # Find best EV bucket with sufficient edge
        best = None
        best_ev = 0.0
        for b in candidates:
            if b["edge"] < min_edge:
                continue
            ev = b["model_prob"] * (1.0 - b["market_ask_est"]) - (1.0 - b["model_prob"]) * b["market_ask_est"]
            if ev > best_ev:
                best_ev = ev
                best = b

        if best is None:
            continue

        our_bid = round(best["model_prob"] - maker_cushion, 2)
        if our_bid <= 0.01:
            our_bid = 0.02
        if our_bid >= 0.99:
            our_bid = 0.98

        pnl = (1.0 - our_bid) if best["is_winner"] else -our_bid

        trades.append({
            **best,
            "our_bid": our_bid,
            "pnl": pnl,
            "won": best["is_winner"],
        })

    return trades


def print_trade_summary(trades, label=""):
    if label:
        print(f"\n{'='*80}")
        print(f"  {label}")
        print(f"{'='*80}")

    if not trades:
        print("  No trades.")
        return

    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl"] for t in trades)
    win_rate = len(wins) / len(trades) * 100
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    pf = abs(sum(t["pnl"] for t in wins)) / abs(sum(t["pnl"] for t in losses)) if losses else float("inf")

    print(f"  Trades: {len(trades)}  |  Win rate: {win_rate:.1f}%  |  PnL: ${total_pnl:+.2f}  |  PF: {pf:.2f}")
    print(f"  Avg win: ${avg_win:+.2f}  |  Avg loss: ${avg_loss:-.2f}")


def main():
    print("Fetching 200 closed temperature events...")
    events = fetch_closed_weather_events(limit=200)
    print(f"Fetched {len(events)} events")

    print("Fetching historical weather data (this may take a minute)...")
    records, skipped = analyze_all_events(events, sigma=2.5)
    print(f"Scored {len(records)} bucket records from {len(events) - skipped} events ({skipped} skipped)\n")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 1: Bucket type performance (tail vs range vs single)
    # ══════════════════════════════════════════════════════════════════════
    print("=" * 80)
    print("ANALYSIS 1: BUCKET TYPE PERFORMANCE")
    print("=" * 80)

    for btype in ["tail_low", "tail_high", "range", "single"]:
        filtered = [r for r in records if r["bucket_type"] == btype]
        winners = [r for r in filtered if r["is_winner"]]
        high_prob = [r for r in filtered if r["model_prob"] > 0.3]
        high_prob_winners = [r for r in high_prob if r["is_winner"]]

        print(f"\n  {btype.upper()}")
        print(f"    Total buckets: {len(filtered)}")
        if high_prob:
            actual_wr = len(high_prob_winners) / len(high_prob) * 100
            avg_prob = sum(r["model_prob"] for r in high_prob) / len(high_prob)
            print(f"    High-prob (>0.30) buckets: {len(high_prob)}")
            print(f"    Actual win rate when model_prob>0.30: {actual_wr:.1f}% (model avg: {avg_prob:.1%})")

        # Simulate trades for this type only
        trades = simulate_trades(records, min_edge=0.08, maker_cushion=0.03,
                                 filters=[lambda b, bt=btype: b["bucket_type"] == bt])
        print_trade_summary(trades, label="")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 2: Sigma calibration — which sigma gives best calibration?
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("ANALYSIS 2: SIGMA CALIBRATION")
    print("=" * 80)
    print(f"\n  Testing sigma values: what sigma makes model_prob best predict actual win rate?\n")
    print(f"  {'Sigma':>6} | {'Trades':>6} | {'Win%':>6} | {'PnL':>8} | {'PF':>5} | {'Avg Prob':>8} | {'Calibration':>12}")
    print(f"  {'-'*6}-+-{'-'*6}-+-{'-'*6}-+-{'-'*8}-+-{'-'*5}-+-{'-'*8}-+-{'-'*12}")

    for sigma_val in [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]:
        recs, _ = analyze_all_events(events, sigma=sigma_val)
        trades = simulate_trades(recs, min_edge=0.08, maker_cushion=0.03)
        if not trades:
            continue
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        wr = len(wins) / len(trades) * 100
        pnl = sum(t["pnl"] for t in trades)
        pf = abs(sum(t["pnl"] for t in wins)) / abs(sum(t["pnl"] for t in losses)) if losses else float("inf")
        avg_prob = sum(t["model_prob"] for t in trades) / len(trades)
        calibration = wr / 100 - avg_prob  # positive = underconfident, negative = overconfident
        cal_label = f"{calibration:+.3f} ({'under' if calibration > 0 else 'over'}conf)"
        print(f"  {sigma_val:>6.1f} | {len(trades):>6} | {wr:>5.1f}% | ${pnl:>+7.2f} | {pf:>5.2f} | {avg_prob:>7.3f} | {cal_label}")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 3: Edge threshold sensitivity
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("ANALYSIS 3: EDGE THRESHOLD SENSITIVITY (sigma=2.5)")
    print("=" * 80)
    print(f"\n  {'Min Edge':>9} | {'Trades':>6} | {'Win%':>6} | {'PnL':>8} | {'PF':>5} | {'Avg Edge':>9}")
    print(f"  {'-'*9}-+-{'-'*6}-+-{'-'*6}-+-{'-'*8}-+-{'-'*5}-+-{'-'*9}")

    for min_e in [0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.20]:
        trades = simulate_trades(records, min_edge=min_e, maker_cushion=0.03)
        if not trades:
            print(f"  {min_e:>9.2f} | {'---':>6}")
            continue
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        wr = len(wins) / len(trades) * 100
        pnl = sum(t["pnl"] for t in trades)
        pf = abs(sum(t["pnl"] for t in wins)) / abs(sum(t["pnl"] for t in losses)) if losses else float("inf")
        avg_edge = sum(t["edge"] for t in trades) / len(trades)
        print(f"  {min_e:>9.2f} | {len(trades):>6} | {wr:>5.1f}% | ${pnl:>+7.2f} | {pf:>5.2f} | {avg_edge:>+8.3f}")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 4: Tail bucket filter (exclude tail buckets)
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("ANALYSIS 4: TAIL BUCKET FILTER (sigma=2.5, min_edge=0.08)")
    print("=" * 80)

    print("\n  ALL buckets:")
    all_trades = simulate_trades(records, min_edge=0.08, maker_cushion=0.03)
    print_trade_summary(all_trades)

    print("\n  RANGE + SINGLE only (exclude tail_low and tail_high):")
    no_tail = simulate_trades(records, min_edge=0.08, maker_cushion=0.03,
                              filters=[lambda b: b["bucket_type"] in ("range", "single")])
    print_trade_summary(no_tail)

    print("\n  RANGE only (2-degree buckets):")
    range_only = simulate_trades(records, min_edge=0.08, maker_cushion=0.03,
                                 filters=[lambda b: b["bucket_type"] == "range"])
    print_trade_summary(range_only)

    print("\n  TAIL only:")
    tail_only = simulate_trades(records, min_edge=0.08, maker_cushion=0.03,
                                filters=[lambda b: b["bucket_type"] in ("tail_low", "tail_high")])
    print_trade_summary(tail_only)

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 5: Model calibration buckets
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("ANALYSIS 5: MODEL CALIBRATION (sigma=2.5)")
    print("=" * 80)
    print("\n  How well does model_prob predict actual win rate?\n")
    print(f"  {'Prob Bucket':>12} | {'Count':>6} | {'Actual Wins':>11} | {'Actual %':>8} | {'Predicted %':>11} | {'Gap':>6}")
    print(f"  {'-'*12}-+-{'-'*6}-+-{'-'*11}-+-{'-'*8}-+-{'-'*11}-+-{'-'*6}")

    cal_buckets = [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3), (0.3, 0.4), (0.4, 0.5),
                   (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]
    for lo, hi in cal_buckets:
        in_bucket = [r for r in records if lo <= r["model_prob"] < hi]
        if not in_bucket:
            continue
        actual_wins = sum(1 for r in in_bucket if r["is_winner"])
        actual_pct = actual_wins / len(in_bucket) * 100
        pred_pct = sum(r["model_prob"] for r in in_bucket) / len(in_bucket) * 100
        gap = actual_pct - pred_pct
        label = f"{lo:.1f}-{hi:.1f}"
        print(f"  {label:>12} | {len(in_bucket):>6} | {actual_wins:>11} | {actual_pct:>7.1f}% | {pred_pct:>10.1f}% | {gap:>+5.1f}%")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 6: Per-city performance
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("ANALYSIS 6: PER-CITY PERFORMANCE (sigma=2.5, min_edge=0.08)")
    print("=" * 80)

    by_city = defaultdict(list)
    for t in all_trades:
        by_city[t["city"]].append(t)

    print(f"\n  {'City':<20} {'Trades':>6} {'Wins':>5} {'Win%':>6} {'PnL':>8} {'Avg Edge':>9} {'Unit':>5}")
    print(f"  {'-'*20} {'-'*6} {'-'*5} {'-'*6} {'-'*8} {'-'*9} {'-'*5}")
    for city in sorted(by_city.keys(), key=lambda c: sum(t["pnl"] for t in by_city[c]), reverse=True):
        ct = by_city[city]
        wins = sum(1 for t in ct if t["won"])
        pnl = sum(t["pnl"] for t in ct)
        wr = wins / len(ct) * 100
        avg_e = sum(t["edge"] for t in ct) / len(ct)
        unit = ct[0].get("unit", "?")
        print(f"  {city:<20} {len(ct):>6} {wins:>5} {wr:>5.1f}% ${pnl:>+7.2f} {avg_e:>+8.3f} {unit:>5}")

    # ══════════════════════════════════════════════════════════════════════
    # ANALYSIS 7: Forecast error impact
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("ANALYSIS 7: FORECAST ERROR SIMULATION")
    print("=" * 80)
    print("\n  Using actual temp ± error to simulate real forecast inaccuracy\n")
    print(f"  {'Error (°)':>10} | {'Trades':>6} | {'Win%':>6} | {'PnL':>8} | {'PF':>5}")
    print(f"  {'-'*10}-+-{'-'*6}-+-{'-'*6}-+-{'-'*8}-+-{'-'*5}")

    import random
    random.seed(42)

    for err_std in [0.0, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0]:
        # Re-score with noisy forecast
        noisy_records = []
        by_event = defaultdict(list)
        for r in records:
            by_event[r["slug"]].append(r)

        for slug_recs in by_event.values():
            if not slug_recs:
                continue
            actual = slug_recs[0]["actual_high"]
            noise = random.gauss(0, err_std) if err_std > 0 else 0
            noisy_forecast = actual + noise

            for r in slug_recs:
                new_prob = compute_bucket_probability(r["lower"], r["upper"], noisy_forecast, 2.5)
                new_ask_est = min(new_prob * 0.85, 0.95) if new_prob > 0.01 else 0.01
                noisy_records.append({**r, "model_prob": new_prob, "market_ask_est": new_ask_est,
                                      "edge": new_prob - new_ask_est})

        trades = simulate_trades(noisy_records, min_edge=0.08, maker_cushion=0.03)
        if not trades:
            print(f"  {err_std:>10.1f} | {'---':>6}")
            continue
        wins = [t for t in trades if t["won"]]
        losses = [t for t in trades if not t["won"]]
        wr = len(wins) / len(trades) * 100
        pnl = sum(t["pnl"] for t in trades)
        pf = abs(sum(t["pnl"] for t in wins)) / abs(sum(t["pnl"] for t in losses)) if losses else float("inf")
        print(f"  {err_std:>10.1f} | {len(trades):>6} | {wr:>5.1f}% | ${pnl:>+7.2f} | {pf:>5.2f}")

    # ══════════════════════════════════════════════════════════════════════
    # TOP 3 RECOMMENDATIONS
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 80)
    print("TOP 3 STRATEGY IMPROVEMENTS (based on analysis)")
    print("=" * 80)
    print("""
  See analysis output above for data supporting each recommendation.
  Recommendations will be printed after all analyses complete.
""")


if __name__ == "__main__":
    main()
