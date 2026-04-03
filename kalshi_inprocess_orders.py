"""
kalshi_inprocess_orders.py — Build the same JSON envelopes as openclaw_kalshi buy/sell
without spawning a subprocess. Used by bot.py when INPROCESS_KALSHI_ORDERS is enabled.
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import config
from kalshi_agent_envelope import failure_envelope, success_envelope

if TYPE_CHECKING:
    from kalshi_client import KalshiClient

log = logging.getLogger(__name__)

STOP_FILE = Path.home() / ".openclaw" / "workspace" / "STOP_TRADING"


def buy_envelope(
    client: "KalshiClient",
    ticker: str,
    side: str,
    count: int,
    price_cents: int,
    *,
    dry_run: bool,
) -> dict:
    """Mirror openclaw_kalshi.cmd_buy validation and API calls; return envelope dict."""
    if STOP_FILE.exists():
        return failure_envelope(
            "STOP_TRADING",
            f"STOP_TRADING file exists at {STOP_FILE}. Remove it to resume trading.",
        )

    if not dry_run and os.environ.get("KALSHI_TRADING_LIVE") != "1":
        return failure_envelope(
            "LIVE_TRADING_BLOCKED",
            "Real trading blocked. Set KALSHI_TRADING_LIVE=1 to enable, "
            "or use dry run.",
        )

    side = side.lower()
    if side not in ("yes", "no"):
        return failure_envelope("INVALID_SIDE", "side must be 'yes' or 'no'")
    if count < 1:
        return failure_envelope("INVALID_COUNT", "count must be >= 1")
    if not (1 <= price_cents <= 99):
        return failure_envelope("INVALID_PRICE_RANGE", "price must be 1-99 (cents)")
    if not (
        config.MIN_CONTRACT_PRICE_CENTS
        <= price_cents
        <= config.MAX_CONTRACT_PRICE_CENTS
    ):
        return failure_envelope(
            "PRICE_OUTSIDE_CONFIG_RANGE",
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]",
        )

    cost_dollars = count * price_cents / 100
    if cost_dollars > config.MAX_TRADE_DOLLARS:
        return failure_envelope(
            "EXCEEDS_MAX_TRADE_DOLLARS",
            f"Order cost ${cost_dollars:.2f} exceeds MAX_TRADE_DOLLARS "
            f"${config.MAX_TRADE_DOLLARS:.2f}",
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
        "order_id": None,
        "order_status": None,
    }

    if dry_run:
        return success_envelope("BUY_DRY_RUN", audit)

    try:
        api_result = client.place_order(ticker, side, count, price_cents, dry_run=False)
    except Exception as exc:
        log.error("place_order failed: %s", exc, exc_info=True)
        return failure_envelope(
            "COMMAND_FAILED",
            str(exc),
            details={"exception_type": type(exc).__name__},
        )

    order = api_result.get("order", {}) if api_result else {}
    audit["order_id"] = order.get("order_id")
    audit["order_status"] = order.get("status")
    return success_envelope("BUY_PLACED", audit)


def sell_envelope(
    client: "KalshiClient",
    ticker: str,
    side: str,
    count: int,
    price_cents: int,
    *,
    dry_run: bool,
) -> dict:
    """Mirror openclaw_kalshi.cmd_sell validation and API calls; return envelope dict."""
    if STOP_FILE.exists():
        return failure_envelope(
            "STOP_TRADING",
            f"STOP_TRADING file exists at {STOP_FILE}. Remove it to resume trading.",
        )

    if not dry_run and os.environ.get("KALSHI_TRADING_LIVE") != "1":
        return failure_envelope(
            "LIVE_TRADING_BLOCKED",
            "Real trading blocked. Set KALSHI_TRADING_LIVE=1 to enable, "
            "or use dry run.",
        )

    side = side.lower()
    if side not in ("yes", "no"):
        return failure_envelope("INVALID_SIDE", "side must be 'yes' or 'no'")
    if count < 1:
        return failure_envelope("INVALID_COUNT", "count must be >= 1")
    if not (1 <= price_cents <= 99):
        return failure_envelope("INVALID_PRICE_RANGE", "price must be 1-99 (cents)")
    if not (
        config.MIN_CONTRACT_PRICE_CENTS
        <= price_cents
        <= config.MAX_CONTRACT_PRICE_CENTS
    ):
        return failure_envelope(
            "PRICE_OUTSIDE_CONFIG_RANGE",
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]",
        )

    requested_count = count
    try:
        held = client.contracts_held_on_side(ticker, side)
    except Exception as exc:
        log.error("contracts_held_on_side failed: %s", exc, exc_info=True)
        return failure_envelope(
            "COMMAND_FAILED",
            str(exc),
            details={"exception_type": type(exc).__name__},
        )

    if held == 0:
        return failure_envelope(
            "NO_POSITION",
            f"No open {side.upper()} contracts for {ticker}. "
            f"Use status to inspect positions (YES = long YES, NO = long NO).",
        )

    sell_count = min(requested_count, held)
    clamped = sell_count < requested_count
    client_order_id = str(uuid.uuid4())
    mode = "DRY_RUN" if dry_run else "LIVE"

    audit = {
        "action": "SELL",
        "ticker": ticker,
        "side": side,
        "count": sell_count,
        "requested_count": requested_count,
        "price_cents": price_cents,
        "position_held": held,
        "client_order_id": client_order_id,
        "mode": mode,
        "order_id": None,
        "order_status": None,
    }

    warnings: list = []
    if clamped:
        warnings.append({
            "code": "POSITION_CLAMPED",
            "message": (
                f"Requested sell of {requested_count} contracts exceeds "
                f"{side.upper()} position of {held}; clamped to {sell_count}."
            ),
        })

    if dry_run:
        code = "SELL_CLAMPED" if clamped else "SELL_DRY_RUN"
        return success_envelope(code, audit, warnings)

    try:
        api_result = client.sell_position(
            ticker, side, sell_count, price_cents, dry_run=False,
        )
    except Exception as exc:
        log.error("sell_position failed: %s", exc, exc_info=True)
        return failure_envelope(
            "COMMAND_FAILED",
            str(exc),
            details={"exception_type": type(exc).__name__},
        )

    order = api_result.get("order", {}) if api_result else {}
    audit["order_id"] = order.get("order_id")
    audit["order_status"] = order.get("status")
    code = "SELL_CLAMPED" if clamped else "SELL_PLACED"
    return success_envelope(code, audit, warnings)
