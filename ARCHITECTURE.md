# Architecture

## System Overview

```
                         +------------------+
                         |     CLI (main)   |
                         |  click + rich    |
                         +--------+---------+
                                  |
                         +--------v---------+
                         |       Bot        |
                         |  (orchestrator)  |
                         +--------+---------+
                                  |
                    +-------------v--------------+
                    |      Trading Engine         |
                    |  30s loop: scan -> execute  |
                    +----+----+----+----+----+---+
                         |    |    |    |    |
          +--------------+    |    |    |    +--------------+
          |                   |    |    |                   |
+---------v-------+  +-------v--+ | +--v--------+  +------v--------+
|  Market Scanner |  |Strategies| | |Risk Manager|  |   Executor    |
|  Gamma + Filter |  |          | | |  + Kelly   |  | paper / live  |
+---------+-------+  +-------+--+ | +--+--------+  +------+--------+
          |                   |    |    |                   |
+---------v-------+           |    |    |            +-----v---------+
|   CLOB Client   |<----------+    |    |            |  Trade Log    |
|  py-clob-client |                |    |            |   SQLite      |
+-----------------+         +------v----v---+        +---------------+
                            | Adaptive Goal |
                            | Tracker+Tuner |
                            +---------------+
```

## Trading Loop (engine.py)

Each cycle runs every 30 seconds:

```
1. SCAN
   MarketScanner -> GammaClient.get_all_active_events()
                 -> extract_markets_from_events()
                 -> normalize_market() (parse JSON fields)
                 -> MarketFilter.filter_markets()
                 Result: list of tradeable markets
   (skipped when only self-discovering strategies are enabled)

2. ANALYZE (concurrent)
   All enabled strategies run in parallel via ThreadPoolExecutor:
     Strategy.analyze(markets, clob_client) -> TradeSignal[]
   Self-discovering strategies (sports_daily, btc_updown, llm_crypto) find their own markets.
   sports_daily batch-fetches orderbooks via get_books_batch() for efficiency.
   llm_crypto runs every Nth cycle (default: 2) to limit LLM API load.

3. RANK
   All signals sorted by expected_value (confidence * return)

4. RESOLVE (every 5th cycle)
   _resolve_predictions() — check unresolved predictions against Gamma
   _resolve_positions() — close positions in resolved markets with PnL

5. RISK GATE
   RiskManager.evaluate(signal, balance, positions, exposure)
   Must pass ALL checks:
     - Circuit breaker not active
     - Balance > hard floor
     - Confidence >= min threshold
     - Open positions < max
     - Portfolio exposure < max
     - Daily loss limit not hit
     - Half-Kelly sizing > min trade
     - Post-trade balance > hard floor

6. EXECUTE
   Executor routes to PaperExecutor or LiveExecutor
   Updates balance, positions, exposure after each fill

7. ADAPT (every 5 minutes)
   GoalTracker calculates progress toward $1000
   AggressionTuner adjusts risk params:
     conservative / moderate / aggressive / ultra / emergency
```

## Data Flow

```
Gamma API (REST)                    CLOB API (REST)
    |                                    |
    v                                    v
GammaClient                        PolymarketClobClient
(cached, 60s TTL,                  (get_price, get_books_batch,
 parallel pagination)               extract_price, post_order)
    |                                    |
    v                                    v
MarketScanner ----normalize----> Strategies analyze
    |                                    |
    v                                    v
MarketFilter                       TradeSignal
(volume, liquidity,                (market_id, token_id,
 spread, time)                      price, confidence, EV)
                                         |
                                         v
                                    RiskManager
                                    (7 gates + Kelly sizing)
                                         |
                                         v
                                    ApprovedTrade
                                    (signal + size + cost)
                                         |
                                         v
                                    Executor
                                   /          \
                          PaperExecutor    LiveExecutor
                          (simulated)      (CLOB orders)
                                   \          /
                                    TradeLog
                                    (SQLite WAL)
```

## Risk Architecture

```
                    TradeSignal
                         |
                         v
              +----------+-----------+
              |     RiskManager      |
              |                      |
              |  1. CircuitBreaker   |--- paused? (losses, API errors, daily limit)
              |  2. Hard Floor       |--- balance > 20% of starting capital?
              |  3. Confidence       |--- signal >= min_confidence (tunable)?
              |  4. Max Positions    |--- open_positions < 5?
              |  5. Exposure Limit   |--- portfolio < 60% of balance?
              |  6. Daily Loss       |--- today's loss < 15%?
              |  7. Kelly Sizing     |--- half_kelly > min_trade ($0.50)?
              |  8. Floor Recheck    |--- post-trade balance > hard floor?
              |                      |
              +----------+-----------+
                         |
                    ALL PASS? ----No----> Rejected
                         |
                        Yes
                         |
                         v
                   ApprovedTrade
                   (sized by half-Kelly,
                    capped at max_trade_pct)
```

### Circuit Breaker States

```
Normal ──3 consecutive losses──> Paused 30min
Normal ──daily loss >= 15%────> Paused 1hr
Normal ──3+ API errors────────> Paused 5min
Normal ──25%+ single-tick drop─> FULL STOP (manual reset)
```

## Adaptive Goal System

