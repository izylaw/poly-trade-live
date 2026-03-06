import unittest
from datetime import datetime, timezone, timedelta
from src.adaptive.goal_tracker import GoalTracker
from src.adaptive.aggression_tuner import AggressionTuner
from src.risk.risk_manager import RiskManager
from src.risk.circuit_breaker import CircuitBreaker
from src.config.settings import Settings


class TestGoalTracker(unittest.TestCase):
    def test_initial_status(self):
        tracker = GoalTracker(10.0, 1000.0, 60)
        status = tracker.get_status(10.0)
        self.assertAlmostEqual(status.progress_pct, 0.0, delta=1)
        self.assertGreater(status.required_daily_rate, 0)

    def test_halfway_progress(self):
        tracker = GoalTracker(10.0, 1000.0, 60)
        # sqrt(10 * 1000) = 100 is geometric midpoint
        status = tracker.get_status(100.0)
        self.assertAlmostEqual(status.progress_pct, 50.0, delta=5)

    def test_at_target(self):
        tracker = GoalTracker(10.0, 1000.0, 60)
        status = tracker.get_status(1000.0)
        self.assertAlmostEqual(status.progress_pct, 100.0, delta=1)

    def test_below_starting_capital(self):
        tracker = GoalTracker(10.0, 1000.0, 60)
        status = tracker.get_status(5.0)
        self.assertLess(status.progress_pct, 0.1)

    def test_daily_rate_tracking(self):
        tracker = GoalTracker(10.0, 1000.0, 60)
        # Simulate multiple days by directly inserting dated entries
        tracker._daily_balances = [
            ("2026-03-01", 10.0),
            ("2026-03-02", 10.5),
            ("2026-03-03", 11.0),
        ]
        rate = tracker._calc_recent_rate(7)
        self.assertGreater(rate, 0)


class TestAggressionTuner(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(starting_capital=10.0, target_balance=1000.0, target_days=60)
        self.cb = CircuitBreaker()
        self.rm = RiskManager(self.settings, self.cb)
        self.tracker = GoalTracker(10.0, 1000.0, 60)
        self.tuner = AggressionTuner(self.tracker, self.rm, 10.0)

    def test_emergency_below_starting(self):
        level = self.tuner.update(5.0)
        self.assertEqual(level, "emergency")

    def test_moderate_at_start(self):
        level = self.tuner.update(10.0)
        # At start, required rate is high, we're behind -> aggressive or ultra
        self.assertIn(level, ["moderate", "aggressive", "ultra"])

    def test_applies_risk_params(self):
        self.tuner.update(5.0)  # emergency
        self.assertEqual(self.rm.max_single_trade_pct, 0.05)
        self.assertEqual(self.rm.min_confidence, 0.90)


if __name__ == "__main__":
    unittest.main()
