import time
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("poly-trade")


@dataclass
class CircuitBreaker:
    daily_loss_limit_pct: float = 0.15
    consecutive_loss_limit: int = 3
    api_error_limit: int = 3

    _consecutive_losses: int = field(default=0, init=False)
    _api_errors: int = field(default=0, init=False)
    _paused_until: float = field(default=0.0, init=False)
    _start_of_day_balance: float = field(default=0.0, init=False)
    _full_stop: bool = field(default=False, init=False)

    def set_start_of_day_balance(self, balance: float):
        self._start_of_day_balance = balance
        self._consecutive_losses = 0
        self._api_errors = 0

    def record_win(self):
        self._consecutive_losses = 0
        self._api_errors = 0

    def record_loss(self):
        self._consecutive_losses += 1
        if self._consecutive_losses >= self.consecutive_loss_limit:
            pause_seconds = 30 * 60
            self._paused_until = time.time() + pause_seconds
            logger.warning(f"Circuit breaker: {self._consecutive_losses} consecutive losses. Pausing {pause_seconds // 60}min")

    def record_api_error(self):
        self._api_errors += 1
        if self._api_errors >= self.api_error_limit:
            pause_seconds = 5 * 60
            self._paused_until = time.time() + pause_seconds
            logger.warning(f"Circuit breaker: {self._api_errors} API errors. Pausing {pause_seconds // 60}min")

    def check_daily_loss(self, current_balance: float) -> bool:
        if self._start_of_day_balance <= 0:
            return True
        daily_loss = (self._start_of_day_balance - current_balance) / self._start_of_day_balance
        if daily_loss >= self.daily_loss_limit_pct:
            pause_seconds = 60 * 60
            self._paused_until = time.time() + pause_seconds
            logger.warning(f"Circuit breaker: daily loss {daily_loss:.1%} >= limit {self.daily_loss_limit_pct:.0%}. Pausing 1hr")
            return False
        return True

    def check_catastrophic_drop(self, current_balance: float, previous_balance: float) -> bool:
        if previous_balance <= 0:
            return True
        drop = (previous_balance - current_balance) / previous_balance
        if drop >= 0.25:
            self._full_stop = True
            logger.critical(f"FULL STOP: balance dropped {drop:.1%} in single tick!")
            return False
        return True

    @property
    def is_paused(self) -> bool:
        if self._full_stop:
            return True
        return time.time() < self._paused_until

    @property
    def pause_remaining_seconds(self) -> float:
        if self._full_stop:
            return float("inf")
        return max(0, self._paused_until - time.time())

    def reset_full_stop(self):
        self._full_stop = False
        self._paused_until = 0
        logger.info("Circuit breaker full stop reset")
