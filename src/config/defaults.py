DEFAULTS = {
    "starting_capital": 10.0,
    "target_balance": 30.0,
    "target_days": 60,
    "goal_start_date": "",  # ISO format (e.g. 2026-03-01T00:00:00+00:00), empty = auto-detect
    "paper_trading": True,
    "polygon_rpc_url": "https://polygon-rpc.com",
    "log_level": "INFO",

    # Risk defaults
    "hard_floor_pct": 0.10,
    "max_single_trade_pct": 0.30,
    "max_portfolio_exposure_pct": 0.70,
    "max_open_positions": 20,
    "daily_loss_limit_pct": 0.20,
    "min_trade_size": 5.0,
    "consecutive_loss_pause": 3,
    "max_positions_per_market": 1,
    "high_prob_max_positions": 8,
    "btc_updown_max_positions": 2,
    "llm_max_positions": 2,
    "max_long_term_positions": 5,
    "long_term_threshold_days": 7,

    # Market filter defaults
    "min_volume_24h": 500.0,
    "min_liquidity": 250.0,
    "max_spread": 0.05,
    "min_time_to_resolution_hours": 0.5,
    "max_markets": 0,  # 0 = no cap (quality filters are the real gate)

    # Scanner defaults
    "scanner_max_event_pages": 100,
    "scanner_clob_cross_ref": False,
    "scanner_clob_ttl": 1800,

    # Engine defaults
    "scan_interval_seconds": 15,
    "adapt_interval_seconds": 300,

    # Strategy defaults
    "high_prob_min_price": 0.88,
    "high_prob_max_price": 0.98,
    "high_prob_longshot_threshold": 0.20,
    "high_prob_longshot_min_price": 0.02,
    "high_prob_longshot_conf_multiplier": 2.0,
    "high_prob_maker_ttl_hours": 4,
    "arb_min_spread": 0.005,
    "arb_fee_rate": 0.0625,
    "arb_min_event_markets": 3,
    "arb_min_event_spread": 0.02,
    "arb_max_event_legs": 6,
    "arb_mono_min_spread": 0.01,

    # Strategy selection
    "only_strategies": [],  # empty = all strategies enabled

    # BTC Up/Down strategy (v2 — price delta model)
    "btc_updown_assets": ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"],
    "btc_updown_intervals": ["5m", "15m"],
    "btc_updown_min_edge": 0.02,          # min (est_prob - ask) to trade
    "btc_updown_5m_vol": 0.0025,          # mixed-crypto 5-min volatility baseline
    "btc_updown_logistic_k": 1.5,         # logistic steepness
    "btc_updown_momentum_weight": 0.3,    # momentum confirmation weight
    "btc_updown_min_ask": 0.03,           # min ask price to consider
    "btc_updown_max_ask": 0.85,           # max ask price to consider
    "btc_updown_taker_fee_rate": 0.0625,  # Polymarket dynamic taker fee rate
    "btc_updown_maker_edge_cushion": 0.05,  # bid = est_prob - cushion
    "btc_updown_min_confidence": 0.60,     # min confidence to create signal
    "btc_updown_fok_threshold_secs": 600,  # use FOK taker when < this time remaining

    # Engine capital management
    "max_signals_per_cycle": 0,            # 0 = unlimited, 1 = concentrate capital
    "max_positions_per_asset": 1,          # max concurrent positions per asset

    # Take-profit
    "take_profit_enabled": True,
    "take_profit_pct": 0.30,              # 30% gain triggers sell
    "take_profit_strategies": ["btc_updown"],
    "take_profit_min_bid": 0.02,          # skip if bid too thin (no real buyer)

    # Crypto Hourly strategy (1h intervals, inherits btc_updown logic)
    "crypto_hourly_assets": ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"],
    "crypto_hourly_intervals": ["1h"],
    "crypto_hourly_min_edge": 0.03,
    "crypto_hourly_min_ask": 0.03,
    "crypto_hourly_max_ask": 0.85,
    "crypto_hourly_maker_edge_cushion": 0.05,
    "crypto_hourly_max_positions": 5,

    # Safe Compounder strategy
    "safe_compounder_assets": ["BTC", "ETH", "SOL"],
    "safe_compounder_intervals": ["5m", "15m", "1h"],
    "safe_compounder_min_confidence": 0.78,
    "safe_compounder_min_edge": 0.03,
    "safe_compounder_maker_edge_cushion": 0.03,
    "safe_compounder_min_window_progress": 0.65,
    "safe_compounder_dual_side_max_combined": 0.95,
    "safe_compounder_cross_asset_boost_cap": 0.10,
    "safe_compounder_hard_floor_pct": 0.30,
    "safe_compounder_max_single_trade_pct": 0.08,
    "safe_compounder_max_positions": 3,

    # Sports Daily strategy
    "sports_daily_tags": [
        "sports", "nba", "nfl", "mlb", "nhl", "mma", "ufc", "soccer",
        "tennis", "cricket", "golf", "f1", "rugby", "esports",
        "premier-league", "la-liga", "bundesliga", "serie-a", "ligue-1",
        "ncaa", "college-basketball", "baseball", "boxing",
    ],
    "sports_daily_min_volume": 5000.0,
    "sports_daily_min_liquidity": 1000.0,
    "sports_daily_min_spread": 0.04,
    "sports_daily_max_hours_to_resolution": 48,
    "sports_daily_favorite_min_prob": 0.82,
    "sports_daily_favorite_max_prob": 0.95,
    "sports_daily_imbalance_threshold": 0.30,
    "sports_daily_maker_cushion": 0.02,
    "sports_daily_min_edge": 0.02,
    "sports_daily_max_spread": 0.40,
    "sports_daily_min_book_depth": 20.0,
    "sports_daily_max_positions": 4,
    "sports_daily_max_single_trade_pct": 0.05,

    # LLM Crypto strategy
    "llm_enabled": False,
    "llm_api_key": "",
    "llm_base_url": "http://100.96.38.49:11434",
    "llm_model": "qwen3.5:27b",
    "llm_batch_size": 5,
    "llm_min_edge": 0.05,
    "llm_cache_ttl": 600,
    "llm_daily_cache_ttl": 1800,
    "llm_run_every_n_cycles": 2,
    "llm_max_tokens": 4096,
    "llm_timeout": 300,
    "llm_context_size": 32768,
    "llm_maker_edge_cushion": 0.05,
    "llm_intervals": ["1h", "4h"],
    "llm_daily_lookahead_days": 5,
}
