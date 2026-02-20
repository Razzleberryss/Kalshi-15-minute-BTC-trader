"""
kalshi_client.py  -  Thin wrapper around the Kalshi REST API v2.

Handles:
  - RSA-PSS request signing (required by Kalshi)
  - get_balance()
  - get_active_btc_market()   → finds the live 15-min BTC market
  - get_orderbook(ticker)
  - get_positions()
  - place_order(ticker, side, count, price_cents, dry_run)
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

    # ── Auth helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _load_private_key(path: str):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """Return base64-encoded RSA-PSS signature for Kalshi auth header."""
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
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
        }

    # ── Generic request ────────────────────────────────────────────────────────────

    def _request(self, method: str, path: str, params: dict = None, json: dict = None) -> dict:
        url = self.base_url + path
        headers = self._auth_headers(method, path)
        resp = self.session.request(method, url, headers=headers, params=params, json=json, timeout=10)
        if not resp.ok:
            log.error("Kalshi API error %s %s -> %s: %s", method, path, resp.status_code, resp.text)
            resp.raise_for_status()
        return resp.json()

    # ── Public API methods ─────────────────────────────────────────────────────────

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
        # Prefer earliest expiry (most liquid / most recently opened)
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

    def place_order(
        self,
        ticker: str,
        side: str,          # 'yes' or 'no'
        count: int,         # number of contracts
        price_cents: int,   # limit price in cents (1-99)
        dry_run: bool = True,
    ) -> Optional[dict]:
        """
        Place a limit order. Returns the order dict or None on dry run.
        side: 'yes' to buy YES contracts, 'no' to buy NO contracts.
        """
        if dry_run:
            log.info(
                "[DRY RUN] Would place %s %s x%d @ %dc on %s",
                side.upper(), ticker, count, price_cents, ticker
            )
            return None

        payload = {
            "ticker": ticker,
            "action": "buy",
            "type": "limit",
            "side": side,
            "count": count,
            "yes_price" if side == "yes" else "no_price": price_cents,
        }
        log.info("Placing order: %s", payload)
        return self._request("POST", "/portfolio/orders", json=payload)

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by ID."""
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
