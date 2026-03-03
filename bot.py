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
                exit_reason = "stop loss"

        # 2. Take Profit
        if config.TAKE_PROFIT_CENTS > 0 and not exit_reason:
            if current_price >= (entry_price + config.TAKE_PROFIT_CENTS):
                exit_reason = "take profit"

        # 3. Signal Reversal
        if config.SIGNAL_REVERSAL_EXIT and current_signal and not exit_reason:
            if current_signal.side != side and current_signal.confidence >= config.MIN_EDGE_THRESHOLD:
                exit_reason = "signal reversal"

        if exit_reason:
            log.warning("EXITING POSITION in %s due to %s", ticker, exit_reason)
            client.sell_position(
                market_id=ticker,
                side=side,
                quantity=count,
                price=max(1, current_price - 1), # aggressive sell
                dry_run=config.DRY_RUN
            )
            yield {
                "market": ticker,
                "side": side,
                "size": count,
                "entry_price": entry_price,
                "exit_price": max(1, current_price - 1),
                "exit_reason": exit_reason,
            }

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
    if not ticker.startswith(f"{config.BTC_SERIES_TICKER}-"):
        log.error("Refusing non-BTC-series market: %s", ticker)
        return False
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
    for closed in manage_positions(client, market, positions, current_signal=sig) or []:
        risk.log_exit_trade(
            market=closed["market"],
            side=closed["side"],
            size=closed["size"],
            entry_price=closed["entry_price"],
            exit_price=closed["exit_price"],
            exit_reason=closed["exit_reason"],
        )

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

    # 8. Log to CSV
    risk.log_entry_trade(ticker, sig.side, contracts, sig.price_cents)
    log.debug("Order id: %s", order_id)
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
