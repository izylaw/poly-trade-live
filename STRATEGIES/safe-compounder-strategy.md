# Safe Compounder Strategy Reference

## Overview
Capital-preserving compound growth via crypto up/down markets. Builds on the btc_updown price delta model with three innovations: dual-side quoting, cross-asset momentum, and late-window sniping.

Self-discovering: finds markets via Gamma slug-based event lookup (same pattern as btc_updown).

## Innovations Over btc_updown

### 1. Dual-Side Quoting
Bid on BOTH Up and Down simultaneously. If both fill, guaranteed profit = `1.0 - (bid_up + bid_down)`. Only valid when combined bids < threshold (default 0.92).

```
bid_up = smart_bid(prob_up, book_up, cushion, min_edge)
bid_down = smart_bid(prob_down, book_down, cushion, min_edge)
combined = bid_up + bid_down
if combined < dual_side_max_combined:
    guaranteed_profit = 1.0 - combined  # e.g., $0.08 on $0.92 outlay
```

### 2. Cross-Asset Momentum (Lead-Lag)
BTC often leads ETH/SOL price movements. When BTC has moved but the target asset hasn't followed yet (lag_ratio < 0.5), boost confidence in the direction BTC is moving.

```
if target_asset != "BTC" and lag_ratio < 0.5:
    boost = min(|btc_delta| / btc_5m_vol * 0.1, cross_asset_boost_cap)
```

### 3. Late-Window Sniping
Only trade when window progress > threshold (default 0.65). Late in the window, price movements are more predictive of the final outcome, and there's less time for reversals.

## Signal Priority
1. **Dual-side quoting** — tried first for every market; if valid, skip directional
2. **High-conviction directional** — fallback when dual-side conditions aren't met

## Probability Model
Same logistic model as btc_updown:
```
z = normalized_delta * sqrt(progress) + momentum * weight * sqrt(progress)
prob = 1 / (1 + exp(-logistic_k * z))
```

Dynamic ATR volatility from Binance 5-min klines. Vol regime scaling on min_edge.

## Order Type
All orders: `GTC` with `post_only=True` (zero maker fees). Book-aware smart bidding: undercut best ask when it's below fair bid, otherwise use model price.

## Configuration
```
safe_compounder_assets: ["BTC", "ETH", "SOL"]
safe_compounder_intervals: ["5m", "15m"]
safe_compounder_min_confidence: 0.78
safe_compounder_min_edge: 0.05
safe_compounder_maker_edge_cushion: 0.05
safe_compounder_min_window_progress: 0.65
safe_compounder_dual_side_max_combined: 0.92
safe_compounder_cross_asset_boost_cap: 0.08
```

Also inherits from btc_updown config:
```
btc_updown_5m_vol: 0.0010
btc_updown_logistic_k: 1.5
btc_updown_momentum_weight: 0.3
btc_updown_min_ask: 0.03
btc_updown_max_ask: 0.85
```

## Risk
- Strategy-specific min_confidence: 0.55 (overrides aggression tuner)
- Enabled in moderate, aggressive, and ultra aggression levels
- Kelly sizing on available balance above hard floor
- Dual-side: combined bid must be < 0.92 (guarantees 8%+ profit if both fill)
- Directional: requires est_prob >= min_confidence AND edge >= effective_min_edge
- Auto-cancel 30s before resolution

## Implementation Files
- `src/strategies/safe_compounder.py` — Strategy implementation
- `src/strategies/crypto_utils.py` — Shared probability/delta/momentum/bid utilities
- `src/market_data/binance_client.py` — Binance price and klines
- `src/market_data/gamma_client.py` — `get_crypto_updown_markets()`
- `tests/test_safe_compounder.py` — Unit tests

## Known Limitations
- Binance API is geo-blocked in some regions (HTTP 451). Strategy will produce no signals in those regions.
- Cross-asset boost assumes BTC leads; correlation varies by market conditions.
