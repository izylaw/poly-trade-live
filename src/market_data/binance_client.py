import threading
import time
import logging
import requests
from src.utils.retry import retry

logger = logging.getLogger("poly-trade")

BINANCE_API_URL = "https://data-api.binance.vision"

ASSET_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}


class BinanceClient:
    def __init__(self, cache_ttl: float = 10.0):
        self._cache: dict[str, tuple[float, any]] = {}
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()

    def _get_cached(self, key: str):
        with self._lock:
            if key in self._cache:
                ts, data = self._cache[key]
                if time.time() - ts < self._cache_ttl:
                    return data
        return None

    def _set_cache(self, key: str, data):
        with self._lock:
            self._cache[key] = (time.time(), data)

    @staticmethod
    def _symbol(asset: str) -> str:
        return ASSET_SYMBOLS.get(asset.upper(), f"{asset.upper()}USDT")

    @retry(max_attempts=3)
    def get_klines(self, asset: str, interval: str = "1m", limit: int = 30) -> list[dict]:
        cache_key = f"klines_{asset}_{interval}_{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.debug(f"Binance {asset} klines: cache hit")
            return cached

        symbol = self._symbol(asset)
        resp = requests.get(
            f"{BINANCE_API_URL}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        candles = []
        for k in resp.json():
            candles.append({
                "open_time": k[0],
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "close_time": k[6],
            })
        self._set_cache(cache_key, candles)
        last_close = candles[-1]["close"] if candles else 0
        total_vol = sum(c["volume"] for c in candles)
        logger.info(f"Binance {asset} klines: {len(candles)} candles | price=${last_close:,.2f} | vol={total_vol:.1f}")
        return candles

    @retry(max_attempts=3)
    def get_recent_trades(self, asset: str, limit: int = 500) -> list[dict]:
        cache_key = f"trades_{asset}_{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.debug(f"Binance {asset} trades: cache hit")
            return cached

        symbol = self._symbol(asset)
        resp = requests.get(
            f"{BINANCE_API_URL}/api/v3/trades",
            params={"symbol": symbol, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        trades = resp.json()
        self._set_cache(cache_key, trades)
        logger.info(f"Binance {asset} trades: {len(trades)} fetched")
        return trades

    @retry(max_attempts=3)
    def get_orderbook(self, asset: str, limit: int = 20) -> dict:
        cache_key = f"book_{asset}_{limit}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.debug(f"Binance {asset} book: cache hit")
            return cached

        symbol = self._symbol(asset)
        resp = requests.get(
            f"{BINANCE_API_URL}/api/v3/depth",
            params={"symbol": symbol, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        book = resp.json()
        self._set_cache(cache_key, book)
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if bids and asks:
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            spread = best_ask - best_bid
            logger.info(f"Binance {asset} book: bid=${best_bid:,.0f} ask=${best_ask:,.0f} spread=${spread:.0f}")
        return book

    def compute_atr(self, asset: str, interval: str = "5m", period: int = 14) -> float | None:
        klines = self.get_klines(asset, interval=interval, limit=period + 1)
        if len(klines) < 2:
            return None
        true_ranges = []
        for i in range(1, len(klines)):
            prev_close = klines[i - 1]["close"]
            h, l = klines[i]["high"], klines[i]["low"]
            tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
            true_ranges.append(tr)
        return sum(true_ranges) / len(true_ranges)

    @retry(max_attempts=3)
    def get_price(self, asset: str) -> float:
        cache_key = f"price_{asset}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            logger.debug(f"Binance {asset} price: cache hit")
            return cached

        symbol = self._symbol(asset)
        resp = requests.get(
            f"{BINANCE_API_URL}/api/v3/ticker/price",
            params={"symbol": symbol},
            timeout=10,
        )
        resp.raise_for_status()
        price = float(resp.json()["price"])
        self._set_cache(cache_key, price)
        return price
