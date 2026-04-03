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
import json
import logging
import os
import signal
import sys
import time
import datetime
import functools
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import colorlog

import config
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import generate_signal, decide_trade, Signal as _Signal, get_btc_momentum, get_orderbook_skew
from agent_decision_engine import AgentAction
import cli_executor
from orderbook_utils import extract_raw_arrays as _extract_raw_arrays
from synthetic_cfb_price import (
    build_synthetic_cfb_snapshot,
    RollingSyntheticCfbBuffer,
)

_dashboard_last_write_mono: float = 0.0
_dashboard_last_payload: str | None = None
_cfb_last_full_monotonic: float | None = None


def write_dashboard_state(state: dict) -> None:
    """
    Write the current bot cycle state to dashboard_state.json for the dashboard.

    Uses compact JSON (no indentation) for faster serialization and smaller file size.
    The dashboard parses JSON programmatically, so human readability is not needed.
    Atomic replace avoids torn reads; optional coalescing reduces disk churn.
    """
    global _dashboard_last_write_mono, _dashboard_last_payload
    path = Path(__file__).parent / "dashboard_state.json"
    try:
        payload = json.dumps(state, separators=(",", ":"))
        if config.DASHBOARD_MIN_WRITE_SECONDS > 0:
            now_mono = time.monotonic()
            if (
                _dashboard_last_payload == payload
                and (now_mono - _dashboard_last_write_mono) < config.DASHBOARD_MIN_WRITE_SECONDS
            ):
                return
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        if config.DASHBOARD_MIN_WRITE_SECONDS > 0:
            _dashboard_last_write_mono = time.monotonic()
            _dashboard_last_payload = payload
    except Exception as e:
        log.debug("Failed to write dashboard_state.json: %s", e)


def _compute_trade_contracts(sig_size, budget_contracts):
    """
    Return the number of contracts to trade, capped by the risk budget.

    This is a thin wrapper around ``min(sig_size, budget_contracts)`` so that
    trade sizing semantics are covered by unit tests and protected from
    regressions if the sizing logic is modified in the future.
    """
    return min(sig_size, budget_contracts)


# ── Time-delay strategy helpers ───────────────────────────────────────────────────────────────

@functools.lru_cache(maxsize=200)
def _parse_close_time(close_time_str: str) -> datetime.datetime:
    """
    Parse ISO datetime string and cache result to avoid redundant parsing.

    Uses functools.lru_cache (Least Recently Used) for automatic cache eviction.
    Cache size of 200 provides ~2 day coverage for 15-min markets (~96 per day),
    preventing LRU thrashing during multi-day runs with many distinct market windows.
    LRU eviction keeps the most frequently accessed entries, providing better
    hit rates than manual FIFO implementation.
    """
    return datetime.datetime.fromisoformat(
        close_time_str.replace("Z", "+00:00")
    )


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
_halt_trading = False

# ── Synthetic CFB rolling buffer ─────────────────────────────────────────────────────────────────
# Persists across bot cycles so that synthetic_cfb_avg_60s accumulates a
# true 60-second rolling mean — the closest proxy to Kalshi's BRTI settlement ref.
_cfb_buffer = RollingSyntheticCfbBuffer(window_seconds=60)

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

# ── CLI execution wrappers ────────────────────────────────────────────────────────────────────

def _cli_buy(client: KalshiClient, ticker, side, count, price_cents, dry_run=False):
    """Execute a buy via in-process envelope or CLI subprocess + decision engine."""
    if config.INPROCESS_KALSHI_ORDERS:
        from kalshi_inprocess_orders import buy_envelope

        def _fn():
            return buy_envelope(
                client, ticker, side, count, price_cents, dry_run=dry_run,
            )

        return cli_executor.execute_with_decision_engine([], envelope_fn=_fn)
    args = ["buy", "--ticker", ticker, side, str(count), str(price_cents)]
    if dry_run:
        args.append("--dry-run")
    return cli_executor.execute_with_decision_engine(args)


def _cli_sell(client: KalshiClient, ticker, side, count, price_cents, dry_run=False):
    """Execute a sell via in-process envelope or CLI subprocess + decision engine."""
    if config.INPROCESS_KALSHI_ORDERS:
        from kalshi_inprocess_orders import sell_envelope

        def _fn():
            return sell_envelope(
                client, ticker, side, count, price_cents, dry_run=dry_run,
            )

        return cli_executor.execute_with_decision_engine([], envelope_fn=_fn)
    args = ["sell", "--ticker", ticker, side, str(count), str(price_cents)]
    if dry_run:
        args.append("--dry-run")
    return cli_executor.execute_with_decision_engine(args)

