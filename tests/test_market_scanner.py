import unittest
from unittest.mock import MagicMock, patch
from src.config.settings import Settings
from src.market_data.gamma_client import GammaClient
from src.market_data.market_filter import MarketFilter
from src.market_data.market_scanner import MarketScanner, normalize_market


def _make_event(title, slug, markets, tags=None):
    return {
        "title": title,
        "slug": slug,
        "tags": tags or [],
        "markets": markets,
    }


def _make_market(cid, question="Q?", volume=5000, liquidity=2000):
    return {
        "conditionId": cid,
        "question": question,
        "clobTokenIds": '["tok_yes","tok_no"]',
        "outcomes": '["Yes","No"]',
        "volume": volume,
        "liquidity": liquidity,
    }


class TestExtractMarketsFromEvents(unittest.TestCase):
    def test_flattens_events(self):
        events = [
            _make_event("E1", "e1", [_make_market("c1"), _make_market("c2")]),
            _make_event("E2", "e2", [_make_market("c3")]),
        ]
        markets = GammaClient.extract_markets_from_events(events)
        self.assertEqual(len(markets), 3)
        self.assertEqual(markets[0]["_event_title"], "E1")
        self.assertEqual(markets[0]["_event_slug"], "e1")
        self.assertEqual(markets[2]["_event_title"], "E2")

    def test_attaches_tags(self):
        events = [_make_event("E1", "e1", [_make_market("c1")], tags=["sports"])]
        markets = GammaClient.extract_markets_from_events(events)
        self.assertEqual(markets[0]["_event_tags"], ["sports"])

    def test_empty_events(self):
        markets = GammaClient.extract_markets_from_events([])
        self.assertEqual(markets, [])

    def test_events_with_no_markets(self):
        events = [_make_event("E1", "e1", [])]
        markets = GammaClient.extract_markets_from_events(events)
        self.assertEqual(markets, [])


class TestNormalizeMarket(unittest.TestCase):
    def test_parses_json_strings(self):
        m = {"clobTokenIds": '["a","b"]', "outcomes": '["Yes","No"]'}
        result = normalize_market(m)
        self.assertEqual(result["clobTokenIds"], ["a", "b"])
        self.assertEqual(result["outcomes"], ["Yes", "No"])

    def test_already_lists(self):
        m = {"clobTokenIds": ["a", "b"], "outcomes": ["Yes", "No"]}
        result = normalize_market(m)
        self.assertEqual(result["clobTokenIds"], ["a", "b"])


class TestMarketScannerScan(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            scanner_max_event_pages=2,
            scanner_clob_cross_ref=False,
            max_markets=500,
        )
        self.gamma = MagicMock(spec=GammaClient)
        self.market_filter = MagicMock(spec=MarketFilter)

    def test_scan_uses_events_pipeline(self):
        events = [
            _make_event("E1", "e1", [_make_market("c1")]),
            _make_event("E2", "e2", [_make_market("c2")]),
        ]
        self.gamma.get_all_active_events.return_value = events
        # extract_markets_from_events is a static method so we mock it on the class
        self.market_filter.filter_markets.side_effect = lambda x: x

        scanner = MarketScanner(self.settings, self.gamma, self.market_filter)
        result = scanner.scan()

        self.gamma.get_all_active_events.assert_called_once_with(max_pages=2)
        self.assertEqual(len(result), 2)

    def test_scan_passes_max_pages(self):
        self.gamma.get_all_active_events.return_value = []
        self.market_filter.filter_markets.return_value = []

        scanner = MarketScanner(self.settings, self.gamma, self.market_filter)
        scanner.scan()

        self.gamma.get_all_active_events.assert_called_once_with(max_pages=2)


class TestClobTradeabilityFilter(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            scanner_max_event_pages=1,
            scanner_clob_cross_ref=True,
            scanner_clob_ttl=300,
            max_markets=500,
        )
        self.gamma = MagicMock(spec=GammaClient)
        self.market_filter = MagicMock(spec=MarketFilter)
        self.clob = MagicMock()

    def test_filters_non_tradeable(self):
        events = [_make_event("E1", "e1", [
            _make_market("c1"),
            _make_market("c2"),
            _make_market("c3"),
        ])]
        self.gamma.get_all_active_events.return_value = events
        self.market_filter.filter_markets.side_effect = lambda x: x
        self.clob.get_all_tradeable_condition_ids.return_value = {"c1", "c3"}

        scanner = MarketScanner(self.settings, self.gamma, self.market_filter, clob_client=self.clob)
        result = scanner.scan()

        cids = [m["conditionId"] for m in result]
        self.assertIn("c1", cids)
        self.assertIn("c3", cids)
        self.assertNotIn("c2", cids)

    def test_graceful_fallback_on_clob_failure(self):
        events = [_make_event("E1", "e1", [_make_market("c1")])]
        self.gamma.get_all_active_events.return_value = events
        self.market_filter.filter_markets.side_effect = lambda x: x
        self.clob.get_all_tradeable_condition_ids.side_effect = Exception("network error")

        scanner = MarketScanner(self.settings, self.gamma, self.market_filter, clob_client=self.clob)
        result = scanner.scan()

        # Should still return markets when CLOB is down
        self.assertEqual(len(result), 1)

    def test_clob_filter_skipped_when_disabled(self):
        self.settings = Settings(
            scanner_max_event_pages=1,
            scanner_clob_cross_ref=False,
            max_markets=500,
        )
        events = [_make_event("E1", "e1", [_make_market("c1")])]
        self.gamma.get_all_active_events.return_value = events
        self.market_filter.filter_markets.side_effect = lambda x: x

        scanner = MarketScanner(self.settings, self.gamma, self.market_filter, clob_client=self.clob)
        result = scanner.scan()

        self.clob.get_all_tradeable_condition_ids.assert_not_called()
        self.assertEqual(len(result), 1)

    def test_clob_ids_cached(self):
        events = [_make_event("E1", "e1", [_make_market("c1")])]
        self.gamma.get_all_active_events.return_value = events
        self.market_filter.filter_markets.side_effect = lambda x: x
        self.clob.get_all_tradeable_condition_ids.return_value = {"c1"}

        scanner = MarketScanner(self.settings, self.gamma, self.market_filter, clob_client=self.clob)
        scanner.scan()
        scanner.scan()

        # Should only call CLOB once (cached)
        self.clob.get_all_tradeable_condition_ids.assert_called_once()


