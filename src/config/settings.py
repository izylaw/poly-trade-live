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
    min_time_to_resolution_hours: float = DEFAULTS["min_time_to_resolution_hours"]
    max_markets: int = DEFAULTS["max_markets"]

    # Scanner
    scanner_max_event_pages: int = DEFAULTS["scanner_max_event_pages"]
    scanner_clob_cross_ref: bool = DEFAULTS["scanner_clob_cross_ref"]
    scanner_clob_ttl: int = DEFAULTS["scanner_clob_ttl"]

    # Engine
    scan_interval_seconds: int = DEFAULTS["scan_interval_seconds"]
    adapt_interval_seconds: int = DEFAULTS["adapt_interval_seconds"]

    # Strategy
    high_prob_min_price: float = DEFAULTS["high_prob_min_price"]
    high_prob_max_price: float = DEFAULTS["high_prob_max_price"]
    high_prob_longshot_threshold: float = DEFAULTS["high_prob_longshot_threshold"]
    high_prob_longshot_min_price: float = DEFAULTS["high_prob_longshot_min_price"]
    high_prob_longshot_conf_multiplier: float = DEFAULTS["high_prob_longshot_conf_multiplier"]
    high_prob_maker_ttl_hours: int = DEFAULTS["high_prob_maker_ttl_hours"]
    arb_min_spread: float = DEFAULTS["arb_min_spread"]

    # Strategy selection
    only_strategies: list[str] = DEFAULTS["only_strategies"]

    # BTC Up/Down strategy (v2 — price delta model)
    btc_updown_assets: list[str] = DEFAULTS["btc_updown_assets"]
    btc_updown_intervals: list[str] = DEFAULTS["btc_updown_intervals"]
    btc_updown_min_edge: float = DEFAULTS["btc_updown_min_edge"]
    btc_updown_5m_vol: float = DEFAULTS["btc_updown_5m_vol"]
    btc_updown_logistic_k: float = DEFAULTS["btc_updown_logistic_k"]
    btc_updown_momentum_weight: float = DEFAULTS["btc_updown_momentum_weight"]
    btc_updown_min_ask: float = DEFAULTS["btc_updown_min_ask"]
    btc_updown_max_ask: float = DEFAULTS["btc_updown_max_ask"]
    btc_updown_taker_fee_rate: float = DEFAULTS["btc_updown_taker_fee_rate"]
    btc_updown_maker_edge_cushion: float = DEFAULTS["btc_updown_maker_edge_cushion"]

    # Safe Compounder strategy
    safe_compounder_assets: list[str] = DEFAULTS["safe_compounder_assets"]
    safe_compounder_intervals: list[str] = DEFAULTS["safe_compounder_intervals"]
    safe_compounder_min_confidence: float = DEFAULTS["safe_compounder_min_confidence"]
    safe_compounder_min_edge: float = DEFAULTS["safe_compounder_min_edge"]
    safe_compounder_maker_edge_cushion: float = DEFAULTS["safe_compounder_maker_edge_cushion"]
    safe_compounder_min_window_progress: float = DEFAULTS["safe_compounder_min_window_progress"]
    safe_compounder_dual_side_max_combined: float = DEFAULTS["safe_compounder_dual_side_max_combined"]
    safe_compounder_cross_asset_boost_cap: float = DEFAULTS["safe_compounder_cross_asset_boost_cap"]
    safe_compounder_hard_floor_pct: float = DEFAULTS["safe_compounder_hard_floor_pct"]
    safe_compounder_max_single_trade_pct: float = DEFAULTS["safe_compounder_max_single_trade_pct"]
    safe_compounder_max_positions: int = DEFAULTS["safe_compounder_max_positions"]

    # Sports Daily strategy
    sports_daily_tags: list[str] = DEFAULTS["sports_daily_tags"]
    sports_daily_min_volume: float = DEFAULTS["sports_daily_min_volume"]
    sports_daily_min_liquidity: float = DEFAULTS["sports_daily_min_liquidity"]
    sports_daily_min_spread: float = DEFAULTS["sports_daily_min_spread"]
    sports_daily_max_hours_to_resolution: int = DEFAULTS["sports_daily_max_hours_to_resolution"]
    sports_daily_favorite_min_prob: float = DEFAULTS["sports_daily_favorite_min_prob"]
    sports_daily_favorite_max_prob: float = DEFAULTS["sports_daily_favorite_max_prob"]
    sports_daily_imbalance_threshold: float = DEFAULTS["sports_daily_imbalance_threshold"]
    sports_daily_maker_cushion: float = DEFAULTS["sports_daily_maker_cushion"]
    sports_daily_min_edge: float = DEFAULTS["sports_daily_min_edge"]
    sports_daily_max_positions: int = DEFAULTS["sports_daily_max_positions"]
    sports_daily_max_single_trade_pct: float = DEFAULTS["sports_daily_max_single_trade_pct"]

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
