"""
kalshi_money.py — Fixed-point dollar strings and API v2 *_dollars field helpers.

Kalshi represents many currency fields as decimal strings (e.g. "0.5500").
Use these helpers instead of deprecated integer / *_fixed fields.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional


def parse_dollars_to_decimal(value: Any) -> Optional[Decimal]:
    """Parse API dollar string or numeric into Decimal, or None if missing/invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_dollars_to_cents_int(value: Any) -> Optional[int]:
    """Convert a dollar string like '0.55' to whole cents (55)."""
    d = parse_dollars_to_decimal(value)
    if d is None:
        return None
    cents = (d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def get_fill_price_cents(fill: dict, side: str) -> Optional[int]:
    """Best-effort fill price in cents using yes_price_dollars / no_price_dollars, then legacy int fields."""
    side_l = (side or "").lower()
    if side_l == "yes":
        c = parse_dollars_to_cents_int(fill.get("yes_price_dollars"))
        if c is not None:
            return c
        p = fill.get("yes_price")
        if p is not None:
            try:
                return int(p)
            except (TypeError, ValueError):
                return None
        return None
    if side_l == "no":
        c = parse_dollars_to_cents_int(fill.get("no_price_dollars"))
        if c is not None:
            return c
        p = fill.get("no_price")
        if p is not None:
            try:
                return int(p)
            except (TypeError, ValueError):
                return None
        return None
    return None


def fill_fee_cents(fill: dict) -> Optional[int]:
    """Parse fee_cost / fee_cost_dollars on a fill if present (whole cents)."""
    d = parse_dollars_to_cents_int(fill.get("fee_cost_dollars"))
    if d is not None:
        return d
    fc = fill.get("fee_cost")
    if fc is None:
        return None
    try:
        return int(fc)
    except (TypeError, ValueError):
        c = parse_dollars_to_cents_int(fc)
        return c


def position_average_price_cents(pos: dict, default_cents: int = 99) -> int:
    """
    Average entry price in cents for a market_positions row.
    Prefer *_dollars fields from the API; fall back to legacy integer average_price.
    """
    for key in ("average_price_dollars", "avg_price_dollars", "avg_entry_price_dollars"):
        c = parse_dollars_to_cents_int(pos.get(key))
        if c is not None:
            return c
    ap = pos.get("average_price")
    if ap is not None:
        try:
            return int(ap)
        except (TypeError, ValueError):
            pass
    return default_cents


def fmt_cents(v: "int | None") -> str:
    """Format a cent-valued integer for logging; returns 'NA' when the value is None."""
    return "NA" if v is None else str(v)


def enrich_market_quotes_from_dollar_fields(market: dict) -> dict:
    """
    Populate legacy cent keys (yes_bid, yes_ask, …, last_price) from *_dollars
    fields when the API no longer sends integer quote columns on market objects.

    Mutates and returns *market* for chaining.
    """
    mapping = (
        ("yes_bid_dollars", "yes_bid"),
        ("yes_ask_dollars", "yes_ask"),
        ("no_bid_dollars", "no_bid"),
        ("no_ask_dollars", "no_ask"),
    )
    for d_key, c_key in mapping:
        cents = parse_dollars_to_cents_int(market.get(d_key))
        if cents is not None:
            market[c_key] = cents

    lp = parse_dollars_to_cents_int(market.get("last_price_dollars"))
    if lp is not None:
        market["last_price"] = lp

    yp = parse_dollars_to_cents_int(market.get("yes_price_dollars"))
    if yp is not None:
        market["yes_price"] = yp
    np_ = parse_dollars_to_cents_int(market.get("no_price_dollars"))
    if np_ is not None:
        market["no_price"] = np_

    # Mirror into best_* names used by the bot/strategy when only REST market payload exists.
    if market.get("yes_bid") is not None:
        market.setdefault("best_yes_bid", market.get("yes_bid"))
    if market.get("yes_ask") is not None:
        market.setdefault("best_yes_ask", market.get("yes_ask"))
    if market.get("no_bid") is not None:
        market.setdefault("best_no_bid", market.get("no_bid"))
    if market.get("no_ask") is not None:
        market.setdefault("best_no_ask", market.get("no_ask"))

    return market


def fmt_cents(value) -> str:
    """Format a cent-valued integer for logging; returns 'NA' when the value is None."""
    if value is None:
        return "NA"
    try:
        return f"{int(round(value))}c"
    except (TypeError, ValueError):
        return "NA"
