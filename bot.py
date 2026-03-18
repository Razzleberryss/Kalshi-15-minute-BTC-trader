"""
bot.py – Main entry point for the Kalshi 15-minute BTC trader.

Usage:
    python bot.py           # runs with DRY_RUN setting from .env (default: true)
    DRY_RUN=false python bot.py # real trading

Loop logic (every LOOP_INTERVAL_SECONDS):
    1. Validate config
    2. Find active 15-min BTC market on Kalshi
    3. Fetch orderbook + account balance + open positions
    4. Manage existing positions (Stop-loss / Take-profit / Reversal)
    5. Generate signal for new trade (strategy.py)
    6. Risk-check the signal (risk_manager.py)
    7. Place order (or log as dry run)
    8. Sleep and repeat
"""
import logging
import signal
import sys
import time
import datetime
import unittest

import colorlog

import config
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import generate_signal, decide_trade, Signal as _Signal


def _compute_trade_contracts(sig_size, budget_contracts):
    """
    Return the number of contracts to trade, capped by the risk budget.

    This is a thin wrapper around ``min(sig_size, budget_contracts)`` so that
    trade sizing semantics are covered by unit tests and protected from
    regressions if the sizing logic is modified in the future.
    """
    return min(sig_size, budget_contracts)


class TestComputeTradeContracts(unittest.TestCase):
    """
    Unit tests for trade sizing semantics.

    Ensures that contract sizing respects the cap of
    ``min(sig.size, budget_contracts)`` both when the signal size is below and
    above the available budget.
    """

    def test_sig_size_smaller_than_budget(self):
        # When the signal size is below the budget, we should trade the full signal size.
        self.assertEqual(_compute_trade_contracts(5, 10), 5)

    def test_sig_size_larger_than_budget(self):
        # When the signal size exceeds the budget, we should be capped by the budget.
        self.assertEqual(_compute_trade_contracts(20, 10), 10)


# ── Time-delay strategy helpers ───────────────────────────────────────────────────────────────

# Cache for parsed datetime to avoid redundant parsing in position management
_parsed_datetime_cache: dict = {}


def _parse_close_time(close_time_str: str) -> datetime.datetime:
    """Parse ISO datetime string and cache result to avoid redundant parsing."""
    if close_time_str not in _parsed_datetime_cache:
        _parsed_datetime_cache[close_time_str] = datetime.datetime.fromisoformat(
            close_time_str.replace("Z", "+00:00")
        )
    return _parsed_datetime_cache[close_time_str]


def _compute_minutes_to_expiry(market: dict, cached_now: datetime.datetime = None) -> int:
    """
    Return whole minutes remaining until market close_time, or 999 if unknown.
    Accepts cached_now to avoid redundant datetime.now() calls.
    """
    close_time_str = market.get("close_time")
    if not close_time_str:
        return 999
    try:
        close_time = _parse_close_time(close_time_str)
        now = cached_now or datetime.datetime.now(datetime.timezone.utc)
        seconds = max(0.0, (close_time - now).total_seconds())
        return int(seconds // 60)
    except (ValueError, TypeError):
        return 999


def _compute_window_id(market: dict) -> str:
    """
    Return a stable string ID for the current 15-minute market window.
    Uses close_time (unique per window) with a fallback to the ticker.
    """
    return market.get("close_time") or market.get("ticker", "unknown")


# ── Logging setup ──────────────────────────────────────────────────────────────────────────────
# Define custom TRADE log level (25) between INFO (20) and WARNING (30)
TRADE_LEVEL = 25
logging.addLevelName(TRADE_LEVEL, "TRADE")

def log_trade(msg, *args, **kwargs):
    """Log a trade entry/exit at the custom TRADE level with bright green color."""
    logging.getLogger("bot").log(TRADE_LEVEL, msg, *args, **kwargs)

def setup_logging():
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
            "TRADE": "bold_green",  # Bright green and bold for trade logs
            "WARNING": "yellow",
            "ERROR": "red",
            "CRITICAL": "bold_red",
        },
    ))
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.addHandler(handler)

