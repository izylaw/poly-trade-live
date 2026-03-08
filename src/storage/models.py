SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    market_id TEXT NOT NULL,
    market_question TEXT,
    token_id TEXT NOT NULL,
    side TEXT NOT NULL,
    outcome TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    cost REAL NOT NULL,
    strategy TEXT NOT NULL,
    confidence REAL,
    kelly_fraction REAL,
    order_type TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    fill_price REAL,
    pnl REAL,
    paper_trade INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    market_question TEXT,
    strategy TEXT NOT NULL DEFAULT 'unknown',
    entry_price REAL NOT NULL,
    size REAL NOT NULL,
    cost REAL NOT NULL,
    current_price REAL,
    unrealized_pnl REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    realized_pnl REAL
);

CREATE TABLE IF NOT EXISTS daily_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    balance REAL NOT NULL,
    portfolio_value REAL NOT NULL,
    total_pnl REAL NOT NULL,
    trades_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    daily_return_pct REAL,
    aggression_level TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    strategy TEXT NOT NULL,
    asset TEXT NOT NULL,
    interval TEXT NOT NULL,
    market_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    est_prob REAL NOT NULL,
    bid_price REAL,
    delta_pct REAL,
    window_progress REAL,
    momentum REAL,
    dynamic_vol REAL,
    resolution_ts REAL,
    traded INTEGER NOT NULL DEFAULT 0,
    trade_id INTEGER,
    resolved INTEGER NOT NULL DEFAULT 0,
    actual_correct INTEGER,
    pnl REAL
);

CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""
