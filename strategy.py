"""
strategy.py  -  Signal generation for the Kalshi 15-minute BTC bot.

Strategy: Momentum + Orderbook Skew
  1. Pull recent BTC spot price history (yfinance, 1-min bars).
  2. Calculate short-term momentum (% change over last N bars).
  3. Pull Kalshi orderbook to measure YES/NO liquidity skew.
  4. Combine signals to produce:
       - side: 'yes' | 'no' | None (no trade)
       - confidence: 0.0 – 1.0
       - target_price_cents: limit price to use
       - size: edge-scaled contract count

This is intentionally simple and rule-based — a solid foundation
you can expand with ML, cross-market arb, etc. later.
"""

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import yfinance as yf

import config

log = logging.getLogger(__name__)

# Cache for BTC momentum data to avoid redundant yfinance API calls
# TTL set to 1 minute (60s) since 1-minute bars update frequently;
# balances freshness against API call overhead in a 15-minute market window
_btc_momentum_cache: dict = {"data": None, "timestamp": 0, "ttl": 60}

# A market whose YES ask ≈ 1.00 and YES bid ≈ 0.00 (spread ≥ this) is considered
# a ghost / dead book and is always skipped.
_GHOST_MARKET_SPREAD_THRESHOLD: float = 0.99


@dataclass
class Signal:
    side: str           # 'yes' or 'no'
    confidence: float   # 0.0 to 1.0
    price_cents: int    # suggested limit price
    reason: str         # human-readable explanation
    size: int = field(default=1)  # edge-scaled contract count from decide_trade


def get_btc_momentum() -> Optional[float]:
    """
    Fetch recent 1-minute BTC/USD bars and return the momentum score.
    Returns a float in roughly [-1, 1]:
      > 0  => bullish (BTC trending up)
      < 0  => bearish (BTC trending down)
    Returns None on data error.

    Uses a 60-second cache to avoid redundant yfinance API calls on every bot cycle.
    """
    global _btc_momentum_cache

    # Check if cached data is still valid
    now = time.time()
    if (_btc_momentum_cache["data"] is not None and
        now - _btc_momentum_cache["timestamp"] < _btc_momentum_cache["ttl"]):
        log.debug("Using cached BTC momentum: %.4f", _btc_momentum_cache["data"])
        return _btc_momentum_cache["data"]

    try:
        ticker = yf.Ticker(config.BTC_TICKER)
        # Use a small intraday lookback by default to keep yfinance downloads cheap
        # in the bot hot path. Allow overriding via config when a larger buffer is
        # explicitly needed for environments with sparse/missing recent 1-minute bars.
        history_period = config.BTC_MOMENTUM_HISTORY_PERIOD
        hist = ticker.history(period=history_period, interval="1m")
        if hist.empty or len(hist) < config.MOMENTUM_LOOKBACK_BARS + 1:
            log.warning("Not enough BTC price history available")
            return None

        closes = hist["Close"].values
        recent = closes[-config.MOMENTUM_LOOKBACK_BARS:]
        baseline = closes[-(config.MOMENTUM_LOOKBACK_BARS + 1)]

        if baseline == 0:
            return None

        pct_change = (recent[-1] - baseline) / baseline  # e.g. 0.003 = +0.3%
        # Normalize: clip to [-2%, +2%] range then scale to [-1, 1]
        momentum = float(np.clip(pct_change / 0.02, -1.0, 1.0))

        # Cache the result
        _btc_momentum_cache["data"] = momentum
        _btc_momentum_cache["timestamp"] = now

        log.debug("BTC momentum: %.4f (raw pct_change=%.4f%%)", momentum, pct_change * 100)
        return momentum
    except Exception as exc:
        log.error("Error fetching BTC price: %s", exc)
        return None