log = logging.getLogger("bot")

# ── Graceful shutdown ─────────────────────────────────────────────────────────────────────────────
_running = True

# ── Time-delay strategy state ─────────────────────────────────────────────────────────────────────
# Tracks the window ID of the last trade placed in reddit_time_delay mode so that
# window-change detection works across bot cycles.
_last_trade_window_id: "str | None" = None
# Number of entries placed in the current 15-minute window (reset when window changes).
_trades_in_current_window: int = 0

def _handle_signal(sig, frame):
    global _running
    log.warning("Shutdown signal received — stopping after this cycle...")
    _running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Position Management ────────────────────────────────────────────────────────────────────────
def manage_positions(client: KalshiClient, market: dict, risk: RiskManager, current_signal=None):
    """
    Check positions opened by this bot for stop-loss, take-profit, signal reversal, or expiry.
    Only positions recorded via risk.record_open_position() are managed here, so pre-existing
    positions from other bots on the same account are never touched.
    Yields a dict for every position that is exited.
    """
    ticker = market["ticker"]
    bot_positions = risk.get_open_positions()

    if ticker not in bot_positions:
        return

    pos = bot_positions[ticker]
    side = pos["side"]
    count = pos["quantity"]
    entry_price = pos["entry_price"]

    # Current best bid for our side — what we can sell for right now
    # Support both old (yes_bid) and new (best_yes_bid) field names.
    best_bid_field = f"best_{side}_bid"
    legacy_bid_field = f"{side}_bid"

    # Prefer best_<side>_bid when present and non-None; otherwise fall back to <side>_bid;
    # if both are missing or None, fall back to entry_price to avoid TypeError.
    current_price = entry_price
    best_bid_value = market.get(best_bid_field)
    if best_bid_value is not None:
        current_price = best_bid_value
    else:
        legacy_bid_value = market.get(legacy_bid_field)
        if legacy_bid_value is not None:
            current_price = legacy_bid_value

    exit_reason = None

    # 1. Stop Loss
    if config.STOP_LOSS_CENTS > 0:
        if current_price <= (entry_price - config.STOP_LOSS_CENTS):
            exit_reason = "stop_loss"

    # 2. Take Profit
    if config.TAKE_PROFIT_CENTS > 0 and not exit_reason:
        if current_price >= (entry_price + config.TAKE_PROFIT_CENTS):
            exit_reason = "take_profit"

    # 3. Signal Reversal
    if config.SIGNAL_REVERSAL_EXIT and current_signal and not exit_reason:
        if current_signal.side != side and current_signal.confidence >= config.MIN_EDGE_THRESHOLD:
            exit_reason = "reversal"

    # 4. Expiry: exit when fewer than EXPIRY_EXIT_SECONDS remain before contract close
    if not exit_reason:
        close_time_str = market.get("close_time")
        if close_time_str:
            try:
                close_time = _parse_close_time(close_time_str)
                # Use cached datetime from risk manager to avoid redundant calls
                now = risk._get_current_datetime()
                if (close_time - now).total_seconds() <= config.EXPIRY_EXIT_SECONDS:
                    exit_reason = "expiry"
            except (ValueError, TypeError):
                pass

    if exit_reason:
        exit_price = max(1, current_price - 1)  # aggressive limit sell
        pnl_cents = (
            (exit_price - entry_price) * count
            if side == "yes"
            else (entry_price - exit_price) * count
        )
        log_trade(
            "EXIT %s | side=%s | entry=%dc | exit=%dc | pnl=%+dc | reason=%s",
            ticker, side, entry_price, exit_price, pnl_cents, exit_reason,
        )
        client.close_position(
            market_id=ticker,
            side=side,
            quantity=count,
            price=exit_price,
            dry_run=config.DRY_RUN,
        )
        yield {
            "market": ticker,
            "side": side,
            "size": count,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": exit_reason,
        }

