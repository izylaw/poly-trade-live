# Arbitrage Strategy Reference (v2 — Multi-Mode)

## Overview

Three independent arbitrage techniques running in a single strategy pass. All produce FOK (fill-or-kill) orders with confidence=0.95. Arbitrage trades are risk-free by construction — profit is locked in at entry regardless of outcome.

## Mode A: Single-Market YES+NO Arb

### How it works
In any binary market, buying both YES and NO guarantees a $1.00 payout (exactly one resolves to $1.00). If the combined ask price is less than $1.00 minus taker fees, the difference is guaranteed profit.

### Math
```
cost = ask_YES + ask_NO
net_payout = 1.0 - fee_rate          # 1.0 - 0.0625 = 0.9375
spread = net_payout - cost
if spread > arb_min_spread (0.5%):
    emit BUY YES + BUY NO
```

### When it fires
Rarely. Market makers keep YES+NO sums >= $1.00. Most opportunities last milliseconds.

## Mode B: Multi-Outcome Event Arb

### How it works
Events with 3+ mutually exclusive outcomes (e.g., "Who will win the election?") have exactly one YES token resolve to $1.00. If the sum of all YES ask prices is less than the net payout, buying all YES tokens locks in profit.

### Math
```
total_cost = sum(ask_YES for each outcome)
net_payout = 1.0 - fee_rate
spread = net_payout - total_cost
if spread > arb_min_event_spread (2%):
    emit BUY YES for each outcome (FOK)
```

### Filtering
- Only events with 3-6 markets (`arb_min_event_markets` to `arb_max_event_legs`)
- **Excludes above/below events** — these are NOT mutually exclusive (multiple strikes can be true simultaneously)
- All markets in the event must have valid orderbook data

### Example
Event "Who will win?" with 4 candidates:
- Candidate A: YES ask = $0.22
- Candidate B: YES ask = $0.20
- Candidate C: YES ask = $0.23
- Candidate D: YES ask = $0.20
- Total cost = $0.85, net payout = $0.9375, spread = $0.0875 (8.75%)

## Mode C: Cross-Market Monotonicity Arb

### How it works
"Above $X" markets for the same asset and date must have monotonically decreasing YES prices as strike increases (higher strike = less likely to be above). When this ordering is violated, a guaranteed profit exists:

- **Buy lower-strike YES** (underpriced — should be more expensive)
- **Buy higher-strike NO** (the complement of the overpriced YES)

### Why it's guaranteed
Consider strikes $90k and $100k with BTC settling at any price:
- BTC > $100k: both YES tokens pay $1.00, lower-strike YES wins, higher-strike NO loses → net $1.00
- $90k < BTC < $100k: lower-strike YES pays $1.00, higher-strike NO pays $1.00 → net $2.00
- BTC < $90k: both YES lose, both NO win → higher-strike NO pays $1.00

In all scenarios, at least one leg pays $1.00.

### Math
```
For each adjacent pair (sorted by strike ascending):
    if yes_ask[low_strike] < yes_ask[high_strike]:  # violation!
        cost = yes_ask[low_strike] + no_ask[high_strike]
        spread = 1.0 - cost - 2 * fee_rate           # fees on both legs
        if spread > arb_mono_min_spread (1%):
            emit BUY low-strike YES + BUY high-strike NO
```

### Strike parsing
Extracts dollar amounts from market questions using regex: `\$([\d,]+)`
- "Will BTC be above $100,000?" → 100000
- "Will ETH be above $5000?" → 5000

## Configuration

```
arb_min_spread: 0.005        # min spread for single-market arb
arb_fee_rate: 0.0625         # Polymarket taker fee on $1.00 payout
arb_min_event_markets: 3     # min markets per event for multi-outcome
arb_min_event_spread: 0.02   # min spread after fees for multi-outcome
arb_max_event_legs: 6        # max legs (avoid consuming all position slots)
arb_mono_min_spread: 0.01    # min spread for monotonicity pair trades
```

## Risk Analysis

- **Confidence**: 0.95 (hard-coded) — arbitrage is near-certain by construction
- **Min confidence override**: 0.01 in `STRATEGY_MIN_CONFIDENCE` — almost never rejected on confidence
- **Order type**: FOK — must fill immediately or not at all (stale prices = no arb)
- **arb_group field**: Groups related legs so the execution layer can handle them atomically
- **Position slots**: Arbitrage is exempt from the global position cap. Multi-outcome arb consumes one slot per leg; `arb_max_event_legs` caps this at 6. Per-market limit = 2 (YES + NO legs)
- **Fee accounting**: All spread calculations deduct the 6.25% taker fee from expected payout

## Signals

Each signal includes:
- `arb_group`: Links legs of the same arb opportunity
  - `single:{market_id}` — single-market pair
  - `multi:{event_slug}` — multi-outcome event legs
  - `mono:{slug}:{low_strike}-{high_strike}` — monotonicity pair
- `order_type`: Always "FOK"
- `expected_value`: Per-leg share of the total spread
