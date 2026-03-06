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
   MarketScanner -> GammaClient.get_all_active_markets()
                 -> normalize_market() (parse JSON fields)
                 -> MarketFilter.filter_markets()
                 Result: list of tradeable markets

2. ANALYZE
   For each enabled strategy (based on aggression level):
     Strategy.analyze(markets, clob_client) -> TradeSignal[]
   Strategies query CLOB orderbooks for live prices

3. RANK
   All signals sorted by expected_value (confidence * return)

4. RISK GATE
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

5. EXECUTE
   Executor routes to PaperExecutor or LiveExecutor
   Updates balance, positions, exposure after each fill

6. ADAPT (every 5 minutes)
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
(cached, 60s TTL)                  (get_price, post_order)
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

## Module Dependencies

```
config/settings ─────────────────────────────────────> (used by everything)

utils/logger ──> setup once at boot
utils/retry ───> clob_client, gamma_client

storage/db ────> trade_log ──> paper_executor, live_executor,
                               position_tracker, order_manager

market_data/gamma_client ──> market_scanner
market_data/clob_client ───> strategies, live_executor
market_data/market_filter ─> market_scanner

risk/kelly ────────> risk_manager
risk/circuit_breaker > risk_manager

strategies/base ───> high_probability, arbitrage

adaptive/goal_tracker ───> aggression_tuner

core/engine ──> (all of the above)
bot ──────────> engine (wires everything)
main ─────────> bot (CLI entry point)
```
