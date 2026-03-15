# Polymarket Wallet Scanner

Scans Polymarket trade history and ranks the **top 100 wallets by estimated profitability** over a configurable time window (default: 30 days).

## Sources

- `graph` (default): GraphQL subgraph endpoint (Goldsky-hosted Polymarket orderbook subgraph)
- `api`: Polymarket Data API `/trades` (fallback)

> The legacy endpoint `https://api.thegraph.com/subgraphs/name/polymarket/polymarket` has been deprecated upstream; scanner auto-resolves to a working GraphQL endpoint by default.

## Metrics per wallet

- `total_pnl_usd` (realized + unrealized estimate)
- `win_rate_pct` (% profitable closed lots, or profitable open lots when no closes)
- `avg_trade_size_usd`
- `trading_frequency_per_day`

## Profitability methodology

PnL is estimated from public trade flow using:
1. FIFO lot matching for BUY/SELL per asset (realized PnL)
2. Remaining open lots marked to latest observed trade price per asset (unrealized)

For graph source:
- Reads `orderFilledEvents`
- Converts each fill to maker/taker wallet trade legs using USDC/token transfer direction

## Usage

```bash
cd /Users/openclaw/.openclaw/workspace/projects/poly-trade/scanner
```

### Default (graph source)

```bash
node scanner.js --source graph --days 30 --limit 500 --max-pages 5000 --output results.json --format json
```

### API fallback

```bash
node scanner.js --source api --days 30 --limit 500 --max-pages 5000 --output results.json --format json
```

### CSV output

```bash
node scanner.js --source graph --output results.csv --format csv
```

## Output

- `results.json` (default) or `results.csv`
- Contains ranked top 100 wallets and required metrics
- Includes metadata: source endpoint, trades analyzed, wallets analyzed, generation timestamp

## Reliability

- Exponential-backoff retries for HTTP/GraphQL failures
- Progress logs per page
- No credentials required
- No sensitive wallet secrets stored
