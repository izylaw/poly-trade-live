# Sports Daily Strategy Reference

## Overview
Trade daily sports events using market microstructure signals. No external odds feed required — all edges derived from CLOB orderbook structure and known behavioral biases.

Self-discovering: finds markets via Gamma API tag search with parallel pagination, then batch-fetches orderbooks for efficiency.

## Edge Sources

### 1. Spread Capture
Place maker bids in the middle of wide spreads. When the spread is wide enough (> min_spread), place a GTC post-only bid slightly above the best bid. Edge comes from the spread itself.

```
if spread >= min_spread:
    our_bid = mid - maker_cushion
    if our_bid <= best_bid:
        our_bid = best_bid + 0.01
    edge = mid - our_bid
    EV = mid * (1 - our_bid) - (1 - mid) * our_bid
```

Time bonus: markets closer to game time get a slight EV boost (higher fill probability).

### 2. Book Imbalance
Follow informed flow when the order book is heavily skewed toward the bid side. A positive imbalance (more bids than asks) is bullish for the outcome.

```
imbalance = (bid_depth - ask_depth) / (bid_depth + ask_depth)  # top 10 levels
if imbalance > threshold:
    boost = imbalance * 0.05  # up to 5% probability boost
    adjusted_prob = mid + boost
    our_bid = adjusted_prob - maker_cushion
```

Only follows positive imbalance (bid-heavy). Negative imbalance is ignored.

### 3. Favorite Value
Exploit the favorite-longshot bias: sports bettors systematically over-bet underdogs and under-bet favorites. When implied probability is high (0.82-0.95), true probability is typically higher.

```
if favorite_min_prob <= mid <= favorite_max_prob:
    bias_boost = (mid - 0.50) * 0.06  # ~3% boost at mid=0.85
    adjusted_prob = mid + bias_boost
```

## Market Discovery

1. **Tag search**: For each configured tag (sports, nba, nfl, etc.), call `get_all_events_by_tag()` with parallel pagination (ThreadPoolExecutor, 10 workers, up to 30 pages)
2. **Fallback**: If no tag results, scan all active events and filter by sport keywords/patterns
3. **Deduplication**: By conditionId
4. **Filters**: resolution within max_hours (default 24h), volume >= min, liquidity >= min, must have 2 outcomes with CLOB token IDs

## Batch Orderbook Optimization

Instead of fetching orderbooks one-by-one (4 HTTP calls per market x 83 markets = 332 calls), the strategy:
1. Collects all token IDs from all discovered markets
2. Calls `get_books_batch()` which fetches ~50 tokens per chunked request (~4 total requests)
3. Passes the book cache to `_analyze_market()` which looks up books locally
4. Falls back to individual `get_book()` on cache miss

## Order Type
All orders: `GTC` with `post_only=True` (zero maker fees). Orders include `cancel_after_ts` set to 5 minutes before market resolution.

## Configuration
```
sports_daily_min_volume: 5000
sports_daily_min_liquidity: 1000
sports_daily_min_spread: 0.04          # minimum spread for spread capture
sports_daily_max_hours_to_resolution: 24
sports_daily_favorite_min_prob: 0.82
sports_daily_favorite_max_prob: 0.95
sports_daily_imbalance_threshold: 0.3
sports_daily_maker_cushion: 0.02
sports_daily_min_edge: 0.015
sports_daily_tags: ["sports", "nba", "nfl", "mlb", "nhl", "mma", "soccer", "tennis"]
```

## Risk
- Strategy-specific min_confidence override: none (uses aggression tuner default)
- Enabled in moderate, aggressive, and ultra aggression levels
- Kelly sizing on available balance above hard floor
- Signal priority: spread_capture > book_imbalance > favorite_value (first match wins per outcome)
- Prediction tracking: every evaluated outcome logged (traded or not) for calibration

## Sport Detection
Keywords: nba, nfl, mlb, nhl, mma, ufc, soccer, football, tennis, boxing, epl, premier league, champions league, serie a, la liga, bundesliga, ligue 1, cricket, rugby, f1, nascar, pga, golf, ncaa, wnba, mls, copa, world cup, afl, ipl

Game patterns: "win game", "beat", "vs", "defeat", "win match", "win series", "over under", "moneyline", "spread"

## Implementation Files
- `src/strategies/sports_daily.py` — Strategy implementation
- `src/market_data/gamma_client.py` — `get_all_events_by_tag()` parallel pagination
- `src/market_data/clob_client.py` — `get_books_batch()`, `extract_price()`
- `tests/test_sports_daily.py` — Unit tests
