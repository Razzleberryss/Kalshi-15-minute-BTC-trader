"""
kalshi_client.py — Thin wrapper around the official Kalshi Python SDK (sync).

This module preserves the public interface used by:
  - bot.py (main loop)
  - openclaw_kalshi.py (CLI)
  - kalshi_inprocess_orders.py (in-process envelopes)

It intentionally keeps most method names and return shapes stable to minimize
blast radius while migrating from a hand-rolled REST client to:
  from kalshi_python_sync import Configuration, KalshiClient
"""

from __future__ import annotations

import datetime
import logging
import random
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

import config
from kalshi_money import enrich_market_quotes_from_dollar_fields, fmt_cents
import requests

try:
    from kalshi_python_sync import Configuration as _SdkConfiguration
    from kalshi_python_sync import KalshiClient as _SdkKalshiClient
except Exception:  # pragma: no cover (covered by init error handling tests via mocking)
    _SdkConfiguration = None
    _SdkKalshiClient = None


class _NoopSdk:
    """Fallback SDK stand-in for unit tests with invalid PEM material."""

    def __getattr__(self, name: str):
        raise RuntimeError(
            f"Kalshi SDK not initialized (missing/invalid private key). Tried to access '{name}'."
        )

log = logging.getLogger(__name__)

# Order create responses: Kalshi removed "pending" from the public status enum.
_ORDER_CREATE_OK_STATUSES = frozenset(
    {"resting", "queued", "open", "executed", "filled", "partially_filled"}
)

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
    # Retained for backward compatibility. The SDK supports historical endpoints,
    # but this repo only uses these cutoffs in optional debug helpers.
    market_settled_ts: datetime.datetime
    trades_created_ts: datetime.datetime
    orders_updated_ts: datetime.datetime


def _pick_trade_api_host() -> str:
    """
    Choose Trade API host based on config.KALSHI_ENV.

    - prod -> https://api.elections.kalshi.com/trade-api/v2
    - demo -> prefer the repo's existing demo host (config.BASE_URL) if it looks valid.
    """
    env = (config.KALSHI_ENV or "prod").lower()
    if env == "prod":
        return "https://api.elections.kalshi.com/trade-api/v2"
    # Demo: keep centralized + easy to edit; prefer existing repo config if present
    demo_default = "https://demo-api.kalshi.co/trade-api/v2"
    base = getattr(config, "BASE_URL", None) or getattr(config, "KALSHI_BASE_URL", None)
    if isinstance(base, str) and base.startswith("https://") and "/trade-api/v2" in base:
        return base
    return demo_default


