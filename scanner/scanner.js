#!/usr/bin/env node
/**
 * Polymarket wallet profitability scanner.
 *
 * Sources:
 * - graph (default): Goldsky-hosted Polymarket orderbook subgraph
 * - api: Polymarket Data API /trades
 */

import fs from 'node:fs';
import path from 'node:path';

const API_BASE = process.env.POLYMARKET_DATA_API || 'https://data-api.polymarket.com';
const GRAPH_ENDPOINT = process.env.POLYMARKET_GRAPH_ENDPOINT || 'https://api.goldsky.com/api/public/project_cl6mb8i9h0003e201j6li0diw/subgraphs/orderbook-subgraph/0.0.1/gn';
const LEGACY_GRAPH_ENDPOINT = 'https://api.thegraph.com/subgraphs/name/polymarket/polymarket';
const OUT_DIR = process.cwd();
const USDC_ASSET_ID = '0';
const DECIMALS = 1_000_000;

function parseArgs(argv) {
  const args = {
    source: 'graph',
    days: 30,
    limit: 500,
    output: 'results.json',
    format: 'json',
    maxPages: 5000,
    retries: 4,
    retryMs: 1200,
  };

  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = argv[i + 1];
    if (a === '--source') args.source = String(next || 'graph').toLowerCase(), i++;
    else if (a === '--days') args.days = Number(next), i++;
    else if (a === '--limit') args.limit = Number(next), i++;
    else if (a === '--output') args.output = next, i++;
    else if (a === '--format') args.format = next, i++;
    else if (a === '--max-pages') args.maxPages = Number(next), i++;
  }
  return args;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function fetchJsonWithRetry(url, retries = 4, retryMs = 1000) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(url, { headers: { accept: 'application/json' } });
      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status} ${text.slice(0, 300)}`);
      }
      return await res.json();
    } catch (err) {
      lastErr = err;
      if (attempt < retries) {
        const wait = retryMs * Math.pow(2, attempt);
        console.warn(`[retry ${attempt + 1}/${retries}] ${url} -> ${err.message}; waiting ${wait}ms`);
        await sleep(wait);
      }
    }
  }
  throw lastErr;
}

async function fetchGraphqlWithRetry(endpoint, query, variables, retries = 4, retryMs = 1000) {
  let lastErr;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(endpoint, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ query, variables }),
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status} ${text.slice(0, 300)}`);
      }

      const data = await res.json();
      if (data.errors?.length) {
        throw new Error(`GraphQL error: ${data.errors.map((e) => e.message).join(' | ')}`);
      }
      return data.data;
    } catch (err) {
      lastErr = err;
      if (attempt < retries) {
        const wait = retryMs * Math.pow(2, attempt);
        console.warn(`[retry ${attempt + 1}/${retries}] gql ${endpoint} -> ${err.message}; waiting ${wait}ms`);
        await sleep(wait);
      }
    }
  }
  throw lastErr;
}

