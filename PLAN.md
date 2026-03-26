# Implementation Plan: IMPROVEMENTS.md TODO Items

Implement the remaining TODO improvements from the Nunchi auto-researchtrading analysis.
Scoped to items 1, 2, 3 (remaining), 5, 6, 8, 9 (remaining). Item 7 (backtest harness) deferred — high effort, needs historical data accumulation first.

---

## Step 1: Add technical indicators to BinanceClient (items 1, 2, 8)

**File:** `src/market_data/binance_client.py`

Add three new methods that reuse the existing `get_klines()` with its caching:

- `compute_rsi(asset, interval="1m", period=8, limit=50) -> float | None` — RSI using SMA-based calculation
- `compute_ema_pair(asset, fast=7, slow=26, interval="1m", limit=50) -> tuple[float, float] | None` — returns (ema_fast, ema_slow)
- `compute_macd(asset, fast=14, slow=23, signal=9, interval="1m", limit=50) -> float | None` — returns histogram value
- `compute_bb_percentile(asset, period=20, interval="1m", limit=100) -> float | None` — returns BB width percentile (0-100)

All methods compute from kline close prices. No new API calls — they call `get_klines()` which is already cached.

**Tests:** `tests/test_binance_indicators.py` — unit tests with synthetic kline data for each indicator.

---

## Step 2: Add ensemble voting to btc_updown prefetch and signal generation (item 1)

**File:** `src/strategies/btc_updown.py`

### 2a. Extend `_prefetch_asset_data()` to compute indicators

Add to the prefetched dict:
```python
"rsi": self.binance.compute_rsi(asset, interval="1m", period=8),
"ema_pair": self.binance.compute_ema_pair(asset, fast=7, slow=26, interval="1m"),
"macd_hist": self.binance.compute_macd(asset, fast=14, slow=23, signal=9, interval="1m"),
"bb_percentile": self.binance.compute_bb_percentile(asset, period=20, interval="1m"),
```

### 2b. Add `_count_ensemble_votes()` method

```python
def _count_ensemble_votes(self, outcome: str, delta_pct: float, prefetched: dict) -> int:
    direction = 1.0 if outcome.lower() == "up" else -1.0
    votes = 0
    # Signal 1: price momentum (existing delta)
    if direction * delta_pct > 0: votes += 1
    # Signal 2: EMA crossover
    ema_pair = prefetched.get("ema_pair")
    if ema_pair and direction * (ema_pair[0] - ema_pair[1]) > 0: votes += 1
    # Signal 3: RSI(8) direction
    rsi = prefetched.get("rsi")
    if rsi is not None and direction * (rsi - 50) > 0: votes += 1
    # Signal 4: MACD histogram
    macd = prefetched.get("macd_hist")
    if macd is not None and direction * macd > 0: votes += 1
    return votes
```

### 2c. Gate signal generation on vote count

In `_evaluate_both_sides()`, after computing `market_ref_bid` and before the model probability call, add:
```python
votes = self._count_ensemble_votes(outcome, delta_info["delta_pct"], prefetched)
if votes < self.min_ensemble_votes:
    # Log skip prediction and continue
    continue
```

### 2d. New settings

- `btc_updown_min_ensemble_votes: int = 3` (require 3 of 4 signals to agree)
- `btc_updown_ensemble_enabled: bool = True` (feature flag for A/B testing)

**Tests:** Add `TestEnsembleVoting` class in `tests/test_btc_updown.py` — test vote counting with various indicator combinations, test that signals are filtered when votes < min.

---

## Step 3: Add RSI-based exit to take-profit check (items 2, 3)

**File:** `src/core/engine.py`

Extend `_check_take_profit()` to also check RSI mean-reversion exits:

- For each open crypto position, fetch RSI(8) via Binance
- If position is "Up" and RSI > 69: trigger early sell (mean-reversion risk)
- If position is "Down" and RSI < 31: trigger early sell
- Reuse the existing `sell_position()` path

Add a shared `BinanceClient` instance to the engine (currently each strategy creates its own). Pass it in from `bot.py`.

**New settings:**
- `rsi_exit_enabled: bool = True`
- `rsi_exit_overbought: float = 69.0`
- `rsi_exit_oversold: float = 31.0`

**Tests:** `tests/test_rsi_exit.py` — test RSI exit triggers for Up/Down positions.

---

## Step 4: Composite performance scoring per strategy (item 5)

**File:** `src/adaptive/strategy_scorer.py` (new)

```python
def compute_strategy_score(trade_log, strategy: str, lookback_days: int = 7) -> float:
```

Queries `trades` table for the given strategy over the lookback window. Computes:
- Sharpe-like ratio: mean_pnl / std_pnl
- Trade factor: sqrt(min(count / 20, 1.0))
- Drawdown penalty: max(0, max_drawdown - 0.15) * 0.05
- Returns: sharpe * trade_factor - dd_penalty