class TestClobClientTradeability(unittest.TestCase):
    @patch("src.market_data.clob_client.requests.get")
    def test_get_all_tradeable_condition_ids(self, mock_get):
        from src.market_data.clob_client import PolymarketClobClient

        page1 = MagicMock()
        page1.json.return_value = {
            "data": [
                {"condition_id": "c1", "accepting_orders": True, "closed": False},
                {"condition_id": "c2", "accepting_orders": True, "closed": False},
            ],
            "next_cursor": "abc",
        }
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.json.return_value = {
            "data": [{"condition_id": "c3", "accepting_orders": True, "closed": False}],
            "next_cursor": "LTE=",
        }
        page2.raise_for_status = MagicMock()

        mock_get.side_effect = [page1, page2]

        settings = Settings()
        client = PolymarketClobClient(settings)
        ids = client.get_all_tradeable_condition_ids()

        self.assertEqual(ids, {"c1", "c2", "c3"})
        self.assertEqual(mock_get.call_count, 2)

    @patch("src.market_data.clob_client.requests.get")
    def test_filters_closed_and_non_accepting(self, mock_get):
        from src.market_data.clob_client import PolymarketClobClient

        resp = MagicMock()
        resp.json.return_value = {
            "data": [
                {"condition_id": "c1", "accepting_orders": True, "closed": False},
                {"condition_id": "c2", "accepting_orders": False, "closed": False},
                {"condition_id": "c3", "accepting_orders": True, "closed": True},
            ],
            "next_cursor": "LTE=",
        }
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        settings = Settings()
        client = PolymarketClobClient(settings)
        ids = client.get_all_tradeable_condition_ids()

        self.assertEqual(ids, {"c1"})

    @patch("src.market_data.clob_client.requests.get")
    def test_empty_response(self, mock_get):
        from src.market_data.clob_client import PolymarketClobClient

        resp = MagicMock()
        resp.json.return_value = {"data": [], "next_cursor": "LTE="}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        settings = Settings()
        client = PolymarketClobClient(settings)
        ids = client.get_all_tradeable_condition_ids()

        self.assertEqual(ids, set())


class TestGammaGetAllActiveEvents(unittest.TestCase):
    @patch("src.market_data.gamma_client.requests.get")
    def test_paginates_until_empty(self, mock_get):
        # With parallel fetching, side_effect must be a function keyed on offset
        def _mock_response(*args, **kwargs):
            offset = kwargs.get("params", {}).get("offset", 0)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.status_code = 200
            if offset == 0:
                resp.json.return_value = [{"title": "E1", "markets": []}] * 100
            elif offset == 100:
                resp.json.return_value = [{"title": "E2", "markets": []}] * 50
            else:
                resp.json.return_value = []
            return resp

        mock_get.side_effect = _mock_response

        gamma = GammaClient()
        events = gamma.get_all_active_events(max_pages=5)

        self.assertEqual(len(events), 150)

    @patch("src.market_data.gamma_client.requests.get")
    def test_respects_max_pages(self, mock_get):
        page = MagicMock()
        page.json.return_value = [{"title": "E", "markets": []}] * 100
        page.raise_for_status = MagicMock()
        page.status_code = 200
        mock_get.return_value = page

        gamma = GammaClient()
        events = gamma.get_all_active_events(max_pages=2)

        self.assertEqual(len(events), 200)

    @patch("src.market_data.gamma_client.requests.get")
    def test_caches_result(self, mock_get):
        page = MagicMock()
        page.json.return_value = []
        page.raise_for_status = MagicMock()
        page.status_code = 200
        mock_get.return_value = page

        gamma = GammaClient()
        gamma.get_all_active_events(max_pages=1)
        gamma.get_all_active_events(max_pages=1)

        # Second call should be cached
        self.assertEqual(mock_get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
