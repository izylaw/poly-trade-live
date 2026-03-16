# High Probability Strategy Reference

## Overview
Buy outcomes priced $0.92-$0.98 that are near-certain to resolve YES. Small edge per trade (2-8%) but very high win rate. Uses GTC limit orders for patient fills.

Also includes a longshot component: back underpriced outcomes at $0.03-$0.20 when volume confirms interest.

## Core Model

### High Probability (Primary)
```
For each market with outcome priced $0.92-$0.98:
    confidence = price + volume_bonus(5%) + liquidity_bonus(3%)
    expected_value = confidence * (1.0 - price)
    if confidence >= min_confidence and EV > 0:
        emit BUY signal at GTC limit
```

### Longshot (Secondary)
```
For each market with outcome priced $0.03-$0.20:
    confidence = longshot_conf_multiplier * price
    expected_value = confidence * (1.0 - price) - (1.0 - confidence) * price
    if EV > 0:
        emit BUY signal via maker bid
```

Smart bidding: if the best ask is below our fair bid, undercut it by 1 tick for faster fills.

## Maker Orders
- High-prob orders: `GTC` limit orders (patient fills, willing to wait)
- Longshot orders: `GTC` with `post_only=True` and `cancel_after_ts` (auto-cancel after configurable TTL)
- Both benefit from zero maker fees on Polymarket

## Configuration
```
high_prob_min_price: 0.92
high_prob_max_price: 0.98
high_prob_longshot_threshold: 0.20
high_prob_longshot_min_price: 0.03
high_prob_longshot_conf_multiplier: 1.5
high_prob_maker_ttl_hours: 48
```

## Position Limits
- Per-strategy cap: 8 short-term positions
- Long-term positions (resolution >7 days) use a separate shared bucket of 5
- Per-market limit: 1
- **Fill-time enforcement**: pending GTC orders are NOT counted against the per-strategy cap (allowing wider orderbook coverage). The limit is enforced when orders fill — once 8 positions are open, remaining pending HP orders are cancelled.

## Risk
- Enabled at ALL aggression levels (core strategy)
- Position sizing via half-Kelly criterion
- Each trade is small relative to balance (high confidence = small Kelly fraction)
- No external data dependencies (pure Gamma + CLOB)

## Implementation Files
- `src/strategies/high_probability.py` — Strategy implementation
- `tests/test_strategies.py` — Unit tests