def get_orderbook_skew(orderbook: dict, max_levels: int = 10) -> float:
    """
    Compute YES orderbook skew from Kalshi orderbook data.
    Returns a float in [-1, 1]:
      > 0  => more YES bids (market leans YES)
      < 0  => more NO bids  (market leans NO)

    Performance optimization: Only processes top N orderbook levels (default 10).
    Deep levels far from best bid contribute minimal signal but add processing
    overhead. Top 10 levels typically capture 95%+ of meaningful skew information.

    Supports all orderbook formats:
      - orderbook_fp.yes_dollars_fp / no_dollars_fp  (new fixed-point format, string prices)
      - orderbook_fp.yes_dollars / no_dollars        (older fp variant, string prices)
      - orderbook["orderbook"].yes_dollars_fp / no_dollars_fp  (WebSocket-wrapped _fp)
      - orderbook["orderbook"].yes_dollars / no_dollars        (WebSocket-wrapped _dollars)
      - top-level yes_dollars / no_dollars           (market-level dollar price fields)
      - orderbook["orderbook"].yes / no              (legacy integer-cents, wrapped)
      - top-level yes / no                           (legacy integer-cents, direct)

    Args:
        orderbook: Orderbook dict from API or WebSocket
        max_levels: Maximum number of levels to process per side (default: 10)

    Returns:
        Skew value in [-1.0, 1.0]
    """
    try:
        from orderbook_utils import get_weighted_bid_liquidity, extract_raw_arrays

        yes_raw, no_raw = extract_raw_arrays(orderbook)

        yes_liquidity = get_weighted_bid_liquidity(yes_raw, top_n=max_levels)
        no_liquidity = get_weighted_bid_liquidity(no_raw, top_n=max_levels)

        total = yes_liquidity + no_liquidity

        if total == 0:
            return 0.0

        skew = (yes_liquidity - no_liquidity) / total
        log.debug("Orderbook skew: %.3f (YES=%d, NO=%d, levels=%d)", skew, yes_liquidity, no_liquidity, max_levels)
        return skew
    except Exception as exc:
        log.error("Error computing orderbook skew: %s", exc)
        return 0.0


def suggest_limit_price(market: dict, side: str) -> int:
    """
    Pick a conservative limit price to ensure fills without crossing the spread.
    Returns a price in cents (1-99).

    Supports both old market dict format (yes_ask, yes_bid fields) and new
    orderbook-based quotes (best_yes_ask, best_yes_bid fields).
    """
    if side == "yes":
        # Pay up to the current yes_ask but no more than mid + 2c
        # Support both old (yes_ask) and new (best_yes_ask) field names
        ask = market.get("best_yes_ask") or market.get("yes_ask", 50)
        bid = market.get("best_yes_bid") or market.get("yes_bid", max(1, ask - 4))
        price = min(ask, bid + 2)  # slightly above best bid
    else:
        ask = market.get("best_no_ask") or market.get("no_ask", 50)
        bid = market.get("best_no_bid") or market.get("no_bid", max(1, ask - 4))
        price = min(ask, bid + 2)

    return max(config.MIN_CONTRACT_PRICE_CENTS, min(config.MAX_CONTRACT_PRICE_CENTS, price))


