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
from typing import Optional

import requests
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
        We prepend /trade-api/v2 for the signature and the full URL.
        """
        full_path = "/trade-api/v2" + path
        url = "https://demo-api.kalshi.co" + full_path if "demo" in self.base_url else "https://trading-api.kalshi.com" + full_path
        headers = self._auth_headers(method, full_path)
        resp = self.session.request(
            method, url, headers=headers, params=params, json=json, timeout=10
        )
        if not resp.ok:
            log.error(
                "Kalshi API error %s %s -> %s: %s",
                method, path, resp.status_code, resp.text
            )
            resp.raise_for_status()
        return resp.json()

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

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by ID."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