**File:** `src/storage/trade_log.py`

Add `get_strategy_trades(strategy, lookback_days)` method — queries trades with pnl != NULL for the given strategy within the lookback window.

**File:** `src/adaptive/aggression_tuner.py`

In `_determine_level()`, after choosing a level based on goal progress, check strategy scores. If a strategy scores < -0.5 over the last 7 days, remove it from the enabled list for that level. This is additive — the goal-based level selection stays, but underperformers get filtered out.

**Tests:** `tests/test_strategy_scorer.py` — test scoring with mock trade data.

---

## Step 5: Uniform position sizing option (item 6)

**File:** `src/risk/kelly.py`

Add:
```python
def uniform_size(available_balance: float, pct: float, min_trade: float = 0.50) -> float:
    size = available_balance * pct
    return round(size, 2) if size >= min_trade else 0.0
```

**File:** `src/risk/risk_manager.py`

In `evaluate()`, after Kelly sizing, check if the strategy should use uniform sizing:
```python
if self.settings.uniform_sizing_strategies and signal.strategy in self.settings.uniform_sizing_strategies:
    size = uniform_size(available, self.settings.uniform_sizing_pct, self.settings.min_trade_size)
else:
    size = half_kelly(...)
```

**New settings:**
- `uniform_sizing_pct: float = 0.0` (0 = disabled)
- `uniform_sizing_strategies: list[str] = []` (e.g., ["btc_updown", "safe_compounder"])

**Tests:** Add tests in `tests/test_risk_manager.py`.

---

## Step 6: Simplification — remove vol ratio scaling and dynamic cushion (item 9)

**File:** `src/strategies/btc_updown.py`

### 6a. Remove volatility regime scaling on min_edge

Replace:
```python
vol_ratio = dynamic_vol / self.btc_5m_vol if self.btc_5m_vol > 0 else 1.0
vol_ratio = min(max(vol_ratio, 1.0), 2.0)
effective_min_edge = self.min_edge * vol_ratio
```
With:
```python
effective_min_edge = self.min_edge
```

### 6b. Remove dynamic cushion

Replace the `_get_smart_bid` cushion logic:
```python
if est_prob > 0.65:
    cushion = 0.02
else:
    cushion = self.maker_edge_cushion
```
With:
```python
cushion = self.maker_edge_cushion
```

### 6c. Collapse aggression levels from 5 to 3

**File:** `src/adaptive/aggression_tuner.py`

Reduce to 3 levels:
- `conservative`: balance ahead or emergency (< 60% start) → max_trade 5%, min_conf 0.85
- `moderate`: on track (within 20% of expected) → max_trade 10%, min_conf 0.70
- `aggressive`: behind > 20% → max_trade 15%, min_conf 0.60

All levels enable all strategies (including llm_crypto when configured).

**Tests:** Update existing tests that reference 5 levels.

---

## Step 7: Wire ensemble data through safe_compounder (item 1 extension)

**File:** `src/strategies/safe_compounder.py`

Safe compounder uses `crypto_utils.estimate_outcome_probability()`. It should also benefit from the ensemble voting gate. Add the same `_count_ensemble_votes()` check before generating signals, reusing the indicators from prefetched data.

---

## Summary of new/modified files

| File | Action |
|------|--------|
| `src/market_data/binance_client.py` | Add 4 indicator methods |
| `src/strategies/btc_updown.py` | Add ensemble voting, remove vol scaling + dynamic cushion |
| `src/strategies/safe_compounder.py` | Add ensemble voting gate |
| `src/core/engine.py` | Add RSI exit check in take-profit |
| `src/risk/kelly.py` | Add `uniform_size()` |
| `src/risk/risk_manager.py` | Add uniform sizing path |
| `src/adaptive/strategy_scorer.py` | New file — composite scoring |
| `src/adaptive/aggression_tuner.py` | Collapse to 3 levels, integrate scorer |
| `src/storage/trade_log.py` | Add `get_strategy_trades()` |
| `src/config/defaults.py` | Add new defaults |
| `src/config/settings.py` | Add new settings fields |
| `tests/test_binance_indicators.py` | New — indicator unit tests |
| `tests/test_btc_updown.py` | Add ensemble voting tests |
| `tests/test_rsi_exit.py` | New — RSI exit tests |
| `tests/test_strategy_scorer.py` | New — scorer tests |
| `tests/test_risk_manager.py` | Add uniform sizing tests |

## Execution order

Steps 1→2→7 (indicators → ensemble → safe_compounder) are the critical path.
Steps 3, 4, 5, 6 are independent and can be done in any order after step 1.
