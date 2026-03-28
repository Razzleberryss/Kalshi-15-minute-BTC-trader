#!/usr/bin/env python3
"""
openclaw_kalshi.py - CLI for OpenClaw agent to trade Kalshi hourly BTC markets.

Designed to be invoked by an OpenClaw skill via shell commands.
All output is JSON by default; use --human for readable output.

Flag convention (no ambiguity):
  --series   Series code (e.g. KXBTCD).  Always auto-resolves to the best
             live market ticker before any market-specific API call.
  --ticker   Exact market ticker (e.g. KXBTCD-28MAR2615-B85000).
             Used directly — must include the date and strike suffix.
  When --ticker is omitted, the command resolves from --series automatically.
  A bare series code passed as --ticker is always rejected.

Safety gates (enforced in code, not optional prompt text):
  - KALSHI_TRADING_LIVE=1 env var required for real orders (or --dry-run)
  - ~/.openclaw/workspace/STOP_TRADING file presence halts all trading
  - All risk limits from config.py are enforced by KalshiClient / CLI

Subcommands:
  status              Balance, positions, active hourly market
  markets             List open markets in the configured series
  orderbook           Orderbook for a live market
  buy                 Buy contracts
  sell                Sell contracts

Invariant:
  No command may pass a series code to get_orderbook(), get_market(),
  place_order(), or sell_position().  All resolution goes through
  _resolve_ticker_from_args() → resolve_live_market_ticker().
"""

import argparse
import datetime
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Optional

STOP_FILE = Path.home() / ".openclaw" / "workspace" / "STOP_TRADING"
PROJECT_DIR = Path(__file__).resolve().parent

# Override BTC_SERIES_TICKER before config import so KalshiClient's
# safety prefix check matches hourly tickers.
_series_override = os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD")
os.environ.setdefault("BTC_SERIES_TICKER", _series_override)

sys.path.insert(0, str(PROJECT_DIR))
import config  # noqa: E402
from kalshi_client import KalshiClient  # noqa: E402

log = logging.getLogger("openclaw_kalshi")


def _check_stop_file():
    if STOP_FILE.exists():
        _die(
            f"STOP_TRADING file exists at {STOP_FILE}. Remove it to resume trading.",
            error_code="stop_trading",
        )


def _check_live_gate(args):
    """Refuse real orders unless KALSHI_TRADING_LIVE=1 or --dry-run."""
    if getattr(args, "dry_run", False):
        return True
    if os.environ.get("KALSHI_TRADING_LIVE") != "1":
        _die(
            "Real trading blocked. Set KALSHI_TRADING_LIVE=1 to enable, "
            "or pass --dry-run.",
            error_code="live_trading_blocked",
        )
    return False


def _die(msg: str, code: int = 1, *, error_code: Optional[str] = None):
    payload = {"error": msg}
    if error_code is not None:
        payload["error_code"] = error_code
    print(json.dumps(payload))
    sys.exit(code)


def _out(data: dict, human: bool = False):
    if human:
        for k, v in data.items():
            print(f"  {k}: {v}")
    else:
        print(json.dumps(data, indent=2, default=str))


def _debug_print(msg: str, debug: bool):
    """Print debug info to stderr so it doesn't corrupt JSON stdout."""
    if debug:
        print(f"[DEBUG] {msg}", file=sys.stderr)


def _is_exact_market_ticker(value: str, series: str) -> bool:
    """True if value looks like 'SERIES-...' (exact market ticker, not bare series)."""
    return value.startswith(f"{series}-") and len(value) > len(series) + 1


def _resolve_ticker_from_args(
    client: KalshiClient,
    args,
    caller: str,
) -> str:
    """Shared ticker resolution used by orderbook, buy, and sell.

    Rules (single source of truth for --series / --ticker):
      - If --ticker is given → validate exact market ticker, use directly.
      - If --ticker is omitted → resolve from --series via resolve_live_market_ticker().
      - A bare series code passed as --ticker is always rejected.
    Returns the validated exact market ticker string.
    """
    series = args.series
    debug = getattr(args, "debug", False)
    explicit_ticker = getattr(args, "ticker", None)

    if explicit_ticker:
        ticker = explicit_ticker.upper()
        _debug_print(f"[{caller}] Using explicit --ticker: {ticker}", debug)
        if not _is_exact_market_ticker(ticker, series):
            _die(
                f"--ticker '{ticker}' is not a valid market ticker for series '{series}'. "
                f"Expected format: {series}-<date>-<strike> "
                f"(e.g. {series}-28MAR2615-B85000). "
                f"To auto-resolve the live market, omit --ticker and use --series.",
                error_code="invalid_ticker",
            )
        return ticker

    _debug_print(f"[{caller}] No --ticker; resolving from --series '{series}'", debug)
    try:
        ticker = resolve_live_market_ticker(client, series, debug=debug)
    except RuntimeError as exc:
        _die(f"series_resolution_failed: {exc}", error_code="series_resolution_failed")
    except Exception as exc:
        _die(
            f"series_resolution_network_error: Could not reach Kalshi API "
            f"to resolve series '{series}': {exc}",
            error_code="series_resolution_network_error",
        )
    return ticker