# ── Core bot loop ─────────────────────────────────────────────────────────────────────────────
def _quotes_from_orderbook(orderbook: dict) -> dict:
    """
    Derive best bid/ask quotes and a mid price from a Kalshi orderbook dict.

    This avoids making a second /orderbook (or equivalent) API call when we
    already have the raw orderbook data.
    """
    # Default structure in case the orderbook is missing or empty
    result = {
        "best_yes_bid": None,
        "best_yes_ask": None,
        "best_no_bid": None,
        "best_no_ask": None,
        "mid_price": None,
    }

    if not isinstance(orderbook, dict):
        return result

    try:
        yes_book = orderbook.get("yes", {}) or {}
        no_book = orderbook.get("no", {}) or {}

        yes_bids = yes_book.get("bids") or []
        yes_asks = yes_book.get("asks") or []
        no_bids = no_book.get("bids") or []
        no_asks = no_book.get("asks") or []

        def _best_price(entries):
            if not entries:
                return None
            top = entries[0]
            # Entries are expected to be dicts with a "price" field
            return top.get("price") if isinstance(top, dict) else None

        result["best_yes_bid"] = _best_price(yes_bids)
        result["best_yes_ask"] = _best_price(yes_asks)
        result["best_no_bid"] = _best_price(no_bids)
        result["best_no_ask"] = _best_price(no_asks)

        prices = [
            p
            for p in (
                result["best_yes_bid"],
                result["best_yes_ask"],
                result["best_no_bid"],
                result["best_no_ask"],
            )
            if p is not None
        ]
        if prices:
            # Use a simple average of available best prices as a robust mid.
            avg = sum(prices) / len(prices)
            # Keep type consistent with existing prices (typically integer cents)
            result["mid_price"] = round(avg)
    except Exception:
        # On any unexpected structure, fall back to defaults (all None)
        return result

    return result


