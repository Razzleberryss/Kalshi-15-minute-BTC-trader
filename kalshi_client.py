"""
kalshi_client.py - Thin wrapper around the Kalshi REST API v2.
Handles:
  - RSA-PSS request signing (required by Kalshi)
  - get_balance()
  - get_active_btc_market() -> finds the live 15-min BTC market
  - get_orderbook(ticker)
  - get_positions(), contracts_held_on_side(ticker, side)
  - list_markets(series_ticker, ...), fetch_markets(params) for /markets
  - place_order(ticker, side, count, price_cents, dry_run)
  - sell_position(ticker, side, count, price_cents, dry_run)
  - cancel_order(order_id)
"""
import base64
import datetime
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from requests.packages.urllib3.util.retry import Retry
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

log = logging.getLogger(__name__)

_LOG_BODY_MAX_LEN = 500


def _truncate_for_log(text: Optional[str], max_len: int = _LOG_BODY_MAX_LEN) -> str:
    if text is None:
        return ""
    text = str(text)
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... [truncated, {len(text)} chars]"


@dataclass
class HistoricalCutoffs:
    market_settled_ts: datetime.datetime
    trades_created_ts: datetime.datetime
    orders_updated_ts: datetime.datetime


class KalshiClient:
    """Authenticated HTTP client for Kalshi Trade API v2."""

    def __init__(self):
        self.base_url = config.BASE_URL
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._private_key = self._load_private_key(config.KALSHI_PRIVATE_KEY_PATH)
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._cutoffs: Optional[HistoricalCutoffs] = None
        self._cutoffs_fetched_at: Optional[datetime.datetime] = None

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
                            method, path, resp.status_code, error_code,
                            _truncate_for_log(str(error_msg)),
                        )
                    except Exception:
                        log.error(
                            "Kalshi API error %s %s -> HTTP %s: %s",
                            method, path, resp.status_code, _truncate_for_log(resp.text),
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

    # ── Historical helpers ───────────────────────────────────────────────────────────────────────
    @staticmethod
    def _ensure_utc_datetime(ts: datetime.datetime) -> datetime.datetime:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=datetime.timezone.utc)
        return ts.astimezone(datetime.timezone.utc)

    @classmethod
    def _parse_datetime_to_utc(cls, raw_ts, field_name: str) -> datetime.datetime:
        if isinstance(raw_ts, datetime.datetime):
            return cls._ensure_utc_datetime(raw_ts)
        if isinstance(raw_ts, (int, float)):
            return datetime.datetime.fromtimestamp(raw_ts, tz=datetime.timezone.utc)
        if isinstance(raw_ts, str):
            cleaned = raw_ts.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            try:
                parsed = datetime.datetime.fromisoformat(cleaned)
                return cls._ensure_utc_datetime(parsed)
            except ValueError:
                try:
                    return datetime.datetime.fromtimestamp(float(cleaned), tz=datetime.timezone.utc)
                except ValueError as exc:
                    raise ValueError(f"Could not parse datetime field '{field_name}': {raw_ts}") from exc
        raise ValueError(f"Unsupported datetime type for '{field_name}': {type(raw_ts)}")

    @classmethod
    def _to_unix_ts(cls, ts: datetime.datetime) -> int:
        return int(cls._ensure_utc_datetime(ts).timestamp())

    def _fetch_paginated_list(self, path: str, list_key: str, params: dict = None) -> list[dict]:
        out: list[dict] = []
        req_params = dict(params or {})
        seen_cursors = set()

        while True:
            data = self._request("GET", path, params=req_params)
            out.extend(data.get(list_key, []))
            cursor = data.get("cursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                log.warning("Stopping pagination for %s due to repeated cursor '%s'", path, cursor)
                break
            seen_cursors.add(cursor)
            req_params["cursor"] = cursor

        return out

    def _get_historical_cutoffs(self, force_refresh: bool = False) -> HistoricalCutoffs:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        if (
            not force_refresh
            and self._cutoffs is not None
            and self._cutoffs_fetched_at is not None
            and (now_utc - self._cutoffs_fetched_at) < datetime.timedelta(hours=1)
        ):
            return self._cutoffs

        data = self._request("GET", "/historical/cutoff")
        cutoffs = HistoricalCutoffs(
            market_settled_ts=self._parse_datetime_to_utc(data["market_settled_ts"], "market_settled_ts"),
            trades_created_ts=self._parse_datetime_to_utc(data["trades_created_ts"], "trades_created_ts"),
            orders_updated_ts=self._parse_datetime_to_utc(data["orders_updated_ts"], "orders_updated_ts"),
        )
        self._cutoffs = cutoffs
        self._cutoffs_fetched_at = now_utc
        return cutoffs

    def _fill_time(self, fill: dict) -> datetime.datetime:
        created_time = fill.get("created_time")
        if created_time is not None:
            return self._parse_datetime_to_utc(created_time, "fill.created_time")
        ts = fill.get("ts")
        if ts is not None:
            return self._parse_datetime_to_utc(ts, "fill.ts")
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    def _order_update_time(self, order: dict) -> datetime.datetime:
        last_update_time = order.get("last_update_time")
        if last_update_time is not None:
            return self._parse_datetime_to_utc(last_update_time, "order.last_update_time")
        updated_time = order.get("updated_time")
        if updated_time is not None:
            return self._parse_datetime_to_utc(updated_time, "order.updated_time")
        updated_ts = order.get("updated_ts")
        if updated_ts is not None:
            return self._parse_datetime_to_utc(updated_ts, "order.updated_ts")
        created_time = order.get("created_time")
        if created_time is not None:
            return self._parse_datetime_to_utc(created_time, "order.created_time")
        return datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)

    def _fetch_live_fills(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
        params = {
            "min_ts": self._to_unix_ts(start_ts),
            "max_ts": self._to_unix_ts(end_ts),
            "limit": 200,
        }
        return self._fetch_paginated_list("/portfolio/fills", "fills", params=params)

    def _fetch_historical_fills(self, end_ts: datetime.datetime) -> list[dict]:
        params = {
            "max_ts": self._to_unix_ts(end_ts),
            "limit": 200,
        }
        return self._fetch_paginated_list("/historical/fills", "fills", params=params)

    def _fetch_live_orders(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
        params = {
            "min_ts": self._to_unix_ts(start_ts),
            "max_ts": self._to_unix_ts(end_ts),
            "limit": 200,
        }
        return self._fetch_paginated_list("/portfolio/orders", "orders", params=params)

    def _fetch_historical_orders(self, end_ts: datetime.datetime) -> list[dict]:
        params = {
            "max_ts": self._to_unix_ts(end_ts),
            "limit": 200,
        }
        return self._fetch_paginated_list("/historical/orders", "orders", params=params)

    # ── Public API methods ────────────────────────────────────────────────────────────────────────
    def get_fills(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
        """
        Return fills across live + historical data for [start_ts, end_ts].
        """
        start_utc = self._ensure_utc_datetime(start_ts)
        end_utc = self._ensure_utc_datetime(end_ts)
        if end_utc < start_utc:
            raise ValueError("end_ts must be greater than or equal to start_ts")

        cutoffs = self._get_historical_cutoffs()
        cutoff = cutoffs.trades_created_ts

        fills: list[dict] = []

        # Historical segment: [start, min(end, cutoff))
        hist_end = min(end_utc, cutoff)
        if start_utc < hist_end:
            hist_rows = self._fetch_historical_fills(hist_end)
            fills.extend(
                f
                for f in hist_rows
                if start_utc <= self._fill_time(f) < hist_end
            )

        # Live segment: [max(start, cutoff), end]
        live_start = max(start_utc, cutoff)
        if live_start <= end_utc:
            live_rows = self._fetch_live_fills(live_start, end_utc)
            fills.extend(
                f
                for f in live_rows
                if live_start <= self._fill_time(f) <= end_utc
            )

        fills.sort(key=self._fill_time)
        return fills

    def get_orders(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
        """
        Return orders across live + historical data for [start_ts, end_ts].
        """
        start_utc = self._ensure_utc_datetime(start_ts)
        end_utc = self._ensure_utc_datetime(end_ts)
        if end_utc < start_utc:
            raise ValueError("end_ts must be greater than or equal to start_ts")

        cutoffs = self._get_historical_cutoffs()
        cutoff = cutoffs.orders_updated_ts

        orders: list[dict] = []

        # Active/resting orders are always in /portfolio/orders.
        live_rows = self._fetch_live_orders(start_utc, end_utc)
        orders.extend(
            o
            for o in live_rows
            if start_utc <= self._order_update_time(o) <= end_utc
        )

        # Historical orders are canceled/executed orders with update time before cutoff.
        hist_end = min(end_utc, cutoff)
        if start_utc < hist_end:
            hist_rows = self._fetch_historical_orders(hist_end)
            orders.extend(
                o
                for o in hist_rows
                if start_utc <= self._order_update_time(o) < hist_end
            )

        # De-duplicate by order_id in case APIs overlap at boundary conditions.
        # Cache update times to avoid re-parsing timestamps on every comparison.
        deduped_by_id: dict[str, dict] = {}
        deduped_update_times: dict[str, datetime.datetime] = {}
        for order in orders:
            order_id = order.get("order_id")
            if order_id:
                order_time = self._order_update_time(order)
                if order_id not in deduped_update_times or order_time >= deduped_update_times[order_id]:
                    deduped_by_id[order_id] = order
                    deduped_update_times[order_id] = order_time
                continue
            deduped_by_id[f"_anon_{len(deduped_by_id)}"] = order

        merged = list(deduped_by_id.values())
        merged.sort(key=self._order_update_time)
        return merged

    def get_market_with_history(self, ticker: str) -> dict:
        """
        Placeholder: eventually this should route between live /markets/{ticker}
        and historical markets/candlesticks based on market_settled_ts.
        For now just call GET /markets/{ticker}.
        """
        return self._request("GET", f"/markets/{ticker}")

    def debug_historical_cutoffs(self) -> None:
        """
        Log current historical cutoffs and run a small fill sample around cutoff.
        """
        cutoffs = self._get_historical_cutoffs(force_refresh=True)
        log.info(
            "Historical cutoffs: market_settled_ts=%s trades_created_ts=%s orders_updated_ts=%s",
            cutoffs.market_settled_ts.isoformat(),
            cutoffs.trades_created_ts.isoformat(),
            cutoffs.orders_updated_ts.isoformat(),
        )

        sample_start = cutoffs.trades_created_ts - datetime.timedelta(minutes=5)
        sample_end = cutoffs.trades_created_ts + datetime.timedelta(minutes=5)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        sample_end = min(sample_end, now_utc)

        if sample_start >= sample_end:
            return

        try:
            sample_fills = self.get_fills(sample_start, sample_end)
            log.info(
                "Historical cutoff sample fills [%s, %s]: %d rows",
                sample_start.isoformat(),
                sample_end.isoformat(),
                len(sample_fills),
            )
        except Exception as exc:
            log.warning("debug_historical_cutoffs sample fetch failed: %s", exc)

    def get_balance(self) -> float:
        """
        Return available balance in dollars.

        Uses the balance_dollars field (fixed-point string) from API v2.
        The legacy integer-cents 'balance' field is deprecated.
        """
        data = self._request("GET", "/portfolio/balance")
        # API v2 returns balance_dollars as a string (e.g., "123.45")
        # Fall back to legacy balance/100 if balance_dollars not present
        balance_str = data.get("balance_dollars")
        if balance_str is not None:
            return float(balance_str)
        # Legacy fallback (integer cents)
        return data["balance"] / 100

    def get_active_btc_market(self) -> Optional[dict]:
        """
        Find the currently open 15-minute BTC market.
        Returns the first active market dict or None.

        Filters out provisional markets (is_provisional=True) as they may be removed.
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
        # Filter out provisional markets (API v2: is_provisional may be True/False/missing)
        markets = [m for m in markets if not m.get("is_provisional", False)]
        if not markets:
            log.warning("All BTC markets are provisional; skipping trading")
            return None
        markets.sort(key=lambda m: m.get("close_time", ""))
        return markets[0]

    def get_markets(
        self,
        series_ticker: str,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> list:
        """
        Return a list of market dicts for the given series ticker.

        Args:
            series_ticker: The series to filter on, e.g. "KXBTCD".
            status: Optional market status filter, e.g. "open" or "closed".
            limit: Maximum number of markets to return (default 20).

        Returns:
            List of market dicts as returned by the Kalshi /markets endpoint.
        """
        params: dict = {"series_ticker": series_ticker, "limit": limit}
        if status is not None:
            params["status"] = status
        data = self._request("GET", "/markets", params=params)
        return data.get("markets", [])

    def list_markets(
        self,
        series_ticker: str,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        """
        List markets for a series via GET /markets.

        Same data as get_markets(); named for callers (e.g. CLI) that should
        not use the private _request() layer.
        """
        return self.get_markets(series_ticker, status=status, limit=limit)

    def fetch_markets(self, params: dict) -> dict:
        """
        GET /markets with arbitrary query parameters.

        Returns the full JSON body (including cursor for pagination). Use
        list_markets / get_markets when you only need the markets list.
        """
        return self._request("GET", "/markets", params=params)

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
            from orderbook_utils import extract_yes_no_bids

            orderbook = self.get_orderbook(ticker)
            yes_bids, no_bids = extract_yes_no_bids(orderbook)

            best_yes_bid = yes_bids[0][0] if yes_bids else None
            best_yes_bid_size = yes_bids[0][1] if yes_bids else 0
            best_no_bid = no_bids[0][0] if no_bids else None
            best_no_bid_size = no_bids[0][1] if no_bids else 0

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
                "best_yes_bid_size": best_yes_bid_size,
                "best_no_bid_size": best_no_bid_size,
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
                "best_yes_bid_size": 0,
                "best_no_bid_size": 0,
                "mid_price": None,
                "spread": None,
                "yes_depth_near_mid": 0,
                "no_depth_near_mid": 0,
            }

    def get_positions(self) -> list:
        """Return list of current open positions."""
        data = self._request("GET", "/portfolio/positions")
        return data.get("market_positions", [])

    def contracts_held_on_side(self, ticker: str, side: str) -> int:
        """
        Return non-negative contract count held on side for ticker, or 0.

        Kalshi uses a signed ``position`` per market: positive = long YES,
        negative = long NO.
        """
        side = side.lower()
        if side not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        for p in self.get_positions():
            if p.get("ticker") != ticker:
                continue
            pos = int(p.get("position", 0) or 0)
            if side == "yes" and pos > 0:
                return pos
            if side == "no" and pos < 0:
                return abs(pos)
        return 0

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

        # Generate unique client_order_id using UUID4 (per Kalshi API best practices)
        client_order_id = str(uuid.uuid4())

        # Build payload with fixed-point count (count_fp) as required by API v2
        # The 'type' field is deprecated and no longer required/supported
        payload = {
            "ticker": market_id,
            "action": "buy",
            "side": side,
            "count": quantity,
            "count_fp": f"{quantity}.00",  # fixed-point contract quantity
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

        # Generate unique client_order_id using UUID4 (per Kalshi API best practices)
        client_order_id = str(uuid.uuid4())

        # Build payload with fixed-point count (count_fp) as required by API v2
        # The 'type' field is deprecated and no longer required/supported
        payload = {
            "ticker": market_id,
            "action": "sell",
            "side": side,
            "count": quantity,
            "count_fp": f"{quantity}.00",  # fixed-point contract quantity
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