# ── Series-to-ticker resolution ───────────────────────────────────────────────


def resolve_live_market_ticker(
    client: KalshiClient,
    series: str,
    debug: bool = False,
) -> str:
    """
    Resolve a series code (e.g. KXBTCD) to the best currently-live market ticker.

    Flow:
      1. GET /markets?series_ticker=SERIES&status=open
      2. Filter to tickers that start with '{SERIES}-'
      3. Drop provisional markets
      4. Drop markets whose close_time is in the past (UTC)
      5. Sort ascending by close_time (soonest = current live window)
      6. Return the first ticker

    Raises RuntimeError with a detailed message if no live market is found.
    """
    _debug_print(f"Resolving series '{series}' to live market ticker...", debug)

    all_markets = client.list_markets(series, status="open", limit=100)

    _debug_print(
        f"API returned {len(all_markets)} markets for series_ticker={series}",
        debug,
    )

    if not all_markets:
        raise RuntimeError(
            f"No markets returned by Kalshi API for series_ticker={series} "
            f"with status=open. The series may be inactive or misspelled."
        )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    _debug_print(f"Current UTC time: {now_utc.isoformat()}", debug)

    candidates = []
    filtered_out = []

    for m in all_markets:
        ticker = m.get("ticker", "")
        status = m.get("status", "")
        close_time_str = m.get("close_time", "")
        is_provisional = m.get("is_provisional", False)

        reasons = []

        if not ticker.startswith(f"{series}-"):
            reasons.append(f"ticker '{ticker}' missing prefix '{series}-'")

        if is_provisional:
            reasons.append("is_provisional=True")

        close_dt = None
        if close_time_str:
            try:
                close_dt = datetime.datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
                if close_dt <= now_utc:
                    reasons.append(f"close_time {close_time_str} is in the past")
            except (ValueError, TypeError):
                reasons.append(f"unparseable close_time '{close_time_str}'")

        if reasons:
            filtered_out.append({"ticker": ticker, "reasons": reasons})
            _debug_print(f"  FILTERED OUT: {ticker} — {'; '.join(reasons)}", debug)
        else:
            candidates.append(m)
            _debug_print(
                f"  CANDIDATE: {ticker} | status={status} "
                f"| close_time={close_time_str} | title={m.get('title', '')!r}",
                debug,
            )

    if not candidates:
        detail_lines = [
            f"No live markets found for series '{series}'.",
            f"Total markets from API: {len(all_markets)}",
            f"All were filtered out:",
        ]
        for fo in filtered_out[:10]:
            detail_lines.append(f"  {fo['ticker']}: {'; '.join(fo['reasons'])}")
        if len(filtered_out) > 10:
            detail_lines.append(f"  ... and {len(filtered_out) - 10} more")
        raise RuntimeError("\n".join(detail_lines))

    candidates.sort(key=lambda m: (m.get("close_time", ""), -(m.get("volume", 0) or 0)))
    chosen = candidates[0]
    chosen_ticker = chosen["ticker"]

    _debug_print(
        f"RESOLVED: {chosen_ticker} (close_time={chosen.get('close_time')}, "
        f"volume={chosen.get('volume', 0)}, out of {len(candidates)} candidates)",
        debug,
    )

    return chosen_ticker


# ── Subcommands ─────────────────────────────────────────────────────────────


