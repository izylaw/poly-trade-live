import math
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger("poly-trade")


@dataclass
class GoalStatus:
    current_balance: float
    starting_capital: float
    target_balance: float
    target_days: int
    days_elapsed: float
    days_remaining: float
    progress_pct: float
    required_daily_rate: float
    actual_7day_rate: float
    on_track: bool
    projected_days: float
    behind_pct: float


class GoalTracker:
    def __init__(self, starting_capital: float, target_balance: float, target_days: int,
                 start_date: datetime | None = None):
        self.starting_capital = starting_capital
        self.target_balance = target_balance
        self.target_days = target_days
        self.start_date = start_date or datetime.now(timezone.utc)
        self._daily_balances: list[tuple[str, float]] = []

    def record_balance(self, balance: float):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Replace if same day, append if new
        if self._daily_balances and self._daily_balances[-1][0] == today:
            self._daily_balances[-1] = (today, balance)
        else:
            self._daily_balances.append((today, balance))

    def get_status(self, current_balance: float) -> GoalStatus:
        now = datetime.now(timezone.utc)
        days_elapsed = max((now - self.start_date).total_seconds() / 86400, 0.01)
        days_remaining = max(self.target_days - days_elapsed, 0.01)

        # Progress
        total_growth_needed = self.target_balance / self.starting_capital
        current_growth = current_balance / self.starting_capital if self.starting_capital > 0 else 0
        progress_pct = (math.log(current_growth) / math.log(total_growth_needed) * 100
                        if current_growth > 0 and total_growth_needed > 1 else 0)

        # Required daily compound rate to hit target from current balance
        if current_balance > 0 and days_remaining > 0:
            required_daily_rate = (self.target_balance / current_balance) ** (1 / days_remaining) - 1
        else:
            required_daily_rate = 0

        # Actual 7-day rate
        actual_7day_rate = self._calc_recent_rate(7)

        # Projected days at current rate
        if actual_7day_rate > 0 and current_balance > 0:
            projected_days = math.log(self.target_balance / current_balance) / math.log(1 + actual_7day_rate)
        else:
            projected_days = float("inf")

        # How far behind schedule
        expected_balance = self.starting_capital * (1 + required_daily_rate) ** days_elapsed
        behind_pct = (expected_balance - current_balance) / expected_balance if expected_balance > 0 else 0

        on_track = behind_pct <= 0.05

        return GoalStatus(
            current_balance=current_balance,
            starting_capital=self.starting_capital,
            target_balance=self.target_balance,
            target_days=self.target_days,
            days_elapsed=round(days_elapsed, 1),
            days_remaining=round(days_remaining, 1),
            progress_pct=round(max(progress_pct, 0), 1),
            required_daily_rate=round(required_daily_rate, 4),
            actual_7day_rate=round(actual_7day_rate, 4),
            on_track=on_track,
            projected_days=round(projected_days, 0),
            behind_pct=round(max(behind_pct, 0), 3),
        )

    def _calc_recent_rate(self, days: int) -> float:
        if len(self._daily_balances) < 2:
            return 0.0
        recent = self._daily_balances[-days:]
        if len(recent) < 2:
            return 0.0
        start_bal = recent[0][1]
        end_bal = recent[-1][1]
        n_days = max(len(recent) - 1, 1)
        if start_bal <= 0:
            return 0.0
        return (end_bal / start_bal) ** (1 / n_days) - 1
