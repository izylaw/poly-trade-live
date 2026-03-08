import logging
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, BookParams, OrderArgs, OrderType
from src.config.settings import Settings
from src.utils.retry import retry

logger = logging.getLogger("poly-trade")

CLOB_API_URL = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon mainnet


class PolymarketClobClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client: ClobClient | None = None

    def _get_client(self) -> ClobClient:
        if self._client is None:
            creds = ApiCreds(
                api_key=self.settings.poly_api_key,
                api_secret=self.settings.poly_api_secret,
                api_passphrase=self.settings.poly_api_passphrase,
            )
            self._client = ClobClient(
                CLOB_API_URL,
                key=self.settings.poly_private_key,
                chain_id=CHAIN_ID,
                creds=creds,
                signature_type=0,
            )
            logger.info("CLOB client initialized")
        return self._client

    @retry(max_attempts=3)
    def get_balance(self) -> float:
        client = self._get_client()
        result = client.get_balance_allowance(
            BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        )
        raw = float(result.get("balance", 0)) if isinstance(result, dict) else 0.0
        return raw / 1e6  # USDC has 6 decimals

    def get_orderbook(self, token_id: str):
        client = self._get_client()
        return client.get_order_book(token_id)

    def get_book(self, token_id: str):
        """Returns the raw OrderBookSummary object (bids, asks)."""
        try:
            client = self._get_client()
            return client.get_order_book(token_id)
        except Exception as e:
            if "404" in str(e):
                return None
            raise

    def get_price(self, token_id: str) -> dict | None:
        """Returns price dict or None if no orderbook exists."""
        try:
            client = self._get_client()
            book = client.get_order_book(token_id)
        except Exception as e:
            if "404" in str(e):
                return None
            raise
        best_bid = float(book.bids[0].price) if book.bids else 0.0
        best_ask = float(book.asks[0].price) if book.asks else 1.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask != 1.0 else best_bid or best_ask
        return {"bid": best_bid, "ask": best_ask, "mid": mid}

    def get_orderbooks_batch(self, token_ids: list[str], chunk_size: int = 50) -> dict[str, dict | None]:
        """Fetch orderbooks for many token IDs in batched POST requests.

        Returns dict mapping token_id -> {"bid", "ask", "mid"} or None.
        """
        client = self._get_client()
        result: dict[str, dict | None] = {}

        for i in range(0, len(token_ids), chunk_size):
            chunk = token_ids[i : i + chunk_size]
            try:
                params = [BookParams(token_id=tid) for tid in chunk]
                books = client.get_order_books(params)
                for book in books:
                    tid = book.asset_id
                    if tid is None:
                        continue
                    best_bid = float(book.bids[0].price) if book.bids else 0.0
                    best_ask = float(book.asks[0].price) if book.asks else 1.0
                    mid = (best_bid + best_ask) / 2 if best_bid and best_ask != 1.0 else best_bid or best_ask
                    result[tid] = {"bid": best_bid, "ask": best_ask, "mid": mid}
            except Exception as e:
                logger.warning(f"Batch orderbook chunk failed ({len(chunk)} tokens): {e}")

        logger.info(
            f"Batch orderbooks: {len(result)}/{len(token_ids)} tokens returned data"
        )
        return result

    def get_books_batch(self, token_ids: list[str], chunk_size: int = 50) -> dict:
        """Fetch orderbooks for many token IDs, returning full OrderBookSummary objects.

        Returns dict mapping token_id -> OrderBookSummary or None.
        """
        client = self._get_client()
        result = {}

        for i in range(0, len(token_ids), chunk_size):
            chunk = token_ids[i : i + chunk_size]
            try:
                params = [BookParams(token_id=tid) for tid in chunk]
                books = client.get_order_books(params)
                for book in books:
                    tid = book.asset_id
                    if tid is not None:
                        result[tid] = book
            except Exception as e:
                logger.warning(f"Batch book fetch failed ({len(chunk)} tokens): {e}")

        logger.info(f"Batch books: {len(result)}/{len(token_ids)} tokens returned data")
        return result

    @staticmethod
    def extract_price(book) -> dict | None:
        """Extract {bid, ask, mid} from an OrderBookSummary object."""
        if book is None:
            return None
        best_bid = float(book.bids[0].price) if book.bids else 0.0
        best_ask = float(book.asks[0].price) if book.asks else 1.0
        mid = (best_bid + best_ask) / 2 if best_bid and best_ask != 1.0 else best_bid or best_ask
        return {"bid": best_bid, "ask": best_ask, "mid": mid}

    def get_midpoint(self, token_id: str) -> float | None:
        price = self.get_price(token_id)
        return price["mid"] if price else None

    @retry(max_attempts=2)
    def post_order(self, token_id: str, side: str, price: float, size: float,
                   order_type: str = "GTC", post_only: bool = False) -> dict:
        client = self._get_client()
        ot = OrderType.FOK if order_type == "FOK" else OrderType.GTC
        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed = client.create_order(order_args)
        result = client.post_order(signed, ot, post_only=post_only)
        logger.info(f"Order posted: {side} {size}@{price} on {token_id[:16]}... post_only={post_only}")
        return result

    @retry(max_attempts=2)
    def get_order(self, order_id: str) -> dict | None:
        client = self._get_client()
        try:
            return client.get_order(order_id)
        except Exception:
            return None

    @retry(max_attempts=2)
    def cancel_order(self, order_id: str) -> dict:
        client = self._get_client()
        result = client.cancel(order_id)
        logger.info(f"Order cancelled: {order_id}")
        return result

    @retry(max_attempts=3)
    def get_open_orders(self) -> list:
        client = self._get_client()
        return client.get_orders()

    @retry(max_attempts=3)
    def get_all_tradeable_condition_ids(self) -> set[str]:
        """Fetch all tradeable condition IDs from the CLOB simplified-markets endpoint.

        Public endpoint, no auth required. Uses cursor pagination.
        Only collects markets that are open and accepting orders.
        """
        condition_ids: set[str] = set()
        cursor = "MA=="
        while True:
            resp = requests.get(
                f"{CLOB_API_URL}/simplified-markets",
                params={"next_cursor": cursor},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            for market in data.get("data", []):
                if market.get("closed") or not market.get("accepting_orders"):
                    continue
                cid = market.get("condition_id", "")
                if cid:
                    condition_ids.add(cid)
            cursor = data.get("next_cursor", "")
            if not cursor or cursor == "LTE=":
                break
        logger.info(f"CLOB tradeability index: {len(condition_ids)} tradeable markets")
        return condition_ids

    def derive_api_creds(self) -> ApiCreds:
        client = ClobClient(
            CLOB_API_URL,
            key=self.settings.poly_private_key,
            chain_id=CHAIN_ID,
        )
        creds = client.create_or_derive_api_creds()
        logger.info("API credentials derived successfully")
        return creds