function toNum(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

function estimateUsd(trade) {
  if (trade.usdcSize != null) return toNum(trade.usdcSize);
  return toNum(trade.size) * toNum(trade.price);
}

async function fetchTradesFromApi({ cutoffTs, pageLimit, maxPages, retries, retryMs }) {
  const all = [];
  let offset = 0;

  for (let page = 1; page <= maxPages; page++) {
    if (offset > 3000) {
      console.warn('Reached Polymarket API historical offset cap (3000). Stopping pagination.');
      break;
    }

    const url = `${API_BASE}/trades?limit=${pageLimit}&offset=${offset}`;

    let rows;
    try {
      rows = await fetchJsonWithRetry(url, retries, retryMs);
    } catch (err) {
      if (String(err?.message || '').includes('max historical activity offset of 3000 exceeded')) {
        console.warn('Hit API offset cap (3000). Stopping pagination.');
        break;
      }
      throw err;
    }

    if (!Array.isArray(rows) || rows.length === 0) break;

    let keep = 0;
    let olderFound = false;
    for (const t of rows) {
      const ts = toNum(t.timestamp);
      if (ts >= cutoffTs) {
        all.push(t);
        keep++;
      } else {
        olderFound = true;
      }
    }

    const minTs = Math.min(...rows.map((r) => toNum(r.timestamp, Number.MAX_SAFE_INTEGER)));
    const maxTs = Math.max(...rows.map((r) => toNum(r.timestamp, 0)));
    console.log(`api page=${page} offset=${offset} fetched=${rows.length} kept=${keep} ts=[${minTs},${maxTs}] total_kept=${all.length}`);

    if (olderFound || minTs < cutoffTs) break;
    offset += rows.length;
  }

  return { trades: all, actualSource: 'api', graphEndpoint: null };
}

function eventToWalletTrades(e) {
  const out = [];
  const maker = String(e.maker || '').toLowerCase();
  const taker = String(e.taker || '').toLowerCase();

  const makerAsset = String(e.makerAssetId);
  const takerAsset = String(e.takerAssetId);

  const makerAmt = toNum(e.makerAmountFilled) / DECIMALS;
  const takerAmt = toNum(e.takerAmountFilled) / DECIMALS;
  const ts = toNum(e.timestamp);
  const id = String(e.id || '');

  // Maker perspective
  if (makerAsset === USDC_ASSET_ID && takerAsset !== USDC_ASSET_ID && takerAmt > 0) {
    const usdc = makerAmt;
    const qty = takerAmt;
    out.push({ proxyWallet: maker, side: 'BUY', asset: takerAsset, size: qty, usdcSize: usdc, price: usdc / qty, timestamp: ts, transactionHash: id });
  } else if (takerAsset === USDC_ASSET_ID && makerAsset !== USDC_ASSET_ID && makerAmt > 0) {
    const usdc = takerAmt;
    const qty = makerAmt;
    out.push({ proxyWallet: maker, side: 'SELL', asset: makerAsset, size: qty, usdcSize: usdc, price: usdc / qty, timestamp: ts, transactionHash: id });
  }

  // Taker perspective
  if (takerAsset === USDC_ASSET_ID && makerAsset !== USDC_ASSET_ID && makerAmt > 0) {
    const usdc = takerAmt;
    const qty = makerAmt;
    out.push({ proxyWallet: taker, side: 'BUY', asset: makerAsset, size: qty, usdcSize: usdc, price: usdc / qty, timestamp: ts, transactionHash: id });
  } else if (makerAsset === USDC_ASSET_ID && takerAsset !== USDC_ASSET_ID && takerAmt > 0) {
    const usdc = makerAmt;
    const qty = takerAmt;
    out.push({ proxyWallet: taker, side: 'SELL', asset: takerAsset, size: qty, usdcSize: usdc, price: usdc / qty, timestamp: ts, transactionHash: id });
  }

  return out;
}

async function resolveGraphEndpoint(retries, retryMs) {
  // Try user-requested legacy endpoint first for compatibility, then working Goldsky endpoint.
  const candidates = [LEGACY_GRAPH_ENDPOINT, GRAPH_ENDPOINT];
  const probeQuery = 'query{ _meta { deployment } }';

  for (const endpoint of candidates) {
    try {
      await fetchGraphqlWithRetry(endpoint, probeQuery, {}, 1, retryMs);
      return endpoint;
    } catch (_e) {
      // continue
    }
  }

  // fallback to configured graph endpoint even if probe failed (caller will surface detailed error)
  return GRAPH_ENDPOINT;
}

async function fetchTradesFromGraph({ cutoffTs, pageLimit, maxPages, retries, retryMs }) {
  const endpoint = await resolveGraphEndpoint(retries, retryMs);
  const query = `
    query Q($first: Int!, $cutoff: String!, $lastId: String) {
      orderFilledEvents(
        first: $first,
        orderBy: id,
        orderDirection: asc,
        where: { timestamp_gte: $cutoff, id_gt: $lastId }
      ) {
        id
        timestamp
        maker
        taker
        makerAssetId
        takerAssetId
        makerAmountFilled
        takerAmountFilled
      }
    }
  `;

  let lastId = '';
  const allTrades = [];

  for (let page = 1; page <= maxPages; page++) {
    const data = await fetchGraphqlWithRetry(
      endpoint,
      query,
      { first: pageLimit, cutoff: String(cutoffTs), lastId },
      retries,
      retryMs
    );

    const rows = data?.orderFilledEvents || [];
    if (!rows.length) {
      console.log(`graph page=${page} no more rows`);
      break;
    }

    let derived = 0;
    for (const e of rows) {
      const trades = eventToWalletTrades(e);
      for (const t of trades) {
        if (toNum(t.timestamp) >= cutoffTs) {
          allTrades.push(t);
          derived++;
        }
      }
    }

    lastId = rows[rows.length - 1].id;
    const minTs = Math.min(...rows.map((r) => toNum(r.timestamp, Number.MAX_SAFE_INTEGER)));
    const maxTs = Math.max(...rows.map((r) => toNum(r.timestamp, 0)));
    console.log(`graph page=${page} events=${rows.length} derived_trades=${derived} ts=[${minTs},${maxTs}] total_trades=${allTrades.length}`);

    if (rows.length < pageLimit) break;
  }

  return { trades: allTrades, actualSource: 'graph', graphEndpoint: endpoint };
}

function analyzeWalletTrades(trades, cutoffTs) {
  const byWallet = new Map();
  const latestPriceByAsset = new Map();

  for (const t of trades) {
    latestPriceByAsset.set(t.asset, toNum(t.price));

    const wallet = (t.proxyWallet || '').toLowerCase();
    if (!wallet.startsWith('0x')) continue;
    if (!byWallet.has(wallet)) {
      byWallet.set(wallet, { wallet, trades: [], firstTs: Number.MAX_SAFE_INTEGER, lastTs: 0 });
    }
    const g = byWallet.get(wallet);
    g.trades.push(t);
    g.firstTs = Math.min(g.firstTs, toNum(t.timestamp));
    g.lastTs = Math.max(g.lastTs, toNum(t.timestamp));
  }

  const rows = [];

  for (const [, g] of byWallet) {
    g.trades.sort((a, b) => toNum(a.timestamp) - toNum(b.timestamp));
    const lotsByAsset = new Map();

    let realizedPnl = 0;
    let winningClosed = 0;
    let closedCount = 0;
    let totalTradeUsd = 0;

    for (const t of g.trades) {
      const asset = t.asset;
      const side = String(t.side || '').toUpperCase();
      const qty = Math.abs(toNum(t.size));
      const px = toNum(t.price);
      const usd = estimateUsd(t);
      totalTradeUsd += Math.abs(usd);

      if (!lotsByAsset.has(asset)) lotsByAsset.set(asset, []);
      const lots = lotsByAsset.get(asset);

      if (side === 'BUY') {
        lots.push({ qty, costPerUnit: px });
      } else if (side === 'SELL') {
        let remaining = qty;
        while (remaining > 1e-12 && lots.length > 0) {
          const lot = lots[0];
          const take = Math.min(remaining, lot.qty);
          const pnl = (px - lot.costPerUnit) * take;
          realizedPnl += pnl;
          closedCount += 1;
          if (pnl > 0) winningClosed += 1;

          lot.qty -= take;
          remaining -= take;
          if (lot.qty <= 1e-12) lots.shift();
        }
      }
    }

    let unrealizedPnl = 0;
    let openWinningLots = 0;
    let openLotsCount = 0;

    for (const [asset, lots] of lotsByAsset) {
      const mark = latestPriceByAsset.get(asset);
      if (!Number.isFinite(mark)) continue;
      for (const lot of lots) {
        const pnl = (mark - lot.costPerUnit) * lot.qty;
        unrealizedPnl += pnl;
        openLotsCount += 1;
        if (pnl > 0) openWinningLots += 1;
      }
    }

    const totalPnl = realizedPnl + unrealizedPnl;
    const tradeCount = g.trades.length;
    const days = Math.max(1 / 24, (g.lastTs - Math.max(cutoffTs, g.firstTs)) / 86400);

    rows.push({
      wallet: g.wallet,
      total_pnl_usd: Number(totalPnl.toFixed(4)),
      realized_pnl_usd: Number(realizedPnl.toFixed(4)),
      unrealized_pnl_usd: Number(unrealizedPnl.toFixed(4)),
      win_rate_pct: Number(((closedCount > 0 ? (winningClosed / closedCount) : (openLotsCount > 0 ? openWinningLots / openLotsCount : 0)) * 100).toFixed(2)),
      avg_trade_size_usd: Number((tradeCount ? totalTradeUsd / tradeCount : 0).toFixed(4)),
      trading_frequency_per_day: Number((tradeCount / days).toFixed(4)),
      trades_count: tradeCount,
      first_trade_ts: g.firstTs,
      last_trade_ts: g.lastTs,
    });
  }

  rows.sort((a, b) => b.total_pnl_usd - a.total_pnl_usd);
  return rows;
}

function writeCsv(filePath, rows) {
  const headers = ['rank', 'wallet', 'total_pnl_usd', 'realized_pnl_usd', 'unrealized_pnl_usd', 'win_rate_pct', 'avg_trade_size_usd', 'trading_frequency_per_day', 'trades_count', 'first_trade_ts', 'last_trade_ts'];
  const lines = [headers.join(',')];
  rows.forEach((r, i) => lines.push([i + 1, r.wallet, r.total_pnl_usd, r.realized_pnl_usd, r.unrealized_pnl_usd, r.win_rate_pct, r.avg_trade_size_usd, r.trading_frequency_per_day, r.trades_count, r.first_trade_ts, r.last_trade_ts].join(',')));
  fs.writeFileSync(filePath, `${lines.join('\n')}\n`, 'utf8');
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const now = Math.floor(Date.now() / 1000);
  const cutoffTs = now - args.days * 24 * 3600;

  console.log(`Starting scan: source=${args.source} days=${args.days} cutoffTs=${cutoffTs} limit=${args.limit} maxPages=${args.maxPages}`);

  const fetcher = args.source === 'api' ? fetchTradesFromApi : fetchTradesFromGraph;
  const { trades, actualSource, graphEndpoint } = await fetcher({
    cutoffTs,
    pageLimit: args.limit,
    maxPages: args.maxPages,
    retries: args.retries,
    retryMs: args.retryMs,
  });

  console.log(`Collected trades in window: ${trades.length}`);

  const ranked = analyzeWalletTrades(trades, cutoffTs);
  const top100 = ranked.slice(0, 100).map((r, i) => ({ rank: i + 1, ...r }));

  const outputPath = path.resolve(OUT_DIR, args.output);
  const payload = {
    generated_at: new Date().toISOString(),
    source: actualSource,
    source_endpoint: actualSource === 'graph' ? graphEndpoint : API_BASE,
    window_days: args.days,
    cutoff_ts: cutoffTs,
    wallets_analyzed: ranked.length,
    trades_analyzed: trades.length,
    methodology: 'FIFO realized PnL + unrealized mark-to-last-trade by asset from trade flow',
    notes: actualSource === 'graph' ? 'Graph source uses orderFilledEvents and derives per-wallet BUY/SELL legs from USDC/token transfers.' : 'API source may be constrained by historical offset caps.',
    top_100: top100,
  };

  if (args.format === 'csv' || outputPath.endsWith('.csv')) {
    writeCsv(outputPath, top100);
  } else {
    fs.writeFileSync(outputPath, JSON.stringify(payload, null, 2));
  }

  console.log(`Done. Wallets analyzed=${ranked.length}, wrote top100 to ${outputPath}`);
  if (top100[0]) console.log(`Top #1 wallet=${top100[0].wallet} pnl=${top100[0].total_pnl_usd}`);
}

main().catch((err) => {
  console.error('Scanner failed:', err);
  process.exitCode = 1;
});
