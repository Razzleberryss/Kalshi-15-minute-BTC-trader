"""
Unified orderbook parsing utilities for consistent handling across all modules.

This module consolidates orderbook parsing logic that was previously duplicated
across bot.py, strategy.py, kalshi_client.py, and openclaw_kalshi.py.
"""

from typing import Optional, List, Tuple


def to_price_cents(value) -> int:
    """
    Convert price value to integer cents.

    Handles both string dollar amounts ("0.52") and integer cents (52).
    """
    if isinstance(value, str):
        return int(float(value) * 100)
    return int(value)


def parse_bid_array(bid_array, max_levels: Optional[int] = None) -> List[Tuple[int, int]]:
    """
    Parse bid arrays from Kalshi orderbook formats into standardized tuples.

    Args:
        bid_array: Orderbook array in any supported format (string prices or integer cents)
        max_levels: Optional limit on number of levels to parse (for performance)

    Returns:
        List of (price_cents, size) tuples sorted by price descending (best bid first)

    Examples:
        >>> parse_bid_array([["0.52", 100], ["0.51", 200]])
        [(52, 100), (51, 200)]

        >>> parse_bid_array([[52, 100], [51, 200]])
        [(52, 100), (51, 200)]
    """
    if not bid_array:
        return []

    parsed = []
    # Limit iteration if max_levels specified (performance optimization)
    items = bid_array[:max_levels] if max_levels else bid_array

    for entry in items:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            try:
                price = to_price_cents(entry[0])
                size = int(float(entry[1]))
                parsed.append((price, size))
            except (ValueError, TypeError):
                # Skip malformed entries
                continue

    # Sort by price descending (highest price = best bid); skip for 0–1 entries
    if len(parsed) > 1:
        parsed.sort(key=lambda x: x[0], reverse=True)
    return parsed


def get_best_bid(bid_array) -> Optional[Tuple[int, int]]:
    """
    Extract the best (highest price) bid from orderbook array.

    Args:
        bid_array: Orderbook array in any supported format

    Returns:
        Tuple of (price_cents, size) for best bid, or None if no valid bids

    Examples:
        >>> get_best_bid([["0.52", 100], ["0.51", 200]])
        (52, 100)

        >>> get_best_bid([])
        None
    """
    parsed = parse_bid_array(bid_array, max_levels=1)  # Only need the best
    return parsed[0] if parsed else None


def get_best_bid_price(bid_array) -> Optional[int]:
    """
    Extract just the price of the best bid.

    Args:
        bid_array: Orderbook array in any supported format

    Returns:
        Price in cents, or None if no valid bids
    """
    best = get_best_bid(bid_array)
    return best[0] if best else None


def get_bid_depth(bid_array, top_n: int = 10) -> int:
    """
    Calculate total size (number of contracts) in the top N bid levels.

    Args:
        bid_array: Orderbook array in any supported format
        top_n: Number of top levels to include (default: 10)

    Returns:
        Total number of contracts across top N levels

    Examples:
        >>> get_bid_depth([["0.52", 100], ["0.51", 200], ["0.50", 300]], top_n=2)
        300
    """
    parsed = parse_bid_array(bid_array, max_levels=top_n)
    return sum(size for _, size in parsed)


def get_weighted_bid_liquidity(bid_array, top_n: int = 10) -> float:
    """
    Calculate price-weighted liquidity for top N bid levels.

    This is used for orderbook skew calculations. Each level contributes
    price_cents × size to the total liquidity.

    Args:
        bid_array: Orderbook array in any supported format
        top_n: Number of top levels to include (default: 10)

    Returns:
        Sum of (price × size) across top N levels

    Examples:
        >>> get_weighted_bid_liquidity([["0.52", 100], ["0.51", 200]])
        15400.0  # (52 × 100) + (51 × 200)
    """
    parsed = parse_bid_array(bid_array, max_levels=top_n)
    return sum(float(price * size) for price, size in parsed)


def extract_raw_arrays(orderbook: dict) -> Tuple:
    """
    Extract raw YES and NO bid arrays from orderbook dict without parsing.

    Handles all Kalshi orderbook formats (see extract_yes_no_bids for full list).

    Returns:
        Tuple of (yes_raw, no_raw) where each is the raw list from the orderbook.
    """
    orderbook_fp = orderbook.get("orderbook_fp", {}) or {}
    orderbook_data = orderbook.get("orderbook", {}) or {}

    yes_raw = (
        orderbook_fp.get("yes_dollars_fp")
        or orderbook_fp.get("yes_dollars")
        or orderbook_data.get("yes_dollars_fp")
        or orderbook_data.get("yes_dollars")
        or orderbook.get("yes_dollars")
        or orderbook_data.get("yes")
        or orderbook.get("yes")
        or []
    )

    no_raw = (
        orderbook_fp.get("no_dollars_fp")
        or orderbook_fp.get("no_dollars")
        or orderbook_data.get("no_dollars_fp")
        or orderbook_data.get("no_dollars")
        or orderbook.get("no_dollars")
        or orderbook_data.get("no")
        or orderbook.get("no")
        or []
    )

    return yes_raw, no_raw


def extract_yes_no_bids(orderbook: dict, max_levels: Optional[int] = None) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int]]]:
    """
    Extract and parse both YES and NO bid arrays from orderbook dict.

    Handles multiple orderbook formats:
    - orderbook_fp.yes_dollars_fp / no_dollars_fp (new fixed-point format)
    - orderbook_fp.yes_dollars / no_dollars (older fp variant)
    - orderbook["orderbook"].yes_dollars_fp / no_dollars_fp (WebSocket-wrapped)
    - orderbook["orderbook"].yes_dollars / no_dollars (WebSocket-wrapped)
    - top-level yes_dollars / no_dollars (market-level fields)
    - orderbook["orderbook"].yes / no (legacy integer-cents)

    Args:
        orderbook: Orderbook dict from API or WebSocket
        max_levels: Optional limit on levels to parse per side

    Returns:
        Tuple of (yes_bids, no_bids) where each is a list of (price_cents, size) tuples
    """
    yes_raw, no_raw = extract_raw_arrays(orderbook)

    yes_bids = parse_bid_array(yes_raw, max_levels=max_levels)
    no_bids = parse_bid_array(no_raw, max_levels=max_levels)

    return yes_bids, no_bids