def decide_trade_fee_aware(
    market_price: float,
    model_p_yes: float,
    side_allowed_flags: Optional[dict] = None,
    cfg=None,
) -> tuple[str, int]:
    """
    Pure fee-aware entry decision function.

    Parameters
    ----------
    market_price : float
        YES contract price in dollars (e.g. 0.42 for 42 cents).
    model_p_yes : float
        Model's estimated probability of YES outcome (0.0 – 1.0).
    side_allowed_flags : dict, optional
        Which sides are eligible, e.g. {"yes": True, "no": True}.
        Defaults to both sides allowed.
    cfg : module, optional
        Config object/module supplying all fee-aware parameters.
        Defaults to the imported ``config`` module.

    Returns
    -------
    tuple[str, int]
        ("BUY_YES", C), ("BUY_NO", C), or ("NO_TRADE", 0).

    Decision logic
    --------------
    1. Compute mispricing = model_p_yes - market_p_yes.
    2. Only trade if |mispricing| >= cfg.MIN_EDGE_PCT.
    3. Skip trade if entry price is in the forbidden band
       (cfg.FORBIDDEN_PRICE_LOW, cfg.FORBIDDEN_PRICE_HIGH).
    4. Dynamically size contracts based on edge magnitude.
    5. Compute approximate fees (ceil rule) and expected net value per
       contract; skip if it falls below cfg.MIN_EXPECTED_NET_PER_CONTRACT.
    """
    if cfg is None:
        cfg = config

    if side_allowed_flags is None:
        side_allowed_flags = {"yes": True, "no": True}

    # Clip to a valid probability range
    P = float(np.clip(market_price, 0.01, 0.99))
    model_p_yes = float(np.clip(model_p_yes, 0.0, 1.0))

    # ── 1. Mispricing check ────────────────────────────────────────────────────
    market_p_yes = P  # YES price ≈ market-implied probability of YES
    mispricing = model_p_yes - market_p_yes

    if mispricing >= cfg.MIN_EDGE_PCT:
        if not side_allowed_flags.get("yes", True):
            log.debug(
                "decide_trade: BUY_YES indicated (mispricing=%.4f) "
                "but YES side is disabled by side_allowed_flags — NO_TRADE",
                mispricing,
            )
            return "NO_TRADE", 0
        action = "BUY_YES"
        # Expected gross value per contract for buying YES
        ev_gross = model_p_yes * 1.0 - P
    elif mispricing <= -cfg.MIN_EDGE_PCT:
        if not side_allowed_flags.get("no", True):
            log.debug(
                "decide_trade: BUY_NO indicated (mispricing=%.4f) "
                "but NO side is disabled by side_allowed_flags — NO_TRADE",
                mispricing,
            )
            return "NO_TRADE", 0
        action = "BUY_NO"
        # Expected gross value per contract for buying NO
        # (pay 1-P for a NO contract, win 1.00 if outcome is NO)
        ev_gross = (1.0 - model_p_yes) * 1.0 - (1.0 - P)
    else:
        log.debug(
            "decide_trade: mispricing %.4f within no-trade band ±%.4f — NO_TRADE",
            mispricing, cfg.MIN_EDGE_PCT,
        )
        return "NO_TRADE", 0

    # ── 2. Forbidden price band check ─────────────────────────────────────────
    if cfg.FORBIDDEN_PRICE_LOW < P < cfg.FORBIDDEN_PRICE_HIGH:
        log.debug(
            "decide_trade: price %.2f inside forbidden band (%.2f, %.2f) — NO_TRADE",
            P, cfg.FORBIDDEN_PRICE_LOW, cfg.FORBIDDEN_PRICE_HIGH,
        )
        return "NO_TRADE", 0

    # ── 3. Dynamic sizing ─────────────────────────────────────────────────────
    edge_mag = abs(mispricing)
    if cfg.MAX_EDGE_PCT > cfg.MIN_EDGE_PCT:
        edge_ratio = (edge_mag - cfg.MIN_EDGE_PCT) / (cfg.MAX_EDGE_PCT - cfg.MIN_EDGE_PCT)
        edge_ratio = float(np.clip(edge_ratio, 0.0, 1.0))
    else:
        edge_ratio = 1.0

    # Use math.floor(x + 0.5) for half-up rounding instead of Python's built-in
    # round(), which uses banker's rounding (round-half-to-even) and can cause
    # non-monotonic sizing steps at exact half increments.
    C = max(1, math.floor(cfg.BASE_SIZE + edge_ratio * (cfg.MAX_SIZE - cfg.BASE_SIZE) + 0.5))

    # ── 4. Fee and net EV check ────────────────────────────────────────────────
    # P_exit=0.5 is intentionally the worst-case (maximum-fee) assumption:
    # P*(1-P) peaks at P=0.5, so using 0.5 maximises the estimated close fee,
    # making the EV filter more conservative and harder to pass.
    P_exit = 0.5
    # Fees are in cents (ceil of formula); convert to dollars for EV comparison
    fee_open_cents = math.ceil(0.07 * C * P * (1.0 - P))
    fee_close_cents = math.ceil(0.07 * C * P_exit * (1.0 - P_exit))
    ev_net_per_contract = ev_gross - (fee_open_cents + fee_close_cents) / 100.0 / C

    if ev_net_per_contract < cfg.MIN_EXPECTED_NET_PER_CONTRACT:
        log.debug(
            "decide_trade: EV/contract $%.4f < threshold $%.4f — NO_TRADE",
            ev_net_per_contract, cfg.MIN_EXPECTED_NET_PER_CONTRACT,
        )
        return "NO_TRADE", 0

    log.debug(
        "decide_trade: %s C=%d price=%.2f mispricing=%.4f EV/contract=$%.4f",
        action, C, P, mispricing, ev_net_per_contract,
    )
    return action, C


