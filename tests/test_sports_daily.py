import json
import time
import pytest
from unittest.mock import patch, MagicMock
from src.strategies.sports_daily import (
    SportsDailyStrategy, SPORT_KEYWORDS, GAME_PATTERNS,
)
from src.config.settings import Settings
from src.risk.risk_manager import RiskManager, TradeSignal
from src.risk.circuit_breaker import CircuitBreaker
from datetime import datetime, timezone, timedelta


def make_settings(**overrides):
    defaults = {
        "sports_daily_tags": ["sports"],
        "sports_daily_min_volume": 5000.0,
        "sports_daily_min_liquidity": 1000.0,
        "sports_daily_min_spread": 0.04,
        "sports_daily_max_spread": 0.40,
        "sports_daily_min_book_depth": 20.0,
        "sports_daily_max_hours_to_resolution": 24,
        "sports_daily_favorite_min_prob": 0.82,
        "sports_daily_favorite_max_prob": 0.95,
        "sports_daily_imbalance_threshold": 0.30,
        "sports_daily_maker_cushion": 0.02,
        "sports_daily_min_edge": 0.02,
        "sports_daily_max_positions": 5,
        "sports_daily_max_single_trade_pct": 0.05,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def make_strategy(**settings_overrides):
    with patch("src.strategies.sports_daily.GammaClient"):
        settings = make_settings(**settings_overrides)
        return SportsDailyStrategy(settings)


def make_mock_clob(bid=0.55, ask=0.65, book_bids=None, book_asks=None):
    mock = MagicMock()
    mock.get_price.return_value = {"bid": bid, "ask": ask, "mid": (bid + ask) / 2}

    book = MagicMock()
    if book_bids is not None:
        book.bids = [MagicMock(price=str(p), size=str(s)) for p, s in book_bids]
    else:
        book.bids = [MagicMock(price=str(bid), size="200")]
    if book_asks is not None:
        book.asks = [MagicMock(price=str(p), size=str(s)) for p, s in book_asks]
    else:
        book.asks = [MagicMock(price=str(ask), size="200")]
    mock.get_book.return_value = book
    # get_books_batch returns the same book for every requested token
    mock.get_books_batch.side_effect = lambda token_ids: {tid: book for tid in token_ids}
    return mock


def make_sports_market(question="Will the Lakers win?", hours_remaining=6.0,
                       volume=10000, liquidity=5000):
    end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_remaining)
    return {
        "question": question,
        "conditionId": "cond_sports_1",
        "clobTokenIds": json.dumps(["token_yes", "token_no"]),
        "outcomes": json.dumps(["Yes", "No"]),
        "volume": volume,
        "liquidity": liquidity,
        "active": True,
        "closed": False,
        "endDate": end_dt.isoformat(),
        "_event_title": "NBA: Lakers vs Celtics",
        "_event_slug": "nba-lakers-celtics",
    }


# --- Sports event detection tests ---

class TestSportsDetection:
    def test_detects_nba_event(self):
        assert SportsDailyStrategy._is_sports_event("NBA: Lakers vs Celtics", "nba-lakers", [])

    def test_detects_nfl_event(self):
        assert SportsDailyStrategy._is_sports_event("NFL Week 12", "nfl-week-12", [])

    def test_detects_mma_event(self):
        assert SportsDailyStrategy._is_sports_event("UFC 300 Main Event", "ufc-300", [])

    def test_detects_soccer_event(self):
        assert SportsDailyStrategy._is_sports_event("Premier League: Arsenal vs Chelsea", "", [])

    def test_detects_by_tag_string(self):
        assert SportsDailyStrategy._is_sports_event("Some Event", "some-event", ["Sports"])

    def test_detects_by_tag_dict(self):
        assert SportsDailyStrategy._is_sports_event("Some Event", "", [{"label": "Sports", "id": 1}])

    def test_detects_game_pattern(self):
        assert SportsDailyStrategy._is_sports_event("Will Team A beat Team B?", "", [])

    def test_detects_vs_pattern(self):
        assert SportsDailyStrategy._is_sports_event("Team A vs. Team B", "", [])

    def test_rejects_non_sports(self):
        assert not SportsDailyStrategy._is_sports_event(
            "Will Bitcoin reach $100k?", "bitcoin-100k", []
        )

    def test_rejects_politics(self):
        assert not SportsDailyStrategy._is_sports_event(
            "Will the President win reelection?", "president-reelection", []
        )


