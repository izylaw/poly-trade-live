import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, AssetType, BalanceAllowanceParams, BookParams, OrderArgs, OrderType
from web3 import Web3
from eth_account import Account
from src.config.settings import Settings
from src.utils.retry import retry

CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_POLYGON = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# py-clob-client uses a module-level httpx.Client with no timeout, which can
# hang indefinitely on CLOB API slowness.  Patch it with a 30s timeout.
try:
    import py_clob_client.http_helpers.helpers as _clob_helpers
    import httpx as _httpx
    _clob_helpers._http_client = _httpx.Client(http2=True, timeout=30.0)
except Exception:
    pass

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
        """Fetch orderbooks for many token IDs in parallel batched POST requests.

        Returns dict mapping token_id -> {"bid", "ask", "mid"} or None.
        """
        import time as _time

        client = self._get_client()
        result: dict[str, dict | None] = {}
        chunks = [token_ids[i : i + chunk_size] for i in range(0, len(token_ids), chunk_size)]

        def _fetch_chunk(chunk: list[str]) -> dict[str, dict]:
            chunk_result = {}
            params = [BookParams(token_id=tid) for tid in chunk]
            books = client.get_order_books(params)
            for book in books:
                tid = book.asset_id
                if tid is None:
                    continue
                best_bid = float(book.bids[0].price) if book.bids else 0.0
                best_ask = float(book.asks[0].price) if book.asks else 1.0
                mid = (best_bid + best_ask) / 2 if best_bid and best_ask != 1.0 else best_bid or best_ask
                chunk_result[tid] = {"bid": best_bid, "ask": best_ask, "mid": mid}
            return chunk_result

        def _fetch_chunk_with_retry(chunk: list[str], max_attempts: int = 3) -> dict[str, dict]:
            for attempt in range(max_attempts):
                try:
                    return _fetch_chunk(chunk)
                except Exception as e:
                    if attempt < max_attempts - 1:
                        delay = 1.0 * (2 ** attempt)
                        logger.warning(
                            f"Batch orderbook chunk ({len(chunk)} tokens) attempt {attempt + 1} "
                            f"failed: {e}. Retrying in {delay}s"
                        )
                        _time.sleep(delay)
                    else:
                        raise

        if len(chunks) <= 1:
            # Single chunk — no threading overhead
            if chunks:
                try:
                    result = _fetch_chunk_with_retry(chunks[0])
                except Exception as e:
                    logger.warning(f"Batch orderbook chunk failed ({len(chunks[0])} tokens): {e}")
        else:
            workers = min(len(chunks), 6)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_chunk_with_retry, chunk): chunk for chunk in chunks}
                for future in as_completed(futures):
                    chunk = futures[future]
                    try:
                        result.update(future.result())
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
        chunks = [token_ids[i : i + chunk_size] for i in range(0, len(token_ids), chunk_size)]

        def _fetch_chunk(chunk):
            chunk_result = {}
            params = [BookParams(token_id=tid) for tid in chunk]
            books = client.get_order_books(params)
            for book in books:
                tid = book.asset_id
                if tid is not None:
                    chunk_result[tid] = book
            return chunk_result

        if len(chunks) <= 1:
            if chunks:
                try:
                    result = _fetch_chunk(chunks[0])
                except Exception as e:
                    logger.warning(f"Batch book fetch failed ({len(chunks[0])} tokens): {e}")
        else:
            workers = min(len(chunks), 6)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(_fetch_chunk, chunk): chunk for chunk in chunks}
                for future in as_completed(futures):
                    chunk = futures[future]
                    try:
                        result.update(future.result())
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

    def redeem_positions(self, condition_id: str) -> str | None:
        """Redeem resolved CTF positions on-chain. Returns tx hash or None on failure."""
        try:
            w3 = Web3(Web3.HTTPProvider(self.settings.polygon_rpc_url))
            acct = Account.from_key(self.settings.poly_private_key)
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI
            )
            cid_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_POLYGON),
                b"\x00" * 32,  # parent collection (root)
                cid_bytes,
                [1, 2],  # both outcome index sets for binary markets
            ).build_transaction({
                "from": acct.address,
                "nonce": w3.eth.get_transaction_count(acct.address),
                "gas": 150000,
                "gasPrice": int(w3.eth.gas_price * 1.2),
                "chainId": CHAIN_ID,
            })
            signed = acct.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            if receipt.status == 1:
                logger.info(f"REDEEMED condition={condition_id[:16]}... tx={tx_hash.hex()}")
                return tx_hash.hex()
            else:
                logger.error(f"Redeem tx failed: {tx_hash.hex()}")
                return None
        except Exception as e:
            logger.error(f"Redeem failed for {condition_id[:16]}...: {e}")
            return None

    def derive_api_creds(self) -> ApiCreds:
        client = ClobClient(
            CLOB_API_URL,
            key=self.settings.poly_private_key,
            chain_id=CHAIN_ID,
        )
        creds = client.create_or_derive_api_creds()
        logger.info("API credentials derived successfully")
        return creds
