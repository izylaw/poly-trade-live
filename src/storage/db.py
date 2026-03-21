import sqlite3
from pathlib import Path
from src.storage.models import SCHEMA_SQL


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    conn.commit()

    # Migration: add strategy column to positions if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN strategy TEXT NOT NULL DEFAULT 'unknown'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add paper_trade column to positions if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN paper_trade INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Backfill strategy from trades table for existing positions
    conn.execute("""
        UPDATE positions SET strategy = (
            SELECT t.strategy FROM trades t
            WHERE t.market_id = positions.market_id
              AND t.token_id = positions.token_id
              AND t.status = 'filled'
            ORDER BY t.id DESC LIMIT 1
        ) WHERE strategy = 'unknown'
    """)
    conn.commit()

    # Migration: add resolution_ts column to positions if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN resolution_ts REAL NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add is_long_term column to positions if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN is_long_term INTEGER NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add slug column to positions if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE positions ADD COLUMN slug TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    # Migration: add resolution_ts column to trades if missing (existing DBs)
    try:
        conn.execute("ALTER TABLE trades ADD COLUMN resolution_ts REAL NOT NULL DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists

    return conn