def cmd_status(client: KalshiClient, args):
    series = args.series
    balance = client.get_balance()
    positions = client.get_positions()

    markets = client.list_markets(series, status="open", limit=20)
    hourly_markets = [
        m
        for m in markets
        if m.get("ticker", "").startswith(f"{series}-")
        and not m.get("is_provisional", False)
    ]
    hourly_markets.sort(key=lambda m: m.get("close_time", ""))

    btc_positions = [
        p for p in positions if p.get("ticker", "").startswith(f"{series}-")
    ]

    result = {
        "balance_dollars": balance,
        "series_ticker": series,
        "open_hourly_markets": len(hourly_markets),
        "next_close": hourly_markets[0].get("close_time") if hourly_markets else None,
        "btc_positions": [
            {
                "ticker": p.get("ticker"),
                "side": "yes" if p.get("position", 0) > 0 else "no",
                "quantity": abs(p.get("position", 0)),
            }
            for p in btc_positions
            if p.get("position", 0) != 0
        ],
        "total_positions": len([p for p in positions if p.get("position", 0) != 0]),
        "live_trading": os.environ.get("KALSHI_TRADING_LIVE") == "1",
        "dry_run_config": config.DRY_RUN,
        "stop_file_present": STOP_FILE.exists(),
    }
    _out(result, args.human)


def cmd_markets(client: KalshiClient, args):
    series = args.series
    markets = client.list_markets(series, status="open", limit=100)
    markets = [
        m
        for m in markets
        if m.get("ticker", "").startswith(f"{series}-")
        and not m.get("is_provisional", False)
    ]
    markets.sort(key=lambda m: m.get("close_time", ""))

    rows = []
    for m in markets:
        rows.append(
            {
                "ticker": m.get("ticker"),
                "title": m.get("title", ""),
                "subtitle": m.get("subtitle", ""),
                "status": m.get("status", ""),
                "close_time": m.get("close_time"),
                "yes_ask": m.get("yes_ask"),
                "yes_bid": m.get("yes_bid"),
                "no_ask": m.get("no_ask"),
                "no_bid": m.get("no_bid"),
                "last_price": m.get("last_price"),
                "volume": m.get("volume"),
            }
        )

    result = {"series": series, "count": len(rows), "markets": rows}
    _out(result, args.human)


def _parse_bid_array(bid_array):
    """Parse bid arrays from Kalshi orderbook formats into [(price_cents, size), ...]."""
    if not bid_array:
        return []
    parsed = []
    for entry in bid_array:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            if isinstance(entry[0], str):
                try:
                    price_cents = round(float(entry[0]) * 100)
                    size = int(float(entry[1]))
                    parsed.append((price_cents, size))
                except (ValueError, TypeError):
                    continue
            else:
                price = entry[0]
                if isinstance(price, float) and 0.0 <= price <= 1.0:
                    price_cents = int(round(price * 100))
                else:
                    price_cents = int(price)
                parsed.append((price_cents, int(entry[1])))
    return parsed


def _extract_raw_bids(raw_ob: dict) -> tuple:
    """Extract (yes_bids_raw, no_bids_raw) from orderbook response."""
    ob_data = raw_ob.get("orderbook", {})
    ob_fp = raw_ob.get("orderbook_fp", {})

    yes_raw = (
        ob_fp.get("yes_dollars_fp")
        or ob_fp.get("yes_dollars")
        or ob_data.get("yes_dollars_fp")
        or ob_data.get("yes_dollars")
        or raw_ob.get("yes_dollars")
        or ob_data.get("yes", [])
        or raw_ob.get("yes", [])
    )
    no_raw = (
        ob_fp.get("no_dollars_fp")
        or ob_fp.get("no_dollars")
        or ob_data.get("no_dollars_fp")
        or ob_data.get("no_dollars")
        or raw_ob.get("no_dollars")
        or ob_data.get("no", [])
        or raw_ob.get("no", [])
    )
    return yes_raw or [], no_raw or []


