# BTC Up/Down Strategy Plan (v3 — Maker Orders + Multi-Asset)

## Overview
Trade crypto Up/Down prediction markets on Polymarket using maker orders (zero fees) across multiple assets (BTC, ETH, SOL) and intervals (5m, 15m). Uses Binance spot data to estimate outcome probabilities via a logistic price delta model.

## Market Structure
- **Slug pattern**: `{asset}-updown-{interval}-{timestamp}`
- **Assets**: BTC, ETH, SOL
- **Intervals**: 5m, 15m
- **Outcomes**: "Up" / "Down" (binary, resolves via Chainlink price feed)

## Signal Computation (v2)
Price delta model:

1. **Reference price capture** — find price at window start from 1-min klines
2. **Price delta** — `(current - reference) / reference` as percentage
3. **Probability estimation** — logistic function: `1 / (1 + exp(-k * z))` where `z = normalized_delta * sqrt(progress) + momentum * weight * sqrt(progress)`
4. **Both-sides evaluation** — check both "Up" and "Down" tokens, pick highest positive EV
5. **Edge filter** — only trade when `est_prob - bid >= min_edge`

Momentum confirmation (secondary): trade flow (0.6) + orderbook imbalance (0.4)

## Maker Orders (v2.2)
- All orders placed as `GTC` with `post_only=True` — zero maker fees
- Bid price: `est_prob - maker_edge_cushion` (static) or book-aware smart bid
- Orders auto-cancel 30s before resolution via `cancel_after_ts`
- Paper mode: probabilistic fill simulation based on window progress

## Book-Aware Bid Placement (v3)
Smart bid logic replaces static `est_prob - cushion`:
- If `best_ask < fair_bid`: undercut ask by 1 tick to maximize fill probability
- If `best_ask >= fair_bid`: use model-based fair bid
- Always enforce `est_prob - bid >= min_edge`

## Multi-Asset & Multi-Interval (v3)
- Assets: BTC, ETH, SOL (configurable)
- Intervals: 5m (30s-330s window), 15m (60s-870s window)
- Strategy iterates all asset×interval combinations each cycle
- Gamma/Binance clients already handle arbitrary assets/intervals

## Prediction Tracking & Calibration (v3)
- Every evaluated candidate logged to `predictions` table (traded or not)
- After resolution: fetch market outcome from Gamma API, update `actual_correct`
- Calibration stats: probability buckets vs actual win rate, logged every 100 cycles
- Enables model validation without requiring live trading PnL

## Dynamic ATR Volatility (v2.1)
- ATR(14) computed from Binance 5-min klines, converted to percentage of price
- Falls back to static `btc_updown_5m_vol` if ATR fails
- Vol regime scaling: `effective_min_edge = min_edge × max(vol_ratio, 1.0)`

## Implementation Files
- `src/strategies/btc_updown.py` — Strategy (price delta model, smart bid, predictions)
- `src/market_data/binance_client.py` — Binance data pipeline
- `src/market_data/gamma_client.py` — `get_crypto_updown_markets()`, `get_market_resolution()`
- `src/market_data/clob_client.py` — `get_book()` for full orderbook
- `src/risk/risk_manager.py` — Strategy-specific min_confidence override
- `src/adaptive/aggression_tuner.py` — btc_updown in emergency strategies
- `src/storage/models.py` — Schema (trades, positions, predictions)
- `src/storage/trade_log.py` — Trade + prediction CRUD, calibration stats
- `src/core/engine.py` — Prediction logging, resolution, calibration output
- `src/config/defaults.py` + `settings.py` — Configuration
- `tests/test_btc_updown.py` — Unit tests (45 tests)

## Strategy Reference
See `STRATEGIES/btc-updown-strategy.md` for detailed strategy documentation.
