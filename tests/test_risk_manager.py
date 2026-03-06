import unittest
from src.config.settings import Settings
from src.risk.risk_manager import RiskManager, TradeSignal
from src.risk.circuit_breaker import CircuitBreaker


class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            starting_capital=10.0,
            hard_floor_pct=0.20,
            max_single_trade_pct=0.10,
            max_portfolio_exposure_pct=0.60,
            max_open_positions=5,
            min_trade_size=0.50,
        )
        self.cb = CircuitBreaker()
        self.cb.set_start_of_day_balance(10.0)
        self.rm = RiskManager(self.settings, self.cb)

    def _make_signal(self, price=0.93, confidence=0.95):
        return TradeSignal(
            market_id="test",
            token_id="tok",
            market_question="Test market?",
            side="BUY",
            outcome="Yes",
            price=price,
            confidence=confidence,
            strategy="high_probability",
            expected_value=confidence * (1 - price),
        )

    def test_hard_floor_blocks_trade(self):
        signal = self._make_signal()
        # Balance at or below hard floor ($2)
        result = self.rm.evaluate(signal, balance=2.0, open_positions=[], portfolio_exposure=0)
        self.assertIsNone(result)

    def test_trade_approved_above_floor(self):
        signal = self._make_signal()
        result = self.rm.evaluate(signal, balance=10.0, open_positions=[], portfolio_exposure=0)
        self.assertIsNotNone(result)

    def test_max_positions_blocks(self):
        signal = self._make_signal()
        fake_positions = [{"cost": 1.0}] * 5
        result = self.rm.evaluate(signal, balance=10.0, open_positions=fake_positions, portfolio_exposure=5.0)
        self.assertIsNone(result)

    def test_low_confidence_blocks(self):
        signal = self._make_signal(confidence=0.50)
        result = self.rm.evaluate(signal, balance=10.0, open_positions=[], portfolio_exposure=0)
        self.assertIsNone(result)

    def test_portfolio_exposure_blocks(self):
        signal = self._make_signal()
        # Exposure at 60% of $10 = $6
        result = self.rm.evaluate(signal, balance=10.0, open_positions=[], portfolio_exposure=6.0)
        self.assertIsNone(result)

    def test_circuit_breaker_blocks(self):
        signal = self._make_signal()
        for _ in range(3):
            self.cb.record_loss()
        self.assertTrue(self.cb.is_paused)
        result = self.rm.evaluate(signal, balance=10.0, open_positions=[], portfolio_exposure=0)
        self.assertIsNone(result)

    def test_approved_trade_never_breaches_floor(self):
        signal = self._make_signal()
        result = self.rm.evaluate(signal, balance=3.0, open_positions=[], portfolio_exposure=0)
        if result:
            remaining = 3.0 - result.cost
            self.assertGreater(remaining, self.settings.hard_floor)


if __name__ == "__main__":
    unittest.main()
