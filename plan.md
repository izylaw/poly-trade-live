# Plan: Address Copilot Review Comments on PR #6

PR #6 (`feat/live-trading-hardening`) introduces async logging, take-profit selling, and `sell_position()` methods. Copilot flagged 5 issues. Here's the plan:

## 1. Bounded logging queue with drop policy
**File:** `src/utils/logger.py` (line 38: `queue.Queue(-1)`)

**Problem:** Unbounded queue (`maxsize=-1`) risks OOM if log volume spikes during tight trading loops or I/O stalls.

**Fix:** Set `maxsize=10_000` and add a custom `QueueHandler` subclass that drops DEBUG/INFO messages (keeps WARNING+) when the queue is full, rather than blocking the trading thread.

## 2. Fix misleading console handler comment
**File:** `src/utils/logger.py` (line 25: `# Console handler (still sync — stdout is fast)`)

**Problem:** Comment says console is "still sync" but with QueueHandler+QueueListener, console output now runs on the listener thread, not the trading thread.

**Fix:** Update comment to: `# Console handler — runs on QueueListener thread, not trading thread`

## 3. Refresh balance after take-profit sells
**File:** `src/core/engine.py` (lines 114-116)

**Problem:** `_check_take_profit()` is called early in `_cycle()` and internally refreshes balance only when sells happen. But the local `balance` variable at line 106 is set *before* the take-profit call and is never updated afterward, so subsequent logging and trade sizing in the same cycle use a stale value.

**Fix:** After the `_check_take_profit()` call, re-read balance:
```python
if self.settings.take_profit_enabled:
    self._check_take_profit()
    balance = self.executor.get_balance()  # refresh after potential sells
    self.balance_mgr.update(balance)
```

## 4. Use batch orderbook fetch instead of sequential `get_price()`
**File:** `src/core/engine.py` (lines 501-505 in `_check_take_profit`)

**Problem:** The take-profit method calls `self.clob.get_price(tid)` sequentially for each token ID, adding latency and API overhead. The codebase already has `get_orderbooks_batch()`.

**Fix:** Replace the sequential loop with a single batch call:
```python
prices = self.clob.get_orderbooks_batch(token_ids)
```
Adapt the downstream code to use the batch response format (extract bid from orderbook data).

## 5. Capture order ID in `sell_position()`
**File:** `src/execution/live_executor.py` (lines 105-115)

**Problem:** `sell_position()` discards the return value from `clob.post_order()`, losing the order ID. The normal `execute()` method properly captures and logs order IDs for auditability.

**Fix:** Capture the result and extract order ID, store it in the trade record notes:
```python
result = self.clob.post_order(...)
order_id = result.get("orderID", result.get("id", "unknown"))
...
"notes": f"take_profit_sell order_id={order_id}",
```

## Summary of changes

| # | File | Issue | Severity |
|---|------|-------|----------|
| 1 | `src/utils/logger.py` | Bounded queue + drop policy | Medium |
| 2 | `src/utils/logger.py` | Fix misleading comment | Low |
| 3 | `src/core/engine.py` | Refresh balance after take-profit | High |
| 4 | `src/core/engine.py` | Batch price fetching | Medium |
| 5 | `src/execution/live_executor.py` | Capture sell order ID | Medium |

**Branch:** `claude/review-pr6-copilot-QvKky`
**Base:** `feat/live-trading-hardening` (PR #6's head)
