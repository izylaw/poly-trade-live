# LLM Crypto Strategy Reference

## Overview
Uses a local LLM (Ollama/Qwen 27B) to analyze all crypto binary market types on Polymarket. The LLM receives Binance price data and Polymarket orderbook data, then returns probability estimates and trade actions.

Runs every Nth cycle (default: 2) to manage LLM latency (~2 min per batch).

## Market Types

| Type | Slug Pattern | Actions | Kline Interval |
|------|-------------|---------|----------------|
| Up/Down (1h, 4h) | `{asset}-updown-{interval}-{unix_ts}` | BUY_UP / BUY_DOWN | 1m |
| Above/Below (daily) | `{coin}-above-on-{month}-{day}` | BUY_YES / BUY_NO | 15m |
| Price Range (daily) | `{coin}-price-on-{month}-{day}` | BUY_YES / BUY_NO | 15m |
| Daily Up/Down | `{coin}-up-or-down-on-{month}-{day}` | BUY_YES / BUY_NO | 15m |
| Hit Price (weekly) | `what-price-will-bitcoin-hit-{month}-{start}-{end}` | BUY_YES / BUY_NO | 1h |
| Hit Price (monthly) | `what-price-will-{coin}-hit-in-{month}-{year}` | BUY_YES / BUY_NO | 1h |

### Asset Naming
- Interval up/down uses **tickers**: `btc`, `eth`, `sol`, `xrp`
- Daily/weekly/monthly use **full names**: `bitcoin`, `ethereum`, `solana`

## Data Sources

### Binance API (pre-fetched once per asset)
| Endpoint | Data | Use |
|----------|------|-----|
| `/api/v3/ticker/price` | Current price | Live asset price |
| `/api/v3/klines` | OHLCV candles | Reference price, ATR, recent klines |
| `/api/v3/trades` | Recent trades | Trade flow momentum |
| `/api/v3/depth` | Order book | Orderbook imbalance momentum |

### Gamma API (slug-based discovery)
- Up/Down: `get_crypto_updown_markets()` with time window filter
- Daily: `get_crypto_daily_markets()` — above/below, price range, daily up/down
- Weekly: `get_crypto_weekly_markets()` — Bitcoin hit price
- Monthly: `get_crypto_monthly_markets()` — BTC, ETH, SOL hit price
- Results cached with 300s TTL at Gamma level

### CLOB API (per-market)
- `get_price(token_id)` — Yes/No ask prices, best bid/ask
- `get_book(token_id)` — Full orderbook for smart bidding

## Analysis Pipeline

```
1. DISCOVER
   Source 1: Up/Down interval markets (1h, 4h) via crypto_discovery
   Source 2: Daily markets via gamma slug lookup (lookahead: 5 days)
   Source 3: Weekly hit price markets
   Source 4: Monthly hit price markets

2. GATHER DATA (per source)
   Pre-fetch Binance data once per asset (price, ATR, momentum, klines)
   Cap at batch_size * 2 markets to limit CLOB calls
   Skip cached markets (600s TTL for updown, 1800s for daily/weekly/monthly)

3. BATCH → LLM
   Send up to 5 markets per batch (max 2 batches per cycle)
   System prompt describes all market types and valid actions
   User prompt includes: price, delta, volatility, momentum, orderbook, klines

4. PARSE RESPONSE
   LLM returns JSON array of assessments
   Each: {market_id, action, estimated_probability, confidence_level, reasoning}
   Confidence scaling: low=0.85, medium=0.92, high=1.0

5. SIGNAL GENERATION
   Smart bid = undercut best ask if below fair bid, else fair bid
   Edge guard: scaled_prob - bid >= min_edge (0.05)
   EV check: expected value must be positive
   Order: GTC + post_only=True, cancel 30s before resolution
```

## Configuration

```
llm_enabled: false               # Master enable (env: LLM_ENABLED)
llm_base_url: http://...:11434   # Ollama endpoint
llm_model: qwen3.5:27b           # Model name
llm_api_key: ""                  # Optional (Ollama doesn't need one)
llm_batch_size: 5                # Markets per LLM call
llm_min_edge: 0.05               # Min (scaled_prob - bid) to trade
llm_cache_ttl: 600               # Updown assessment cache (seconds)
llm_daily_cache_ttl: 1800        # Daily/weekly/monthly cache (seconds)
llm_run_every_n_cycles: 2        # Run every Nth engine cycle
llm_max_tokens: 4096             # Max output tokens
llm_timeout: 300                 # HTTP timeout (seconds)
llm_context_size: 32768          # Ollama context window (num_ctx)
llm_maker_edge_cushion: 0.05     # fair_bid = est_prob - cushion
llm_intervals: ["1h", "4h"]      # Intervals for updown markets
llm_daily_lookahead_days: 5      # Days ahead for daily market scan
```

## Position Limits
- Per-strategy cap: 2
- Long-term positions (resolution >7 days) use a separate shared bucket of 5
- Per-market limit: 1

## Risk
- Maker GTC orders with `post_only=True` (zero maker fees)
- Inherits global risk gates (hard floor, max positions, daily loss limit)
- Uses btc_updown price range filters (min_ask=0.03, max_ask=0.85)
- Confidence scaling reduces raw LLM probability estimates
- Edge guard ensures minimum 5% edge on every trade
- Auto-cancel 30s before resolution
- Max 2 LLM batches per cycle to limit latency impact

## Known Limitations
- Local Ollama (Qwen 27B) takes ~2 minutes per batch call
- ~50% timeout rate at 5-minute timeout (GitHub issue #2)
- Hundreds of markets discovered but only ~10 analyzed per cycle due to batching