# ── Position Management ────────────────────────────────────────────────────────────────────────
def manage_positions(client: KalshiClient, market: dict, risk: RiskManager, current_signal=None):
    """
    Check positions opened by this bot for stop-loss, take-profit, signal reversal, or expiry.
    Only positions recorded via risk.record_open_position() are managed here, so pre-existing
    positions from other bots on the same account are never touched.
    Yields a dict for every position that is exited.
    """
    global _halt_trading
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
        outcome, _env = _cli_sell(
            client, ticker, side, count, exit_price, dry_run=config.DRY_RUN,
        )
        if outcome.action == AgentAction.HALT_TRADING:
            _halt_trading = True
            return
        if outcome.action != AgentAction.CONTINUE:
            log.error(
                "Position exit failed: action=%s code=%s",
                outcome.action.value, outcome.code,
            )
            return
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

    Supports multiple orderbook formats and handles one-sided books with inference.
    """
    # Default structure in case the orderbook is missing or empty
    result = {
        "best_yes_bid": None,
        "best_yes_ask": None,
        "best_no_bid": None,
        "best_no_ask": None,
        "best_yes_bid_size": 0,
        "best_no_bid_size": 0,
        "mid_price": None,
    }

    if not isinstance(orderbook, dict):
        return result

    try:
        # Support multiple formats, in priority order:
        # 1. orderbook_fp.yes_dollars_fp / no_dollars_fp  (new fixed-point REST format)
        # 2. orderbook_fp.yes_dollars / no_dollars        (older fp variant)
        # 3. orderbook["orderbook"].yes_dollars_fp / no_dollars_fp  (WebSocket-wrapped _fp)
        # 4. orderbook["orderbook"].yes_dollars / no_dollars        (WebSocket-wrapped _dollars)
        # 5. top-level yes_dollars / no_dollars on the orderbook response
        # 6. orderbook["orderbook"].yes / no  (legacy integer-cents, wrapped)
        # 7. top-level yes / no               (legacy integer-cents, direct)

        orderbook_data = orderbook.get("orderbook", {})
        orderbook_fp = orderbook.get("orderbook_fp", {})

        # Try to get yes/no arrays from different possible locations
        yes_array = (
            orderbook_fp.get("yes_dollars_fp")
            or orderbook_fp.get("yes_dollars")
            or orderbook_data.get("yes_dollars_fp")
            or orderbook_data.get("yes_dollars")
            or orderbook.get("yes_dollars")
            or orderbook_data.get("yes")
            or orderbook.get("yes")
        )
        no_array = (
            orderbook_fp.get("no_dollars_fp")
            or orderbook_fp.get("no_dollars")
            or orderbook_data.get("no_dollars_fp")
            or orderbook_data.get("no_dollars")
            or orderbook.get("no_dollars")
            or orderbook_data.get("no")
            or orderbook.get("no")
        )

        # Parse arrays - they could be:
        # - Direct arrays: [[price, size], ...]
        # - Dict with bids/asks: {"bids": [...], "asks": [...]}
        # - String format: [["0.55", "10"], ...]
        def extract_bids(data):
            """Extract bid prices from various formats."""
            if not data:
                return []

            # If data is a dict with 'bids' key
            if isinstance(data, dict):
                bids = data.get("bids") or []
                return bids

            # If data is already a list
            if isinstance(data, list):
                return data

            return []

        yes_bids = extract_bids(yes_array)
        no_bids = extract_bids(no_array)

        def _best_bid(entries):
            """Find the best (highest-priced) bid and its size from bid entries.

            Iterates all entries to find the max, handling ascending (REST API)
            and descending (WebSocket normalized) sort orders.
            """
            if not entries:
                return None, 0

            best_p = None
            best_s = 0
            for entry in entries:
                raw_price = None
                raw_size = 0

                if isinstance(entry, dict):
                    raw_price = entry.get("price")
                    raw_size = entry.get("size", 0)
                elif isinstance(entry, (list, tuple)) and len(entry) >= 1:
                    raw_price = entry[0]
                    raw_size = entry[1] if len(entry) >= 2 else 0
                else:
                    continue

                if isinstance(raw_price, str):
                    try:
                        p = int(round(float(raw_price) * 100))
                    except (ValueError, TypeError):
                        continue
                elif isinstance(raw_price, float) and 0.0 <= raw_price <= 1.0:
                    p = int(round(raw_price * 100))
                elif raw_price is not None:
                    try:
                        p = int(raw_price)
                    except (ValueError, TypeError):
                        continue
                else:
                    continue

                try:
                    s = int(float(raw_size))
                except (ValueError, TypeError):
                    s = 0

                if best_p is None or p > best_p:
                    best_p = p
                    best_s = s

            return best_p, best_s

        best_yes_bid, best_yes_bid_size = _best_bid(yes_bids)
        best_no_bid, best_no_bid_size = _best_bid(no_bids)

        result["best_yes_bid"] = best_yes_bid
        result["best_no_bid"] = best_no_bid
        result["best_yes_bid_size"] = best_yes_bid_size
        result["best_no_bid_size"] = best_no_bid_size

        # Compute asks using complementary pricing
        result["best_yes_ask"] = (100 - result["best_no_bid"]) if result["best_no_bid"] is not None else None
        result["best_no_ask"] = (100 - result["best_yes_bid"]) if result["best_yes_bid"] is not None else None

        # For one-sided books, infer missing ask using minimal spread
        if result["best_yes_bid"] is not None and result["best_yes_ask"] is None:
            # No NO bids, infer YES ask with minimal spread
            result["best_yes_ask"] = min(result["best_yes_bid"] + 1, 99)
            log.debug("Inferred best_yes_ask=%d from best_yes_bid=%d (one-sided book)",
                     result["best_yes_ask"], result["best_yes_bid"])

        if result["best_no_bid"] is not None and result["best_no_ask"] is None:
            # No YES bids, infer NO ask with minimal spread
            result["best_no_ask"] = min(result["best_no_bid"] + 1, 99)
            log.debug("Inferred best_no_ask=%d from best_no_bid=%d (one-sided book)",
                     result["best_no_ask"], result["best_no_bid"])

        # Compute mid price if we have at least one complete bid/ask pair
        if result["best_yes_bid"] is not None and result["best_yes_ask"] is not None:
            result["mid_price"] = (result["best_yes_bid"] + result["best_yes_ask"]) // 2
        elif result["best_no_bid"] is not None and result["best_no_ask"] is not None:
            # Use NO side to compute mid if YES side unavailable
            result["mid_price"] = 100 - ((result["best_no_bid"] + result["best_no_ask"]) // 2)
    except Exception:
        # On any unexpected structure, fall back to defaults (all None)
        return result

    return result


def run_once(client: KalshiClient, risk: RiskManager, ws_client=None):
    """
    Execute one complete bot cycle.
    Returns True if an action was taken, False otherwise.

    Args:
        client: KalshiClient instance for REST API calls
        risk: RiskManager instance for risk checks
        ws_client: Optional WebSocket client for streaming orderbook data
    """
    state = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "active_market_ticker": None,
        "yes_bid": None,
        "yes_ask": None,
        "no_bid": None,
        "no_ask": None,
        "mid_price": None,
        "spread": None,
        "signal_composite": None,
        "signal_momentum": None,
        "signal_skew": None,
        "signal_confidence": None,
        "position_size": 0,
        "realized_pnl_cents": 0,
    }
    try:
        return _run_once_impl(client, risk, ws_client, state)
    finally:
        write_dashboard_state(state)


def _run_once_impl(client: KalshiClient, risk: RiskManager, ws_client=None, state: dict = None):
    """Internal implementation of one bot cycle. Updates *state* in-place for the dashboard."""
    global _last_trade_window_id, _halt_trading, _cfb_last_full_monotonic

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

    if state is not None:
        state["active_market_ticker"] = ticker

    # 1b. Synthetic CF Benchmarks BTC price estimate
    # Scrapes public BTC spot sources to build a best-effort BRTI proxy.
    # The rolling buffer accumulates spot samples to approximate Kalshi's
    # settlement reference (simple average of the last 60 seconds of BRTI).
    # Agent context is enriched when ok=True; degraded fields are set on failure.
    now_mono = time.monotonic()
    skip_firecrawl = False
    if config.CFB_MIN_INTERVAL_SECONDS > 0 and _cfb_last_full_monotonic is not None:
        if (now_mono - _cfb_last_full_monotonic) < config.CFB_MIN_INTERVAL_SECONDS:
            skip_firecrawl = True
    if not skip_firecrawl:
        _cfb_last_full_monotonic = now_mono
    if skip_firecrawl:
        log.debug(
            "SyntheticCFB: API-only refresh (full scrape throttled, interval=%ss)",
            config.CFB_MIN_INTERVAL_SECONDS,
        )
    _cfb_snapshot = build_synthetic_cfb_snapshot(
        config.FIRECRAWL_API_KEY,
        buffer=_cfb_buffer,
        skip_firecrawl=skip_firecrawl,
    )
    if _cfb_snapshot.ok:
        _cfb_ctx: dict = {
            "synthetic_cfb_spot": _cfb_snapshot.synthetic_cfb_spot,
            "synthetic_cfb_mid": _cfb_snapshot.synthetic_cfb_mid,
            "synthetic_cfb_avg_60s": _cfb_snapshot.synthetic_cfb_avg_60s,
            "synthetic_cfb_window_seconds": _cfb_snapshot.window_seconds,
            "synthetic_cfb_sample_count_60s": _cfb_snapshot.sample_count_60s,
            "synthetic_cfb_confidence": _cfb_snapshot.confidence,
            "synthetic_cfb_confidence_score": _cfb_snapshot.confidence_score,
            "synthetic_cfb_spread_bps": _cfb_snapshot.spread_bps,
            "synthetic_cfb_source_count": _cfb_snapshot.source_count,
            "synthetic_cfb_scraped_at": _cfb_snapshot.scraped_at,
        }
        log.debug(
            "SyntheticCFB ok | spot=%.2f avg60s=%s conf=%s spread_bps=%.1f sources=%d samples=%d",
            _cfb_snapshot.synthetic_cfb_spot or 0.0,
            f"{_cfb_snapshot.synthetic_cfb_avg_60s:.2f}" if _cfb_snapshot.synthetic_cfb_avg_60s else "n/a",
            _cfb_snapshot.confidence,
            _cfb_snapshot.spread_bps or 0.0,
            _cfb_snapshot.source_count,
            _cfb_snapshot.sample_count_60s,
        )
    else:
        log.warning(
            "SYNTHETICCFBFAILED | error=%s | continuing cycle with degraded context",
            _cfb_snapshot.error,
        )
        _cfb_ctx = {
            "synthetic_cfb_spot": None,
            "synthetic_cfb_mid": None,
            "synthetic_cfb_avg_60s": None,
            "synthetic_cfb_window_seconds": _cfb_snapshot.window_seconds,
            "synthetic_cfb_sample_count_60s": 0,
            "synthetic_cfb_confidence": _cfb_snapshot.confidence,
            "synthetic_cfb_confidence_score": _cfb_snapshot.confidence_score,
            "synthetic_cfb_spread_bps": _cfb_snapshot.spread_bps,
            "synthetic_cfb_source_count": _cfb_snapshot.source_count,
            "synthetic_cfb_scraped_at": _cfb_snapshot.scraped_at,
        }
    if state is not None:
        state.update(_cfb_ctx)

    # 2. Fetch supporting data
    try:
        # Try to get orderbook from WebSocket if available and connected
        orderbook = None
        if ws_client and ws_client.is_connected():
            # Subscribe to this market if we haven't already
            ws_client.subscribe_to_market(ticker)

            # Try to get orderbook from WebSocket
            ws_orderbook = ws_client.get_latest_orderbook(ticker)
            if ws_orderbook:
                ws_yes, ws_no = _extract_raw_arrays({"orderbook": ws_orderbook})
                if ws_yes or ws_no:
                    # Wrap in same format as REST API response
                    orderbook = {"orderbook": ws_orderbook}
                    log.debug("Using WebSocket orderbook for %s", ticker)
                else:
                    log.debug("WebSocket orderbook for %s is empty, falling back to REST", ticker)

        # Fall back to REST if WebSocket data not available
        if orderbook is None:
            orderbook = client.get_orderbook(ticker)
            if ws_client and ws_client.is_connected():
                log.debug("WebSocket orderbook not available, using REST for %s", ticker)

        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_bal = pool.submit(client.get_balance)
            fut_pos = pool.submit(client.get_positions)
            balance = fut_bal.result()
            positions = fut_pos.result()
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

        # Also propagate legacy field aliases so any code reading yes_bid/yes_ask
        # directly (e.g. time-delay EXIT_POSITION path) gets correct values.
        for new_field, legacy_field in (
            ("best_yes_bid", "yes_bid"),
            ("best_yes_ask", "yes_ask"),
            ("best_no_bid",  "no_bid"),
            ("best_no_ask",  "no_ask"),
        ):
            if quotes.get(new_field) is not None:
                market[legacy_field] = quotes[new_field]

        # Log with orderbook-based prices
        yes_bid = quotes.get("best_yes_bid")
        yes_ask = quotes.get("best_yes_ask")
        no_bid = quotes.get("best_no_bid")
        no_ask = quotes.get("best_no_ask")
        mid = quotes.get("mid_price")

        # Check if orderbook is truly empty (both YES and NO sides have no quotes)
        # With one-sided inference, we should have at least bid OR ask on YES side
        if yes_bid is not None and yes_ask is not None:
            log.info("Active market: %s | last=%sc yes=%dc/%dc no=%dc/%dc mid=%dc (from orderbook)",
                     ticker, market.get("last_price"),
                     yes_bid, yes_ask, no_bid, no_ask, mid)
        else:
            yes_raw, no_raw = _extract_raw_arrays(orderbook)

            yes_display = yes_raw[:5] if yes_raw else []
            no_display = no_raw[:5] if no_raw else []

            log.warning("Active market: %s | orderbook empty (no quotes available) | "
                       "Raw orderbook: yes=%s, no=%s",
                       ticker, yes_display, no_display)
    else:
        # Use old market data fields
        log.info("Active market: %s | last=%sc yes=%s/%s no=%s/%s",
                 ticker, market.get("last_price"),
                 market.get("yes_bid"), market.get("yes_ask"),
                 market.get("no_bid"), market.get("no_ask"))

    # Capture current market prices for dashboard state
    if state is not None:
        state["yes_bid"] = market.get("best_yes_bid") or market.get("yes_bid")
        state["yes_ask"] = market.get("best_yes_ask") or market.get("yes_ask")
        state["no_bid"] = market.get("best_no_bid") or market.get("no_bid")
        state["no_ask"] = market.get("best_no_ask") or market.get("no_ask")
        state["yes_bid_size"] = market.get("best_yes_bid_size", 0)
        state["no_bid_size"] = market.get("best_no_bid_size", 0)
        state["mid_price"] = market.get("mid_price")
        _yb, _ya = state["yes_bid"], state["yes_ask"]
        if _yb is not None and _ya is not None:
            state["spread"] = _ya - _yb
        state["realized_pnl_cents"] = risk._daily_realized_pnl_cents
        # NOTE: kalshi_dislocation_* (mid vs synthetic CFB) is intentionally omitted.
        # mid_price is contract probability in cents, not a BTC/USD level; dislocation
        # vs synthetic_cfb_* requires a proper strike/index reference (future work).

    # ── reddit_time_delay strategy path ───────────────────────────────────────
    if config.STRATEGY_MODE == "reddit_time_delay":
        return _run_once_time_delay(client, risk, market, ticker, balance, positions, orderbook, state)

    # ── fee_aware_model strategy path (default) ────────────────────────────────

    # 3. Generate signal
    log.debug(
        "bot to strategy: best_yes_bid=%s best_yes_ask=%s best_no_bid=%s best_no_ask=%s mid=%s",
        market.get("best_yes_bid"),
        market.get("best_yes_ask"),
        market.get("best_no_bid"),
        market.get("best_no_ask"),
        market.get("mid_price"),
    )
    sig = generate_signal(market, orderbook)

    # Capture signal components for dashboard (get_btc_momentum/get_orderbook_skew use caches,
    # so these calls do not trigger additional API requests).
    # The 0.6/0.4 weights mirror generate_signal() in strategy.py — kept here so the
    # dashboard has a pre-computed composite without modifying the strategy API.
    if state is not None:
        _momentum = get_btc_momentum()
        _skew = get_orderbook_skew(orderbook)
        if _momentum is not None:
            state["signal_momentum"] = round(_momentum, 4)
            state["signal_skew"] = round(_skew, 4)
            state["signal_composite"] = round((0.6 * _momentum) + (0.4 * _skew), 4)
        if sig is not None:
            state["signal_confidence"] = round(sig.confidence, 4)
        _pos = risk.get_open_positions().get(ticker)
        state["position_size"] = _pos["quantity"] if _pos else 0
    
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

    if _halt_trading:
        risk._clear_datetime_cache()
        return False

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

    # 7. Execute via CLI + decision engine
    outcome, envelope = _cli_buy(
        client, ticker, sig.side, contracts, sig.price_cents, dry_run=config.DRY_RUN,
    )
    if outcome.action == AgentAction.HALT_TRADING:
        _halt_trading = True
        risk._clear_datetime_cache()
        return False
    if outcome.action != AgentAction.CONTINUE:
        risk._clear_datetime_cache()
        return False
    order_id = envelope.get("result", {}).get("order_id")

    # 8. Log to CSV and track the open position
    risk.record_open_position(ticker, sig.side, contracts, sig.price_cents)
    risk.log_entry_trade(ticker, sig.side, contracts, sig.price_cents)
    log.debug("Order id: %s", order_id)

    # Refresh dashboard position size after new trade is recorded
    if state is not None:
        _pos = risk.get_open_positions().get(ticker)
        state["position_size"] = _pos["quantity"] if _pos else 0

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
    orderbook: dict,
    state: dict = None,
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
    global _last_trade_window_id, _trades_in_current_window, _halt_trading

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
        risk._clear_datetime_cache()
        return False
    if _halt_trading:
        risk._clear_datetime_cache()
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
        pnl_cents = (
            (exit_price_order - entry_price) * count
            if side == "yes"
            else (entry_price - exit_price_order) * count
        )
        log_trade(
            "EXIT(time_delay) %s | side=%s | entry=%dc | exit=%dc | pnl=%+dc",
            ticker, side, entry_price, exit_price_order, pnl_cents,
        )
        outcome, _env = _cli_sell(
            client, ticker, side, count, exit_price_order, dry_run=config.DRY_RUN,
        )
        if outcome.action == AgentAction.HALT_TRADING:
            _halt_trading = True
            return False
        if outcome.action != AgentAction.CONTINUE:
            return False
        risk.record_closed_position(ticker)
        risk.log_exit_trade(
            market=ticker,
            side=side,
            size=count,
            entry_price=entry_price,
            exit_price=exit_price_order,
            exit_reason="time_delay_exit",
        )
        if state is not None:
            state["position_size"] = 0
            state["realized_pnl_cents"] = risk._daily_realized_pnl_cents
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

    outcome, envelope = _cli_buy(
        client, ticker, side, contracts, sig_stub.price_cents, dry_run=config.DRY_RUN,
    )
    if outcome.action == AgentAction.HALT_TRADING:
        _halt_trading = True
        risk._clear_datetime_cache()
        return False
    if outcome.action != AgentAction.CONTINUE:
        risk._clear_datetime_cache()
        return False
    order_id = envelope.get("result", {}).get("order_id")

    risk.record_open_position(ticker, side, contracts, sig_stub.price_cents)
    risk.log_entry_trade(ticker, side, contracts, sig_stub.price_cents)
    _last_trade_window_id = current_window_id
    _trades_in_current_window += 1
    log.debug("time_delay order id: %s", order_id)

    # Update dashboard state with final position and PnL
    if state is not None:
        _pos = risk.get_open_positions().get(ticker)
        state["position_size"] = _pos["quantity"] if _pos else 0
        state["realized_pnl_cents"] = risk._daily_realized_pnl_cents

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
    log.info(" Use WebSocket: %s", config.USE_WEBSOCKET_ORDERBOOK)
    log.info(" CFB scrape interval: %ss (0=always full)", config.CFB_MIN_INTERVAL_SECONDS)
    log.info(" In-process orders: %s", config.INPROCESS_KALSHI_ORDERS)
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

    # Initialize WebSocket client if enabled
    ws_client = None
    if config.USE_WEBSOCKET_ORDERBOOK:
        try:
            from websocket_client import KalshiWebSocketClient
            ws_client = KalshiWebSocketClient()
            ws_client.start()
            log.info("WebSocket client started")
        except Exception as exc:
            log.warning("Failed to start WebSocket client: %s (falling back to REST)", exc)
            ws_client = None

    log.info("Bot started. Press Ctrl+C to stop.")

    try:
        while _running and not _halt_trading:
            try:
                run_once(client, risk, ws_client=ws_client)
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log.error("Unexpected error in main loop: %s", exc, exc_info=True)
                if not _running:
                    break

            if _halt_trading:
                log.critical("HALT_TRADING — decision engine stopped the bot.")
                break

            log.debug("Sleeping %ds...", config.LOOP_INTERVAL_SECONDS)
            time.sleep(config.LOOP_INTERVAL_SECONDS)
    finally:
        # Clean shutdown of WebSocket client
        if ws_client:
            log.info("Shutting down WebSocket client...")
            ws_client.stop()

    log.info("Bot stopped cleanly.")

if __name__ == "__main__":
    main()
