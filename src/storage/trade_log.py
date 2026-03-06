import sqlite3
from datetime import datetime, timezone
from typing import Any


class TradeLog:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def log_trade(self, trade: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades
               (timestamp, market_id, market_question, token_id, side, outcome,
                price, size, cost, strategy, confidence, kelly_fraction,
                order_type, status, fill_price, pnl, paper_trade, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                trade.get("timestamp", datetime.now(timezone.utc).isoformat()),
                trade["market_id"],
                trade.get("market_question", ""),
                trade["token_id"],
                trade["side"],
                trade["outcome"],
                trade["price"],
                trade["size"],
                trade["cost"],
                trade["strategy"],
                trade.get("confidence"),
                trade.get("kelly_fraction"),
                trade.get("order_type", "GTC"),
                trade.get("status", "filled"),
                trade.get("fill_price"),
                trade.get("pnl"),
                1 if trade.get("paper_trade", True) else 0,
                trade.get("notes"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def update_trade_status(self, trade_id: int, status: str, pnl: float | None = None):
        if pnl is not None:
            self.conn.execute(
                "UPDATE trades SET status=?, pnl=? WHERE id=?",
                (status, pnl, trade_id),
            )
        else:
            self.conn.execute("UPDATE trades SET status=? WHERE id=?", (status, trade_id))
        self.conn.commit()

    def save_position(self, pos: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO positions
               (market_id, token_id, outcome, market_question, entry_price,
                size, cost, current_price, status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos["market_id"],
                pos["token_id"],
                pos["outcome"],
                pos.get("market_question", ""),
                pos["entry_price"],
                pos["size"],
                pos["cost"],
                pos.get("current_price", pos["entry_price"]),
                "open",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def close_position(self, position_id: int, realized_pnl: float):
        self.conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, realized_pnl=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), realized_pnl, position_id),
        )
        self.conn.commit()

    def get_open_positions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status='open'"
        ).fetchall()
        return [dict(r) for r in rows]

    def save_daily_snapshot(self, snapshot: dict):
        self.conn.execute(
            """INSERT OR REPLACE INTO daily_snapshots
               (date, balance, portfolio_value, total_pnl, trades_count,
                wins, losses, daily_return_pct, aggression_level)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                snapshot["date"],
                snapshot["balance"],
                snapshot["portfolio_value"],
                snapshot["total_pnl"],
                snapshot.get("trades_count", 0),
                snapshot.get("wins", 0),
                snapshot.get("losses", 0),
                snapshot.get("daily_return_pct"),
                snapshot.get("aggression_level"),
            ),
        )
        self.conn.commit()

    def get_recent_trades(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_today_trades(self) -> list[dict]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY id",
            (f"{today}%",),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_state(self, key: str, default: str = "") -> str:
        row = self.conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str):
        self.conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def get_daily_snapshots(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM daily_snapshots ORDER BY date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
