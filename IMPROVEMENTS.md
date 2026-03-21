# Improvements from auto-researchtrading for poly-trade-live

Analysis of [Nunchi-trade/auto-researchtrading](https://github.com/Nunchi-trade/auto-researchtrading) — an AI-driven autonomous strategy optimizer for Hyperliquid perp futures that achieved 7.9x Sharpe improvement (2.7 → 21.4) over 103 autonomous experiments.

---

## Status Overview

| # | Improvement | Impact | Effort | Priority | Status |
|---|------------|--------|--------|----------|--------|
| 1 | Multi-signal ensemble voting | High | Medium | P0 | TODO |
| 2 | RSI(8) indicator | High | Low | P0 | TODO |
| 3 | Exit hierarchy / early exit | Medium-High | High | P1 | **PARTIAL** — take-profit at 30%+ gain implemented |
| 4 | Cooldown between trades | Medium | Low | P2 | **DONE** — per-asset cooldown (2 win / 5 loss cycles) |
| 5 | Composite performance scoring | Medium | Medium | P1 | TODO |
| 6 | Uniform sizing option | Medium | Low | P2 | TODO |
| 7 | Backtest harness | High | High | P1 | TODO |
| 8 | BB compression filter | Low-Medium | Low | P2 | TODO |
| 9 | Simplification audit | Medium | Low | P1 | **PARTIAL** — edge relaxation removed, momentum dampened to 0.10 |

### Already implemented (from PR #6):
- **Market anchoring**: probability model blends 70% model + 30% market bid (`_compute_model_probability` + `_estimate_outcome_probability`)
- **Market disagreement filter**: skips trades when unblended model and market diverge >25 points
- **Time factor cap**: linear capped at 0.85 (replaces `sqrt(window_progress)`)
- **Momentum dampening**: effective weight capped at 0.10 (was 0.30)
- **Edge relaxation removed**: flat `effective_min_edge` (no special case for est_prob > 0.65)
- **Per-asset cooldown**: 2 cycles after win, 5 cycles after loss
- **Take-profit**: auto-sell positions with 30%+ gain
- **Async logging**: QueueHandler to avoid blocking trading loop
- **Skip prediction logging**: skipped candidates logged with reasons for observability

---

## 1. Multi-Signal Ensemble with Majority Voting (P0 — TODO)

**What Nunchi does:** Uses a 6-signal ensemble (momentum, very-short momentum, EMA crossover, RSI(8), MACD, BB compression) with a 4/6 majority vote requirement before entering a trade.

**Current state:** `btc_updown` uses a single logistic model (`_compute_model_probability`) combining delta + dampened momentum into one z-score, then blends 70/30 with market bid. Better than before (market anchoring reduces overconfidence), but still a single-signal architecture.

**Recommendation:** Add an ensemble voting layer. Compute multiple independent signals from Binance kline data and require majority agreement:
- Short-term momentum (existing delta_pct)
- EMA crossover (EMA(7) vs EMA(26))
- RSI(8) direction (above/below 50)
- MACD histogram sign
- Require 3/4 signal agreement before generating a trade signal

### Implementation sketch

```python
# In BinanceClient, add:
def compute_rsi(self, asset, period=8, kline_interval="1h", limit=50): ...
def compute_ema(self, asset, period, kline_interval="1h", limit=50): ...
def compute_macd(self, asset, fast=14, slow=23, signal=9, kline_interval="1h"): ...

# In btc_updown.py:
def _count_votes(self, asset, prefetched, outcome) -> int:
    votes = 0
    direction = 1.0 if outcome.lower() == "up" else -1.0
    if direction * delta_pct > threshold: votes += 1  # momentum
    if direction * (ema7 - ema26) > 0: votes += 1     # EMA crossover
    if direction * (rsi - 50) > 0: votes += 1          # RSI(8)
    if direction * macd_hist > 0: votes += 1            # MACD
    return votes

# Only generate signal if votes >= MIN_VOTES (e.g., 3 of 4)
```

---

## 2. RSI with Period 8 (P0 — TODO)

**What Nunchi discovered:** RSI(8) added +5 Sharpe points over RSI(14) for hourly crypto. Standard RSI(14) is too sluggish.

**Current state:** `BinanceClient` computes ATR but no RSI. No mean-reversion indicator exists.

**Recommendation:** Add `compute_rsi(asset, period=8)` to `BinanceClient`, then use in two ways:
- As an ensemble voter signal (improvement #1)
- As an exit/confidence-reduction trigger: skip longs when RSI > 69, skip shorts when RSI < 31

```python
# src/market_data/binance_client.py
def compute_rsi(self, asset: str, interval: str = "1h", period: int = 8, limit: int = 50) -> float | None:
    klines = self.get_klines(asset, interval, limit)
    if len(klines) < period + 1:
        return None
    closes = [k["close"] for k in klines]
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas[-period:]]
    losses = [abs(min(d, 0)) for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))
```

---

## 3. Exit Hierarchy / Trailing Stops (P1 — PARTIAL)

**What Nunchi does:** 3-tier exit: (1) ATR trailing stop at 5.5x, (2) RSI mean-reversion exit, (3) signal flip.

**Current state:** Take-profit at 30%+ gain is implemented via `_check_take_profit()` in `engine.py`. This covers the "let winners exit" case. Missing: trailing stop for losers, RSI-based mean-reversion exit.

**Remaining work:**
- Add ATR-based stop-loss check alongside the take-profit check
- Add RSI(8) mean-reversion exit: sell longs when RSI > 69
- Both require the SELL capability already added in `live_executor.py` and `paper_executor.py`

---

## 4. Cooldown Between Trades (P2 — DONE)

**What Nunchi does:** 2-bar cooldown between exit and re-entry.

**Current state:** Implemented in `engine.py:360-364` — per-asset cooldown of 2 cycles after win, 5 cycles after loss. Enforced in the signal evaluation loop at `engine.py:243-251`.

No further work needed.

---

## 5. Composite Performance Scoring per Strategy (P1 — TODO)

**What Nunchi uses:**
```
score = sharpe × √(min(trades/50, 1.0)) − drawdown_penalty − turnover_penalty
```

**Current state:** Calibration stats track win rate by probability bucket (`get_calibration_stats`). Aggression tuner toggles strategies by balance-vs-goal, not by strategy performance.

**Recommendation:** Add rolling per-strategy scoring:

```python
# src/adaptive/strategy_scorer.py
def compute_strategy_score(trade_log, strategy: str, lookback_days: int = 7) -> float:
    trades = trade_log.get_strategy_trades(strategy, lookback_days)
    if len(trades) < 5:
        return 0.0
    pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
    if not pnls:
        return 0.0
    mean_pnl = sum(pnls) / len(pnls)
    std_pnl = (sum((p - mean_pnl)**2 for p in pnls) / len(pnls)) ** 0.5
    sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0.0
    trade_factor = min(len(pnls) / 20, 1.0) ** 0.5
    max_dd = compute_max_drawdown(pnls)
    dd_penalty = max(0, max_dd - 0.15) * 0.05
    return sharpe * trade_factor - dd_penalty
```

Use this to dynamically disable underperforming strategies rather than relying only on aggression levels.

---

## 6. Uniform Position Sizing Option (P2 — TODO)

**What Nunchi discovered:** Uniform 8% equity per position beat momentum-weighted sizing (+1.7 Sharpe).

**Current state:** Half-Kelly sizing varies position size with confidence.

**Recommendation:** Add a config flag to A/B test:
```python
# settings.py
uniform_sizing_pct: float = 0.0  # 0 = use Kelly, >0 = fixed % of available balance
```
Test fixed 5-8% for `btc_updown` and `safe_compounder`, keep Kelly for `high_probability`.

---

## 7. Automated Strategy Backtesting Loop (P1 — TODO)

**What Nunchi does:** Autonomous strategy iteration: modify → backtest → commit if improved → revert if worse. 103 experiments → 7.9x improvement.

**Current state:** Prediction logging (`log_prediction`/`resolve_prediction`) records all signals with outcomes. Calibration stats available. No replay engine.

**Recommendation:**
1. Build a replay engine that re-runs strategy logic against historical prediction data with different parameters
2. Use the composite score from improvement #5 to evaluate
3. Automate parameter sweeps (e.g., `logistic_k` 1.0–3.0, `min_edge` 0.03–0.08)

```
src/backtest/
├── replay.py          # Re-run strategy against historical predictions
├── parameter_sweep.py # Grid search over parameter combinations
└── scorer.py          # Composite scoring formula
```

---

## 8. Bollinger Band Compression as Volatility Filter (P2 — TODO)

**What Nunchi discovered:** BB width below 85th percentile as an ensemble signal added +0.9 Sharpe. Compression = breakout imminent.

**Current state:** Volatility is ATR-based only (`dynamic_vol`). No compression detection.

**Recommendation:**
```python
def compute_bb_compression(self, asset, period=20, lookback=100, kline_interval="1h"):
    klines = self.get_klines(asset, kline_interval, lookback)
    closes = [k["close"] for k in klines]
    widths = []
    for i in range(period, len(closes)):
        window = closes[i-period:i]
        mean = sum(window) / period
        std = (sum((c - mean)**2 for c in window) / period) ** 0.5
        width = (2 * std) / mean
        widths.append(width)
    if not widths:
        return None
    current = widths[-1]
    percentile = sum(1 for w in widths if w <= current) / len(widths) * 100
    return percentile  # Low percentile = compressed, breakout likely
```

Use as an ensemble voter or pre-filter: only trade when percentile < 85.

---

## 9. Simplification Audit (P1 — PARTIAL)

**Nunchi's core lesson:** Largest gains came from removing complexity. Every "smart" feature degraded performance.

**Already simplified in PR #6:**
- Edge relaxation for high-confidence signals removed (was `min(effective_min_edge, 0.02) if est_prob > 0.65`)
- Momentum dampened from 0.30 → 0.10
- Time factor changed from `sqrt(window_progress)` → `min(window_progress, 0.85)`
- Window progress capped at 1.0 (was 1.5)

**Remaining candidates to test:**

| Feature | Location | Test |
|---------|----------|------|
| Volatility regime scaling on min_edge | `btc_updown.py:182-184` | Remove `vol_ratio` scaling, use flat `min_edge` |
| Dynamic cushion (est_prob > 0.65 → 0.02) | `btc_updown.py:378-381` | Use fixed cushion always |
| 5 aggression levels | `aggression_tuner.py:17-23` | Collapse to 3 levels |
| Market anchoring blend ratio | `btc_updown.py:497` | Test 80/20 vs 70/30 vs 60/40 |

**Method:** Remove one feature at a time, run paper trading for 24-48 hours, compare prediction accuracy via calibration stats.

---

## Key Philosophical Takeaway

> "The strongest gains came from removing complexity — every 'smart' feature eventually degraded performance when tested independently."

Before adding any new feature, first verify existing features are helping. The prediction logging + calibration stats infrastructure provides the data for this analysis.
