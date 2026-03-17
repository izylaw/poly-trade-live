# Performance Optimization: Reduce Cycle Time from ~7min to ~30sec

**Date:** 2026-03-07
**Status:** Implemented and validated

## Problem

The bot took ~7 minutes per trading cycle. Three major time sinks:
1. **~2min gap** after high_probability finishes — sports_daily discovery + sequential orderbook fetches
2. **~37sec** sports_daily evaluating 83 markets one-by-one (4 HTTP calls per market = 332 sequential calls)
3. **~4min gap** before trade execution — `_resolve_predictions()` and `_resolve_positions()` making sequential Gamma API calls for every unresolved prediction/position

## Changes

### Files Modified (4)

1. **`src/market_data/clob_client.py`** — 2 new methods
   - `get_books_batch(token_ids, chunk_size=50)`: Batch-fetches full `OrderBookSummary` objects via `client.get_order_books()`. Returns `dict[token_id -> OrderBookSummary]`. Used by sports_daily to replace 332 sequential calls with ~4 chunked requests.
   - `extract_price(book)`: Static helper extracting `{bid, ask, mid}` from any `OrderBookSummary` object. Reusable by all strategies.

2. **`src/market_data/gamma_client.py`** — 1 new method
   - `get_all_events_by_tag(tag, max_pages=30)`: Parallel pagination of `/events?tag=` using `ThreadPoolExecutor(max_workers=10)`, same pattern as existing `get_all_active_events()`. Cached with 45s TTL. Replaces sequential 30-page pagination in sports_daily discovery.

3. **`src/strategies/sports_daily.py`** — 2 changes
   - `analyze()`: Collects all token IDs from all discovered markets upfront, batch-fetches via `get_books_batch()`, passes cache to `_analyze_market()`.
   - `_analyze_market()`: Looks up books from cache first, falls back to individual fetch. Uses `extract_price()` instead of separate `get_price()` calls.
   - `_discover_sports_markets()`: Calls `get_all_events_by_tag()` instead of manual sequential pagination.

4. **`src/core/engine.py`** — 2 changes
   - Strategy execution parallelized via `ThreadPoolExecutor`. All enabled strategies run concurrently (total time = MAX instead of SUM). Prediction logging moved after executor block. Thread-safe because strategies only read shared state and each has its own `GammaClient`.
   - Resolution checks (`_resolve_predictions()` + `_resolve_positions()`) gated to every 5th cycle instead of every cycle: `if self._cycle_count % 5 == 0`.

### Files Modified (tests)

5. **`tests/test_sports_daily.py`** — Updated mocks
   - `make_mock_clob()`: Added `get_books_batch` mock returning the same book for every requested token.
   - All discovery test mocks updated from `get_events_by_tag` to `get_all_events_by_tag`.

## Expected Impact

| Change | Before | After |
|--------|--------|-------|
| Batch sports orderbooks | ~2-5 min (332 sequential HTTP calls) | ~5-8 sec (~4 chunked requests) |
| Reduce resolve frequency | ~4 min every cycle | ~4 min every 5th cycle |
| Parallel Gamma pagination | ~30-50 sec (sequential pages) | ~3-5 sec (10 workers) |
| Parallel strategies | SUM of all strategy times | MAX of all strategy times |
| **Total cycle** | **~7 min** | **~15-30 sec** |

## Design Decisions

1. **`get_books_batch()` returns full OrderBookSummary, not just prices** — sports_daily needs the full book for imbalance computation (`_compute_book_imbalance()`), not just bid/ask/mid. A separate `extract_price()` helper handles price extraction.

2. **Resolution every 5th cycle, not disabled** — Predictions still need resolving for calibration tracking and position PnL. Every 5th cycle is a good balance: at 30s intervals that's ~2.5 min between resolution checks, and the HTTP cost only matters when there are unresolved items.

3. **ThreadPoolExecutor for strategies, not asyncio** — All existing code is synchronous (requests library, py-clob-client). Thread pool is the simplest concurrency model that works without rewriting the IO layer. Each strategy has its own `GammaClient` instance, so no shared mutable state.

4. **Cache fallback in `_analyze_market()`** — If a token ID is missing from the batch cache (e.g., API hiccup for that chunk), falls back to individual `get_book()` call. Ensures no signal is silently dropped.

## Validation

- 151 tests pass, zero regressions
