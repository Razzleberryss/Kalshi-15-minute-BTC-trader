"""
kalshi_client.py - Thin wrapper around the Kalshi REST API v2.
Handles:
  - RSA-PSS request signing (required by Kalshi)
  - get_balance()
  - get_active_btc_market() -> finds the live 15-min BTC market
  - get_orderbook(ticker)
  - get_positions()
  - place_order(ticker, side, count, price_cents, dry_run)
  - sell_position(ticker, side, count, price_cents, dry_run)
  - cancel_order(order_id)
"""
import base64
import datetime
import logging
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

log = logging.getLogger(__name__)


class KalshiClient:
    """Authenticated HTTP client for Kalshi Trade API v2."""

    def __init__(self):
        self.base_url = config.BASE_URL
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._private_key = self._load_private_key(config.KALSHI_PRIVATE_KEY_PATH)
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        # Configure HTTP connection pooling for better performance
        # Pool connections to reduce TCP handshake overhead
        retry_strategy = Retry(
            total=0,  # We handle retries manually in _request()
            status_forcelist=[],
        )
        adapter = HTTPAdapter(
            pool_connections=10,  # Number of connection pools to cache
            pool_maxsize=20,      # Max connections in each pool
            max_retries=retry_strategy,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ── Auth helpers ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _load_private_key(path: str):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Return base64-encoded RSA-PSS signature.
        Kalshi signs: timestamp_ms + METHOD + /trade-api/v2/path
        The path passed in is already the full path e.g. /trade-api/v2/portfolio/balance
        """
        message = (timestamp_ms + method.upper() + path).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _auth_headers(self, method: str, path: str) -> dict:
        """
        path must be the full API path including /trade-api/v2 prefix,
        e.g. /trade-api/v2/portfolio/balance
        """
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
        }

    # ── Generic request ─────────────────────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, params: dict = None, json: dict = None) -> dict:
        """
        path is the short path, e.g. /portfolio/balance
        We prepend /trade-api/v2 for the signature; the full URL is built from
        config.BASE_URL (which already contains the /trade-api/v2 prefix) to
        avoid duplicating host-selection logic.
        Retries up to REQUEST_MAX_RETRIES times on transient errors (timeout,
        connection error, 5xx) with exponential backoff. 4xx errors are raised
        immediately without retrying.
        """
        full_path = "/trade-api/v2" + path   # for signing only; path must NOT include this prefix
        url = config.BASE_URL + path          # BASE_URL ends with /trade-api/v2, path starts with /
        headers = self._auth_headers(method, full_path)

        last_exc: Exception = RuntimeError("No attempts made")
        max_attempts = 1 + max(0, config.REQUEST_MAX_RETRIES)
        for attempt in range(max_attempts):
            try:
                resp = self.session.request(
                    method, url, headers=headers, params=params, json=json,
                    timeout=config.REQUEST_TIMEOUT_SECONDS,
                )
                if not resp.ok:
                    resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as exc:
                # Do not retry client errors (4xx); always retry server errors (5xx)
                status = exc.response.status_code if exc.response is not None else 0
                if status < 500:
                    log.error(
                        "Kalshi API client error %s %s -> %s: %s",
                        method, path, status,
                        exc.response.text if exc.response is not None else exc,
                    )
                    raise
                last_exc = exc
                log.warning(
                    "Server error %s %s status=%s (attempt %d/%d)",
                    method, path, status, attempt + 1, max_attempts,
                )
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                last_exc = exc
                log.warning(
                    "Transient error %s %s (attempt %d/%d): %s",
                    method, path, attempt + 1, max_attempts, exc,
                )

            if attempt < max_attempts - 1:
                backoff = 2 ** attempt
                log.info("Retrying in %ds...", backoff)
                time.sleep(backoff)

        log.error(
            "Kalshi API %s %s failed after %d attempt(s): %s",
            method, path, max_attempts, last_exc,
        )
        raise last_exc

    # ── Public API methods ────────────────────────────────────────────────────────────────────────
    def get_balance(self) -> float:
        """Return available balance in dollars."""
        data = self._request("GET", "/portfolio/balance")
        return data["balance"] / 100  # Kalshi returns cents

    def get_active_btc_market(self) -> Optional[dict]:
        """
        Find the currently open 15-minute BTC market.
        Returns the first active market dict or None.
        """
        params = {
            "series_ticker": config.BTC_SERIES_TICKER,
            "status": "open",
            "limit": 10,
        }
        data = self._request("GET", "/markets", params=params)
        markets = data.get("markets", [])
        if not markets:
            log.warning("No open BTC 15-min markets found for series %s", config.BTC_SERIES_TICKER)
            return None
        markets = [m for m in markets if self._is_btc_series_market(m.get("ticker", ""))]
        if not markets:
            log.warning("No open markets matched BTC series prefix %s-", config.BTC_SERIES_TICKER)
            return None
        markets.sort(key=lambda m: m.get("close_time", ""))
        return markets[0]

    def get_orderbook(self, ticker: str) -> dict:
        """Return the full orderbook dict for a market ticker."""
        return self._request("GET", f"/markets/{ticker}/orderbook")

    def get_market(self, ticker: str) -> dict:
        """Return market details including last_price, yes_ask, no_ask."""
        return self._request("GET", f"/markets/{ticker}")

    def get_market_quotes(self, ticker: str) -> dict:
        """
        Get best bid/ask quotes from the orderbook for a market.

        Returns a dict with:
            best_yes_bid: int | None - highest YES bid in cents (best buy price for YES)
            best_yes_ask: int | None - lowest YES ask in cents (best sell price for YES)
            best_no_bid: int | None - highest NO bid in cents (best buy price for NO)
            best_no_ask: int | None - lowest NO ask in cents (best sell price for NO)
            mid_price: int | None - midpoint of yes bid/ask in cents, or None if no quotes

        All prices are in cents (1-99 range). Returns None values if orderbook is empty.

        Note: Kalshi's orderbook contains bids only (no asks). To compute YES ask,
        we use: YES ask ≈ 100 - best_no_bid (since buying YES is equivalent to
        selling NO at the complementary price).
        """
        try:
            orderbook = self.get_orderbook(ticker)
            orderbook_data = orderbook.get("orderbook", {})
            yes_bids = orderbook_data.get("yes", [])  # List of [price_cents, size]
            no_bids = orderbook_data.get("no", [])

            # Extract best bids (highest price = index 0, as they're sorted descending)
            best_yes_bid = yes_bids[0][0] if yes_bids else None
            best_no_bid = no_bids[0][0] if no_bids else None

            # Compute asks from the complementary side
            # YES ask = what you pay to buy YES = 100 - (what NO buyers are willing to pay)
            # NO ask = what you pay to buy NO = 100 - (what YES buyers are willing to pay)
            best_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
            best_no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None

            # Compute mid price only if we have both yes bid and ask
            if best_yes_bid is not None and best_yes_ask is not None:
                mid_price = (best_yes_bid + best_yes_ask) // 2
            else:
                mid_price = None

            return {
                "best_yes_bid": best_yes_bid,
                "best_yes_ask": best_yes_ask,
                "best_no_bid": best_no_bid,
                "best_no_ask": best_no_ask,
                "mid_price": mid_price,
            }
        except Exception as exc:
            log.warning("Error fetching market quotes from orderbook for %s: %s", ticker, exc)
            return {
                "best_yes_bid": None,
                "best_yes_ask": None,
                "best_no_bid": None,
                "best_no_ask": None,
                "mid_price": None,
            }

    def get_positions(self) -> list:
        """Return list of current open positions."""
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    def place_order_yes(
        self, market_id: str, quantity: int, price: int, dry_run: bool = True
    ) -> Optional[dict]:
        return self._place_buy_order(market_id, "yes", quantity, price, dry_run)

    def place_order_no(
        self, market_id: str, quantity: int, price: int, dry_run: bool = True
    ) -> Optional[dict]:
        return self._place_buy_order(market_id, "no", quantity, price, dry_run)

    def _place_buy_order(
        self, market_id: str, side: str, quantity: int, price: int, dry_run: bool
    ) -> Optional[dict]:
        self._ensure_btc_market(market_id)
        if dry_run:
            log.info(
                "[DRY RUN] Would place BUY %s %s x%d @ %dc",
                side.upper(), market_id, quantity, price
            )
            return None
        payload = {
            "ticker": market_id,
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": quantity,
        }
        if side == "yes":
            payload["yes_price"] = price
        else:
            payload["no_price"] = price
        log.info("Placing BUY order: %s", payload)
        return self._request("POST", "/portfolio/orders", json=payload)

    def sell_position(
        self,
        market_id: str,
        side: str,
        quantity: int,
        price: int,
        dry_run: bool = True,
    ) -> Optional[dict]:
        """
        Sell (exit) an existing position by placing a limit sell order.
        side: 'yes' if you hold YES contracts, 'no' if you hold NO contracts.
        price_cents: the limit price you are willing to accept for the sale.
        On Kalshi, selling YES contracts = placing a sell action on the yes side.
        """
        self._ensure_btc_market(market_id)
        if dry_run:
            log.info(
                "[DRY RUN] Would place SELL %s %s x%d @ %dc",
                side.upper(), market_id, quantity, price
            )
            return None
        payload = {
            "ticker": market_id,
            "action": "sell",
            "type": "limit",
            "side": side,
            "count": quantity,
        }
        if side == "yes":
            payload["yes_price"] = price
        else:
            payload["no_price"] = price
        log.info("Placing SELL order: %s", payload)
        return self._request("POST", "/portfolio/orders", json=payload)

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        dry_run: bool = True,
    ) -> Optional[dict]:
        """
        Backward-compatible wrapper for buy helpers.
        side: 'yes' to buy YES contracts, 'no' to buy NO contracts.
        """
        if side == "yes":
            return self.place_order_yes(ticker, count, price_cents, dry_run)
        return self.place_order_no(ticker, count, price_cents, dry_run)

    @staticmethod
    def _is_btc_series_market(market_id: str) -> bool:
        return market_id.startswith(f"{config.BTC_SERIES_TICKER}-")

    def _ensure_btc_market(self, market_id: str) -> None:
        if not self._is_btc_series_market(market_id):
            raise ValueError(
                f"Refusing to trade non-BTC-series market '{market_id}'. Expected prefix {config.BTC_SERIES_TICKER}-"
            )

    def close_position(
        self,
        market_id: str,
        side: str,
        quantity: int,
        price: int,
        dry_run: bool = True,
    ) -> Optional[dict]:
        """
        Close (exit) an open position by placing a limit sell order.
        Delegates to sell_position(); provided as a semantically clear entry point
        for position-management code.
        side: 'yes' if you hold YES contracts, 'no' if you hold NO contracts.
        price: the minimum price (in cents) you are willing to accept.
        """
        return self.sell_position(market_id, side, quantity, price, dry_run)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by ID."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