def run_once(client: KalshiClient, risk: RiskManager):
    """
    Execute one complete bot cycle.
    Returns True if an action was taken, False otherwise.
    """
    global _last_trade_window_id

    # 1. Find the active market
    market = client.get_active_btc_market()
    if not market:
        log.warning("No active BTC 15-min market found. Skipping cycle.")
        risk._clear_datetime_cache()
        return False

    ticker = market["ticker"]
    if not ticker.startswith(f"{config.BTC_SERIES_TICKER}-"):
        log.error("Refusing non-BTC-series market: %s", ticker)
        risk._clear_datetime_cache()
        return False

    # 2. Fetch supporting data
    try:
        orderbook = client.get_orderbook(ticker)
        balance = client.get_balance()
        positions = client.get_positions()
    except Exception as exc:
        log.error("API fetch error: %s", exc)
        risk._clear_datetime_cache()
        return False

    # 2b. Populate market dict with orderbook-based quotes if enabled
    if config.USE_ORDERBOOK_PRICES:
        # Derive quotes directly from the already-fetched orderbook to avoid
        # an extra network call and potential rate-limit pressure.
        quotes = _quotes_from_orderbook(orderbook)
        # Merge quotes into market dict, using new field names (best_yes_bid, etc.)
        market.update(quotes)

        # Log with orderbook-based prices
        yes_bid = quotes.get("best_yes_bid")
        yes_ask = quotes.get("best_yes_ask")
        no_bid = quotes.get("best_no_bid")
        no_ask = quotes.get("best_no_ask")
        mid = quotes.get("mid_price")

        if yes_bid is not None and yes_ask is not None:
            log.info("Active market: %s | last=%sc yes=%dc/%dc no=%dc/%dc mid=%dc (from orderbook)",
                     ticker, market.get("last_price"),
                     yes_bid, yes_ask, no_bid, no_ask, mid)
        else:
            log.warning("Active market: %s | orderbook empty (no quotes available)", ticker)
    else:
        # Use old market data fields
        log.info("Active market: %s | last=%sc yes=%s/%s no=%s/%s",
                 ticker, market.get("last_price"),
                 market.get("yes_bid"), market.get("yes_ask"),
                 market.get("no_bid"), market.get("no_ask"))

    # ── reddit_time_delay strategy path ───────────────────────────────────────
    if config.STRATEGY_MODE == "reddit_time_delay":
        return _run_once_time_delay(client, risk, market, ticker, balance, positions)

    # ── fee_aware_model strategy path (default) ────────────────────────────────

    # 3. Generate signal
    sig = generate_signal(market, orderbook)
    
    # 4. Manage existing positions first
    exit_error = False
    try:
        for closed in manage_positions(client, market, risk, current_signal=sig) or []:
            risk.record_closed_position(closed["market"])
            risk.log_exit_trade(
                market=closed["market"],
                side=closed["side"],
                size=closed["size"],
                entry_price=closed["entry_price"],
                exit_price=closed["exit_price"],
                exit_reason=closed["exit_reason"],
            )
    except Exception as exc:
        log.error("Error while managing positions: %s", exc, exc_info=True)
        exit_error = True

    # If position management failed, skip new entries to avoid trading with
    # unrecorded/un-exited positions.
    if exit_error:
        risk._clear_datetime_cache()
        return False

    # 5. Risk check for NEW trade
    # sig.size == 0 means decide_trade_fee_aware blocked the entry (fee/band
    # filters) but still returned a directional Signal for reversal-exit
    # purposes; skip entry.
    if sig is None or sig.size == 0:
        risk._clear_datetime_cache()
        return False
        
    approved, reason = risk.approve_trade(sig, balance, positions, ticker)
    if not approved:
        # Don't log "Already have position" as an error, it's normal if we didn't exit
        if "Already have" not in reason:
            log.info("New trade rejected by risk manager: %s", reason)
        risk._clear_datetime_cache()
        return False

    # 6. Size the trade
    # decide_trade_fee_aware already computed an edge-based size (sig.size); cap
    # it by the dollar budget so existing risk limits are always respected.
    budget_contracts = risk.calculate_contracts(sig.price_cents)
    contracts = _compute_trade_contracts(sig.size, budget_contracts)
    if contracts < 1:
        log.warning("Contract count is 0 — price too high for budget. Skipping.")
        risk._clear_datetime_cache()
        return False

    log_trade(
        "Placing BUY %s %s x%d @ %dc (est. cost $%.2f) | reason: %s",
        sig.side.upper(), ticker, contracts, sig.price_cents,
        contracts * sig.price_cents / 100, sig.reason
    )

    # 7. Execute
    if sig.side == "yes":
        order = client.place_order_yes(
            market_id=ticker,
            quantity=contracts,
            price=sig.price_cents,
            dry_run=config.DRY_RUN,
        )
    else:
        order = client.place_order_no(
            market_id=ticker,
            quantity=contracts,
            price=sig.price_cents,
            dry_run=config.DRY_RUN,
        )
    order_id = order.get("order", {}).get("order_id") if order else None

    # 8. Log to CSV and track the open position
    risk.record_open_position(ticker, sig.side, contracts, sig.price_cents)
    risk.log_entry_trade(ticker, sig.side, contracts, sig.price_cents)
    log.debug("Order id: %s", order_id)

    # Clear datetime cache at end of cycle
    risk._clear_datetime_cache()
    return True


