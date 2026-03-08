"""Shared crypto up/down market discovery logic."""

import json
import time
import logging

logger = logging.getLogger("poly-trade")

# Trade during the observation window when real price delta data exists
INTERVAL_WINDOWS = {
    "5m": (30, 330),      # 30s after window starts to 30s before resolution
    "15m": (60, 870),     # 60s after start, 30s before resolution
    "1h": (120, 3570),    # 2min after start, 30s before resolution
    "4h": (300, 14370),   # 5min after start, 30s before resolution
}


def parse_json_field(value):
    """Parse a JSON-encoded string field, returning list."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    return value if isinstance(value, list) else []


def discover_crypto_markets(
    gamma,
    asset: str,
    interval: str,
    strategy_name: str = "crypto",
) -> list[dict]:
    """Discover active crypto up/down markets within the tradeable time window.

    Uses INTERVAL_WINDOWS to filter markets that are in the right phase
    of their observation window.
    """
    from src.market_data.gamma_client import GammaClient
    interval_secs = GammaClient.INTERVAL_SECONDS.get(interval, 300)

    raw_markets = gamma.get_crypto_updown_markets(asset, interval)
    if not raw_markets:
        logger.info(f"{strategy_name}: {asset} {interval}: no events found on Gamma")
        return []

    now = time.time()
    window = INTERVAL_WINDOWS.get(interval, (30, 330))
    min_time, max_time = window

    filtered = []
    for m in raw_markets:
        start_ts = m.get("_start_ts")
        if start_ts is None:
            continue
        resolution_ts = start_ts + interval_secs

        elapsed = now - start_ts
        time_to_resolution = resolution_ts - now
        if min_time <= elapsed and time_to_resolution >= (interval_secs - max_time):
            m["clobTokenIds"] = parse_json_field(m.get("clobTokenIds", []))
            m["outcomes"] = parse_json_field(m.get("outcomes", []))
            if len(m["clobTokenIds"]) >= 2 and len(m["outcomes"]) >= 2:
                m["_resolution_ts"] = resolution_ts
                filtered.append(m)

    if filtered:
        logger.info(
            f"{strategy_name}: {asset} {interval} discovery: "
            f"{len(filtered)} markets in window (of {len(raw_markets)} raw)"
        )
    else:
        nearest = None
        for m in raw_markets:
            start_ts = m.get("_start_ts")
            if start_ts is None:
                continue
            res_ts = start_ts + interval_secs
            if res_ts > now:
                diff = res_ts - now
                if nearest is None or diff < nearest:
                    nearest = diff
        if nearest is not None:
            logger.info(
                f"{strategy_name}: {asset} {interval}: no markets in time window "
                f"(next resolves in {nearest / 60:.0f}m, window is {min_time}s-{max_time}s)"
            )
        else:
            logger.info(
                f"{strategy_name}: {asset} {interval}: no markets in time window "
                f"(of {len(raw_markets)} raw)"
            )
    return filtered