```
GoalTracker
  inputs: current_balance, start_date, target ($1000), target_days (60)
  outputs: progress_pct, required_daily_rate, actual_7day_rate, behind_pct

AggressionTuner
  reads GoalStatus, sets RiskManager params:

  behind_pct <= 0%  -> conservative (5% max trade, 0.85 confidence, high-prob only)
  behind_pct <= 5%  -> moderate     (10% max trade, 0.70 confidence, both strategies)
  behind_pct <= 20% -> aggressive   (15% max trade, 0.60 confidence, both strategies)
  behind_pct >  20% -> ultra        (15% max trade, 0.55 confidence, both strategies)
  balance < start   -> emergency    (5% max trade, 0.90 confidence, high-prob only)
```

## Storage Schema

```
SQLite (WAL mode)

trades
  id, timestamp, market_id, token_id, side, outcome,
  price, size, cost, strategy, confidence, kelly_fraction,
  order_type, status, fill_price, pnl, paper_trade

positions
  id, market_id, token_id, outcome, entry_price,
  size, cost, current_price, unrealized_pnl,
  status (open/closed), opened_at, closed_at, realized_pnl

daily_snapshots
  date, balance, portfolio_value, total_pnl,
  trades_count, wins, losses, daily_return_pct, aggression_level

bot_state
  key-value store for runtime state persistence
```

## Strategy Details

### High Probability

```
For each market with outcome priced $0.92-$0.98:
  confidence = price_level + volume_bonus(5%) + liquidity_bonus(3%)
  expected_value = confidence * (1.0 - price)
  order_type = GTC (patient fills)
```

### Arbitrage

```
For each binary market:
  if ask(YES) + ask(NO) < $1.00:
    spread = 1.0 - (ask_YES + ask_NO)
    if spread > 0.5%:
      emit BUY YES + BUY NO signals
      order_type = FOK (immediate fill)
      confidence = 0.95
```

### Sports Daily (self-discovering)

```
Discovery:
  For each configured tag (sports, nba, nfl, etc.):
    get_all_events_by_tag(tag) — parallel pagination via ThreadPoolExecutor
  Filter: resolution < 24h, volume > min, liquidity > min

Analysis (batch):
  Collect all token IDs upfront
  get_books_batch(all_tokens) — ~4 chunked requests instead of 332 sequential
  For each market, extract prices via extract_price() from cached books

Signal types:
  1. Spread capture — maker bid in wide spreads (GTC, post-only)
  2. Book imbalance — follow informed flow when bid side heavy
  3. Favorite value — exploit favorite-longshot bias (0.82-0.95 range)
```

### LLM Crypto (self-discovering)

```
Uses a local LLM (Ollama/Qwen 27B) to analyze crypto markets on Polymarket.
Runs every Nth cycle (default: 2) to limit LLM load.

Discovery (4 sources):
  1. Up/Down interval markets (1h, 4h) via slug: {asset}-updown-{interval}-{unix_ts}
  2. Daily markets via slug: {coin}-above-on-{month}-{day},
     {coin}-price-on-{month}-{day}, {coin}-up-or-down-on-{month}-{day}
  3. Weekly hit price via slug: what-price-will-bitcoin-hit-{month}-{start}-{end}
  4. Monthly hit price via slug: what-price-will-{coin}-hit-in-{month}-{year}
  Assets: BTC, ETH, SOL (tickers for updown, full names for daily/weekly/monthly)

Data gathering:
  Pre-fetches Binance data once per asset (price, ATR, momentum, klines)
  Caps markets at batch_size * 2 to limit CLOB calls
  Uses 1m klines for updown, 15m for daily, 1h for weekly/monthly

LLM analysis:
  Sends batches of up to 5 markets to LLM with system + user prompt
  LLM returns JSON array of assessments with action, probability, confidence
  Actions: BUY_UP/BUY_DOWN (updown), BUY_YES/BUY_NO (yes/no markets)
  Confidence scaling: low=0.85, medium=0.92, high=1.0

Order execution:
  Maker GTC orders with post_only=True (zero maker fees)
  Smart bidding: undercuts best ask when below fair bid
  Edge guard: bid must maintain min_edge (default: 0.05)
```

## Module Dependencies

```
config/settings ─────────────────────────────────────> (used by everything)

utils/logger ──> setup once at boot
utils/retry ───> clob_client, gamma_client

storage/db ────> trade_log ──> paper_executor, live_executor,
                               position_tracker, order_manager

market_data/gamma_client ──────> market_scanner, llm_crypto
market_data/clob_client ───────> strategies, live_executor
market_data/binance_client ────> btc_updown, safe_compounder, llm_crypto
market_data/crypto_discovery ──> btc_updown, safe_compounder, llm_crypto
market_data/market_filter ─────> market_scanner

llm/client ────> llm_crypto (OpenAI-compatible API, Ollama)
llm/prompts ───> llm_crypto

risk/kelly ────────> risk_manager
risk/circuit_breaker > risk_manager

strategies/base ───> high_probability, arbitrage, sports_daily,
                     btc_updown, safe_compounder, llm_crypto

adaptive/goal_tracker ───> aggression_tuner

core/engine ──> (all of the above)
bot ──────────> engine (wires everything)
main ─────────> bot (CLI entry point)
```
