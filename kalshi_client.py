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

                # Log full response for non-2xx status codes
                if not resp.ok:
                    try:
                        error_data = resp.json()
                        error_code = error_data.get("error", {}).get("code", "unknown")
                        error_msg = error_data.get("error", {}).get("message", resp.text)
                        log.error(
                            "Kalshi API error %s %s -> HTTP %s: code=%s message=%s",
                            method, path, resp.status_code, error_code, error_msg
                        )
                    except Exception:
                        log.error(
                            "Kalshi API error %s %s -> HTTP %s: %s",
                            method, path, resp.status_code, resp.text
                        )
                    resp.raise_for_status()
                return resp.json()
            except requests.exceptions.HTTPError as exc:
                # Do not retry client errors (4xx); always retry server errors (5xx)
                status = exc.response.status_code if exc.response is not None else 0
                if status < 500:
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
            spread: float | None - spread in probability terms (0.0-1.0), or None if no quotes
            yes_depth_near_mid: int - total YES contracts within DEPTH_BAND of mid
            no_depth_near_mid: int - total NO contracts within DEPTH_BAND of mid

        All prices are in cents (1-99 range). Returns None values if orderbook is empty.

        Note: Kalshi's orderbook contains bids only (no asks). To compute YES ask,
        we use: YES ask ≈ 100 - best_no_bid (since buying YES is equivalent to
        selling NO at the complementary price). When only one side has bids, we can
        infer the other side's ask using the complementary price relationship.
        """
        try:
            orderbook = self.get_orderbook(ticker)
            orderbook_data = orderbook.get("orderbook", {})

            # Support multiple orderbook formats, in priority order:
            # 1. orderbook_fp.yes_dollars_fp / no_dollars_fp  (new fixed-point REST format)
            # 2. orderbook_fp.yes_dollars / no_dollars        (older fp variant, kept for compat)
            # 3. orderbook["orderbook"].yes_dollars_fp / no_dollars_fp  (WebSocket-wrapped _fp)
            # 4. orderbook["orderbook"].yes_dollars / no_dollars        (WebSocket-wrapped _dollars)
            # 5. top-level yes_dollars / no_dollars on the orderbook response
            # 6. orderbook["orderbook"].yes / no              (legacy integer-cents, wrapped)
            # 7. top-level yes / no                           (legacy integer-cents, direct)
            # All string-price entries are converted to integer cents in parse_bids().
            orderbook_fp = orderbook.get("orderbook_fp", {})
            yes_bids = (
                orderbook_fp.get("yes_dollars_fp")
                or orderbook_fp.get("yes_dollars")
                or orderbook_data.get("yes_dollars_fp")
                or orderbook_data.get("yes_dollars")
                or orderbook.get("yes_dollars")
                or orderbook_data.get("yes", [])
                or orderbook.get("yes", [])
            )
            no_bids = (
                orderbook_fp.get("no_dollars_fp")
                or orderbook_fp.get("no_dollars")
                or orderbook_data.get("no_dollars_fp")
                or orderbook_data.get("no_dollars")
                or orderbook.get("no_dollars")
                or orderbook_data.get("no", [])
                or orderbook.get("no", [])
            )

            # Parse bid arrays - support both [price, size] and ["price_string", "count_string"] formats
            def parse_bids(bid_array):
                """Parse bid array, handling both numeric and string formats."""
                if not bid_array:
                    return []

                parsed = []
                for entry in bid_array:
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        # Handle string format: ["0.55", "10"]
                        if isinstance(entry[0], str):
                            try:
                                price_cents = round(float(entry[0]) * 100)
                                size = int(float(entry[1]))
                                parsed.append([price_cents, size])
                            except (ValueError, TypeError):
                                continue
                        # Handle numeric format: [55, 10]
                        else:
                            parsed.append([int(entry[0]), int(entry[1])])
                return parsed

            yes_bids = parse_bids(yes_bids)
            no_bids = parse_bids(no_bids)

            # Extract best bids (highest price = index 0, as they're sorted descending)
            best_yes_bid = yes_bids[0][0] if yes_bids else None
            best_no_bid = no_bids[0][0] if no_bids else None

            # Compute asks from the complementary side
            # YES ask = what you pay to buy YES = 100 - (what NO buyers are willing to pay)
            # NO ask = what you pay to buy NO = 100 - (what YES buyers are willing to pay)
            best_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
            best_no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None

            # For one-sided books, infer the missing ask from the available bid
            # If we have YES bid but no YES ask (because NO side is empty),
            # we can still infer best_yes_ask from best_yes_bid using market convention:
            # The ask should be at least 1 cent higher than the bid
            if best_yes_bid is not None and best_yes_ask is None:
                # No NO bids available, but we can still estimate YES ask
                # Use a minimal spread assumption: ask = bid + 1 cent (minimum tick)
                best_yes_ask = min(best_yes_bid + 1, 99)
                log.debug("Inferred best_yes_ask=%d from best_yes_bid=%d (no NO bids available)",
                         best_yes_ask, best_yes_bid)

            if best_no_bid is not None and best_no_ask is None:
                # No YES bids available, but we can still estimate NO ask
                best_no_ask = min(best_no_bid + 1, 99)
                log.debug("Inferred best_no_ask=%d from best_no_bid=%d (no YES bids available)",
                         best_no_ask, best_no_bid)

            # Compute mid price if we have at least one complete bid/ask pair
            if best_yes_bid is not None and best_yes_ask is not None:
                mid_price = (best_yes_bid + best_yes_ask) // 2
            elif best_no_bid is not None and best_no_ask is not None:
                # Use NO side to compute mid if YES side unavailable
                mid_price = 100 - ((best_no_bid + best_no_ask) // 2)
            else:
                mid_price = None

            # Compute spread in probability terms (0.0-1.0)
            if best_yes_bid is not None and best_yes_ask is not None:
                spread = (best_yes_ask - best_yes_bid) / 100.0
            else:
                spread = None

            # Compute depth near mid (within DEPTH_BAND)
            yes_depth_near_mid = 0
            no_depth_near_mid = 0

            if mid_price is not None:
                depth_band_cents = int(config.DEPTH_BAND * 100)
                lower_bound = mid_price - depth_band_cents
                upper_bound = mid_price + depth_band_cents

                # Sum YES contract sizes within band
                for price_cents, size in yes_bids:
                    if lower_bound <= price_cents <= upper_bound:
                        yes_depth_near_mid += size

                # Sum NO contract sizes within band
                for price_cents, size in no_bids:
                    if lower_bound <= price_cents <= upper_bound:
                        no_depth_near_mid += size

            return {
                "best_yes_bid": best_yes_bid,
                "best_yes_ask": best_yes_ask,
                "best_no_bid": best_no_bid,
                "best_no_ask": best_no_ask,
                "mid_price": mid_price,
                "spread": spread,
                "yes_depth_near_mid": yes_depth_near_mid,
                "no_depth_near_mid": no_depth_near_mid,
            }
        except Exception as exc:
            log.warning("Error fetching market quotes from orderbook for %s: %s", ticker, exc)
            return {
                "best_yes_bid": None,
                "best_yes_ask": None,
                "best_no_bid": None,
                "best_no_ask": None,
                "mid_price": None,
                "spread": None,
                "yes_depth_near_mid": 0,
                "no_depth_near_mid": 0,
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

        # Generate unique client_order_id (timestamp + side + market)
        client_order_id = f"{int(time.time() * 1000)}_{side}_{market_id}"

        payload = {
            "ticker": market_id,
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": quantity,
            "client_order_id": client_order_id,
        }
        if side == "yes":
            payload["yes_price"] = price
        else:
            payload["no_price"] = price
        log.info("Placing BUY order: %s", payload)

        try:
            response = self._request("POST", "/portfolio/orders", json=payload)

            # Validate order status
            order = response.get("order", {})
            status = order.get("status")

            if status not in ("resting", "pending", "queued"):
                log.warning(
                    "Order placed but status is '%s' (expected resting/pending/queued): %s",
                    status, order
                )

            log.info("Order placed successfully: order_id=%s status=%s", order.get("order_id"), status)
            return response
        except Exception as exc:
            log.error("Failed to place order: %s", exc)
            raise

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

        # Generate unique client_order_id (timestamp + side + market)
        client_order_id = f"{int(time.time() * 1000)}_sell_{side}_{market_id}"

        payload = {
            "ticker": market_id,
            "action": "sell",
            "type": "limit",
            "side": side,
            "count": quantity,
            "client_order_id": client_order_id,
        }
        if side == "yes":
            payload["yes_price"] = price
        else:
            payload["no_price"] = price
        log.info("Placing SELL order: %s", payload)

        try:
            response = self._request("POST", "/portfolio/orders", json=payload)

            # Validate order status
            order = response.get("order", {})
            status = order.get("status")

            if status not in ("resting", "pending", "queued"):
                log.warning(
                    "Sell order placed but status is '%s' (expected resting/pending/queued): %s",
                    status, order
                )

            log.info("Sell order placed successfully: order_id=%s status=%s", order.get("order_id"), status)
            return response
        except Exception as exc:
            log.error("Failed to place sell order: %s", exc)
            raise

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
