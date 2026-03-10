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
               (market_id, token_id, outcome, market_question, strategy,
                entry_price, size, cost, current_price, status, opened_at, paper_trade)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pos["market_id"],
                pos["token_id"],
                pos["outcome"],
                pos.get("market_question", ""),
                pos.get("strategy", "unknown"),
                pos["entry_price"],
                pos["size"],
                pos["cost"],
                pos.get("current_price", pos["entry_price"]),
                "open",
                datetime.now(timezone.utc).isoformat(),
                1 if pos.get("paper_trade", True) else 0,
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

    def get_open_positions(self, paper_trade: bool | None = None) -> list[dict]:
        if paper_trade is not None:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open' AND paper_trade=?",
                (1 if paper_trade else 0,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
        return [dict(r) for r in rows]

    def compute_paper_balance(self, starting_capital: float) -> float:
        """Derive paper balance from DB: starting_capital + closed PnL - open cost (paper only)."""
        row = self.conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) as total_pnl FROM positions WHERE status='closed' AND paper_trade=1"
        ).fetchone()
        row2 = self.conn.execute(
            "SELECT COALESCE(SUM(cost), 0) as open_cost FROM positions WHERE status='open' AND paper_trade=1"
        ).fetchone()
        return starting_capital + row["total_pnl"] - row2["open_cost"]

    def get_position_by_id(self, position_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
        return dict(row) if row else None

    def get_trade_for_position(self, market_id: str, token_id: str) -> dict | None:
        """Find the filled trade record linked to a position."""
        row = self.conn.execute(
            "SELECT * FROM trades WHERE market_id=? AND token_id=? AND status='filled' ORDER BY id DESC LIMIT 1",
            (market_id, token_id),
        ).fetchone()
        return dict(row) if row else None

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

    # --- Prediction tracking ---

    def log_prediction(self, pred: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO predictions
               (timestamp, strategy, asset, interval, market_id, token_id,
                outcome, est_prob, bid_price, delta_pct, window_progress,
                momentum, dynamic_vol, resolution_ts, traded, trade_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pred.get("timestamp", datetime.now(timezone.utc).isoformat()),
                pred["strategy"],
                pred["asset"],
                pred["interval"],
                pred["market_id"],
                pred["token_id"],
                pred["outcome"],
                pred["est_prob"],
                pred.get("bid_price"),
                pred.get("delta_pct"),
                pred.get("window_progress"),
                pred.get("momentum"),
                pred.get("dynamic_vol"),
                pred.get("resolution_ts"),
                1 if pred.get("traded") else 0,
                pred.get("trade_id"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def get_unresolved_predictions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM predictions WHERE resolved=0 AND resolution_ts IS NOT NULL"
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_prediction(self, pred_id: int, actual_correct: bool, pnl: float | None = None):
        self.conn.execute(
            "UPDATE predictions SET resolved=1, actual_correct=?, pnl=? WHERE id=?",
            (1 if actual_correct else 0, pnl, pred_id),
        )
        self.conn.commit()

    def get_calibration_stats(self) -> dict:
        """Returns calibration stats grouped by probability buckets."""
        rows = self.conn.execute(
            "SELECT est_prob, actual_correct FROM predictions WHERE resolved=1"
        ).fetchall()

        buckets: dict[str, dict] = {}
        for row in rows:
            prob = row["est_prob"]
            correct = row["actual_correct"]
            # Bucket by 0.10 intervals
            bucket_low = int(prob * 10) / 10
            bucket_high = bucket_low + 0.10
            key = f"{bucket_low:.2f}-{bucket_high:.2f}"
            if key not in buckets:
                buckets[key] = {"count": 0, "wins": 0, "total_prob": 0.0}
            buckets[key]["count"] += 1
            buckets[key]["wins"] += 1 if correct else 0
            buckets[key]["total_prob"] += prob

        result = {}
        for key, b in sorted(buckets.items()):
            result[key] = {
                "count": b["count"],
                "wins": b["wins"],
                "win_rate": b["wins"] / b["count"] if b["count"] > 0 else 0,
                "avg_prob": b["total_prob"] / b["count"] if b["count"] > 0 else 0,
            }
        return result