def decide_trade_time_delay(
    up_price: float,
    down_price: float,
    minutes_to_expiry: int,
    current_position_side: "str | None",
    current_window_id: str,
    last_trade_window_id: "str | None",
    cfg,
    trades_in_current_window: int = 0,
    up_bid: "float | None" = None,
    down_bid: "float | None" = None,
) -> tuple[str, "int | None"]:
    """
    Reddit-style "time delay + stop-loss" entry/exit decision.

    Returns one of:
      ("ENTER_YES", size)     – buy YES contracts
      ("ENTER_NO", size)      – buy NO contracts
      ("EXIT_POSITION", None) – exit the current open position
      ("NO_TRADE", None)      – do nothing this cycle

    Parameters
    ----------
    up_price : float
        Current YES ask price in dollars (0.0–1.0).  Used for entry checks.
    down_price : float
        Current NO ask price in dollars (0.0–1.0).  Used for entry checks.
    minutes_to_expiry : int
        Minutes remaining until the 15-minute window closes.
    current_position_side : str | None
        "YES", "NO", or None (no open position this window).
    current_window_id : str
        Stable identifier for the current 15-minute window.
    last_trade_window_id : str | None
        Window ID of the most recent entry placed by this bot.
    cfg : module or SimpleNamespace
        Config object supplying TRIGGER_POINT_PRICE, EXIT_POINT_PRICE,
        TRIGGER_MINUTE_REMAINING, MAX_TRADES_PER_WINDOW, and BASE_SIZE.
    trades_in_current_window : int, optional
        Number of entries already placed in the current window.  Resets to 0
        when the window ID changes.  Default is 0.
    up_bid : float | None, optional
        Current YES bid price in dollars.  When provided, used for the YES
        stop-loss exit check instead of ``up_price``.  Defaults to ``up_price``.
    down_bid : float | None, optional
        Current NO bid price in dollars.  When provided, used for the NO
        stop-loss exit check instead of ``down_price``.  Defaults to ``down_price``.
    """
    trigger = cfg.TRIGGER_POINT_PRICE
    exit_point_price = cfg.EXIT_POINT_PRICE
    trigger_minutes = cfg.TRIGGER_MINUTE_REMAINING
    max_trades = cfg.MAX_TRADES_PER_WINDOW
    size = cfg.BASE_SIZE

    # Use bid prices for exit comparisons (the price we can actually sell at);
    # fall back to the ask prices when bid data is not available.
    _exit_up = up_bid if up_bid is not None else up_price
    _exit_down = down_bid if down_bid is not None else down_price

    if current_position_side is None:
        # Not yet armed — too much time left
        if minutes_to_expiry > trigger_minutes:
            return ("NO_TRADE", None)

        # Already reached the per-window entry limit
        if trades_in_current_window >= max_trades:
            return ("NO_TRADE", None)

        # Enter YES if only the UP side qualifies (using ask prices for entry)
        if up_price >= trigger and down_price < trigger:
            return ("ENTER_YES", size)

        # Enter NO if only the DOWN side qualifies (using ask prices for entry)
        if down_price >= trigger and up_price < trigger:
            return ("ENTER_NO", size)

        # Both or neither qualify — stay out
        return ("NO_TRADE", None)

    elif current_position_side == "YES":
        # Stop-loss check uses bid price (the price we can exit at)
        if _exit_up <= exit_point_price:
            return ("EXIT_POSITION", None)
        return ("NO_TRADE", None)

    elif current_position_side == "NO":
        # Stop-loss check uses bid price (the price we can exit at)
        if _exit_down <= exit_point_price:
            return ("EXIT_POSITION", None)
        return ("NO_TRADE", None)

    else:
        # Unexpected / invalid position side — do nothing safely
        return ("NO_TRADE", None)


