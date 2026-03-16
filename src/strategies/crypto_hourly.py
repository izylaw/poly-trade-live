import logging
from src.strategies.btc_updown import BtcUpdownStrategy
from src.risk.risk_manager import TradeSignal
from src.config.settings import Settings
from src.market_data.binance_client import BinanceClient

logger = logging.getLogger("poly-trade")


class CryptoHourlyStrategy(BtcUpdownStrategy):
    name = "crypto_hourly"

    def __init__(self, settings: Settings, binance: BinanceClient | None = None):
        super().__init__(settings, binance=binance)
        self.assets = settings.crypto_hourly_assets
        self.intervals = settings.crypto_hourly_intervals
        self.min_edge = settings.crypto_hourly_min_edge
        self.min_ask = settings.crypto_hourly_min_ask
        self.max_ask = settings.crypto_hourly_max_ask
        self.maker_edge_cushion = settings.crypto_hourly_maker_edge_cushion
