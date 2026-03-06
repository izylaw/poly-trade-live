import logging
from src.config.settings import Settings
from src.utils.logger import setup_logger
from src.storage.db import init_db
from src.storage.trade_log import TradeLog
from src.market_data.gamma_client import GammaClient
from src.market_data.clob_client import PolymarketClobClient
from src.market_data.market_filter import MarketFilter
from src.market_data.market_scanner import MarketScanner
from src.risk.risk_manager import RiskManager
from src.risk.circuit_breaker import CircuitBreaker
from src.execution.paper_executor import PaperExecutor
from src.execution.live_executor import LiveExecutor
from src.execution.executor import Executor
from src.adaptive.goal_tracker import GoalTracker
from src.adaptive.aggression_tuner import AggressionTuner
from src.core.balance_manager import BalanceManager
from src.core.position_tracker import PositionTracker
from src.core.order_manager import OrderManager
from src.core.engine import TradingEngine
from src.strategies.high_probability import HighProbabilityStrategy
from src.strategies.arbitrage import ArbitrageStrategy

logger = logging.getLogger("poly-trade")


class Bot:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.engine: TradingEngine | None = None

    def build(self) -> "Bot":
        s = self.settings

        # Logger
        setup_logger("poly-trade", s.log_level, s.log_dir)

        # Storage
        conn = init_db(s.db_path)
        trade_log = TradeLog(conn)

        # Market data
        gamma = GammaClient()
        clob = PolymarketClobClient(s)
        market_filter = MarketFilter(s)
        scanner = MarketScanner(s, gamma, market_filter)

        # Risk
        circuit_breaker = CircuitBreaker(
            daily_loss_limit_pct=s.daily_loss_limit_pct,
            consecutive_loss_limit=s.consecutive_loss_pause,
        )
        risk_manager = RiskManager(s, circuit_breaker)

        # Execution
        paper = PaperExecutor(s.starting_capital, trade_log) if s.paper_trading else None
        live = LiveExecutor(clob, trade_log) if not s.paper_trading else None
        executor = Executor(s, paper=paper, live=live)

        # Strategies
        strategies = [
            HighProbabilityStrategy(s),
            ArbitrageStrategy(s),
        ]

        # Adaptive
        goal_tracker = GoalTracker(s.starting_capital, s.target_balance, s.target_days)
        aggression_tuner = AggressionTuner(goal_tracker, risk_manager, s.starting_capital)

        # Core
        balance_mgr = BalanceManager(s)
        position_tracker = PositionTracker(trade_log)
        order_manager = OrderManager(trade_log)

        self.engine = TradingEngine(
            settings=s,
            scanner=scanner,
            clob_client=clob,
            strategies=strategies,
            risk_manager=risk_manager,
            circuit_breaker=circuit_breaker,
            executor=executor,
            goal_tracker=goal_tracker,
            aggression_tuner=aggression_tuner,
            balance_manager=balance_mgr,
            position_tracker=position_tracker,
            order_manager=order_manager,
            trade_log=trade_log,
        )
        return self

    def run(self):
        if not self.engine:
            self.build()
        mode = "PAPER" if self.settings.paper_trading else "LIVE"
        logger.info(f"Bot starting in {mode} mode | capital=${self.settings.starting_capital} -> ${self.settings.target_balance}")
        self.engine.start()

    def stop(self):
        if self.engine:
            self.engine.stop()