def cmd_orderbook(client: KalshiClient, args):
    series = args.series
    debug = getattr(args, "debug", False)
    explicit_ticker = getattr(args, "ticker", None)

    ticker = _resolve_ticker_from_args(client, args, caller="orderbook")
    _debug_print(f"Final orderbook request target: {ticker}", debug)

    try:
        raw_ob = client.get_orderbook(ticker)
    except Exception as exc:
        _die(
            f"orderbook_fetch_error: GET /markets/{ticker}/orderbook failed: {exc}",
            error_code="orderbook_fetch_error",
        )

    _debug_print(f"Raw orderbook response keys: {list(raw_ob.keys())}", debug)
    ob_data = raw_ob.get("orderbook", {})
    ob_fp = raw_ob.get("orderbook_fp", {})
    _debug_print(
        f"orderbook sub-keys: {list(ob_data.keys()) if ob_data else '(missing)'}",
        debug,
    )
    _debug_print(
        f"orderbook_fp sub-keys: {list(ob_fp.keys()) if ob_fp else '(missing)'}",
        debug,
    )
    if debug:
        _debug_print(
            f"Raw orderbook (truncated): {json.dumps(raw_ob, default=str)[:2000]}",
            True,
        )

    yes_raw, no_raw = _extract_raw_bids(raw_ob)
    _debug_print(f"yes entries: {len(yes_raw)}, no entries: {len(no_raw)}", debug)

    market_data = {}
    try:
        market_info = client.get_market(ticker)
        market_data = market_info.get("market", market_info)
    except Exception as exc:
        _debug_print(f"Could not fetch market details: {exc}", debug)

    if not yes_raw and not no_raw:
        result = {
            "ticker": ticker,
            "resolved_from_series": series if not explicit_ticker else None,
            "title": market_data.get("title", ""),
            "close_time": market_data.get("close_time"),
            "error": "orderbook_empty",
            "message": (
                f"Orderbook for '{ticker}' has zero bids on both sides. "
                f"The market may have just opened or have no liquidity."
            ),
            "raw_response_keys": list(raw_ob.keys()),
            "orderbook_subkeys": list(ob_data.keys()) if ob_data else [],
        }
        _out(result, args.human)
        return

    yes_bids = _parse_bid_array(yes_raw)
    no_bids = _parse_bid_array(no_raw)

    best_yes_bid = yes_bids[0][0] if yes_bids else None
    best_no_bid = no_bids[0][0] if no_bids else None
    best_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
    best_no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None

    if best_yes_bid is not None and best_yes_ask is not None:
        mid_price = (best_yes_bid + best_yes_ask) // 2
        spread = (best_yes_ask - best_yes_bid) / 100.0
    elif best_no_bid is not None and best_no_ask is not None:
        mid_price = 100 - ((best_no_bid + best_no_ask) // 2)
        spread = None
    else:
        mid_price = None
        spread = None

    result = {
        "ticker": ticker,
        "resolved_from_series": series if not explicit_ticker else None,
        "title": market_data.get("title", ""),
        "close_time": market_data.get("close_time"),
        "best_yes_bid": best_yes_bid,
        "best_yes_ask": best_yes_ask,
        "best_no_bid": best_no_bid,
        "best_no_ask": best_no_ask,
        "mid_price": mid_price,
        "spread": spread,
        "yes_bid_levels": len(yes_bids),
        "no_bid_levels": len(no_bids),
    }

    if best_yes_bid is None and best_no_bid is None:
        result["warning"] = (
            f"Raw orderbook had {len(yes_raw)} YES and {len(no_raw)} NO entries "
            f"but none could be parsed. Possible response format mismatch."
        )

    _out(result, args.human)


def cmd_buy(client: KalshiClient, args):
    _check_stop_file()
    dry_run = _check_live_gate(args)

    ticker = _resolve_ticker_from_args(client, args, caller="buy")
    side = args.side.lower()
    count = args.count
    price_cents = args.price

    if side not in ("yes", "no"):
        _die("side must be 'yes' or 'no'", error_code="invalid_side")
    if count < 1:
        _die("count must be >= 1", error_code="invalid_count")
    if not (1 <= price_cents <= 99):
        _die("price must be 1-99 (cents)", error_code="invalid_price_range")
    if not (
        config.MIN_CONTRACT_PRICE_CENTS
        <= price_cents
        <= config.MAX_CONTRACT_PRICE_CENTS
    ):
        _die(
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]",
            error_code="price_outside_config_range",
        )

    cost_dollars = count * price_cents / 100
    if cost_dollars > config.MAX_TRADE_DOLLARS:
        _die(
            f"Order cost ${cost_dollars:.2f} exceeds MAX_TRADE_DOLLARS "
            f"${config.MAX_TRADE_DOLLARS:.2f}",
            error_code="exceeds_max_trade_dollars",
        )

    client_order_id = str(uuid.uuid4())
    mode = "DRY_RUN" if dry_run else "LIVE"

    audit = {
        "action": "BUY",
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": price_cents,
        "cost_dollars": cost_dollars,
        "client_order_id": client_order_id,
        "mode": mode,
    }

    if dry_run:
        audit["result"] = "simulated"
        _out(audit, args.human)
        return

    result = client.place_order(ticker, side, count, price_cents, dry_run=False)
    order = result.get("order", {}) if result else {}
    audit["order_id"] = order.get("order_id")
    audit["status"] = order.get("status")
    audit["result"] = "placed"
    _out(audit, args.human)


