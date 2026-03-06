DEFAULTS = {
    "starting_capital": 10.0,
    "target_balance": 1000.0,
    "target_days": 60,
    "paper_trading": True,
    "polygon_rpc_url": "https://polygon-rpc.com",
    "log_level": "INFO",

    # Risk defaults
    "hard_floor_pct": 0.20,
    "max_single_trade_pct": 0.10,
    "max_portfolio_exposure_pct": 0.60,
    "max_open_positions": 5,
    "daily_loss_limit_pct": 0.15,
    "min_trade_size": 0.50,
    "consecutive_loss_pause": 3,

    # Market filter defaults
    "min_volume_24h": 1000.0,
    "min_liquidity": 500.0,
    "max_spread": 0.05,
    "min_time_to_resolution_hours": 2,
    "max_markets": 50,

    # Engine defaults
    "scan_interval_seconds": 30,
    "adapt_interval_seconds": 300,

    # Strategy defaults
    "high_prob_min_price": 0.92,
    "high_prob_max_price": 0.98,
    "arb_min_spread": 0.005,
}