def _read_private_key_pem(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _to_dict(obj: Any) -> dict:
    """Best-effort SDK response normalization to plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    # Many generated clients expose to_dict()
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            return d if isinstance(d, dict) else {"value": d}
        except Exception:
            pass
    # Fallback for dataclasses / simple objects
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return dict(d)
    return {"value": obj}


def _get_first_present(d: dict, *keys: str) -> Any:
    for k in keys:
        if k in d:
            return d.get(k)
    return None


def _price_cents_to_dollars_fp(price_cents: int) -> str:
    """
    Convert integer cents (1-99) to a dollars fixed-point string for SDK fields.
    Prefer 4dp ("0.5500") to align with *_dollars_fp conventions.
    """
    try:
        cents = int(price_cents)
    except (TypeError, ValueError):
        raise ValueError(f"price_cents must be an int, got {price_cents!r}")
    if not (0 <= cents <= 100):
        raise ValueError(f"price_cents out of range 0-100: {cents}")
    d = (Decimal(cents) / Decimal(100)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return format(d, "f")


class KalshiClient:
    """
    Thin SDK-backed wrapper around the Kalshi Trade API v2.

    Public methods aim to preserve the previous hand-rolled client's interface.
    """

    def __init__(self):
        self.base_url = _pick_trade_api_host()
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._cutoffs: Optional[HistoricalCutoffs] = None
        self._cutoffs_fetched_at: Optional[datetime.datetime] = None
        # Retain a requests.Session for legacy tests that validate retry behavior.
        # The trading bot itself uses the SDK for all network calls.
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

        # Initialize SDK client with defensive logging.
        try:
            if _SdkConfiguration is None or _SdkKalshiClient is None:
                raise RuntimeError(
                    "kalshi_python_sync is not importable. "
                    "Install dependencies with `pip install -r requirements.txt`."
                )

            sdk_cfg = _SdkConfiguration(host=self.base_url)
            # Official SDK expects these attributes to be set post-construction.
            sdk_cfg.api_key_id = self.api_key_id
            # Keep key material loading centralized here.
            try:
                sdk_cfg.private_key_pem = _read_private_key_pem(config.KALSHI_PRIVATE_KEY_PATH)
                log.debug("Private key loaded successfully [REDACTED]")
            except Exception as key_exc:
                # Unit tests in this repo instantiate KalshiClient without a real
                # key file; don't hard-fail in that scenario. bot.py calls
                # config.validate() before construction, so production runs still
                # fail fast when misconfigured.
                if Path(str(config.KALSHI_PRIVATE_KEY_PATH)).exists():
                    raise
                log.warning(
                    "Private key PEM not found at %s (continuing with empty key for tests): %s",
                    config.KALSHI_PRIVATE_KEY_PATH,
                    key_exc,
                )
                sdk_cfg.private_key_pem = ""
            try:
                self._sdk = _SdkKalshiClient(sdk_cfg)
            except Exception as sdk_exc:
                # If the key material isn't a real PEM (common in unit tests),
                # do not fail client construction. Methods that need the SDK
                # will raise via _NoopSdk.
                log.warning("Kalshi SDK init failed (using noop SDK): %s", sdk_exc)
                self._sdk = _NoopSdk()

            key_id_status = "SET" if self.api_key_id else "MISSING"
            log.debug(
                "Kalshi SDK client initialized (env=%s host=%s key_id=%s)",
                config.KALSHI_ENV,
                self.base_url,
                key_id_status,
            )
        except Exception as exc:
            log.error("Failed to initialize Kalshi SDK client: %s", exc, exc_info=True)
            raise

    # ── Auth helpers ──────────────────────────────────────────────────────────────────────────
    @staticmethod
    def _load_private_key(path: str):
        # Backward-compatibility shim: some unit tests patch this symbol.
        return None

    # ── Legacy request helper (tests rely on this) ─────────────────────────────────────────────
    def _request(self, method: str, path: str, params: dict = None, json: dict = None) -> dict:
        """
        Legacy helper retained for unit tests and a few historical utilities.

        NOTE: Production code paths should prefer SDK methods.
        """
        url = (getattr(config, "BASE_URL", self.base_url) or self.base_url) + path

        last_exc: Exception = RuntimeError("No attempts made")
        max_attempts = 1 + max(0, int(getattr(config, "REQUEST_MAX_RETRIES", 0)))
        for attempt in range(max_attempts):
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json,
                    timeout=int(getattr(config, "REQUEST_TIMEOUT_SECONDS", 5)),
                )

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
                backoff = random.uniform(0.1, max(0.2, 2 ** attempt))
                log.info("Retrying in %.2fs...", backoff)
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

    def _fetch_paginated_list(self, fetch_fn, list_key: str, params: dict | None = None) -> list[dict]:
        """
        Generic cursor pagination helper for SDK list endpoints.

        fetch_fn should accept **params and return an object/dict with:
          - list_key: list[dict]
          - cursor: str | None
        """
        out: list[dict] = []
        req_params = dict(params or {})
        seen_cursors: set[str] = set()

        while True:
            raw = fetch_fn(**req_params)
            data = _to_dict(raw)
            rows = data.get(list_key, []) or []
            if isinstance(rows, list):
                out.extend(rows)
            cursor = data.get("cursor")
            if not cursor:
                break
            if cursor in seen_cursors:
                log.warning("Stopping pagination due to repeated cursor '%s'", cursor)
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

        # SDK method naming is stable for this endpoint; normalize to dict regardless.
        data = _to_dict(self._sdk.get_historical_cutoff())
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
        return self._fetch_paginated_list(self._sdk.get_fills, "fills", params=params)

    def _fetch_historical_fills(self, end_ts: datetime.datetime) -> list[dict]:
        params = {"max_ts": self._to_unix_ts(end_ts), "limit": 200}
        return self._fetch_paginated_list(self._sdk.get_historical_fills, "fills", params=params)

    def _fetch_live_orders(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
        params = {
            "min_ts": self._to_unix_ts(start_ts),
            "max_ts": self._to_unix_ts(end_ts),
            "limit": 200,
        }
        return self._fetch_paginated_list(self._sdk.get_orders, "orders", params=params)

    def _fetch_historical_orders(self, end_ts: datetime.datetime) -> list[dict]:
        params = {"max_ts": self._to_unix_ts(end_ts), "limit": 200}
        return self._fetch_paginated_list(self._sdk.get_historical_orders, "orders", params=params)

    # ── Public API methods ────────────────────────────────────────────────────────────────────────
    # New SDK-style public methods (limit-based), plus compatibility shims.
    def get_fills(self, *args: Any, limit: Optional[int] = None) -> list[dict]:
        """Return recent fills, or support legacy get_fills(start_ts, end_ts)."""
        try:
            if len(args) == 2:
                start_ts, end_ts = args
                if not (
                    isinstance(start_ts, datetime.datetime)
                    and isinstance(end_ts, datetime.datetime)
                ):
                    raise TypeError(
                        "get_fills(start_ts, end_ts) requires datetime arguments"
                    )
                return self.get_fills_in_range(start_ts, end_ts)
            if len(args) > 1:
                raise TypeError(
                    "get_fills accepts either (limit) or (start_ts, end_ts)"
                )

            effective_limit = limit if limit is not None else (args[0] if args else 100)
            raw = self._sdk.get_fills(limit=int(effective_limit))
            data = _to_dict(raw)
            return data.get("fills", []) or []
        except Exception as exc:
            log.error("get_fills failed: %s", exc, exc_info=True)
            raise

    def get_orders(self, limit: int = 100) -> list[dict]:
        """Return recent orders (portfolio) as a list of dicts."""
        try:
            raw = self._sdk.get_orders(limit=int(limit))
            data = _to_dict(raw)
            return data.get("orders", []) or []
        except Exception as exc:
            log.error("get_orders failed: %s", exc, exc_info=True)
            raise

    # Compatibility: keep the previous time-ranged methods available under old names.
    def get_fills_in_range(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
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

    def get_orders_in_range(self, start_ts: datetime.datetime, end_ts: datetime.datetime) -> list[dict]:
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
        Placeholder: route between live GET /markets/{ticker} and historical
        market/candle endpoints using GET /historical/cutoff market_settled_ts
        when the contract is settled. For now this is a thin alias of get_market().
        """
        return self.get_market(ticker)

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
        try:
            raw = self._sdk.get_balance()
            data = _to_dict(raw)
            # Prefer *_dollars string if present.
            bal_dollars = _get_first_present(data, "balance_dollars", "available_balance_dollars")
            if bal_dollars is not None:
                try:
                    return float(str(bal_dollars))
                except (TypeError, ValueError):
                    pass
            # Some SDK examples show balance in integer cents under "balance".
            bal_cents = _get_first_present(data, "balance", "available_balance")
            if bal_cents is not None:
                try:
                    return float(int(bal_cents)) / 100.0
                except (TypeError, ValueError):
                    pass
            # Fall back to 0.0 if the response shape is unexpected.
            log.warning("Unexpected balance response shape: %s", data)
            return 0.0
        except Exception as exc:
            log.error("get_balance failed: %s", exc, exc_info=True)
            raise

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
        data = _to_dict(self._sdk.get_markets(**params))
        markets = data.get("markets", []) or []
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
        return enrich_market_quotes_from_dollar_fields(markets[0])

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
        data = _to_dict(self._sdk.get_markets(**params))
        return data.get("markets", []) or []

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
        # Preserve existing shape used by historical.py: includes cursor for pagination.
        return _to_dict(self._sdk.get_markets(**(params or {})))

    def get_orderbook(self, ticker: str, depth: int = 10) -> dict:
        """Return the full orderbook dict for a market ticker."""
        try:
            # SDK exposes get_market_orderbook per provided facts.
            raw = self._sdk.get_market_orderbook(ticker=ticker, depth=int(depth))
            return _to_dict(raw)
        except TypeError:
            # Some generated clients use positional args.
            raw = self._sdk.get_market_orderbook(ticker, int(depth))
            return _to_dict(raw)
        except Exception as exc:
            log.error("get_orderbook failed for %s: %s", ticker, exc, exc_info=True)
            raise

    def get_market(self, ticker: str) -> dict:
        """Return raw GET /markets/{ticker} JSON, with *_dollars quote fields mirrored into legacy cent keys where possible."""
        data = _to_dict(self._sdk.get_market(ticker=ticker))
        if isinstance(data.get("market"), dict):
            enrich_market_quotes_from_dollar_fields(data["market"])
        elif isinstance(data, dict) and data.get("ticker") is not None:
            enrich_market_quotes_from_dollar_fields(data)
        return data

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
                log.debug(
                    "Inferred best_yes_ask=%s from best_yes_bid=%s (no NO bids available)",
                    fmt_cents(best_yes_ask),
                    fmt_cents(best_yes_bid),
                )

            if best_no_bid is not None and best_no_ask is None:
                # No YES bids available, but we can still estimate NO ask
                best_no_ask = min(best_no_bid + 1, 99)
                log.debug(
                    "Inferred best_no_ask=%s from best_no_bid=%s (no YES bids available)",
                    fmt_cents(best_no_ask),
                    fmt_cents(best_no_bid),
                )

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
        """
        Return unsettled market_positions only (per API v2).

        Settled / closed exposure is not included here; use get_settlements() and/or
        get_fills() for historical P&L and closed positions.
        """
        try:
            data = _to_dict(self._sdk.get_positions())
            # API v2 uses "market_positions" but some clients use "positions".
            rows = _get_first_present(data, "market_positions", "positions")
            return rows or []
        except Exception as exc:
            log.error("get_positions failed: %s", exc, exc_info=True)
            raise

    def get_settlements(self, params: Optional[dict] = None) -> list[dict]:
        """Portfolio settlements (settled positions / payouts). Pass-through query params (pagination, filters) as supported by the API."""
        req = dict(params or {})
        req.setdefault("limit", 200)
        return self._fetch_paginated_list(self._sdk.get_settlements, "settlements", params=req)

    def get_account_limits(self) -> dict:
        """GET /account/limits — trading / account limits for the authenticated user."""
        return _to_dict(self._sdk.get_account_limits())

    def get_markets_orderbooks(self, tickers: list[str]) -> dict[str, dict]:
        """
        GET /markets/orderbooks for up to 100 tickers per request.
        Returns ticker -> {"ticker", "orderbook_fp", ...} rows (suitable for orderbook_utils.extract_raw_arrays).
        """
        out: dict[str, dict] = {}
        if not tickers:
            return out
        for i in range(0, len(tickers), 100):
            chunk = tickers[i : i + 100]
            # SDK expects repeated 'tickers' query params; accept list.
            data = _to_dict(self._sdk.get_markets_orderbooks(tickers=chunk))
            for row in (data.get("orderbooks", []) or []):
                t = row.get("ticker")
                if t:
                    out[t] = row
        return out

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
        return self.buy_yes(market_id, quantity, price, dry_run=False) if side == "yes" else self.buy_no(market_id, quantity, price, dry_run=False)

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
        return self.sell_yes(market_id, quantity, price, dry_run=False) if side == "yes" else self.sell_no(market_id, quantity, price, dry_run=False)

    # ── SDK-backed wrapper methods (required by migration task) ─────────────────────────────────
    def _build_order_payload(
        self,
        *,
        ticker: str,
        side: str,
        action: str,
        contracts: int,
        price_cents: int,
        reduce_only: bool = False,
        post_only: bool = False,
        client_order_id: str | None = None,
    ) -> dict:
        """
        Single private helper for SDK order creation.

        - Converts cents → dollars-string fields (yes_price_dollars/no_price_dollars)
        - Uses count_fp (repo convention)
        - Sets cancel_order_on_pause=True by default
        """
        side_l = (side or "").lower()
        if side_l not in ("yes", "no"):
            raise ValueError("side must be 'yes' or 'no'")
        action_l = (action or "").lower()
        if action_l not in ("buy", "sell"):
            raise ValueError("action must be 'buy' or 'sell'")
        if contracts < 1:
            raise ValueError("contracts must be >= 1")
        if not (1 <= int(price_cents) <= 99):
            raise ValueError("price_cents must be 1-99")

        coid = client_order_id or str(uuid.uuid4())
        payload: dict[str, Any] = {
            "ticker": ticker,
            "side": side_l,
            "action": action_l,
            # Keep order type explicit and stable (limit).
            "type": "limit",
            "count": int(contracts),
            "count_fp": f"{int(contracts)}.00",
            "client_order_id": coid,
            "reduce_only": bool(reduce_only),
            "post_only": bool(post_only),
            "cancel_order_on_pause": True,
        }
        price_dollars = _price_cents_to_dollars_fp(int(price_cents))
        if side_l == "yes":
            payload["yes_price_dollars"] = price_dollars
        else:
            payload["no_price_dollars"] = price_dollars
        return payload

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price_cents: int,
        dry_run: bool = True,
        *,
        reduce_only: bool = False,
        post_only: bool = False,
        client_order_id: str | None = None,
    ) -> Optional[dict]:
        """Primary order entry used by in-process envelopes and CLI."""
        self._ensure_btc_market(ticker)
        if dry_run:
            log.info(
                "[DRY RUN] Would place %s %s %s x%d @ %dc (reduce_only=%s post_only=%s)",
                "BUY" if (side or "").lower() in ("yes", "no") else "ORDER",
                (side or "").upper(),
                ticker,
                count,
                price_cents,
                reduce_only,
                post_only,
            )
            return None

        payload = self._build_order_payload(
            ticker=ticker,
            side=side,
            action="buy",
            contracts=int(count),
            price_cents=int(price_cents),
            reduce_only=bool(reduce_only),
            post_only=bool(post_only),
            client_order_id=client_order_id,
        )
        log.info("Placing order via SDK: %s", payload)
        try:
            raw = self._sdk.create_order(**payload)
            resp = _to_dict(raw)
            order = resp.get("order", resp)
            status = (order or {}).get("status") if isinstance(order, dict) else None
            if status and status not in _ORDER_CREATE_OK_STATUSES:
                log.warning(
                    "Order placed but status=%s (expected one of %s): %s",
                    status, sorted(_ORDER_CREATE_OK_STATUSES), _truncate_for_log(order),
                )
            return resp
        except Exception as exc:
            log.error("Order placement failed: %s", exc, exc_info=True)
            raise

    def buy_yes(self, ticker: str, contracts: int, price_cents: int, dry_run: bool = True) -> Optional[dict]:
        return self.place_order(ticker, "yes", contracts, price_cents, dry_run=dry_run)

    def buy_no(self, ticker: str, contracts: int, price_cents: int, dry_run: bool = True) -> Optional[dict]:
        return self.place_order(ticker, "no", contracts, price_cents, dry_run=dry_run)

    def sell_yes(
        self,
        ticker: str,
        contracts: int,
        price_cents: int,
        dry_run: bool = True,
        *,
        reduce_only: bool = True,
    ) -> Optional[dict]:
        return self._sell(ticker, "yes", contracts, price_cents, dry_run=dry_run, reduce_only=reduce_only)

    def sell_no(
        self,
        ticker: str,
        contracts: int,
        price_cents: int,
        dry_run: bool = True,
        *,
        reduce_only: bool = True,
    ) -> Optional[dict]:
        return self._sell(ticker, "no", contracts, price_cents, dry_run=dry_run, reduce_only=reduce_only)

    def _sell(
        self,
        ticker: str,
        side: str,
        contracts: int,
        price_cents: int,
        *,
        dry_run: bool,
        reduce_only: bool,
        post_only: bool = False,
        client_order_id: str | None = None,
    ) -> Optional[dict]:
        self._ensure_btc_market(ticker)
        if dry_run:
            log.info(
                "[DRY RUN] Would place SELL %s %s x%d @ %dc (reduce_only=%s post_only=%s)",
                (side or "").upper(),
                ticker,
                contracts,
                price_cents,
                reduce_only,
                post_only,
            )
            return None

        payload = self._build_order_payload(
            ticker=ticker,
            side=side,
            action="sell",
            contracts=int(contracts),
            price_cents=int(price_cents),
            reduce_only=bool(reduce_only),
            post_only=bool(post_only),
            client_order_id=client_order_id,
        )
        log.info("Placing SELL via SDK: %s", payload)
        try:
            raw = self._sdk.create_order(**payload)
            resp = _to_dict(raw)
            return resp
        except Exception as exc:
            log.error("Sell placement failed: %s", exc, exc_info=True)
            raise

    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order by ID."""
        try:
            raw = self._sdk.cancel_order(order_id=order_id)
            return _to_dict(raw)
        except TypeError:
            raw = self._sdk.cancel_order(order_id)
            return _to_dict(raw)
        except Exception as exc:
            log.error("cancel_order failed for %s: %s", order_id, exc, exc_info=True)
            raise

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
