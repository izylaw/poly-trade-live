"""Shared utilities for crypto up/down strategies (btc_updown, safe_compounder)."""

import math


def estimate_outcome_probability(
    outcome: str,
    delta_pct: float,
    window_progress: float,
    momentum: float,
    vol: float,
    logistic_k: float = 1.5,
    momentum_weight: float = 0.3,
) -> float:
    """Logistic model for crypto up/down outcome probability."""
    directional_delta = delta_pct if outcome.lower() == "up" else -delta_pct
    normalized = directional_delta / vol if vol > 0 else 0.0
    time_factor = math.sqrt(max(window_progress, 0.0))

    momentum_direction = 1.0 if outcome.lower() == "up" else -1.0
    z = normalized * time_factor + momentum_direction * momentum * momentum_weight * time_factor

    prob = 1.0 / (1.0 + math.exp(-logistic_k * z))
    return clamp(prob, 0.05, 0.95)


def compute_price_delta(binance, asset: str, market: dict, btc_5m_vol: float, logger=None) -> dict:
    """Compute price delta info for an asset within a market window."""
    import time as _time

    current_price = binance.get_price(asset)
    start_ts = market.get("_start_ts", 0)
    resolution_ts = market.get("_resolution_ts", 0)

    try:
        klines = binance.get_klines(asset, interval="1m", limit=30)
    except Exception as e:
        if logger:
            logger.warning(f"crypto_utils: failed to fetch klines for {asset}: {e}")
        klines = []

    reference_price = current_price
    if klines:
        start_ts_ms = start_ts * 1000
        for k in klines:
            if k["open_time"] <= start_ts_ms <= k["close_time"]:
                reference_price = k["open"]
                break
        else:
            if klines[0]["open_time"] > start_ts_ms:
                reference_price = klines[0]["open"]

    now = _time.time()
    total_window = resolution_ts - start_ts if resolution_ts > start_ts else 300
    elapsed = now - start_ts
    window_progress = clamp(elapsed / total_window, 0.0, 1.5)

    delta_pct = (current_price - reference_price) / reference_price if reference_price > 0 else 0.0

    dynamic_vol = btc_5m_vol
    try:
        atr = binance.compute_atr(asset, "5m", 14)
        if atr is not None and current_price > 0:
            dynamic_vol = atr / current_price
    except Exception as e:
        if logger:
            logger.warning(f"crypto_utils: ATR failed for {asset}, using default vol: {e}")

    return {
        "current_price": current_price,
        "reference_price": reference_price,
        "delta_pct": delta_pct,
        "time_remaining": resolution_ts - now,
        "window_progress": window_progress,
        "dynamic_vol": dynamic_vol,
        "resolution_ts": resolution_ts,
    }


def compute_momentum(binance, asset: str) -> float:
    """Combined trade flow + orderbook imbalance momentum signal."""
    try:
        trade_flow = _calc_trade_flow(binance, asset)
    except Exception:
        trade_flow = 0.0

    try:
        ob_imbalance = _calc_orderbook_imbalance(binance, asset)
    except Exception:
        ob_imbalance = 0.0

    return trade_flow * 0.6 + ob_imbalance * 0.4


def get_smart_bid(est_prob: float, book, cushion: float, min_edge: float, tick_size: float = 0.01) -> float:
    """Place bid intelligently based on book state."""
    fair_bid = round(est_prob - cushion, 2)

    if book is None or not hasattr(book, 'asks') or not book.asks:
        return fair_bid

    best_ask = float(book.asks[0].price)

    if best_ask < fair_bid:
        undercut_bid = round(best_ask - tick_size, 2)
        if est_prob - undercut_bid >= min_edge:
            return undercut_bid

    return fair_bid


def _calc_trade_flow(binance, asset: str) -> float:
    trades = binance.get_recent_trades(asset, limit=500)
    if not trades:
        return 0.0

    buy_volume = 0.0
    sell_volume = 0.0
    for t in trades:
        qty = float(t["qty"])
        if t["isBuyerMaker"]:
            sell_volume += qty
        else:
            buy_volume += qty

    total = buy_volume + sell_volume
    if total == 0:
        return 0.0

    ratio = buy_volume / total
    return clamp((ratio - 0.5) * 2, -1.0, 1.0)


def _calc_orderbook_imbalance(binance, asset: str) -> float:
    book = binance.get_orderbook(asset, limit=20)
    bids = book.get("bids", [])
    asks = book.get("asks", [])

    if not bids or not asks:
        return 0.0

    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    mid = (best_bid + best_ask) / 2
    threshold = mid * 0.001

    bid_vol = sum(float(b[1]) for b in bids if mid - float(b[0]) <= threshold)
    ask_vol = sum(float(a[1]) for a in asks if float(a[0]) - mid <= threshold)

    total = bid_vol + ask_vol
    if total == 0:
        return 0.0

    imbalance = (bid_vol - ask_vol) / total
    return clamp(imbalance * 2, -1.0, 1.0)


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
