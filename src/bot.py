import fcntl
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
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
from src.strategies.btc_updown import BtcUpdownStrategy
from src.strategies.safe_compounder import SafeCompounderStrategy
from src.strategies.sports_daily import SportsDailyStrategy

logger = logging.getLogger("poly-trade")


ALL_STRATEGIES = [
    "high_probability", "arbitrage", "btc_updown",
    "safe_compounder", "sports_daily", "llm_crypto",
]


class Bot:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings()
        self.engine: TradingEngine | None = None
        self._lock_files: list = []

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
        scanner = MarketScanner(s, gamma, market_filter, clob_client=clob)

        # Risk
        circuit_breaker = CircuitBreaker(
            daily_loss_limit_pct=s.daily_loss_limit_pct,
            consecutive_loss_limit=s.consecutive_loss_pause,
        )
        risk_manager = RiskManager(s, circuit_breaker)

        # Execution
        paper = PaperExecutor(s.starting_capital, trade_log, s.max_open_positions) if s.paper_trading else None
        live = LiveExecutor(clob, trade_log, s.max_open_positions) if not s.paper_trading else None
        executor = Executor(s, paper=paper, live=live)

        # Strategies
        strategies = [
            HighProbabilityStrategy(s),
            ArbitrageStrategy(s),
            BtcUpdownStrategy(s),
            SafeCompounderStrategy(s),
            SportsDailyStrategy(s),
        ]

        if s.llm_enabled:
            from src.strategies.llm_crypto import LLMCryptoStrategy
            strategies.append(LLMCryptoStrategy(s))
            logger.info("LLM Crypto strategy enabled")

        # Adaptive — resolve goal start date with priority chain:
        # 1. .env GOAL_START_DATE  2. SQLite bot_state  3. datetime.now(UTC)
        if s.goal_start_date:
            start_date = datetime.fromisoformat(s.goal_start_date)
            logger.info(f"Goal start date from .env: {start_date.isoformat()}")
        else:
            stored = trade_log.get_state("goal_start_date")
            if stored:
                start_date = datetime.fromisoformat(stored)
                logger.info(f"Goal start date from DB: {start_date.isoformat()}")
            else:
                start_date = datetime.now(timezone.utc)
                trade_log.set_state("goal_start_date", start_date.isoformat())
                logger.info(f"Goal start date initialized: {start_date.isoformat()}")

        goal_tracker = GoalTracker(s.starting_capital, s.target_balance, s.target_days, start_date=start_date)
        aggression_tuner = AggressionTuner(goal_tracker, risk_manager, s.starting_capital)

        # Core
        balance_mgr = BalanceManager(s)
        position_tracker = PositionTracker(trade_log, paper_mode=s.paper_trading)
        order_manager = OrderManager(trade_log, s.max_open_positions)

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

    def _acquire_strategy_locks(self):
        strategies = self.settings.only_strategies or ALL_STRATEGIES
        lock_dir = Path("data/locks")
        lock_dir.mkdir(parents=True, exist_ok=True)
        for name in sorted(strategies):
            lock_path = lock_dir / f"{name}.lock"
            f = open(lock_path, "w")
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                # Release any locks already acquired
                self._release_strategy_locks()
                f.close()
                logger.error(f"{name} is already running in another session")
                sys.exit(1)
            self._lock_files.append(f)
        locked = ", ".join(sorted(strategies))
        logger.info(f"Strategy locks acquired: {locked}")

    def _release_strategy_locks(self):
        for f in self._lock_files:
            try:
                fcntl.flock(f, fcntl.LOCK_UN)
                f.close()
            except Exception:
                pass
        self._lock_files.clear()

    def run(self):
        if not self.engine:
            self.build()
        self._acquire_strategy_locks()
        try:
            mode = "PAPER" if self.settings.paper_trading else "LIVE"
            logger.info(f"Bot starting in {mode} mode | capital=${self.settings.starting_capital} -> ${self.settings.target_balance}")
            self.engine.start()
        finally:
            self._release_strategy_locks()

    def stop(self):
        if self.engine:
            self.engine.stop()
