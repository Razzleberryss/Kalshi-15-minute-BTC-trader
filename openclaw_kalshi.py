#!/usr/bin/env python3
"""
openclaw_kalshi.py – CLI tool for the Kalshi prediction market API.

Subcommands:
  status      – Show account balance and open positions.
  markets     – List markets for a given series (requires --series).
  orderbook   – Show the live orderbook for a market.
                Use --ticker for an exact market ticker (e.g. KXBTCD-26MAR2802),
                or --series to auto-select the current active market.

Usage examples:
  python openclaw_kalshi.py status --json
  python openclaw_kalshi.py markets --series KXBTCD --json
  python openclaw_kalshi.py orderbook --ticker KXBTCD-26MAR2802 --json --limit 10
  python openclaw_kalshi.py orderbook --series KXBTCD --json --limit 10
"""
import argparse
import datetime
import json
import logging
import sys
from typing import Optional, Tuple

import config  # noqa: F401 – imported so .env is loaded before KalshiClient
from kalshi_client import KalshiClient

log = logging.getLogger(__name__)


# ── Datetime helpers ───────────────────────────────────────────────────────────

def _parse_iso_datetime(ts: str) -> Optional[datetime.datetime]:
    """
    Parse an ISO 8601 timestamp string into a timezone-aware datetime (UTC).

    Handles the 'Z' suffix and '+00:00' offset.  If the parsed timestamp is
    naive (no timezone info), it is interpreted as UTC.  Returns None on any
    parse error so callers can safely skip malformed timestamps.
    """
    if not ts:
        return None
    try:
        # Replace 'Z' with '+00:00' for consistent fromisoformat() parsing
        dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
        # Always return a timezone-aware datetime in UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        else:
            dt = dt.astimezone(datetime.timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


# ── Market-selection logic ─────────────────────────────────────────────────────

def find_active_market(
    client: KalshiClient,
    series: str,
) -> Tuple[Optional[dict], str]:
    """
    Find the current live/active market for *series* (e.g. "KXBTCD").

    Selection strategy
    ------------------
    1. Fetch markets with ``status=open`` for the series (up to 20).
    2. Filter out provisional markets (``is_provisional=True``).
    3. Among remaining markets, prefer those whose ``open_time .. close_time``
       window **spans the current UTC time** – i.e. the contract is live right now.
       Sort those by ``close_time`` ascending and return the soonest-expiring one
       (= the contract currently being traded).
    4. If no market spans the current time, fall back to the market with the
       soonest ``close_time`` that is still in the future (next-imminent contract).
    5. If the ``status=open`` query returns nothing, retry without a status
       filter in case the Kalshi API uses a different status value for live markets.

    Returns
    -------
    ``(market_dict, reason_string)`` on success, or ``(None, error_message)``
    when no suitable market can be found.
    """
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    log.debug("Searching for active market in series=%s at %s", series, now_utc.isoformat())

    # --- first attempt: status=open ------------------------------------------
    try:
        markets = client.get_markets(series, status="open", limit=20)
        log.debug("status=open query returned %d markets", len(markets))
    except Exception as exc:
        return None, f"API error fetching markets for series {series!r}: {exc}"

    # --- fallback: no status filter ------------------------------------------
    if not markets:
        log.debug("No open markets found; retrying without status filter")
        try:
            markets = client.get_markets(series, limit=20)
            log.debug("No-status query returned %d markets", len(markets))
        except Exception as exc:
            return None, f"API error fetching markets for series {series!r}: {exc}"

    if not markets:
        return None, f"No markets found for series {series!r}"

    # --- filter out provisional markets --------------------------------------
    non_provisional = [m for m in markets if not m.get("is_provisional", False)]
    if not non_provisional:
        reason = f"All markets for series {series!r} are provisional; none selected"
        log.debug(reason)
        return None, reason
    markets = non_provisional
    log.debug("%d non-provisional markets after filtering", len(markets))

    # --- find markets whose window spans now ---------------------------------
    # Build enriched tuples (market, close_dt) so sorting uses the parsed datetime,
    # not the raw ISO string (which could have formatting/timezone differences).
    spanning: list = []   # (market, close_dt)
    future: list = []     # (market, close_dt)
    for m in markets:
        close_dt = _parse_iso_datetime(m.get("close_time", ""))
        open_dt = _parse_iso_datetime(m.get("open_time", ""))

        if close_dt is None:
            # No close_time: can't evaluate; skip for time-based selection
            continue

        if close_dt > now_utc:
            future.append((m, close_dt))
            # "Spans now" = close is in the future AND (no open_time OR open_time is in the past)
            if open_dt is None or open_dt <= now_utc:
                spanning.append((m, close_dt))

    if spanning:
        spanning.sort(key=lambda t: t[1])
        selected = spanning[0][0]
        ticker = selected.get("ticker", "?")
        reason = "nearest active hourly market"
        log.debug("Selected ticker=%s (%s)", ticker, reason)
        return selected, reason

    if future:
        future.sort(key=lambda t: t[1])
        selected = future[0][0]
        ticker = selected.get("ticker", "?")
        reason = "nearest upcoming market (no contract spans current time)"
        log.debug("Selected ticker=%s (%s)", ticker, reason)
        return selected, reason

    # Markets exist but all have already expired or are missing close_time.
    # Returning an expired/invalid ticker would silently query a dead market,
    # so surface a structured error instead.
    tickers = [m.get("ticker", "?") for m in markets]
    reason = (
        f"No active market with future close_time found for series {series!r}; "
        f"{len(markets)} markets returned by API but all are expired or missing close_time. "
        f"Tickers: {tickers}"
    )
    log.warning(
        "No active market for series %s; all %d markets expired or missing close_time. Tickers=%s",
        series, len(markets), tickers,
    )
    return None, reason


# ── Orderbook trimming helper ─────────────────────────────────────────────────

def _trim_orderbook(ob_response: dict, limit: int) -> dict:
    """
    Return a copy of *ob_response* with bid arrays truncated to *limit* entries.

    Handles all orderbook formats returned by the Kalshi API (integer-cents,
    dollar-string, and fixed-point variants) without mutating the original dict.
    """
    if not ob_response:
        return ob_response

    _BID_KEYS = (
        "yes", "no",
        "yes_dollars", "no_dollars",
        "yes_dollars_fp", "no_dollars_fp",
    )

    result = dict(ob_response)

    # Trim inside the nested "orderbook" dict if present
    if "orderbook" in result and isinstance(result["orderbook"], dict):
        inner = dict(result["orderbook"])
        for key in _BID_KEYS:
            if key in inner and isinstance(inner[key], list):
                inner[key] = inner[key][:limit]
        result["orderbook"] = inner

    # Also trim any top-level bid arrays (some API variants put them here)
    for key in _BID_KEYS:
        if key in result and isinstance(result[key], list):
            result[key] = result[key][:limit]

    return result


# ── Subcommand handlers ────────────────────────────────────────────────────────

def cmd_status(client: KalshiClient, _args) -> dict:
    """Return account status as a JSON-serialisable dict."""
    balance = client.get_balance()
    positions = client.get_positions()
    return {
        "balance_dollars": balance,
        "open_positions": len(positions),
        "positions": positions,
    }


def cmd_markets(client: KalshiClient, args) -> dict:
    """List markets for a series and return as a JSON-serialisable dict."""
    series = args.series.upper()
    status_filter = getattr(args, "status", None)
    limit = getattr(args, "limit", 20)

    markets = client.get_markets(series, status=status_filter, limit=limit)
    return {
        "series": series,
        "count": len(markets),
        "markets": markets,
    }


def cmd_orderbook(client: KalshiClient, args) -> dict:
    """
    Fetch and return the orderbook for a market.

    If ``--ticker`` is given, call the orderbook endpoint directly.
    If only ``--series`` is given, auto-select the current live market first.
    ``--limit`` controls how many bid levels per side are included in the output.
    """
    ticker: Optional[str] = getattr(args, "ticker", None) or None
    series: Optional[str] = getattr(args, "series", None) or None
    limit: int = getattr(args, "limit", 10)

    # ---- Direct ticker mode ----
    if ticker:
        ticker = ticker.upper()
        log.debug("Direct ticker mode: ticker=%s", ticker)
        try:
            ob_response = client.get_orderbook(ticker)
        except Exception as exc:
            return {"error": str(exc), "ticker": ticker}
        return {
            "ticker": ticker,
            "reason": "explicit --ticker argument",
            "orderbook": _trim_orderbook(ob_response, limit),
        }

    # ---- Series auto-select mode ----
    if not series:
        return {
            "error": "Either --ticker or --series is required for the orderbook subcommand."
        }

    series = series.upper()
    log.debug("Series auto-select mode: series=%s", series)

    market, reason = find_active_market(client, series)
    if market is None:
        return {
            "error": f"No active market found for series {series}",
            "series": series,
            "detail": reason,
        }

    selected_ticker = market.get("ticker", "")
    if not selected_ticker:
        return {
            "error": "Selected market is missing a ticker field",
            "series": series,
        }
    log.debug("Auto-selected ticker=%s reason=%r", selected_ticker, reason)

    try:
        ob_response = client.get_orderbook(selected_ticker)
    except Exception as exc:
        return {
            "error": str(exc),
            "series": series,
            "selected_ticker": selected_ticker,
        }

    return {
        "series": series,
        "selected_ticker": selected_ticker,
        "reason": reason,
        "market_close_time": market.get("close_time"),
        "market_status": market.get("status"),
        "orderbook": _trim_orderbook(ob_response, limit),
    }


# ── Argument parser ────────────────────────────────────────────────────────────

def _positive_int(value: str) -> int:
    """argparse type that accepts only integers >= 1."""
    try:
        n = int(value)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid integer")
    if n < 1:
        raise argparse.ArgumentTypeError(f"limit must be >= 1, got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openclaw_kalshi.py",
        description="CLI tool for the Kalshi prediction market API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(__doc__.split("Usage examples:")[1].strip()
                if __doc__ and "Usage examples:" in __doc__ else ""),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as pretty-printed JSON (always on; flag kept for compatibility).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable DEBUG logging to stderr.",
    )

    subs = parser.add_subparsers(dest="command", required=True)

    # ---- status ---------------------------------------------------------------
    subs.add_parser(
        "status",
        help="Show account balance and open positions.",
    )

    # ---- markets --------------------------------------------------------------
    markets_p = subs.add_parser(
        "markets",
        help="List markets for a series (requires --series).",
    )
    markets_p.add_argument(
        "--series",
        required=True,
        metavar="SERIES",
        help="Kalshi series ticker, e.g. KXBTCD.",
    )
    markets_p.add_argument(
        "--status",
        default=None,
        metavar="STATUS",
        help="Filter by market status (e.g. 'open', 'closed'). Omit to return all statuses.",
    )
    markets_p.add_argument(
        "--limit",
        type=_positive_int,
        default=20,
        metavar="N",
        help="Maximum number of markets to return (default: 20, must be >= 1).",
    )

    # ---- orderbook ------------------------------------------------------------
    ob_p = subs.add_parser(
        "orderbook",
        help=(
            "Show the live orderbook for a market.  "
            "Use --ticker for an exact market (e.g. KXBTCD-26MAR2802), "
            "or --series to auto-select the current live contract."
        ),
    )
    ob_p.add_argument(
        "--ticker",
        default=None,
        metavar="TICKER",
        help=(
            "Exact market ticker (e.g. KXBTCD-26MAR2802).  "
            "When provided, --series is ignored for market selection "
            "(it is still shown in log output if supplied)."
        ),
    )
    ob_p.add_argument(
        "--series",
        default=None,
        metavar="SERIES",
        help=(
            "Kalshi series ticker (e.g. KXBTCD).  "
            "Auto-selects the currently live market when --ticker is not given."
        ),
    )
    ob_p.add_argument(
        "--limit",
        type=_positive_int,
        default=10,
        metavar="N",
        help="Maximum number of bid levels per side to include in the output (default: 10, must be >= 1).",
    )

    return parser


# ── Main entry point ──────────────────────────────────────────────────────────

def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging (to stderr so it doesn't pollute JSON stdout)
    log_level = logging.DEBUG if args.debug else logging.WARNING
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    # Initialise the Kalshi client (reads credentials from .env via config)
    try:
        client = KalshiClient()
    except Exception as exc:
        out = {"error": f"Failed to initialise Kalshi client: {exc}"}
        print(json.dumps(out, indent=2))
        sys.exit(1)

    # Dispatch to the appropriate subcommand
    if args.command == "status":
        result = cmd_status(client, args)
    elif args.command == "markets":
        result = cmd_markets(client, args)
    elif args.command == "orderbook":
        result = cmd_orderbook(client, args)
    else:
        parser.print_help()
        sys.exit(1)

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
