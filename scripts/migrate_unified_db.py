#!/usr/bin/env python3
"""Merge per-strategy fragment DBs into the unified poly_trade.db.

Idempotent — safe to run multiple times. Migrated fragments are renamed to .db.migrated.

Usage:
    python scripts/migrate_unified_db.py
"""
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path("data")
UNIFIED_DB = DATA_DIR / "poly_trade.db"

# Tables to merge and their dedup strategies
TABLES = {
    "trades": "INSERT OR IGNORE",
    "positions": "INSERT OR IGNORE",
    "predictions": "INSERT OR IGNORE",
    "daily_snapshots": "INSERT OR REPLACE",
    "bot_state": None,  # special handling
}


def _get_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def _backup_unified():
    if UNIFIED_DB.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup = DATA_DIR / f"poly_trade.db.backup_{ts}"
        shutil.copy2(UNIFIED_DB, backup)
        print(f"Backed up {UNIFIED_DB} -> {backup}")


def _find_fragments() -> list[Path]:
    """Find poly_trade_*.db files (excluding the unified DB and already-migrated)."""
    return sorted(
        p for p in DATA_DIR.glob("poly_trade_*.db")
        if not p.name.endswith(".migrated")
    )


def _ensure_dedup_indexes(conn: sqlite3.Connection):
    """Create temp unique indexes so INSERT OR IGNORE deduplicates correctly."""
    # trades: unique on (timestamp, market_id, token_id, strategy, side, price, size)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS _tmp_trades_dedup
        ON trades (timestamp, market_id, token_id, strategy, side, price, size)
    """)
    # positions: unique on (market_id, token_id, strategy, opened_at, entry_price, size)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS _tmp_positions_dedup
        ON positions (market_id, token_id, strategy, opened_at, entry_price, size)
    """)
    # predictions: unique on (timestamp, strategy, market_id, token_id, interval)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS _tmp_predictions_dedup
        ON predictions (timestamp, strategy, market_id, token_id, interval)
    """)
    conn.commit()


def _merge_fragment(unified_conn: sqlite3.Connection, fragment_path: Path):
    """Attach a fragment DB and merge its data into the unified DB."""
    print(f"\nMerging {fragment_path.name}...")
    unified_conn.execute(f"ATTACH DATABASE ? AS frag", (str(fragment_path),))

    for table, insert_mode in TABLES.items():
        # Check if table exists in fragment
        exists = unified_conn.execute(
            "SELECT name FROM frag.sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if not exists:
            print(f"  {table}: not found in fragment, skipping")
            continue

        frag_cols = _get_columns(unified_conn, table)
        # Use the unified DB's columns (fragment may have fewer)
        unified_cols = _get_columns(unified_conn, table)
        # Only copy columns that exist in both
        common_cols = [c for c in unified_cols if c in frag_cols]
        cols_str = ", ".join(common_cols)

        if table == "bot_state":
            # Keep the row with the latest updated_at
            rows = unified_conn.execute(f"SELECT key, value, updated_at FROM frag.bot_state").fetchall()
            for key, value, updated_at in rows:
                existing = unified_conn.execute(
                    "SELECT updated_at FROM bot_state WHERE key = ?", (key,)
                ).fetchone()
                if existing is None:
                    unified_conn.execute(
                        "INSERT INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
                        (key, value, updated_at),
                    )
                elif updated_at > existing[0]:
                    unified_conn.execute(
                        "UPDATE bot_state SET value = ?, updated_at = ? WHERE key = ?",
                        (value, updated_at, key),
                    )
            count = len(rows)
        else:
            # Exclude autoincrement 'id' column from insert
            insert_cols = [c for c in common_cols if c != "id"]
            insert_cols_str = ", ".join(insert_cols)
            result = unified_conn.execute(
                f"{insert_mode} INTO {table} ({insert_cols_str}) "
                f"SELECT {insert_cols_str} FROM frag.{table}"
            )
            count = result.rowcount

        print(f"  {table}: {count} rows merged")

    unified_conn.execute("DETACH DATABASE frag")
    unified_conn.commit()


def main():
    if not DATA_DIR.exists():
        print("No data/ directory found. Nothing to migrate.")
        return

    fragments = _find_fragments()
    if not fragments:
        print("No fragment databases found (poly_trade_*.db). Nothing to migrate.")
        return

    print(f"Found {len(fragments)} fragment(s): {[f.name for f in fragments]}")

    _backup_unified()

    # Open (or create) unified DB with schema
    from src.storage.db import init_db
    unified_conn = init_db(UNIFIED_DB)

    _ensure_dedup_indexes(unified_conn)

    for frag_path in fragments:
        _merge_fragment(unified_conn, frag_path)

    unified_conn.close()

    # Rename migrated fragments
    for frag_path in fragments:
        migrated = frag_path.with_suffix(".db.migrated")
        frag_path.rename(migrated)
        print(f"Renamed {frag_path.name} -> {migrated.name}")
        # Also rename WAL/SHM files if present
        for ext in (".db-wal", ".db-shm"):
            aux = frag_path.with_name(frag_path.name + ext.removeprefix(".db"))
            if aux.exists():
                aux.rename(aux.with_suffix(ext + ".migrated"))

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
