import os
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field
from src.config.defaults import DEFAULTS


class Settings(BaseSettings):
    # Credentials
    poly_private_key: str = ""
    poly_api_key: str = ""
    poly_api_secret: str = ""
    poly_api_passphrase: str = ""
    polygon_rpc_url: str = DEFAULTS["polygon_rpc_url"]

    # Goal
    starting_capital: float = DEFAULTS["starting_capital"]
    target_balance: float = DEFAULTS["target_balance"]
    target_days: int = DEFAULTS["target_days"]
    paper_trading: bool = DEFAULTS["paper_trading"]
    log_level: str = DEFAULTS["log_level"]

    # Risk
    hard_floor_pct: float = DEFAULTS["hard_floor_pct"]
    max_single_trade_pct: float = DEFAULTS["max_single_trade_pct"]
    max_portfolio_exposure_pct: float = DEFAULTS["max_portfolio_exposure_pct"]
    max_open_positions: int = DEFAULTS["max_open_positions"]
    daily_loss_limit_pct: float = DEFAULTS["daily_loss_limit_pct"]
    min_trade_size: float = DEFAULTS["min_trade_size"]
    consecutive_loss_pause: int = DEFAULTS["consecutive_loss_pause"]

    # Market filter
    min_volume_24h: float = DEFAULTS["min_volume_24h"]
    min_liquidity: float = DEFAULTS["min_liquidity"]
    max_spread: float = DEFAULTS["max_spread"]
    min_time_to_resolution_hours: int = DEFAULTS["min_time_to_resolution_hours"]
    max_markets: int = DEFAULTS["max_markets"]

    # Engine
    scan_interval_seconds: int = DEFAULTS["scan_interval_seconds"]
    adapt_interval_seconds: int = DEFAULTS["adapt_interval_seconds"]

    # Strategy
    high_prob_min_price: float = DEFAULTS["high_prob_min_price"]
    high_prob_max_price: float = DEFAULTS["high_prob_max_price"]
    arb_min_spread: float = DEFAULTS["arb_min_spread"]

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    @property
    def hard_floor(self) -> float:
        return self.starting_capital * self.hard_floor_pct

    @property
    def db_path(self) -> Path:
        return Path("data/poly_trade.db")

    @property
    def log_dir(self) -> Path:
        return Path("logs")

    def has_credentials(self) -> bool:
        return bool(self.poly_private_key and self.poly_api_key)


def get_settings() -> Settings:
    return Settings()