def decide_trade(
    up_price: float,
    down_price: float,
    minutes_to_expiry: int,
    current_position_side: "str | None",
    current_window_id: str,
    last_trade_window_id: "str | None",
    cfg,
    trades_in_current_window: int = 0,
    up_bid: "float | None" = None,
    down_bid: "float | None" = None,
) -> tuple[str, "int | None"]:
    """
    Strategy-mode router.  Delegates to the appropriate strategy function
    based on ``cfg.STRATEGY_MODE``.

    Returns the same tuple shape as :func:`decide_trade_time_delay`:
      ("ENTER_YES" | "ENTER_NO" | "EXIT_POSITION" | "NO_TRADE", size | None)

    For ``fee_aware_model`` mode this wrapper is not the primary entry point
    (bot.py uses :func:`generate_signal` directly); it is included here so
    that any future caller can route through a single function regardless of
    mode.
    """
    if cfg.STRATEGY_MODE == "reddit_time_delay":
        return decide_trade_time_delay(
            up_price=up_price,
            down_price=down_price,
            minutes_to_expiry=minutes_to_expiry,
            current_position_side=current_position_side,
            current_window_id=current_window_id,
            last_trade_window_id=last_trade_window_id,
            cfg=cfg,
            trades_in_current_window=trades_in_current_window,
            up_bid=up_bid,
            down_bid=down_bid,
        )
    # fee_aware_model — signal generation requires market/orderbook data and
    # is handled by generate_signal() in bot.py; return NO_TRADE here so that
    # callers that go through this wrapper for the fee-aware path do not place
    # duplicate orders.
    return ("NO_TRADE", None)


def _extract_best_bid_depth(raw_array) -> tuple:
    """
    Return ``(best_bid_price_dollars, best_bid_count)`` from a raw orderbook side.

    Each entry may be one of:
      - ``[price, count]`` where *price* is a string dollar (``"0.55"``),
        a float dollar (``0.55``), or an integer cent value (``55``).
      - A dict with ``"price"`` / ``"price_dollars"`` and ``"size"`` / ``"count"`` keys.

    Returns ``(None, 0)`` when the array is empty or completely unparseable.
    """
    if not raw_array or not isinstance(raw_array, list):
        return None, 0

    best_price: Optional[float] = None
    best_count: int = 0

    for entry in raw_array:
        price = None
        count = 0

        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            price, count = entry[0], entry[1]
        elif isinstance(entry, dict):
            price = entry.get("price") or entry.get("price_dollars")
            count = entry.get("size") if entry.get("size") is not None else entry.get("count", 0)
        else:
            continue

        # Convert price to the 0-1 dollar range
        try:
            if isinstance(price, str):
                price_d = float(price)
                if price_d > 1.0:       # treat as cents by mistake
                    price_d /= 100.0
            elif isinstance(price, float):
                price_d = price if price <= 1.0 else price / 100.0
            elif isinstance(price, int):
                # Integer prices from Kalshi are always in cents (1=1¢, 99=99¢).
                # Always divide by 100 to convert to dollars.
                price_d = price / 100.0
            else:
                price_d = float(price)
                if price_d > 1.0:
                    price_d /= 100.0
        except (TypeError, ValueError):
            continue

        try:
            count_i = int(float(count))
        except (TypeError, ValueError):
            count_i = 0

        if count_i <= 0:
            continue

        # Keep the highest-priced (best bid) level
        if best_price is None or price_d > best_price:
            best_price = price_d
            best_count = count_i

    return best_price, best_count