def _run_once_time_delay(
    client: KalshiClient,
    risk: RiskManager,
    market: dict,
    ticker: str,
    balance: float,
    positions: list,
) -> bool:
    """
    One bot cycle for the ``reddit_time_delay`` strategy mode.

    Flow:
      1. Run manage_positions for safety exits (stop-loss, take-profit, expiry).
      2. Compute current window context (minutes_to_expiry, window_id).
      3. Call decide_trade to get an action.
      4. Act on EXIT_POSITION (price-triggered stop-loss from strategy).
      5. Act on ENTER_YES / ENTER_NO (pass through risk manager first).
    """
    global _last_trade_window_id, _trades_in_current_window

    # 1. Safety exits first (stop-loss, take-profit, expiry — always active)
    exit_error = False
    try:
        for closed in manage_positions(client, market, risk) or []:
            risk.record_closed_position(closed["market"])
            risk.log_exit_trade(
                market=closed["market"],
                side=closed["side"],
                size=closed["size"],
                entry_price=closed["entry_price"],
                exit_price=closed["exit_price"],
                exit_reason=closed["exit_reason"],
            )
    except Exception as exc:
        log.error("Error while managing positions (time_delay): %s", exc, exc_info=True)
        exit_error = True

    if exit_error:
        return False

    # 2. Compute window context
    # Use cached datetime from risk manager for consistency and performance
    cached_now = risk._get_current_datetime()
    minutes_to_expiry = _compute_minutes_to_expiry(market, cached_now)
    current_window_id = _compute_window_id(market)

    # Reset per-window entry counter when the market window has rolled over.
    if current_window_id != _last_trade_window_id:
        _trades_in_current_window = 0

    # Derive entry prices from ask (realistic cost to open a position)
    # and exit prices from bid (the price we can realistically sell at).
    # Support both old field names (yes_ask, yes_bid) and new (best_yes_ask, best_yes_bid)
    # Consolidate market data lookups to avoid redundant dictionary access
    yes_ask_cents = market.get("best_yes_ask") or market.get("yes_ask", 50)
    no_ask_cents = market.get("best_no_ask") or market.get("no_ask", 50)
    yes_bid_cents = market.get("best_yes_bid") or market.get("yes_bid", 50)
    no_bid_cents = market.get("best_no_bid") or market.get("no_bid", 50)
    up_price = float(yes_ask_cents) / 100.0   # ask — used for entry trigger
    down_price = float(no_ask_cents) / 100.0  # ask — used for entry trigger
    up_bid = float(yes_bid_cents) / 100.0     # bid — used for stop-loss exit
    down_bid = float(no_bid_cents) / 100.0    # bid — used for stop-loss exit

    # Determine whether this bot currently holds a YES or NO position
    # Cache the lookup to avoid redundant dictionary comprehension
    bot_positions = risk.get_open_positions()
    bot_pos = bot_positions.get(ticker)
    current_position_side: "str | None" = bot_pos["side"].upper() if bot_pos else None

    log.info(
        "time_delay | ticker=%s | up_ask=%.2f up_bid=%.2f | down_ask=%.2f down_bid=%.2f"
        " | minutes_left=%d | position=%s | trades_in_window=%d",
        ticker, up_price, up_bid, down_price, down_bid,
        minutes_to_expiry, current_position_side, _trades_in_current_window,
    )

    # 3. Ask the strategy what to do
    action, size = decide_trade(
        up_price=up_price,
        down_price=down_price,
        minutes_to_expiry=minutes_to_expiry,
        current_position_side=current_position_side,
        current_window_id=current_window_id,
        last_trade_window_id=_last_trade_window_id,
        cfg=config,
        trades_in_current_window=_trades_in_current_window,
        up_bid=up_bid,
        down_bid=down_bid,
    )

    log.debug("time_delay decide_trade → action=%s size=%s", action, size)

    # 4. Handle strategy-triggered EXIT_POSITION
    if action == "EXIT_POSITION" and bot_pos:
        side = bot_pos["side"]
        count = bot_pos["quantity"]
        entry_price = bot_pos["entry_price"]
        current_price = market.get(f"{side}_bid", entry_price)
        exit_price_order = max(1, current_price - 1)
        # PnL = (sell price - buy price) × contracts — same for YES and NO
        pnl_cents = (exit_price_order - entry_price) * count
        log_trade(
            "EXIT(time_delay) %s | side=%s | entry=%dc | exit=%dc | pnl=%+dc",
            ticker, side, entry_price, exit_price_order, pnl_cents,
        )
        client.close_position(
            market_id=ticker,
            side=side,
            quantity=count,
            price=exit_price_order,
            dry_run=config.DRY_RUN,
        )
        risk.record_closed_position(ticker)
        risk.log_exit_trade(
            market=ticker,
            side=side,
            size=count,
            entry_price=entry_price,
            exit_price=exit_price_order,
            exit_reason="time_delay_exit",
        )
        return True

    # 5. Handle new entries
    if action not in ("ENTER_YES", "ENTER_NO"):
        risk._clear_datetime_cache()
        return False

    side = "yes" if action == "ENTER_YES" else "no"
    entry_price_cents = yes_ask_cents if side == "yes" else no_ask_cents

    # Build a minimal Signal-like object so the existing risk manager can gate
    # the trade using all the same dollar / exposure / daily limits.
    sig_stub = _Signal(
        side=side,
        confidence=1.0,
        price_cents=entry_price_cents,
        reason=f"time_delay {action} window={current_window_id}",
        size=size if size else config.BASE_SIZE,
    )

    approved, reason = risk.approve_trade(sig_stub, balance, positions, ticker)
    if not approved:
        if "Already have" not in reason:
            log.info("time_delay trade rejected by risk manager: %s", reason)
        risk._clear_datetime_cache()
        return False

    budget_contracts = risk.calculate_contracts(sig_stub.price_cents)
    contracts = _compute_trade_contracts(sig_stub.size, budget_contracts)
    if contracts < 1:
        log.warning("time_delay: contract count is 0 — price too high for budget. Skipping.")
        risk._clear_datetime_cache()
        return False

    log_trade(
        "time_delay: Placing BUY %s %s x%d @ %dc (est. cost $%.2f)",
        side.upper(), ticker, contracts, sig_stub.price_cents,
        contracts * sig_stub.price_cents / 100,
    )

    if side == "yes":
        order = client.place_order_yes(
            market_id=ticker,
            quantity=contracts,
            price=sig_stub.price_cents,
            dry_run=config.DRY_RUN,
        )
    else:
        order = client.place_order_no(
            market_id=ticker,
            quantity=contracts,
            price=sig_stub.price_cents,
            dry_run=config.DRY_RUN,
        )
    order_id = order.get("order", {}).get("order_id") if order else None

    risk.record_open_position(ticker, side, contracts, sig_stub.price_cents)
    risk.log_entry_trade(ticker, side, contracts, sig_stub.price_cents)
    _last_trade_window_id = current_window_id
    _trades_in_current_window += 1
    log.debug("time_delay order id: %s", order_id)

    # Clear datetime cache at end of cycle
    risk._clear_datetime_cache()
    return True

