# BTC Up/Down Strategy Reference (v3 — Maker Orders + Multi-Asset)

## Data Sources (Binance API - no API key required)

| Endpoint | Data | Use |
|----------|------|-----|
| `/api/v3/ticker/price` | Current price | Live asset price |
| `/api/v3/klines` | OHLCV candles | Reference price at window start |
| `/api/v3/trades` | Recent trades | Trade flow (momentum confirmation) |
| `/api/v3/depth` | Order book | Orderbook imbalance (momentum confirmation) |

## Core Model: Price Delta Probability

### How it works
1. When a window starts, capture reference price from 1-min klines
2. Compare current price to reference → delta percentage
3. Convert to directional probability via logistic function
4. Evaluate BOTH "Up" and "Down" tokens for edge
5. Trade whichever side has highest positive EV with edge > min_edge

### Probability Formula
```
directional_delta = delta_pct if outcome=="Up" else -delta_pct
normalized = directional_delta / dynamic_volatility   # z-score
time_factor = sqrt(window_progress)
z = normalized * time_factor + momentum_direction * momentum_weight * time_factor
prob = 1 / (1 + exp(-logistic_k * z))
clamped to [0.05, 0.95]
```

### Example Outputs

| Scenario | delta | progress | est_prob | Action |
|----------|-------|----------|----------|--------|
| No data yet | 0.00% | 0.0 | 0.50 | No edge |
| Early, small up | +0.05% | 0.2 | ~0.58 | Bid $0.53 (fair) |
| Mid, moderate up | +0.10% | 0.5 | ~0.74 | Bid $0.69 or undercut ask |
| Late, strong up | +0.15% | 0.8 | ~0.88 | Bid $0.83 or undercut ask |

## Maker Order Model
All orders use `GTC` with `post_only=True` — zero maker fees on Polymarket.

### Bid Pricing
1. **Fair bid**: `est_prob - maker_edge_cushion` (default cushion: 0.05)
2. **Smart bid**: if the best ask on the CLOB is below our fair bid, undercut it by 1 tick ($0.01). This maximizes fill probability while maintaining edge.
3. **Edge guard**: never place a bid where `est_prob - bid < min_edge`

### Order Lifecycle
- Signal → `GTC` + `post_only=True` order placed
- Order tracked by `OrderManager` with `cancel_after_ts = resolution_ts - 30`
- If filled: position created, balance deducted
- If not filled by deadline: auto-cancelled
- Paper mode: probabilistic fill simulation

## Momentum Confirmation (Secondary Signal)

- **Trade flow** (weight: 0.6) — buy/sell volume ratio from last 500 trades
- **Orderbook imbalance** (weight: 0.4) — bid vs ask volume near mid price
- Combined score [-1, 1] feeds into probability as a minor adjustment

## Book-Aware Bidding
Smart bid replaces static formula when CLOB depth is available:
```
if best_ask < fair_bid and est_prob - (best_ask - tick) >= min_edge:
    bid = best_ask - tick_size   # undercut to be first in line
else:
    bid = fair_bid               # model-based price
```

## Dynamic ATR Volatility
ATR(14) from Binance 5-min klines provides real-time volatility:
- `dynamic_vol = ATR / current_price` (percentage)
- Falls back to static `btc_updown_5m_vol` on failure
- Used in probability model: higher vol → same delta is less significant
- Vol regime scaling: `effective_min_edge = min_edge × max(vol_ratio, 1.0)`

## Prediction Tracking
Every evaluated candidate (traded or not) is logged to the `predictions` table:
- `est_prob`, `bid_price`, `delta_pct`, `window_progress`, `momentum`, `dynamic_vol`
- After resolution: `actual_correct` and `pnl` updated via Gamma API
- Calibration stats logged every 100 cycles:
```
CALIBRATION | bucket=0.50-0.60 | n=23 | wins=12 | rate=52% | avg_pred=0.55
CALIBRATION | bucket=0.60-0.70 | n=18 | wins=11 | rate=61% | avg_pred=0.65
```

## Configuration
```
btc_updown_assets: ["BTC", "ETH", "SOL"]
btc_updown_intervals: ["5m", "15m"]
btc_updown_min_edge: 0.05         # min (est_prob - bid) to trade
btc_updown_5m_vol: 0.0010         # 5-min volatility baseline (0.10%)
btc_updown_logistic_k: 1.5        # logistic steepness
btc_updown_momentum_weight: 0.3   # momentum confirmation weight
btc_updown_min_ask: 0.03          # min bid price to consider
btc_updown_max_ask: 0.85          # max bid price to consider
btc_updown_maker_edge_cushion: 0.05  # fair_bid = est_prob - cushion
```

## Risk
- GTC maker orders with `post_only=True` (no taker fees)
- Strategy-specific min_confidence: 0.55 (overrides aggression tuner level)
- Enabled in ALL aggression levels including emergency
- Kelly sizing on available balance above hard floor
- Timing windows: 5m (30s–330s), 15m (60s–870s)
- Auto-cancel 30s before resolution
- Book-aware bidding ensures min_edge is always maintained

## Plan Reference
See `PLANS/btc-updown-plan.md` for implementation plan details.
