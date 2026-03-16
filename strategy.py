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
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import yfinance as yf

import config

log = logging.getLogger(__name__)


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
    """
    try:
        ticker = yf.Ticker(config.BTC_TICKER)
        # Grab last 30 minutes of 1-min bars
        hist = ticker.history(period="1d", interval="1m")
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
        log.debug("BTC momentum: %.4f (raw pct_change=%.4f%%)", momentum, pct_change * 100)
        return momentum
    except Exception as exc:
        log.error("Error fetching BTC price: %s", exc)
        return None


def get_orderbook_skew(orderbook: dict) -> float:
    """
    Compute YES orderbook skew from Kalshi orderbook data.
    Returns a float in [-1, 1]:
      > 0  => more YES bids (market leans YES)
      < 0  => more NO bids  (market leans NO)
    """
    try:
        yes_bids = orderbook.get("orderbook", {}).get("yes", [])
        no_bids = orderbook.get("orderbook", {}).get("no", [])

        # Each entry is [price_cents, size]
        yes_liquidity = sum(p * s for p, s in yes_bids) if yes_bids else 0
        no_liquidity = sum(p * s for p, s in no_bids) if no_bids else 0
        total = yes_liquidity + no_liquidity

        if total == 0:
            return 0.0

        skew = (yes_liquidity - no_liquidity) / total  # -1 to +1
        log.debug("Orderbook skew: %.3f (YES=%d, NO=%d)", skew, yes_liquidity, no_liquidity)
        return skew
    except Exception as exc:
        log.error("Error computing orderbook skew: %s", exc)
        return 0.0


def suggest_limit_price(market: dict, side: str) -> int:
    """
    Pick a conservative limit price to ensure fills without crossing the spread.
    Returns a price in cents (1-99).
    """
    if side == "yes":
        # Pay up to the current yes_ask but no more than mid + 2c
        ask = market.get("yes_ask", 50)
        bid = market.get("yes_bid", max(1, ask - 4))
        price = min(ask, bid + 2)  # slightly above best bid
    else:
        ask = market.get("no_ask", 50)
        bid = market.get("no_bid", max(1, ask - 4))
        price = min(ask, bid + 2)

    return max(config.MIN_CONTRACT_PRICE_CENTS, min(config.MAX_CONTRACT_PRICE_CENTS, price))


def decide_trade(
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

    if mispricing >= cfg.MIN_EDGE_PCT and side_allowed_flags.get("yes", True):
        action = "BUY_YES"
        # Expected gross value per contract for buying YES
        ev_gross = model_p_yes * 1.0 - P
    elif mispricing <= -cfg.MIN_EDGE_PCT and side_allowed_flags.get("no", True):
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

    C = max(1, round(cfg.BASE_SIZE + edge_ratio * (cfg.MAX_SIZE - cfg.BASE_SIZE)))

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


def generate_signal(market: dict, orderbook: dict) -> Optional[Signal]:
    """
    Main entry point.  Returns a Signal or None if no trade warranted.

    Combines:
      - BTC short-term momentum  (weight 0.6)
      - Kalshi orderbook skew    (weight 0.4)

    The composite score is mapped to a model_p_yes probability and passed
    through decide_trade(), which enforces fee-aware entry filters and
    dynamic sizing before a Signal is emitted.
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

    # Derive market-implied YES price from the market dict (mid of bid/ask, in dollars)
    yes_bid = market.get("yes_bid", 50)
    yes_ask = market.get("yes_ask", 50)
    if "yes_bid" not in market or "yes_ask" not in market:
        log.warning(
            "Market data missing yes_bid/yes_ask — defaulting to 50c mid price, "
            "which falls inside the forbidden price band and will prevent new entries"
        )
    market_price = float(np.clip((yes_bid + yes_ask) / 2 / 100.0, 0.01, 0.99))

    # Map composite score to a model probability estimate.
    # A composite of ±1.0 shifts the market price by up to ±0.50,
    # so at MIN_EDGE_PCT=0.10 a composite of 0.20 is the minimum qualifying signal.
    model_p_yes = float(np.clip(market_price + composite * 0.5, 0.01, 0.99))

    # Fee-aware entry decision (handles edge threshold, forbidden bands, sizing)
    action, size = decide_trade(market_price, model_p_yes)

    if action == "NO_TRADE":
        # No new entry allowed (forbidden band, edge filter, etc.), but we still
        # emit a zero-size Signal so downstream logic (e.g. reversal exits) can
        # see the current strategy direction.
        log.info(
            "decide_trade returned NO_TRADE (composite=%.3f market_price=%.2f "
            "model_p_yes=%.2f) — blocking new entries this cycle",
            composite, market_price, model_p_yes,
        )
        # Derive directional side from the composite signal; tie-break towards YES
        # when composite is very close to 0 so callers still see a consistent side.
        side = "yes" if composite >= 0 else "no"
        price = suggest_limit_price(market, side)
        reason = (
            "NO_TRADE from decide_trade (entry filters) — emitting zero-size signal "
            f"for direction only: momentum={momentum:+.3f} skew={skew:+.3f} "
            f"composite={composite:+.3f} → {side.upper()} @ {price}c "
            f"(confidence={confidence:.2%} size=0)"
        )
        return Signal(side=side, confidence=confidence, price_cents=price, reason=reason, size=0)

    side = "yes" if action == "BUY_YES" else "no"
    price = suggest_limit_price(market, side)

    # Re-run fee-aware decision at the expected entry price (in dollars) to avoid
    # overstating edge/net EV when the execution price is worse than the mid.
    entry_price = float(np.clip(price / 100.0, 0.01, 0.99))
    action_at_entry, size_at_entry = decide_trade(entry_price, model_p_yes)

    # If the trade no longer passes filters at the true entry price (or flips side),
    # skip it to avoid executing a trade that fails the fee-aware check.
    if action_at_entry == "NO_TRADE":
        log.info(
            "decide_trade rejected trade at entry price (composite=%.3f market_price=%.2f "
            "entry_price=%.2f model_p_yes=%.2f) — no trade this cycle",
            composite, market_price, entry_price, model_p_yes,
        )
        return None

    side_at_entry = "yes" if action_at_entry == "BUY_YES" else "no"
    if side_at_entry != side:
        log.info(
            "decide_trade side flipped at entry price (composite=%.3f market_price=%.2f "
            "entry_price=%.2f model_p_yes=%.2f initial_side=%s entry_side=%s) — no trade",
            composite, market_price, entry_price, model_p_yes, side, side_at_entry,
        )
        return None

    final_size = size_at_entry

    reason = (
        f"momentum={momentum:+.3f} skew={skew:+.3f} composite={composite:+.3f} → "
        f"{side.upper()} @ {price}c (confidence={confidence:.2%} size={final_size})"
    )

    return Signal(side=side, confidence=confidence, price_cents=price, reason=reason, size=final_size)
