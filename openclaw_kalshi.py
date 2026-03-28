#!/usr/bin/env python3
"""
openclaw_kalshi.py - CLI for OpenClaw agent to trade Kalshi hourly BTC markets.

Designed to be invoked by an OpenClaw skill via shell commands.
All output is JSON for easy agent parsing; use --human for readable output.

Safety gates (enforced in code, not optional prompt text):
  - KALSHI_TRADING_LIVE=1 env var required for real orders (or --dry-run)
  - ~/.openclaw/workspace/STOP_TRADING file presence halts all trading
  - All risk limits from config.py are enforced by KalshiClient

Subcommands:
  status              Balance, positions, active hourly market
  markets             List open markets in the configured series
  orderbook TICKER    Best bid/ask for a specific market
  buy                 Buy contracts on a specific market
  sell                Sell contracts on a specific market
"""
import argparse
import json
import logging
import os
import sys
import uuid
from pathlib import Path

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
        _die(f"STOP_TRADING file exists at {STOP_FILE}. Remove it to resume trading.")


def _check_live_gate(args):
    """Refuse real orders unless KALSHI_TRADING_LIVE=1 or --dry-run."""
    if getattr(args, "dry_run", False):
        return True  # dry-run is always allowed
    if os.environ.get("KALSHI_TRADING_LIVE") != "1":
        _die(
            "Real trading blocked. Set KALSHI_TRADING_LIVE=1 to enable, "
            "or pass --dry-run."
        )
    return False  # not a dry run


def _die(msg: str, code: int = 1):
    print(json.dumps({"error": msg}))
    sys.exit(code)


def _out(data: dict, human: bool = False):
    if human:
        for k, v in data.items():
            print(f"  {k}: {v}")
    else:
        print(json.dumps(data, indent=2, default=str))


# ── Subcommands ────────────────────────────────────────────────────────────────