def main():
    setup_logging()
    log.info("=" * 60)
    log.info("      Kalshi 15-minute BTC Trader (with Early Exit)")
    log.info(" Environment : %s", config.KALSHI_ENV.upper())
    log.info(" Dry run     : %s", config.DRY_RUN)
    log.info(" Strategy    : %s", config.STRATEGY_MODE)
    log.info(" Stop Loss   : %sc", config.STOP_LOSS_CENTS)
    log.info(" Take Profit : %sc", config.TAKE_PROFIT_CENTS)
    log.info(" Reversal Ex : %s", config.SIGNAL_REVERSAL_EXIT)
    log.info(" Expiry Exit : %ss before close", config.EXPIRY_EXIT_SECONDS)
    log.info(" Max Daily Loss : %sc", config.MAX_DAILY_LOSS_CENTS)
    log.info(" Max Daily Trades: %s", config.MAX_DAILY_TRADES)
    log.info("=" * 60)
    if config.KALSHI_ENV == "prod" and not config.DRY_RUN:
        log.warning("!" * 60)
        log.warning("!!! LIVE TRADING ENABLED ON PRODUCTION - REAL MONEY AT RISK !!!")
        log.warning("!" * 60)

    # Validate config before doing anything else
    try:
        config.validate()
    except EnvironmentError as e:
        log.critical("Configuration error:\n%s", e)
        sys.exit(1)

    client = KalshiClient()
    risk = RiskManager()

    log.info("Bot started. Press Ctrl+C to stop.")

    while _running:
        try:
            run_once(client, risk)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error("Unexpected error in main loop: %s", exc, exc_info=True)
            if not _running:
                break

        log.debug("Sleeping %ds...", config.LOOP_INTERVAL_SECONDS)
        time.sleep(config.LOOP_INTERVAL_SECONDS)

    log.info("Bot stopped cleanly.")

if __name__ == "__main__":
    main()
