"""
websocket_client.py - WebSocket client for streaming Kalshi market data.

Connects to Kalshi's WebSocket API to receive real-time orderbook updates,
maintaining an in-memory snapshot of the latest orderbook for subscribed markets.

Thread-safe accessor methods allow the bot to query the latest orderbook without
making REST API calls.
"""
import json
import logging
import threading
import time
from typing import Optional
import datetime
import base64

import websocket

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

log = logging.getLogger(__name__)


class KalshiWebSocketClient:
    """
    WebSocket client for streaming Kalshi orderbook data.

    Connects to Kalshi's WebSocket endpoint, authenticates, and subscribes to
    orderbook updates for specified markets. Maintains an in-memory snapshot of
    the latest orderbook for each subscribed market.
    """

    def __init__(self):
        """Initialize the WebSocket client."""
        self.base_url = config.BASE_URL
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._private_key = self._load_private_key(config.KALSHI_PRIVATE_KEY_PATH)

        # Determine WebSocket URL based on environment
        if config.KALSHI_ENV == "prod":
            self.ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
        else:
            self.ws_url = "wss://demo-api.kalshi.co/trade-api/ws/v2"

        self.ws = None
        self.ws_thread = None
        self._running = False
        self._connected = False

        # Thread-safe storage for orderbook snapshots
        # Key: market ticker, Value: orderbook dict with yes/no arrays
        self._orderbooks = {}
        self._lock = threading.Lock()

        # Track subscribed markets
        self._subscribed_markets = set()
        self._message_id = 1

    @staticmethod
    def _load_private_key(path: str):
        """Load RSA private key from file."""
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Return base64-encoded RSA-PSS signature for WebSocket authentication.
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

    def start(self):
        """Start the WebSocket connection in a background thread."""
        if self._running:
            log.warning("WebSocket client already running")
            return

        self._running = True
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()

        # Wait briefly for connection to establish
        for _ in range(50):  # 5 seconds max
            if self._connected:
                log.info("WebSocket client connected successfully")
                return
            time.sleep(0.1)

        log.warning("WebSocket connection not established within timeout")

    def stop(self):
        """Stop the WebSocket connection and clean up."""
        log.info("Stopping WebSocket client...")
        self._running = False
        if self.ws:
            self.ws.close()
        if self.ws_thread:
            self.ws_thread.join(timeout=5)
        log.info("WebSocket client stopped")

    def _run_websocket(self):
        """Main WebSocket event loop (runs in background thread)."""
        reconnect_attempts = 0
        while self._running:
            try:
                ts = str(int(datetime.datetime.now().timestamp() * 1000))
                signature = self._sign(ts, "GET", "/trade-api/ws/v2")
                headers = [
                    f"KALSHI-ACCESS-KEY: {self.api_key_id}",
                    f"KALSHI-ACCESS-SIGNATURE: {signature}",
                    f"KALSHI-ACCESS-TIMESTAMP: {ts}",
                ]
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                reconnect_attempts = 0
                self.ws.run_forever()
            except Exception as exc:
                log.error("WebSocket connection error: %s", exc)

            if self._running:
                self._connected = False
                backoff = min(2 ** reconnect_attempts, 60)
                reconnect_attempts += 1
                log.info("Reconnecting WebSocket in %ds...", backoff)
                time.sleep(backoff)

    def _on_open(self, ws):
        """Called when WebSocket connection is opened."""
        log.info("WebSocket connection opened")
        self._connected = True

    def _on_message(self, ws, message):
        """Called when a message is received from the WebSocket."""
        try:
            data = json.loads(message)

            # Handle orderbook update messages
            if data.get("type") == "orderbook_snapshot" or data.get("type") == "orderbook_delta":
                self._handle_orderbook_update(data)
            elif data.get("type") == "subscribed":
                channel = data.get("msg", {}).get("channel")
                log.info("Successfully subscribed to channel: %s", channel)
            elif data.get("type") == "error":
                log.error("WebSocket error message: %s", data.get("msg"))
        except json.JSONDecodeError as exc:
            log.error("Failed to decode WebSocket message: %s", exc)
        except Exception as exc:
            log.error("Error processing WebSocket message: %s", exc)

    def _handle_orderbook_update(self, data):
        """Process an orderbook update and update the internal snapshot."""
        try:
            payload = data.get("msg", {})
            ticker = payload.get("market_ticker")
            if not ticker:
                return

            # For snapshot messages, replace the entire orderbook
            if data.get("type") == "orderbook_snapshot":
                orderbook_data = payload
                with self._lock:
                    self._orderbooks[ticker] = self._normalize_orderbook(orderbook_data)
                log.debug("Received orderbook snapshot for %s", ticker)

            # For delta messages, apply the incremental update
            elif data.get("type") == "orderbook_delta":
                delta = payload
                with self._lock:
                    if ticker not in self._orderbooks:
                        log.warning(
                            "Received delta without snapshot for %s; waiting for orderbook_snapshot",
                            ticker,
                        )
                        return

                    current = self._orderbooks[ticker]
                    self._orderbooks[ticker] = self._apply_delta(current, delta)
                    log.debug("Applied orderbook delta for %s", ticker)

        except Exception as exc:
            log.error("Error handling orderbook update: %s", exc)

    def _on_error(self, ws, error):
        """Called when a WebSocket error occurs."""
        log.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        """Called when WebSocket connection is closed."""
        log.info("WebSocket connection closed (code=%s, msg=%s)", close_status_code, close_msg)
        self._connected = False

    def subscribe_to_market(self, ticker: str):
        """
        Subscribe to orderbook updates for a specific market.

        Args:
            ticker: Market ticker to subscribe to (e.g., "BTCZ-25DEC3100-T3PM")
        """
        if ticker in self._subscribed_markets:
            log.debug("Already subscribed to %s", ticker)
            return

        if not self._connected:
            log.warning("Cannot subscribe to %s: WebSocket not connected", ticker)
            return

        try:
            subscribe_msg = {
                "id": self._next_message_id(),
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_ticker": ticker,
                }
            }

            self.ws.send(json.dumps(subscribe_msg))
            self._subscribed_markets.add(ticker)
            log.info("Subscribed to orderbook updates for %s", ticker)
        except Exception as exc:
            log.error("Error subscribing to %s: %s", ticker, exc)

    def get_latest_orderbook(self, ticker: str) -> Optional[dict]:
        """
        Get the latest orderbook snapshot for a market.

        Args:
            ticker: Market ticker

        Returns:
            Orderbook dict with 'yes' and 'no' arrays, or None if not available.
            Format matches the REST API orderbook response.
        """
        with self._lock:
            orderbook = self._orderbooks.get(ticker)
            if orderbook:
                yes = orderbook.get("yes")
                no = orderbook.get("no")
                return {
                    "yes": list(yes) if yes else [],
                    "no": list(no) if no else [],
                }
            return None

    @staticmethod
    def _normalize_price(price) -> Optional[int]:
        """Normalize a price into integer cents."""
        if price is None:
            return None
        try:
            if isinstance(price, str):
                return int(float(price) * 100) if "." in price else int(price)
            if isinstance(price, float) and 0 <= price <= 1:
                return int(round(price * 100))
            return int(price)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_levels(cls, levels) -> list[list[int]]:
        """Normalize a side of the book into [[price_cents, size], ...]."""
        if not levels:
            return []

        if isinstance(levels, dict):
            levels = levels.get("bids") or levels.get("levels") or []

        normalized = []
        for level in levels:
            price = None
            size = None

            if isinstance(level, dict):
                price = level.get("price")
                if price is None:
                    price = level.get("price_dollars")
                size = (
                    level.get("size")
                    if level.get("size") is not None
                    else level.get("count")
                )
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]

            price_cents = cls._normalize_price(price)
            if price_cents is None:
                continue

            try:
                size_int = int(size)
            except (TypeError, ValueError):
                continue

            if size_int > 0:
                normalized.append([price_cents, size_int])

        if len(normalized) > 1:
            normalized.sort(key=lambda item: item[0], reverse=True)
        return normalized

    @classmethod
    def _normalize_orderbook(cls, orderbook) -> dict:
        """Normalize orderbook payloads into a consistent yes/no list format."""
        if not isinstance(orderbook, dict):
            return {"yes": [], "no": []}

        yes_levels = (
            orderbook.get("yes")
            or orderbook.get("yes_dollars")
            or orderbook.get("yes_dollars_fp")
            or []
        )
        no_levels = (
            orderbook.get("no")
            or orderbook.get("no_dollars")
            or orderbook.get("no_dollars_fp")
            or []
        )

        return {
            "yes": cls._normalize_levels(yes_levels),
            "no": cls._normalize_levels(no_levels),
        }

    @classmethod
    def _apply_side_delta(cls, current_levels, side_delta) -> list[list[int]]:
        """Apply a delta payload to a single orderbook side."""
        current_map = {price: size for price, size in cls._normalize_levels(current_levels)}

        # Full replacement delta: {"yes": [[55, 10], ...]} or side list payload
        if isinstance(side_delta, (list, tuple)):
            return cls._normalize_levels(side_delta)
        if isinstance(side_delta, dict) and (
            side_delta.get("bids") is not None or side_delta.get("levels") is not None
        ):
            return cls._normalize_levels(side_delta)

        updates = side_delta if isinstance(side_delta, list) else [side_delta]
        for update in updates:
            if not isinstance(update, dict):
                continue
            price_cents = cls._normalize_price(update.get("price"))
            if price_cents is None:
                price_cents = cls._normalize_price(update.get("price_dollars"))
            if price_cents is None:
                continue

            raw_size = update.get("size")
            if raw_size is None:
                raw_size = update.get("count")
            if raw_size is None:
                raw_size = update.get("quantity")
            if raw_size is None and update.get("delta_fp") is not None:
                raw_size = None

            if raw_size is not None:
                try:
                    size_int = int(raw_size)
                except (TypeError, ValueError):
                    continue
                if size_int <= 0:
                    current_map.pop(price_cents, None)
                else:
                    current_map[price_cents] = size_int
                continue

            raw_delta = update.get("delta")
            if raw_delta is None:
                raw_delta = update.get("delta_fp")
            if raw_delta is not None:
                try:
                    delta_int = int(float(raw_delta))
                except (TypeError, ValueError):
                    continue
                new_size = current_map.get(price_cents, 0) + delta_int
                if new_size <= 0:
                    current_map.pop(price_cents, None)
                else:
                    current_map[price_cents] = new_size

        return [[price, size] for price, size in sorted(current_map.items(), reverse=True)]

    @classmethod
    def _apply_delta(cls, current_orderbook, delta) -> dict:
        """Apply a delta payload without discarding the existing snapshot."""
        current = cls._normalize_orderbook(current_orderbook)

        if not isinstance(delta, dict):
            return current

        # Some payloads send a full yes/no replacement inside delta.
        if "yes" in delta or "no" in delta:
            return {
                "yes": cls._apply_side_delta(current.get("yes", []), delta.get("yes", [])),
                "no": cls._apply_side_delta(current.get("no", []), delta.get("no", [])),
            }

        # Other payloads send a single side/price update.
        side = delta.get("side")
        if side in ("yes", "no"):
            updated = dict(current)
            updated[side] = cls._apply_side_delta(current.get(side, []), delta)
            return updated

        return current

    def _next_message_id(self) -> int:
        """Return the next client message id for websocket commands."""
        message_id = self._message_id
        self._message_id += 1
        return message_id

    def is_connected(self) -> bool:
        """Check if the WebSocket is currently connected."""
        return self._connected

    def has_orderbook(self, ticker: str) -> bool:
        """Check if we have an orderbook snapshot for the given market."""
        with self._lock:
            return ticker in self._orderbooks