def cmd_status(client: KalshiClient, args):
    balance = client.get_balance()
    positions = client.get_positions()

    series = config.BTC_SERIES_TICKER
    params = {"series_ticker": series, "status": "open", "limit": 20}
    data = client._request("GET", "/markets", params=params)
    markets = data.get("markets", [])
    hourly_markets = [
        m for m in markets
        if m.get("ticker", "").startswith(f"{series}-")
        and not m.get("is_provisional", False)
    ]
    hourly_markets.sort(key=lambda m: m.get("close_time", ""))

    btc_positions = [
        p for p in positions
        if p.get("ticker", "").startswith(f"{series}-")
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
    series = config.BTC_SERIES_TICKER
    params = {"series_ticker": series, "status": "open", "limit": 50}
    data = client._request("GET", "/markets", params=params)
    markets = data.get("markets", [])
    markets = [
        m for m in markets
        if m.get("ticker", "").startswith(f"{series}-")
        and not m.get("is_provisional", False)
    ]
    markets.sort(key=lambda m: m.get("close_time", ""))

    rows = []
    for m in markets:
        rows.append({
            "ticker": m.get("ticker"),
            "title": m.get("title", ""),
            "subtitle": m.get("subtitle", ""),
            "close_time": m.get("close_time"),
            "yes_ask": m.get("yes_ask"),
            "yes_bid": m.get("yes_bid"),
            "no_ask": m.get("no_ask"),
            "no_bid": m.get("no_bid"),
            "last_price": m.get("last_price"),
            "volume": m.get("volume"),
        })

    result = {"series": series, "count": len(rows), "markets": rows}
    _out(result, args.human)


def cmd_orderbook(client: KalshiClient, args):
    ticker = args.ticker.upper()
    if not ticker.startswith(f"{config.BTC_SERIES_TICKER}-"):
        _die(f"Ticker '{ticker}' does not match series {config.BTC_SERIES_TICKER}")

    quotes = client.get_market_quotes(ticker)
    market_info = client.get_market(ticker)
    market_data = market_info.get("market", market_info)

    result = {
        "ticker": ticker,
        "title": market_data.get("title", ""),
        "close_time": market_data.get("close_time"),
        "best_yes_bid": quotes.get("best_yes_bid"),
        "best_yes_ask": quotes.get("best_yes_ask"),
        "best_no_bid": quotes.get("best_no_bid"),
        "best_no_ask": quotes.get("best_no_ask"),
        "mid_price": quotes.get("mid_price"),
        "spread": quotes.get("spread"),
        "yes_depth_near_mid": quotes.get("yes_depth_near_mid"),
        "no_depth_near_mid": quotes.get("no_depth_near_mid"),
    }
    _out(result, args.human)


def cmd_buy(client: KalshiClient, args):
    _check_stop_file()
    dry_run = _check_live_gate(args)

    ticker = args.ticker.upper()
    side = args.side.lower()
    count = args.count
    price_cents = args.price

    if side not in ("yes", "no"):
        _die("side must be 'yes' or 'no'")
    if count < 1:
        _die("count must be >= 1")
    if not (1 <= price_cents <= 99):
        _die("price must be 1-99 (cents)")
    if not (config.MIN_CONTRACT_PRICE_CENTS <= price_cents <= config.MAX_CONTRACT_PRICE_CENTS):
        _die(
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]"
        )

    cost_dollars = count * price_cents / 100
    if cost_dollars > config.MAX_TRADE_DOLLARS:
        _die(
            f"Order cost ${cost_dollars:.2f} exceeds MAX_TRADE_DOLLARS "
            f"${config.MAX_TRADE_DOLLARS:.2f}"
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

    ticker = args.ticker.upper()
    side = args.side.lower()
    count = args.count
    price_cents = args.price

    if side not in ("yes", "no"):
        _die("side must be 'yes' or 'no'")
    if count < 1:
        _die("count must be >= 1")
    if not (1 <= price_cents <= 99):
        _die("price must be 1-99 (cents)")

    client_order_id = str(uuid.uuid4())
    mode = "DRY_RUN" if dry_run else "LIVE"

    audit = {
        "action": "SELL",
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": price_cents,
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw CLI for Kalshi hourly BTC trading",
    )
    parser.add_argument(
        "--series", default=os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD"),
        help="Kalshi series ticker (default: KXBTCD)",
    )
    parser.add_argument("--human", action="store_true", help="Human-readable output")
    parser.add_argument(
        "--log-level", default="WARNING", help="Log level (default: WARNING)",
    )
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--human", action="store_true", help="Human-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", parents=[shared], help="Balance, positions, active hourly market")
    sub.add_parser("markets", parents=[shared], help="List open markets in the series")

    ob = sub.add_parser("orderbook", parents=[shared], help="Orderbook for a specific market")
    ob.add_argument("ticker", help="Market ticker (e.g. KXBTCD-26MAR2815-B87500)")

    buy_p = sub.add_parser("buy", parents=[shared], help="Buy contracts")
    buy_p.add_argument("ticker", help="Market ticker")
    buy_p.add_argument("side", choices=["yes", "no"], help="Contract side")
    buy_p.add_argument("count", type=int, help="Number of contracts")
    buy_p.add_argument("price", type=int, help="Limit price in cents (1-99)")
    buy_p.add_argument("--dry-run", action="store_true", help="Simulate only")

    sell_p = sub.add_parser("sell", parents=[shared], help="Sell contracts")
    sell_p.add_argument("ticker", help="Market ticker")
    sell_p.add_argument("side", choices=["yes", "no"], help="Contract side")
    sell_p.add_argument("count", type=int, help="Number of contracts")
    sell_p.add_argument("price", type=int, help="Limit price in cents (1-99)")
    sell_p.add_argument("--dry-run", action="store_true", help="Simulate only")

    args = parser.parse_args()

    # Apply series override before any client calls
    os.environ["BTC_SERIES_TICKER"] = args.series
    config.BTC_SERIES_TICKER = args.series

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config.validate()
    except EnvironmentError as e:
        _die(f"Config error: {e}")

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
    except Exception as exc:
        _die(f"Command failed: {exc}")


if __name__ == "__main__":
    main()