def generate_signal(market: dict, orderbook: dict) -> Optional[Signal]:
    """
    Main entry point.  Returns a Signal or None if no trade warranted.

    Combines:
      - BTC short-term momentum  (weight 0.6)
      - Kalshi orderbook skew    (weight 0.4)

    The composite score is mapped to a model_p_yes probability and passed
    through decide_trade_fee_aware(), which enforces fee-aware entry filters
    and dynamic sizing before a Signal is emitted.

    A Signal with size=0 is returned when decide_trade_fee_aware blocks the
    entry but a clear directional preference exists; manage_positions can still
    use this for SIGNAL_REVERSAL_EXIT even though no new contract will be
    opened.
    Returns None only when momentum data is unavailable (no directional view).
    """
    momentum = get_btc_momentum()
    if momentum is None:
        log.warning("Could not compute momentum — skipping this cycle")
        return None

    skew = get_orderbook_skew(orderbook)

    # Weighted composite score  (-1 = strong NO, +1 = strong YES)
    composite = (0.6 * momentum) + (0.4 * skew)
    confidence = abs(composite)  # 0.0 to 1.0

    log.info(
        "Signal composite=%.3f | momentum=%.3f | skew=%.3f | confidence=%.3f",
        composite, momentum, skew, confidence,
    )

    # Apply MIN_CONFIDENCE filter: skip if signal is too weak
    if confidence < config.MIN_CONFIDENCE:
        log.info(
            "Signal confidence %.4f below MIN_CONFIDENCE threshold %.4f — skipping cycle",
            confidence, config.MIN_CONFIDENCE
        )
        return None

    # Determine directional preference from composite.  This is set early so it
    # is available for the NO_TRADE path (reversal signal with size=0).
    side = "yes" if composite > 0 else "no"

    # Market mid is used solely to anchor model_p_yes (independent of spread).
    # Support both old market dict format and new orderbook-based quotes.
    # With one-sided orderbook inference, we should have yes_bid/yes_ask available
    # even when only one side of the orderbook has liquidity.
    yes_bid = market.get("best_yes_bid") or market.get("yes_bid")
    yes_ask = market.get("best_yes_ask") or market.get("yes_ask")

    if yes_bid is None or yes_ask is None:
        # Only emit the WARNING when both quotes are absent; a single missing
        # value is unusual but not necessarily an error (log at DEBUG instead).
        if yes_bid is None and yes_ask is None:
            log.warning(
                "Market data missing YES bid/ask quotes — skipping cycle "
                "(best_yes_bid and best_yes_ask are both None) | "
                "prices: best_yes_bid=%s best_yes_ask=%s yes_bid=%s yes_ask=%s mid=%s",
                market.get("best_yes_bid"),
                market.get("best_yes_ask"),
                market.get("yes_bid"),
                market.get("yes_ask"),
                market.get("mid_price"),
            )
        else:
            log.debug(
                "Market data incomplete: yes_bid=%s yes_ask=%s — skipping cycle",
                yes_bid, yes_ask,
            )
        return None

    market_mid = float(np.clip((yes_bid + yes_ask) / 2 / 100.0, 0.01, 0.99))

    # ── Orderbook-based spread and depth ─────────────────────────────────────
    # Parse the raw orderbook directly so spread and depth are always accurate,
    # even when the market dict has no pre-computed "spread" or
    # "yes_depth_near_mid" fields (those are only populated by
    # get_market_quotes(), which is not called in the main bot loop).
    from orderbook_utils import extract_raw_arrays as _extract_raw
    yes_raw, no_raw = _extract_raw(orderbook)

    yes_bid_price, yes_depth = _extract_best_bid_depth(yes_raw)
    no_bid_price, _          = _extract_best_bid_depth(no_raw)

    # YES ask = 1 − best NO bid (standard Kalshi complementary pricing).
    # Fall back to the market-dict value when the NO side is empty.
    if no_bid_price is not None:
        yes_ask_price = round(1.0 - no_bid_price, 4)
    elif yes_ask is not None:
        yes_ask_price = round(yes_ask / 100.0, 4)
    else:
        yes_ask_price = None

    # If the YES side of the book is also empty, fall back to market-dict cents.
    if yes_bid_price is None and yes_bid is not None:
        yes_bid_price = round(yes_bid / 100.0, 4)

    # market_spread in dollars
    if yes_bid_price is not None and yes_ask_price is not None:
        ob_spread = round(yes_ask_price - yes_bid_price, 4)
    else:
        ob_spread = None

    log.info(
        "Orderbook: yes_bid=%.4f yes_ask=%.4f yes_depth=%d spread=%.4f",
        yes_bid_price or 0.0, yes_ask_price or 0.0, yes_depth, ob_spread or 0.0,
    )

    # ── Ghost-market guard ────────────────────────────────────────────────────
    # Skip markets where quotes are effectively 0¢ / 100¢ (dead / ghost books).
    if ob_spread is not None and ob_spread >= _GHOST_MARKET_SPREAD_THRESHOLD:
        log.info(
            "Skipping ghost market: spread=%.4f (effectively 0¢/100¢ book) — skipping cycle",
            ob_spread,
        )
        return None

    # ── Spread / slippage check ───────────────────────────────────────────────
    # Prefer the orderbook-derived spread; fall back to market-dict spread so
    # the filter still fires even when the raw orderbook is unavailable.
    effective_spread = ob_spread if ob_spread is not None else (yes_ask - yes_bid) / 100.0
    if effective_spread > config.MAX_SLIPPAGE:
        log.info(
            "Market spread %.4f ($%.2fc) exceeds MAX_SLIPPAGE %.4f — skipping cycle",
            effective_spread, effective_spread * 100, config.MAX_SLIPPAGE,
        )
        return None

    # ── YES depth check ───────────────────────────────────────────────────────
    if yes_depth < config.MIN_YES_DEPTH:
        log.info(
            "YES depth %d < MIN_YES_DEPTH %d — skipping cycle",
            yes_depth, config.MIN_YES_DEPTH,
        )
        return None

    log.info(
        "Liquidity OK: spread=%.4f ($%.2fc) <= MAX_SLIPPAGE %.4f, "
        "yes_depth=%d >= MIN_YES_DEPTH %d — proceeding",
        effective_spread, effective_spread * 100, config.MAX_SLIPPAGE,
        yes_depth, config.MIN_YES_DEPTH,
    )

    # Map composite score to a model probability estimate.
    # A composite of ±1.0 shifts the market price by up to ±0.50,
    # so at MIN_EDGE_PCT=0.10 a composite of 0.20 is the minimum qualifying signal.
    model_p_yes = float(np.clip(market_mid + composite * 0.5, 0.01, 0.99))

    # Apply MAX_PRICE_DEVIATION filter: skip if model deviates too far from market
    price_deviation = abs(model_p_yes - market_mid)
    if price_deviation > config.MAX_PRICE_DEVIATION:
        log.info(
            "Price deviation %.4f exceeds MAX_PRICE_DEVIATION threshold %.4f — skipping cycle",
            price_deviation, config.MAX_PRICE_DEVIATION
        )
        return None

    # Use the side-specific suggested entry price (what the bot actually pays)
    # rather than the mid, so that decide_trade's mispricing and fee/EV checks
    # reflect the real cost of entry and are not overstated.
    entry_price_cents = suggest_limit_price(market, side)
    if side == "yes":
        # YES contracts: cost = entry_price_cents / 100 dollars
        entry_p = float(np.clip(entry_price_cents / 100.0, 0.01, 0.99))
    else:
        # NO contracts: a NO contract at X cents ≡ YES price of (100-X) cents.
        # Expressing cost in YES-equivalent terms lets decide_trade use its
        # standard formulas: mispricing = model_p_yes - entry_p (<0 for NO edge),
        # and fee ∝ entry_p * (1 - entry_p) = no_price * (1 - no_price).
        entry_p = float(np.clip(1.0 - entry_price_cents / 100.0, 0.01, 0.99))

    # Fee-aware entry decision (handles edge threshold, forbidden bands, sizing)
    action, size = decide_trade_fee_aware(entry_p, model_p_yes)

    if action == "NO_TRADE":
        # Entry is blocked by fee/band filters.  Return a Signal with size=0 so
        # that SIGNAL_REVERSAL_EXIT in manage_positions can still trigger when the
        # composite direction opposes an open position, even though no new entry
        # will be opened (bot.py skips entry logic when sig.size == 0).
        log.info(
            "decide_trade returned NO_TRADE (composite=%.3f entry_p=%.2f "
            "model_p_yes=%.2f) — no new entry this cycle",
            composite, entry_p, model_p_yes,
        )
        return Signal(
            side=side,
            confidence=confidence,
            price_cents=entry_price_cents,
            reason=f"NO_TRADE: composite={composite:+.3f}",
            size=0,
        )

    # Guard: if decide_trade's action disagrees with the composite-derived side,
    # entry_price_cents (computed for `side`) would be wrong for the opposite side.
    # Treat the mismatch as NO_TRADE to avoid placing an order at an incorrect price.
    action_side = "yes" if action == "BUY_YES" else "no"
    if action_side != side:
        log.info(
            "decide_trade action (%s) disagrees with composite side (%s) "
            "(composite=%.3f entry_p=%.2f model_p_yes=%.2f) — treating as NO_TRADE",
            action_side, side, composite, entry_p, model_p_yes,
        )
        return Signal(
            side=side,
            confidence=confidence,
            price_cents=entry_price_cents,
            reason=f"NO_TRADE: side mismatch composite={composite:+.3f}",
            size=0,
        )

    reason = (
        f"momentum={momentum:+.3f} skew={skew:+.3f} composite={composite:+.3f} → "
        f"{side.upper()} @ {entry_price_cents}c (confidence={confidence:.2%} size={size})"
    )

    return Signal(side=side, confidence=confidence, price_cents=entry_price_cents, reason=reason, size=size)
