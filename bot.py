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

import colorlog

import config
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import generate_signal

# ── Logging setup ──────────────────────────────────────────────────────────────────────────────
def setup_logging():
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG": "cyan",
            "INFO": "green",
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

def _handle_signal(sig, frame):
    global _running
    log.warning("Shutdown signal received — stopping after this cycle...")
    _running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Position Management ────────────────────────────────────────────────────────────────────────
def manage_positions(client: KalshiClient, market: dict, positions: list, current_signal=None):
    """
    Check open positions for stop-loss, take-profit, or signal reversal.
    Exits positions if any criteria are met.
    """
    ticker = market["ticker"]
    active_pos = [p for p in positions if p.get("ticker") == ticker]
    
    if not active_pos:
        return

    for pos in active_pos:
        side = pos.get("side") # 'yes' or 'no'
        count = abs(pos.get("position", 0))
        entry_price = pos.get("average_price", 50)
        
        # Get current market price for our side
        current_price = market.get(f"{side}_bid", entry_price) # what we can sell for right now
        
        exit_reason = None
        
        # 1. Stop Loss
        if config.STOP_LOSS_CENTS > 0:
            if current_price <= (entry_price - config.STOP_LOSS_CENTS):
                exit_reason = f"STOP_LOSS hit: {current_price}c <= {entry_price}-{config.STOP_LOSS_CENTS}c"

        # 2. Take Profit
        if config.TAKE_PROFIT_CENTS > 0 and not exit_reason:
            if current_price >= (entry_price + config.TAKE_PROFIT_CENTS):
                exit_reason = f"TAKE_PROFIT hit: {current_price}c >= {entry_price}+{config.TAKE_PROFIT_CENTS}c"

        # 3. Signal Reversal
        if config.SIGNAL_REVERSAL_EXIT and current_signal and not exit_reason:
            if current_signal.side != side and current_signal.confidence >= config.MIN_EDGE_THRESHOLD:
                exit_reason = f"SIGNAL_REVERSAL: signal is {current_signal.side.upper()} but holding {side.upper()}"

        if exit_reason:
            log.warning("EXITING POSITION in %s: %s", ticker, exit_reason)
            client.sell_position(
                ticker=ticker,
                side=side,
                count=count,
                price_cents=max(1, current_price - 1), # aggressive sell
                dry_run=config.DRY_RUN
            )

# ── Core bot loop ─────────────────────────────────────────────────────────────────────────────
def run_once(client: KalshiClient, risk: RiskManager):
    """
    Execute one complete bot cycle.
    Returns True if an action was taken, False otherwise.
    """
    # 1. Find the active market
    market = client.get_active_btc_market()
    if not market:
        log.warning("No active BTC 15-min market found. Skipping cycle.")
        return False

    ticker = market["ticker"]
    log.info("Active market: %s | last=%sc yes=%s/%s no=%s/%s", 
             ticker, market.get("last_price"), 
             market.get("yes_bid"), market.get("yes_ask"),
             market.get("no_bid"), market.get("no_ask"))

    # 2. Fetch supporting data
    try:
        orderbook = client.get_orderbook(ticker)
        balance = client.get_balance()
        positions = client.get_positions()
    except Exception as exc:
        log.error("API fetch error: %s", exc)
        return False

    # 3. Generate signal
    sig = generate_signal(market, orderbook)
    
    # 4. Manage existing positions first
    manage_positions(client, market, positions, current_signal=sig)

    # 5. Risk check for NEW trade
    if sig is None:
        return False
        
    approved, reason = risk.approve_trade(sig, balance, positions, ticker)
    if not approved:
        # Don't log "Already have position" as an error, it's normal if we didn't exit
        if "Already have" not in reason:
            log.info("New trade rejected by risk manager: %s", reason)
        return False

    # 6. Size the trade
    contracts = risk.calculate_contracts(sig.price_cents)
    if contracts < 1:
        log.warning("Contract count is 0 — price too high for budget. Skipping.")
        return False

    log.info(
        "Placing BUY %s %s x%d @ %dc (est. cost $%.2f) | reason: %s",
        sig.side.upper(), ticker, contracts, sig.price_cents,
        contracts * sig.price_cents / 100, sig.reason
    )

    # 7. Execute
    order = client.place_order(
        ticker=ticker,
        side=sig.side,
        count=contracts,
        price_cents=sig.price_cents,
        dry_run=config.DRY_RUN,
    )
    order_id = order.get("order", {}).get("order_id") if order else None

    # 8. Log to CSV
    risk.log_trade(
        ticker=ticker,
        side=sig.side,
        contracts=contracts,
        price_cents=sig.price_cents,
        confidence=sig.confidence,
        dry_run=config.DRY_RUN,
        order_id=order_id,
        reason=sig.reason,
    )
    return True

def main():
    setup_logging()
    log.info("=" * 60)
    log.info("      Kalshi 15-minute BTC Trader (with Early Exit)")
    log.info(" Environment : %s", config.KALSHI_ENV.upper())
    log.info(" Dry run     : %s", config.DRY_RUN)
    log.info(" Stop Loss   : %sc", config.STOP_LOSS_CENTS)
    log.info(" Take Profit : %sc", config.TAKE_PROFIT_CENTS)
    log.info(" Reversal Ex : %s", config.SIGNAL_REVERSAL_EXIT)
    log.info("=" * 60)

    # Validate config before doing anything else
    try:
        config.validate()
    except EnvironmentError as e:
        log.critical("Configuration error:
%s", e)
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
