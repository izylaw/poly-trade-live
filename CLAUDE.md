# Polymarket Trading Bot

## Project
Adaptive Polymarket trading bot. Python, uses py-clob-client. Starts with $10 targeting $1000.

## Architecture
- `src/core/engine.py` — Main trading loop (scan -> signal -> risk -> execute -> adapt). Strategies run concurrently via ThreadPoolExecutor. Resolution checks run every 5th cycle.
- `src/strategies/` — Strategy implementations (high_probability, arbitrage, sports_daily, btc_updown, safe_compounder, llm_crypto)
- `src/llm/` — LLM client (OpenAI-compatible, Ollama) and prompt templates for llm_crypto strategy
- `src/risk/` — Risk management (kelly sizing, circuit breakers, hard floor)
- `src/adaptive/` — Goal tracking and aggression tuning
- `src/market_data/` — Gamma + CLOB + Binance API wrappers, market scanning. Supports batch orderbook fetches, parallel Gamma pagination, and slug-based crypto market discovery.
- `src/execution/` — Paper and live executors
- `src/storage/` — SQLite persistence

## Key Rules
- Balance must NEVER reach $0. Hard floor at 20% of starting capital.
- Every trade passes through risk_manager before execution.
- Paper trading mode by default (PAPER_TRADING=true).
- Half-Kelly position sizing.

## Commands
```
python -m src.main start --paper    # Start bot in paper mode
python -m src.main status           # Check balance and positions
python -m src.main goal             # Goal progress
python -m src.main history          # Trade history
python -m src.main setup            # Credential wizard
```
