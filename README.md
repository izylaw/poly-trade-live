# Polymarket Trading Bot

Adaptive trading bot for [Polymarket](https://polymarket.com) prediction markets. Starts with $10 USDC targeting $1,000 through automated strategy execution with built-in risk management.

## How It Works

The bot runs a 30-second trading loop:

1. **Scan** — Fetches active markets from Gamma API, filters by volume/liquidity/spread
2. **Analyze** — Enabled strategies score markets and emit trade signals
3. **Rank** — Signals sorted by expected value (confidence × return)
4. **Risk** — Every trade passes through 7 risk gates before approval
5. **Execute** — Approved trades routed to paper or live executor
6. **Adapt** — Every 5 minutes, adjusts aggression level based on goal progress

## Strategies

**High Probability** — Buys outcomes priced $0.92–$0.98 that are near-certain to resolve YES. Small edge (2–8%) but very high win rate. Uses GTC limit orders for patient fills.

**Arbitrage** — Detects when `ask(YES) + ask(NO) < $1.00` for guaranteed profit. Uses FOK orders for immediate execution. Rare but risk-free when found.

**Sports Daily** — Trades daily sports events using market microstructure signals: spread capture (maker bids in wide spreads), book imbalance (follow informed flow), and favorite value (exploit favorite-longshot bias). Self-discovering — finds markets via Gamma tag search with parallel pagination. Uses batch orderbook fetches for efficiency.

## Risk Management

Every trade must pass all checks:

| Rule | Default | Purpose |
|------|---------|---------|
| Hard Floor | 20% of starting capital ($2) | Balance never reaches $0 |
| Max Single Trade | 10% of balance | No catastrophic single loss |
| Max Portfolio Exposure | 60% of balance | Keep cash reserve |
| Max Open Positions | 5 | Prevent over-diversification |
| Daily Loss Limit | 15% of start-of-day balance | Stop bleeding |
| Min Trade Size | $0.50 | Skip meaningless trades |
| Consecutive Loss Limit | 3 losses → 30min pause | Break losing streaks |

Position sizing uses **half-Kelly criterion** — mathematically optimal sizing scaled to 50% for safety.

Circuit breakers auto-pause trading on drawdowns and halt completely if balance drops 25%+ in a single tick.

## Adaptive Goal System

The bot tracks progress toward $1,000 and adjusts behavior:

| Condition | Level | Max Trade | Min Confidence | Strategies |
|-----------|-------|-----------|---------------|------------|
| Ahead of schedule | Conservative | 5% | 0.85 | High-prob only |
| On track | Moderate | 10% | 0.70 | All enabled |
| Behind <20% | Aggressive | 15% | 0.60 | All enabled |
| Behind >20% | Ultra | 15% | 0.55 | All enabled |
| Below starting capital | Emergency | 5% | 0.90 | High-prob only |

If the initial target rate isn't sustainable, the bot recalculates the timeline automatically.

## Setup

### Requirements

- Python 3.10+
- Polygon wallet with USDC.e
- Polymarket API credentials

### Install

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Configure Credentials

Run the interactive setup wizard:

```bash
python scripts/setup_credentials.py
```

This walks you through:
1. Entering your Polygon wallet private key
2. Funding your wallet with USDC.e
3. Deriving Polymarket API credentials
4. Saving everything to `.env`

Or copy `.env.example` to `.env` and fill in manually:

```
POLY_PRIVATE_KEY=0x...
POLY_API_KEY=...
POLY_API_SECRET=...
POLY_API_PASSPHRASE=...
STARTING_CAPITAL=10
TARGET_BALANCE=1000
TARGET_DAYS=60
PAPER_TRADING=true
```

## Usage

```bash
# Start bot in paper trading mode (default)
python -m src.main start --paper

# Start bot in live trading mode
python -m src.main start --live

# Check balance and open positions
python -m src.main status

# View goal progress
python -m src.main goal

# View trade history
python -m src.main history

# Quick balance check
python scripts/check_balance.py
```

### Paper Trading

Paper trading is on by default. It uses real market data but simulates execution with 0.1% adverse slippage. All trades are logged to SQLite with `paper_trade=True`. Run paper mode for at least 24 hours before going live.

## Project Structure

```
src/
├── main.py                  # CLI entry point (click + rich)
├── bot.py                   # Orchestrator — wires all components
├── config/                  # Pydantic settings, defaults
├── core/
│   ├── engine.py            # Trading loop (scan → signal → risk → execute)
│   ├── balance_manager.py   # Balance queries, hard floor enforcement
│   ├── position_tracker.py  # Open positions, P&L
│   └── order_manager.py     # Order lifecycle
├── strategies/
│   ├── base.py              # Abstract strategy interface
│   ├── high_probability.py  # Near-certain outcome buying
│   ├── arbitrage.py         # YES+NO spread detection
│   ├── sports_daily.py      # Sports microstructure (spread, imbalance, favorite)
│   ├── btc_updown.py        # BTC up/down interval trading
│   └── safe_compounder.py   # Safe compounding strategy
├── risk/
│   ├── risk_manager.py      # Gates every trade
│   ├── kelly.py             # Half-Kelly position sizing
│   └── circuit_breaker.py   # Auto-pause on drawdowns
├── adaptive/
│   ├── goal_tracker.py      # $10→$1000 progress tracking
│   └── aggression_tuner.py  # Adjusts risk params dynamically
├── market_data/
│   ├── gamma_client.py      # Gamma API (markets, events)
│   ├── clob_client.py       # CLOB API wrapper (orders, book)
│   ├── market_scanner.py    # Scans for opportunities
│   └── market_filter.py     # Volume, liquidity, spread filters
├── execution/
│   ├── executor.py          # Routes to paper or live
│   ├── paper_executor.py    # Simulated execution
│   └── live_executor.py     # Real CLOB order submission
├── storage/
│   ├── db.py                # SQLite setup (WAL mode)
│   ├── models.py            # Table schemas
│   └── trade_log.py         # Trade & snapshot persistence
└── utils/
    ├── logger.py            # Structured logging (console + file)
    └── retry.py             # Exponential backoff for API calls
```

## Tests

```bash
python -m pytest tests/ -v
```

Covers: Kelly sizing, risk manager gates, strategy signal generation, goal tracking, aggression tuning, sports daily discovery/signals, and market scanner pipeline.