def cmd_sell(client: KalshiClient, args):
    _check_stop_file()
    dry_run = _check_live_gate(args)

    ticker = _resolve_ticker_from_args(client, args, caller="sell")
    side = args.side.lower()
    count = args.count
    price_cents = args.price

    if side not in ("yes", "no"):
        _die("side must be 'yes' or 'no'", error_code="invalid_side")
    if count < 1:
        _die("count must be >= 1", error_code="invalid_count")
    if not (1 <= price_cents <= 99):
        _die("price must be 1-99 (cents)", error_code="invalid_price_range")
    if not (
        config.MIN_CONTRACT_PRICE_CENTS
        <= price_cents
        <= config.MAX_CONTRACT_PRICE_CENTS
    ):
        _die(
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]",
            error_code="price_outside_config_range",
        )

    held = client.contracts_held_on_side(ticker, side)
    if held == 0:
        _die(
            f"no_position: no open {side.upper()} contracts for {ticker}. "
            f"Check `status` for positions (YES = long YES, NO = long NO).",
            error_code="no_position",
        )
    if count > held:
        _die(
            f"insufficient_position: sell count {count} exceeds {side.upper()} "
            f"position size {held} on {ticker}",
            error_code="insufficient_position",
        )

    client_order_id = str(uuid.uuid4())
    mode = "DRY_RUN" if dry_run else "LIVE"

    audit = {
        "action": "SELL",
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": price_cents,
        "position_held": held,
        "client_order_id": client_order_id,
        "mode": mode,
    }

    if dry_run:
        audit["result"] = "simulated"
        _out(audit, args.human)
        return

    result = client.sell_position(ticker, side, count, price_cents, dry_run=False)
    order = result.get("order", {}) if result else {}
    audit["order_id"] = order.get("order_id")
    audit["status"] = order.get("status")
    audit["result"] = "placed"
    _out(audit, args.human)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw CLI for Kalshi hourly BTC trading",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Log level (default: WARNING)",
    )

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--series",
        default=os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD"),
        help="Kalshi series ticker (default: KXBTCD)",
    )
    shared.add_argument("--human", action="store_true", help="Human-readable output")
    shared.add_argument("--json", action="store_true", help="JSON output (default)")
    shared.add_argument(
        "--debug", action="store_true", help="Verbose debug output to stderr"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "status", parents=[shared], help="Balance, positions, active hourly market"
    )
    sub.add_parser("markets", parents=[shared], help="List open markets in the series")

    ob = sub.add_parser(
        "orderbook", parents=[shared], help="Orderbook for a live market"
    )
    ob.add_argument(
        "--ticker",
        default=None,
        help=(
            "Exact market ticker (e.g. KXBTCD-26MAR2815-B87500). "
            "If omitted, resolves the best live ticker from --series."
        ),
    )
    ob.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max orderbook levels to display (currently informational)",
    )

    buy_p = sub.add_parser("buy", parents=[shared], help="Buy contracts")
    buy_p.add_argument(
        "--ticker",
        default=None,
        help=(
            "Exact market ticker (e.g. KXBTCD-28MAR2615-B85000). "
            "If omitted, resolves the best live ticker from --series."
        ),
    )
    buy_p.add_argument("side", choices=["yes", "no"], help="Contract side")
    buy_p.add_argument("count", type=int, help="Number of contracts")
    buy_p.add_argument("price", type=int, help="Limit price in cents (1-99)")
    buy_p.add_argument("--dry-run", action="store_true", help="Simulate only")

    sell_p = sub.add_parser("sell", parents=[shared], help="Sell contracts")
    sell_p.add_argument(
        "--ticker",
        default=None,
        help=(
            "Exact market ticker (e.g. KXBTCD-28MAR2615-B85000). "
            "If omitted, resolves the best live ticker from --series."
        ),
    )
    sell_p.add_argument("side", choices=["yes", "no"], help="Contract side")
    sell_p.add_argument("count", type=int, help="Number of contracts")
    sell_p.add_argument("price", type=int, help="Limit price in cents (1-99)")
    sell_p.add_argument("--dry-run", action="store_true", help="Simulate only")

    args = parser.parse_args()

    series = getattr(args, "series", os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD"))
    os.environ["BTC_SERIES_TICKER"] = series
    config.BTC_SERIES_TICKER = series

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config.validate()
    except EnvironmentError as e:
        _die(f"Config error: {e}", error_code="config_error")

    client = KalshiClient()
    dispatch = {
        "status": cmd_status,
        "markets": cmd_markets,
        "orderbook": cmd_orderbook,
        "buy": cmd_buy,
        "sell": cmd_sell,
    }
    try:
        dispatch[args.command](client, args)
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("Dispatch failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        err_obj = {
            "error": f"Command failed: {exc}",
            "error_code": type(exc).__name__,
        }
        print(json.dumps(err_obj))
        sys.exit(1)


if __name__ == "__main__":
    main()
