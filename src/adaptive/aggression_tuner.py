import logging
from dataclasses import dataclass
from src.adaptive.goal_tracker import GoalTracker, GoalStatus
from src.risk.risk_manager import RiskManager

logger = logging.getLogger("poly-trade")


@dataclass
class AggressionLevel:
    name: str
    max_trade_pct: float
    min_confidence: float
    strategies: list[str]


LEVELS = {
    "conservative": AggressionLevel("conservative", 0.05, 0.85, ["high_probability", "arbitrage", "btc_updown", "safe_compounder", "sports_daily", "weather_temperature"]),
    "moderate": AggressionLevel("moderate", 0.10, 0.70, ["high_probability", "arbitrage", "btc_updown", "safe_compounder", "sports_daily", "llm_crypto", "weather_temperature"]),
    "aggressive": AggressionLevel("aggressive", 0.15, 0.60, ["high_probability", "arbitrage", "btc_updown", "safe_compounder", "sports_daily", "llm_crypto", "weather_temperature"]),
    "ultra": AggressionLevel("ultra", 0.15, 0.55, ["high_probability", "arbitrage", "btc_updown", "safe_compounder", "sports_daily", "llm_crypto", "weather_temperature"]),
    "emergency": AggressionLevel("emergency", 0.05, 0.90, ["high_probability", "arbitrage", "btc_updown", "safe_compounder", "sports_daily", "weather_temperature"]),
}


class AggressionTuner:
    def __init__(self, goal_tracker: GoalTracker, risk_manager: RiskManager, starting_capital: float):
        self.goal_tracker = goal_tracker
        self.risk_manager = risk_manager
        self.starting_capital = starting_capital
        self.current_level = "moderate"

    def update(self, current_balance: float) -> str:
        status = self.goal_tracker.get_status(current_balance)
        new_level = self._determine_level(status, current_balance)

        if new_level != self.current_level:
            logger.info(f"Aggression level: {self.current_level} -> {new_level}")
            self.current_level = new_level

        self._apply_level(new_level)
        return new_level

    def _determine_level(self, status: GoalStatus, current_balance: float) -> str:
        # Emergency: below 60% of starting capital
        if current_balance < self.starting_capital * 0.60:
            return "emergency"

        # Ahead of schedule
        if status.on_track and status.behind_pct <= 0:
            return "conservative"

        # On track (within 5% of expected)
        if status.behind_pct <= 0.05:
            return "moderate"

        # Behind < 20%
        if status.behind_pct <= 0.20:
            return "aggressive"

        # Behind > 20%
        return "ultra"

    def _apply_level(self, level_name: str):
        level = LEVELS[level_name]
        self.risk_manager.max_single_trade_pct = level.max_trade_pct
        self.risk_manager.min_confidence = level.min_confidence

    def get_enabled_strategies(self) -> list[str]:
        return LEVELS[self.current_level].strategies
