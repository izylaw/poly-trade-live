# Comprehensive Market Discovery

**Date:** 2026-03-07
**Status:** Implemented and validated

## Problem

The bot discovered markets via `GammaClient.get_all_active_markets()` which fetched at most 500 markets (5 pages of `/markets`). Polymarket has 27,000+ active markets across 3,000+ events. This meant `high_probability` and `arbitrage` strategies were blind to 95%+ of opportunities. Meanwhile, `sports_daily` capped its own discovery at 100 events per tag (1 page).

## Solution

Replaced flat `/markets` pagination with event-based full pagination via `/events`, added CLOB tradeability cross-reference (opt-in), and paginated sports tag discovery.

## Changes

### Files Modified (7)

1. **`src/market_data/gamma_client.py`** — 2 new methods
   - `get_all_active_events(max_pages=30)`: Paginates `/events` endpoint fully with 120s TTL cache
   - `extract_markets_from_events(events)`: Static method flattening events into market dicts with `_event_title`, `_event_slug`, `_event_tags` metadata
   - `_get_cached()` updated to accept optional `ttl` parameter

2. **`src/market_data/clob_client.py`** — 1 new method
   - `get_all_tradeable_condition_ids()`: Cursor-paginates CLOB `/simplified-markets` (public, no auth), returns `set[str]` of condition IDs for open, accepting-orders markets

3. **`src/market_data/market_scanner.py`** — Rewritten
   - `scan()` pipeline: events -> flatten -> normalize -> filter -> CLOB cross-ref
   - `_apply_tradeability_filter()`: Filters against CLOB tradeable set with graceful fallback
   - `_get_clob_ids()`: Cached CLOB set with configurable TTL

4. **`src/config/defaults.py`** — 3 new settings + 1 changed default
   - `scanner_max_event_pages: 30`
   - `scanner_clob_cross_ref: False` (opt-in, CLOB fetch takes ~186s)
   - `scanner_clob_ttl: 1800`
   - `max_markets`: 50 -> 500

5. **`src/config/settings.py`** — 3 new fields
   - `scanner_max_event_pages`, `scanner_clob_cross_ref`, `scanner_clob_ttl`

6. **`src/bot.py`** — 1 line
   - Passes `clob_client=clob` to `MarketScanner`

7. **`src/strategies/sports_daily.py`** — Paginated discovery
   - Tag search and fallback event scan loop up to 30 pages instead of 1

### Files Added (1)

- **`tests/test_market_scanner.py`** — 18 unit tests covering event extraction, scanner pipeline, CLOB filtering (cache, graceful fallback, disabled mode), and Gamma pagination

### Unchanged

- `btc_updown` — zero changes, keeps fast slug-based discovery
- `safe_compounder` — zero changes, same slug-based pattern
- Strategy base class — `self_discovering` flag and `analyze()` signature unchanged
- Engine flow — same `needs_scanner` gate
- Old `get_all_active_markets()` — preserved in GammaClient, not deleted

## Validation Results

### Static Discovery Comparison (old vs new)

| Metric | OLD | NEW | Gain |
|---|---|---|---|
| Events fetched | — | 3,000 | — |
| Raw markets | 500 | 27,796 | +5,459% |
| After filters (max_markets) | 50 | 500 | +900% |
| High probability candidates | 10 | 79 | +690% |
| Binary / arb-eligible | 50 | 500 | +900% |
| Sports markets | 40 | 271 | +578% |
| Scan time | 0.6s | 9.9s | acceptable |

Uncapped (max_markets=99999): 3,989 markets pass volume/liquidity/spread filters, 914 high-probability candidates, 1,132 sports markets.

### Paper Trading Session (10 min, 2 cycles)

| Metric | Result |
|---|---|
| Events fetched | 3,000 |
| Raw markets extracted | 27,796 |
| After filters | 500 |
| Sports markets discovered | 102 (fully paginated) |
| **Total signals generated** | **392** |
| SPREAD_CAPTURE | 372 |
| BOOK_IMBALANCE | 18 |
| high_probability approved | 2 |
| Trades executed | 1 (No@$0.971, conf=0.99) |
| Balance | $9.89 -> $9.12 (position open) |

### Test Suite

- 147 tests pass (129 existing + 18 new), zero regressions

## Design Decisions

1. **CLOB cross-ref defaults to OFF** — The `/simplified-markets` endpoint returns 541k condition IDs across ~542 pages, taking ~186s. Too expensive for routine scans. Gamma's active/closed flags are sufficient for most use cases. Set `SCANNER_CLOB_CROSS_REF=true` for exhaustive filtering.

2. **max_markets raised to 500, not unlimited** — With 3,989 markets passing filters, unlimited would cause each cycle to fetch 8,000+ orderbooks (2 per binary market). At ~0.15s per request that's ~20 min/cycle. 500 is a practical balance.

3. **Event-based > market-based** — `/events` returns nested markets, so one event fetch gives all its markets. More efficient than flat `/markets` for full coverage, and provides event metadata (`_event_title`, `_event_tags`) that strategies can use.

4. **120s TTL for event cache** — Events don't change rapidly. Separate from the default 60s TTL used for individual market/event lookups.

## Known Limitations

- ~~Cycle time is ~5 min with 500 markets (each needs CLOB orderbook fetch). Strategies that don't need orderbooks for every market could be optimized.~~ **Resolved** — see `2026-03-07_performance_optimization.md`. Batch orderbook fetches, parallel strategy execution, parallel Gamma pagination, and reduced resolve frequency bring cycle time down to ~15-30s.
- Binance API geo-blocked (451 errors) prevents `btc_updown` and `safe_compounder` from functioning. Unrelated to this change.
- `sports_daily` keyword matching may flag non-sports events (e.g., Colombian Senate elections matched via "copa" patterns).