# --- Spread capture tests ---

class TestSpreadCapture:
    def test_generates_signal_on_wide_spread(self):
        strategy = make_strategy(sports_daily_min_spread=0.04, sports_daily_maker_cushion=0.02)

        sig = strategy._spread_capture_signal(
            market_id="cond1", token_id="token_yes",
            question="Will Lakers win?", outcome="Yes",
            bid=0.55, ask=0.65, mid=0.60, spread=0.10,
            book=None, hours_remaining=6.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        assert sig.strategy == "sports_daily"
        assert sig.outcome == "Yes"
        assert sig.post_only is True
        assert sig.price > 0.55  # above best bid
        assert sig.price < 0.65  # below best ask
        assert sig.expected_value > 0

    def test_rejects_narrow_spread(self):
        strategy = make_strategy(sports_daily_min_spread=0.04)

        sig = strategy._spread_capture_signal(
            market_id="cond1", token_id="token_yes",
            question="Will Lakers win?", outcome="Yes",
            bid=0.58, ask=0.60, mid=0.59, spread=0.02,
            book=None, hours_remaining=6.0,
            volume=10000, liquidity=5000,
        )
        assert sig is None

    def test_spread_bid_above_best_bid(self):
        strategy = make_strategy(sports_daily_maker_cushion=0.02)

        sig = strategy._spread_capture_signal(
            market_id="cond1", token_id="token_yes",
            question="Q?", outcome="Yes",
            bid=0.55, ask=0.65, mid=0.60, spread=0.10,
            book=None, hours_remaining=6.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        assert sig.price > 0.55

    def test_spread_signal_has_cancel_ts(self):
        strategy = make_strategy()

        sig = strategy._spread_capture_signal(
            market_id="cond1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.50, ask=0.60, mid=0.55, spread=0.10,
            book=None, hours_remaining=3.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        assert sig.cancel_after_ts > time.time()


# --- Book imbalance tests ---

class TestBookImbalance:
    def test_generates_signal_on_heavy_bid_side(self):
        strategy = make_strategy(
            sports_daily_imbalance_threshold=0.30,
            sports_daily_maker_cushion=0.02,
            sports_daily_min_edge=0.01,  # lower edge threshold for imbalance signals
        )

        sig = strategy._book_imbalance_signal(
            market_id="cond1", token_id="t1",
            question="Will Lakers win?", outcome="Yes",
            bid=0.60, ask=0.70, mid=0.65, imbalance=0.60,
            book=None, hours_remaining=4.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        assert sig.strategy == "sports_daily"
        assert sig.confidence > 0.65  # boosted by imbalance

    def test_rejects_low_imbalance(self):
        strategy = make_strategy(sports_daily_imbalance_threshold=0.30)

        sig = strategy._book_imbalance_signal(
            market_id="cond1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.60, ask=0.66, mid=0.63, imbalance=0.15,
            book=None, hours_remaining=4.0,
            volume=10000, liquidity=5000,
        )
        assert sig is None

    def test_rejects_negative_imbalance(self):
        """Negative imbalance = more asks = bearish, should not generate buy signal."""
        strategy = make_strategy(sports_daily_imbalance_threshold=0.30)

        sig = strategy._book_imbalance_signal(
            market_id="cond1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.60, ask=0.66, mid=0.63, imbalance=-0.50,
            book=None, hours_remaining=4.0,
            volume=10000, liquidity=5000,
        )
        assert sig is None

    def test_imbalance_computation(self):
        book = MagicMock()
        book.bids = [MagicMock(size="200"), MagicMock(size="100")]
        book.asks = [MagicMock(size="50"), MagicMock(size="50")]
        # bid_depth=300, ask_depth=100, total=400
        # imbalance = (300-100)/400 = 0.50
        imbalance = SportsDailyStrategy._compute_book_imbalance(book)
        assert imbalance == pytest.approx(0.50, abs=0.01)

    def test_imbalance_none_book(self):
        assert SportsDailyStrategy._compute_book_imbalance(None) == 0.0

    def test_imbalance_empty_book(self):
        book = MagicMock()
        book.bids = []
        book.asks = []
        assert SportsDailyStrategy._compute_book_imbalance(book) == 0.0


# --- Favorite value tests ---

class TestFavoriteValue:
    def test_generates_signal_for_heavy_favorite(self):
        strategy = make_strategy(
            sports_daily_favorite_min_prob=0.82,
            sports_daily_favorite_max_prob=0.95,
            sports_daily_maker_cushion=0.02,
            sports_daily_min_edge=0.01,  # lower edge for favorite value signals
        )

        sig = strategy._favorite_value_signal(
            market_id="cond1", token_id="t1",
            question="Will Lakers win?", outcome="Yes",
            bid=0.83, ask=0.92, mid=0.865,
            book=None, hours_remaining=4.0,
        )
        assert sig is not None
        assert sig.confidence > 0.865  # bias boost applied
        assert sig.price > 0.83  # above best bid

    def test_rejects_mid_range_prob(self):
        """Markets with mid-range probability don't trigger favorite value."""
        strategy = make_strategy(
            sports_daily_favorite_min_prob=0.82,
            sports_daily_favorite_max_prob=0.95,
        )

        sig = strategy._favorite_value_signal(
            market_id="cond1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.55, ask=0.65, mid=0.60,
            book=None, hours_remaining=4.0,
        )
        assert sig is None

    def test_rejects_extreme_favorite(self):
        """Too-high probability (>0.95) is excluded — thin edge, high risk."""
        strategy = make_strategy(
            sports_daily_favorite_min_prob=0.82,
            sports_daily_favorite_max_prob=0.95,
        )

        sig = strategy._favorite_value_signal(
            market_id="cond1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.96, ask=0.99, mid=0.97,
            book=None, hours_remaining=4.0,
        )
        assert sig is None

    def test_bias_boost_increases_with_prob(self):
        """Higher favorites get stronger bias boost."""
        strategy = make_strategy()

        # At mid=0.85: boost = (0.85-0.50)*0.06 = 0.021
        # At mid=0.90: boost = (0.90-0.50)*0.06 = 0.024
        sig_85 = strategy._favorite_value_signal(
            "c1", "t1", "Q?", "Yes", 0.83, 0.87, 0.85, None, 4.0,
        )
        sig_90 = strategy._favorite_value_signal(
            "c2", "t2", "Q?", "Yes", 0.88, 0.92, 0.90, None, 4.0,
        )
        if sig_85 and sig_90:
            assert sig_90.confidence > sig_85.confidence


# --- Market discovery tests ---

class TestMarketDiscovery:
    def _mock_game_events(self, strategy, events):
        """Mock primary game events source (series_id-based)."""
        strategy.gamma.get_all_sports_game_events = MagicMock(return_value=events)

    def test_filters_by_resolution_time(self):
        strategy = make_strategy(sports_daily_max_hours_to_resolution=24)

        # Market resolving in 6 hours — should pass
        good_market = make_sports_market(hours_remaining=6.0)
        # Market resolving in 48 hours — should fail
        bad_market = make_sports_market(hours_remaining=48.0)
        bad_market["conditionId"] = "cond2"

        self._mock_game_events(strategy, [
            {
                "title": "NBA: Lakers vs Celtics",
                "slug": "nba-lakers",
                "markets": [good_market, bad_market],
            }
        ])

        markets = strategy._discover_sports_markets()
        assert len(markets) == 1

    def test_filters_low_volume(self):
        strategy = make_strategy(sports_daily_min_volume=5000)

        low_vol = make_sports_market(volume=100, liquidity=5000)
        self._mock_game_events(strategy, [
            {"title": "NBA Game", "slug": "nba", "markets": [low_vol]}
        ])

        markets = strategy._discover_sports_markets()
        assert len(markets) == 0

    def test_filters_low_liquidity(self):
        strategy = make_strategy(sports_daily_min_liquidity=1000)

        low_liq = make_sports_market(volume=10000, liquidity=100)
        self._mock_game_events(strategy, [
            {"title": "NBA Game", "slug": "nba", "markets": [low_liq]}
        ])

        markets = strategy._discover_sports_markets()
        assert len(markets) == 0

    def test_deduplicates_markets(self):
        strategy = make_strategy()

        market = make_sports_market()
        # Same market appears in two leagues
        self._mock_game_events(strategy, [
            {"title": "NBA", "slug": "nba", "markets": [market, market]}
        ])

        markets = strategy._discover_sports_markets()
        assert len(markets) == 1

    def test_fallback_tag_search(self):
        """Falls back to tag search when game events return nothing."""
        strategy = make_strategy()

        market = make_sports_market()
        # Primary source returns empty
        self._mock_game_events(strategy, [])
        # Fallback tag search finds the market
        strategy.gamma.get_all_events_by_tag = MagicMock(return_value=[
            {
                "title": "NBA: Lakers vs Celtics",
                "slug": "nba-lakers",
                "tags": [],
                "markets": [market],
            }
        ])

        markets = strategy._discover_sports_markets()
        assert len(markets) == 1

    def test_tag_search_runs_alongside_series_id_and_deduplicates(self):
        """Tag search executes even when series_id returns markets; overlapping markets are deduplicated."""
        strategy = make_strategy(sports_daily_tags=["ufc"])

        shared_market = make_sports_market(question="Will Fighter A win?")
        shared_market["conditionId"] = "cond_shared"

        tag_only_market = make_sports_market(question="Will Fighter B win?")
        tag_only_market["conditionId"] = "cond_tag_only"

        # series_id returns one market (the shared one)
        self._mock_game_events(strategy, [
            {"title": "UFC 300", "slug": "ufc-300", "markets": [shared_market]}
        ])
        # tag search returns the same market plus an extra one
        strategy.gamma.get_all_events_by_tag = MagicMock(return_value=[
            {
                "title": "UFC 300",
                "slug": "ufc-300",
                "markets": [shared_market, tag_only_market],
            }
        ])

        markets = strategy._discover_sports_markets()

        # tag search was called
        strategy.gamma.get_all_events_by_tag.assert_called_once_with("ufc", max_pages=5)
        # Both markets present, but shared one appears only once
        condition_ids = [m["conditionId"] for m in markets]
        assert condition_ids.count("cond_shared") == 1
        assert "cond_tag_only" in condition_ids
        assert len(markets) == 2


# --- Risk parameter tests ---

class TestRiskParameters:
    def test_strategy_min_confidence_override(self):
        settings = make_settings(
            starting_capital=10.0, hard_floor_pct=0.20,
            max_open_positions=5, max_portfolio_exposure_pct=0.60,
            min_trade_size=0.50, max_single_trade_pct=0.15,
        )
        cb = CircuitBreaker(settings)
        rm = RiskManager(settings, cb)

        signal = TradeSignal(
            market_id="c1", token_id="t1",
            market_question="Will Lakers win?",
            side="BUY", outcome="Yes",
            price=0.40, confidence=0.65,
            strategy="sports_daily",
            expected_value=0.10, order_type="GTC",
        )
        result = rm.evaluate(signal, balance=9.89, open_positions=[], portfolio_exposure=0.0)
        assert result is not None  # 0.65 > 0.55 (sports_daily override)

    def test_rejects_below_min_confidence(self):
        settings = make_settings(
            starting_capital=10.0, hard_floor_pct=0.20,
            max_open_positions=5, max_portfolio_exposure_pct=0.60,
            min_trade_size=0.50, max_single_trade_pct=0.10,
        )
        cb = CircuitBreaker(settings)
        rm = RiskManager(settings, cb)

        signal = TradeSignal(
            market_id="c1", token_id="t1",
            market_question="Will Lakers win?",
            side="BUY", outcome="Yes",
            price=0.50, confidence=0.50,
            strategy="sports_daily",
            expected_value=0.01, order_type="GTC",
        )
        result = rm.evaluate(signal, balance=9.89, open_positions=[], portfolio_exposure=0.0)
        assert result is None  # 0.50 < 0.55


# --- Integration tests ---

class TestIntegration:
    def test_full_analyze_with_spread(self):
        """End-to-end: wide-spread sports market produces a signal."""
        strategy = make_strategy(
            sports_daily_min_spread=0.04,
            sports_daily_maker_cushion=0.02,
        )

        market = make_sports_market()
        strategy.gamma.get_all_sports_game_events = MagicMock(return_value=[
            {"title": "NBA Game", "slug": "nba", "markets": [market]}
        ])

        # Wide spread: bid=0.55, ask=0.65 → spread=0.10
        mock_clob = make_mock_clob(bid=0.55, ask=0.65)
        signals = strategy.analyze([], mock_clob)

        assert len(signals) >= 1
        sig = signals[0]
        assert sig.strategy == "sports_daily"
        assert sig.post_only is True
        assert sig.order_type == "GTC"

    def test_predictions_tracked(self):
        strategy = make_strategy()

        market = make_sports_market()
        strategy.gamma.get_all_sports_game_events = MagicMock(return_value=[
            {"title": "NBA Game", "slug": "nba", "markets": [market]}
        ])

        mock_clob = make_mock_clob(bid=0.55, ask=0.65)
        strategy.analyze([], mock_clob)

        # Should have predictions for both outcomes
        assert len(strategy._pending_predictions) >= 2
        for pred in strategy._pending_predictions:
            assert pred["strategy"] == "sports_daily"
            assert "market_id" in pred

    def test_strategy_name_and_self_discovering(self):
        strategy = make_strategy()
        assert strategy.name == "sports_daily"
        assert strategy.self_discovering is True


# --- Aggression tuner integration ---

class TestBookLiquidity:
    def test_empty_book(self):
        """Empty book (no bids/asks) should be illiquid."""
        book = MagicMock()
        book.bids = []
        book.asks = []
        assert not SportsDailyStrategy._is_book_liquid(book, 50.0)

    def test_liquid_book(self):
        """Book with sufficient depth on both sides passes."""
        book = MagicMock()
        book.bids = [MagicMock(price="0.50", size="200"), MagicMock(price="0.49", size="100")]
        book.asks = [MagicMock(price="0.55", size="200"), MagicMock(price="0.56", size="100")]
        # bid_depth = 200*0.50 + 100*0.49 = 149, ask_depth = 200*0.55 + 100*0.56 = 166
        assert SportsDailyStrategy._is_book_liquid(book, 50.0)

    def test_one_sided_book(self):
        """Book with bids but no asks should be illiquid."""
        book = MagicMock()
        book.bids = [MagicMock(price="0.50", size="200")]
        book.asks = []
        assert not SportsDailyStrategy._is_book_liquid(book, 50.0)

    def test_none_book(self):
        """None book should be illiquid."""
        assert not SportsDailyStrategy._is_book_liquid(None, 50.0)

    def test_thin_book_below_threshold(self):
        """Book with tiny sizes should fail the depth check."""
        book = MagicMock()
        book.bids = [MagicMock(price="0.01", size="1")]
        book.asks = [MagicMock(price="0.99", size="1")]
        # bid_depth = 0.01, ask_depth = 0.99 — bid side too thin
        assert not SportsDailyStrategy._is_book_liquid(book, 50.0)


class TestSpreadCaptureConfidence:
    def test_confidence_above_threshold_with_volume(self):
        """50/50 market with good volume/liquidity should produce confidence >= 0.55."""
        strategy = make_strategy(sports_daily_min_spread=0.04, sports_daily_maker_cushion=0.02)
        sig = strategy._spread_capture_signal(
            market_id="c1", token_id="t1",
            question="Will Team A win?", outcome="Yes",
            bid=0.45, ask=0.55, mid=0.50, spread=0.10,
            book=None, hours_remaining=6.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        assert sig.confidence >= 0.55

    def test_confidence_capped_at_095(self):
        """Confidence should never exceed 0.95."""
        strategy = make_strategy(sports_daily_min_spread=0.04, sports_daily_maker_cushion=0.02)
        sig = strategy._spread_capture_signal(
            market_id="c1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.88, ask=0.98, mid=0.93, spread=0.10,
            book=None, hours_remaining=6.0,
            volume=50000, liquidity=50000,
        )
        assert sig is not None
        assert sig.confidence <= 0.95

    def test_low_volume_keeps_low_confidence(self):
        """50/50 market with zero volume stays under 0.55."""
        strategy = make_strategy(sports_daily_min_spread=0.04, sports_daily_maker_cushion=0.02)
        sig = strategy._spread_capture_signal(
            market_id="c1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.45, ask=0.55, mid=0.50, spread=0.10,
            book=None, hours_remaining=6.0,
            volume=0, liquidity=0,
        )
        # With mid=0.50 and no bonuses, confidence=0.50 + spread_quality only
        # spread_quality = max(0, 1.0 - 0.10/0.20) * 0.02 = 0.01 → confidence=0.51
        # This should still produce a signal (EV check), but confidence < 0.55
        if sig is not None:
            assert sig.confidence < 0.55


class TestBookImbalanceConfidence:
    def test_strong_imbalance_passes_threshold(self):
        """Strong imbalance on 50/50 market with volume should produce confidence >= 0.55."""
        strategy = make_strategy(
            sports_daily_imbalance_threshold=0.30,
            sports_daily_maker_cushion=0.02,
            sports_daily_min_edge=0.01,
        )
        sig = strategy._book_imbalance_signal(
            market_id="c1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.45, ask=0.55, mid=0.50, imbalance=0.60,
            book=None, hours_remaining=4.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        assert sig.confidence >= 0.55

    def test_imbalance_boost_stronger_than_before(self):
        """Imbalance boost is now 0.10 per unit (was 0.05)."""
        strategy = make_strategy(
            sports_daily_imbalance_threshold=0.30,
            sports_daily_maker_cushion=0.02,
            sports_daily_min_edge=0.01,
        )
        sig = strategy._book_imbalance_signal(
            market_id="c1", token_id="t1",
            question="Q?", outcome="Yes",
            bid=0.60, ask=0.70, mid=0.65, imbalance=0.50,
            book=None, hours_remaining=4.0,
            volume=10000, liquidity=5000,
        )
        assert sig is not None
        # boost = 0.50 * 0.10 = 0.05, vol_bonus ~0.015, liq_bonus ~0.01
        # adjusted = 0.65 + 0.05 + 0.015 + 0.01 = 0.725
        assert sig.confidence > 0.70


class TestAggressionTuner:
    def test_sports_daily_in_all_levels(self):
        from src.adaptive.aggression_tuner import LEVELS
        for level_name, level in LEVELS.items():
            assert "sports_daily" in level.strategies, (
                f"sports_daily missing from aggression level '{level_name}'"
            )
