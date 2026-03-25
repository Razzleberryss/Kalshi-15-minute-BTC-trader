"""
config.py – Central configuration loader.

Loads all settings from .env and exposes them as typed constants.
Import this module everywhere instead of calling os.getenv() directly.
"""
import os
import logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

# -- Load .env file from project root ----------------------------------------
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# =============================================================================
# Kalshi API
# =============================================================================
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
KALSHI_ENV: str = os.getenv("KALSHI_ENV", "prod").lower()  # 'demo' or 'prod'

# Base URLs (also exposed as KALSHI_BASE_URL for kalshi_client.py)
# Using api.elections.kalshi.com (v2 production endpoint) per Kalshi API changelog
if KALSHI_ENV == "prod":
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
else:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

KALSHI_BASE_URL = BASE_URL  # alias used in kalshi_client.py

# =============================================================================
# Risk Controls
# =============================================================================
MAX_TRADE_DOLLARS: float = float(os.getenv("MAX_TRADE_DOLLARS", "10"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_TOTAL_EXPOSURE: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "50"))
MAX_DAILY_LOSS_CENTS: int = int(os.getenv("MAX_DAILY_LOSS_CENTS", "1000"))
MAX_DAILY_TRADES: int = int(os.getenv("MAX_DAILY_TRADES", "20"))

# Contract price range allowed (in cents, 1-99)
MIN_CONTRACT_PRICE_CENTS: int = int(os.getenv("MIN_CONTRACT_PRICE_CENTS", "10"))
MAX_CONTRACT_PRICE_CENTS: int = int(os.getenv("MAX_CONTRACT_PRICE_CENTS", "90"))

# =============================================================================
# Exit / Position Management
# =============================================================================
# Stop-loss: sell if current contract price drops this many cents below entry
# Example: entry=55c, STOP_LOSS_CENTS=20 -> sell if price falls to 35c
# Set to 0 to disable stop-loss
STOP_LOSS_CENTS: int = int(os.getenv("STOP_LOSS_CENTS", "20"))

# Take-profit: sell if current contract price rises this many cents above entry
# Example: entry=55c, TAKE_PROFIT_CENTS=30 -> sell if price hits 85c
# Set to 0 to disable take-profit
TAKE_PROFIT_CENTS: int = int(os.getenv("TAKE_PROFIT_CENTS", "30"))

# Signal reversal exit: if True, sell open position when signal flips against it
# Example: holding YES contracts but momentum + skew now strongly favor NO -> sell
SIGNAL_REVERSAL_EXIT: bool = os.getenv("SIGNAL_REVERSAL_EXIT", "true").lower() == "true"

# =============================================================================
# Strategy Mode
# =============================================================================
# Which strategy to use. Allowed values:
#   "fee_aware_model"   – the default momentum + fee-aware model strategy
#   "reddit_time_delay" – time-delay + price-trigger strategy
STRATEGY_MODE: str = os.getenv("STRATEGY_MODE", "fee_aware_model")

# reddit_time_delay parameters
# Price (0.0–1.0, i.e. dollars) that the winning side must reach to trigger entry.
TRIGGER_POINT_PRICE: float = float(os.getenv("TRIGGER_POINT_PRICE", "0.90"))
# Price (0.0–1.0) at which an open position is exited early (stop-loss reversal).
EXIT_POINT_PRICE: float = float(os.getenv("EXIT_POINT_PRICE", "0.40"))
# Only arm the strategy when this many minutes (or fewer) remain before expiry.
TRIGGER_MINUTE_REMAINING: int = int(os.getenv("TRIGGER_MINUTE_REMAINING", "14"))
# Maximum number of entries allowed per 15-minute window (set 1 to allow only one entry).
MAX_TRADES_PER_WINDOW: int = int(os.getenv("MAX_TRADES_PER_WINDOW", "1"))

# =============================================================================
# Strategy / Signal
# =============================================================================
# BTC_SERIES_TICKER: Kalshi 15-min BTC Up/Down series.
# The live series ticker is BTCZ (e.g. BTCZ-25DEC3100-T3PM).
# Override in .env if Kalshi changes the series name.
BTC_SERIES_TICKER: str = os.getenv("BTC_SERIES_TICKER", "BTCZ")
BTC_TICKER: str = os.getenv("BTC_TICKER", "BTC-USD")  # yfinance symbol
MOMENTUM_LOOKBACK_BARS: int = int(os.getenv("MOMENTUM_LOOKBACK_BARS", "5"))

# USE_ORDERBOOK_PRICES: When True, fetch quotes from the orderbook instead of
# the /markets API response. Defaults to True (new behavior). Set
# USE_ORDERBOOK_PRICES=false in your .env to restore the previous behavior
# based on the /markets API prices.
USE_ORDERBOOK_PRICES: bool = os.getenv("USE_ORDERBOOK_PRICES", "true").lower() == "true"

# USE_WEBSOCKET_ORDERBOOK: When True, use WebSocket streaming for orderbook data
# instead of REST API polling. Falls back to REST if WebSocket is unavailable.
USE_WEBSOCKET_ORDERBOOK: bool = os.getenv("USE_WEBSOCKET_ORDERBOOK", "false").lower() == "true"

# Trading threshold parameters - tunable for more/less aggressive trading
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.02"))
MIN_CONFIDENCE: float = float(os.getenv("MIN_CONFIDENCE", "0.003"))
MAX_PRICE_DEVIATION: float = float(os.getenv("MAX_PRICE_DEVIATION", "0.12"))
MAX_SLIPPAGE: float = float(os.getenv("MAX_SLIPPAGE", "0.08"))

# =============================================================================
# Liquidity Filters
# =============================================================================
# Maximum allowed spread (in probability terms, 0.0-1.0) before skipping trade
# Example: 0.12 means 12 cents or 12 percentage points spread
MAX_SPREAD: float = float(os.getenv("MAX_SPREAD", "0.12"))

# Minimum orderbook depth at best YES/NO bid level.
# Lowered from 50 to 5 so the bot trades on thin-but-real books;
# raise via MIN_YES_DEPTH / MIN_NO_DEPTH env vars to be more selective.
MIN_YES_DEPTH: int = int(os.getenv("MIN_YES_DEPTH", "5"))
MIN_NO_DEPTH: int = int(os.getenv("MIN_NO_DEPTH", "5"))

# Depth band: how many cents/probability points around mid to count depth
# Example: 0.05 means ±5 cents or ±5 percentage points around mid
DEPTH_BAND: float = float(os.getenv("DEPTH_BAND", "0.05"))

# =============================================================================
# Fee-Aware Entry Parameters
# =============================================================================
# Minimum probability mispricing (in percentage points) required to place a
# trade.  E.g. 0.10 means model must be at least 10 pp above/below market.
MIN_EDGE_PCT: float = float(os.getenv("MIN_EDGE_PCT", "0.10"))

# Forbidden price band: skip entry when the YES price is in this range.
# Fees bite hardest near 0.50, so the default excludes the 0.30–0.70 region.
FORBIDDEN_PRICE_LOW: float = float(os.getenv("FORBIDDEN_PRICE_LOW", "0.30"))
FORBIDDEN_PRICE_HIGH: float = float(os.getenv("FORBIDDEN_PRICE_HIGH", "0.70"))

# Minimum expected net value per contract after fees (in dollars).
# E.g. 0.02 = at least 2 cents of edge after paying open + close fees.
MIN_EXPECTED_NET_PER_CONTRACT: float = float(
    os.getenv("MIN_EXPECTED_NET_PER_CONTRACT", "0.02")
)

# Position-sizing bounds (in contracts) for the edge-based dynamic sizer.
BASE_SIZE: int = int(os.getenv("BASE_SIZE", "1"))
MAX_SIZE: int = int(os.getenv("MAX_SIZE", "10"))

# At this mispricing level (and above) the sizer uses MAX_SIZE contracts.
MAX_EDGE_PCT: float = float(os.getenv("MAX_EDGE_PCT", "0.30"))

# Optional fractional position sizing (in fixed-point contract counts)
# If set, overrides BASE_SIZE/MAX_SIZE for position sizing
# Example: ORDER_SIZE_FP=5 means always trade 5 contracts
ORDER_SIZE_FP: Optional[int] = None
_order_size_fp_str = os.getenv("ORDER_SIZE_FP", "")
if _order_size_fp_str:
    try:
        ORDER_SIZE_FP = int(_order_size_fp_str)
    except ValueError:
        pass

# =============================================================================
# API Client
# =============================================================================
# Per-request timeout in seconds
REQUEST_TIMEOUT_SECONDS: int = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "10"))
# Maximum number of retry attempts for transient API errors (0 = no retries)
REQUEST_MAX_RETRIES: int = int(os.getenv("REQUEST_MAX_RETRIES", "3"))

# =============================================================================
# Bot Loop
# =============================================================================
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOOP_INTERVAL_SECONDS: int = POLL_INTERVAL_SECONDS  # alias used in bot.py

# How many seconds before contract close_time to trigger the expiry exit
EXPIRY_EXIT_SECONDS: int = int(os.getenv("EXPIRY_EXIT_SECONDS", "120"))

# =============================================================================
# Logging & Output
# =============================================================================
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
TRADE_LOG_FILE: str = os.getenv("TRADE_LOG_FILE", "trades.csv")

# =============================================================================
# Validation
# =============================================================================
def validate() -> None:
    """
    Raise EnvironmentError listing every missing or invalid config value.
    Called once at bot startup before any API requests are made.
    """
    errors: list[str] = []
    if not KALSHI_API_KEY_ID:
        errors.append("KALSHI_API_KEY_ID is not set")
    if not KALSHI_PRIVATE_KEY_PATH or not Path(KALSHI_PRIVATE_KEY_PATH).exists():
        errors.append(
            f"KALSHI_PRIVATE_KEY_PATH '{KALSHI_PRIVATE_KEY_PATH}' does not exist"
        )
    if KALSHI_ENV not in ("prod", "demo"):
        errors.append(f"KALSHI_ENV must be 'prod' or 'demo', got '{KALSHI_ENV}'")
    if MAX_TRADE_DOLLARS <= 0:
        errors.append("MAX_TRADE_DOLLARS must be > 0")
    if MAX_OPEN_POSITIONS < 1:
        errors.append("MAX_OPEN_POSITIONS must be >= 1")
    if MAX_TOTAL_EXPOSURE < MAX_TRADE_DOLLARS:
        errors.append("MAX_TOTAL_EXPOSURE must be >= MAX_TRADE_DOLLARS")
    if MAX_DAILY_LOSS_CENTS < 0:
        errors.append("MAX_DAILY_LOSS_CENTS must be >= 0")
    if MAX_DAILY_TRADES < 1:
        errors.append("MAX_DAILY_TRADES must be >= 1")
    if not (1 <= MIN_CONTRACT_PRICE_CENTS <= 99):
        errors.append("MIN_CONTRACT_PRICE_CENTS must be between 1 and 99")
    if not (1 <= MAX_CONTRACT_PRICE_CENTS <= 99):
        errors.append("MAX_CONTRACT_PRICE_CENTS must be between 1 and 99")
    if MIN_CONTRACT_PRICE_CENTS >= MAX_CONTRACT_PRICE_CENTS:
        errors.append("MIN_CONTRACT_PRICE_CENTS must be < MAX_CONTRACT_PRICE_CENTS")
    if MOMENTUM_LOOKBACK_BARS < 1:
        errors.append("MOMENTUM_LOOKBACK_BARS must be >= 1")
    if not (0.0 < MIN_EDGE_THRESHOLD < 1.0):
        errors.append("MIN_EDGE_THRESHOLD must be between 0 and 1")
    if not (0.0 < MIN_CONFIDENCE < 1.0):
        errors.append("MIN_CONFIDENCE must be between 0 and 1")
    if not (0.0 < MAX_PRICE_DEVIATION <= 1.0):
        errors.append("MAX_PRICE_DEVIATION must be between 0 (exclusive) and 1 (inclusive)")
    if not (0.0 < MAX_SLIPPAGE <= 1.0):
        errors.append("MAX_SLIPPAGE must be between 0 (exclusive) and 1 (inclusive)")
    if not (0.0 < MIN_EDGE_PCT < 1.0):
        errors.append("MIN_EDGE_PCT must be between 0 and 1")
    if not (0.0 <= FORBIDDEN_PRICE_LOW < FORBIDDEN_PRICE_HIGH <= 1.0):
        errors.append(
            "FORBIDDEN_PRICE_LOW must be >= 0, < FORBIDDEN_PRICE_HIGH, and FORBIDDEN_PRICE_HIGH <= 1"
        )
    if MIN_EXPECTED_NET_PER_CONTRACT < 0:
        errors.append("MIN_EXPECTED_NET_PER_CONTRACT must be >= 0")
    if BASE_SIZE < 1:
        errors.append("BASE_SIZE must be >= 1")
    if MAX_SIZE < BASE_SIZE:
        errors.append("MAX_SIZE must be >= BASE_SIZE")
    if not (0.0 < MAX_EDGE_PCT <= 1.0):
        errors.append("MAX_EDGE_PCT must be between 0 (exclusive) and 1 (inclusive)")
    if MAX_EDGE_PCT <= MIN_EDGE_PCT:
        errors.append("MAX_EDGE_PCT must be > MIN_EDGE_PCT")
    if REQUEST_TIMEOUT_SECONDS < 1:
        errors.append("REQUEST_TIMEOUT_SECONDS must be >= 1")
    if REQUEST_MAX_RETRIES < 0:
        errors.append("REQUEST_MAX_RETRIES must be >= 0")
    if EXPIRY_EXIT_SECONDS < 0:
        errors.append("EXPIRY_EXIT_SECONDS must be >= 0")
    if STOP_LOSS_CENTS < 0:
        errors.append("STOP_LOSS_CENTS must be >= 0")
    if TAKE_PROFIT_CENTS < 0:
        errors.append("TAKE_PROFIT_CENTS must be >= 0")
    if STRATEGY_MODE not in ("fee_aware_model", "reddit_time_delay"):
        errors.append(
            f"STRATEGY_MODE must be 'fee_aware_model' or 'reddit_time_delay', got '{STRATEGY_MODE}'"
        )
    if not (0.0 < TRIGGER_POINT_PRICE <= 1.0):
        errors.append("TRIGGER_POINT_PRICE must be between 0 (exclusive) and 1 (inclusive)")
    if not (0.0 < EXIT_POINT_PRICE < TRIGGER_POINT_PRICE):
        errors.append(
            "EXIT_POINT_PRICE must be > 0 and < TRIGGER_POINT_PRICE"
        )
    if TRIGGER_MINUTE_REMAINING < 0:
        errors.append("TRIGGER_MINUTE_REMAINING must be >= 0")
    if MAX_TRADES_PER_WINDOW < 1:
        errors.append("MAX_TRADES_PER_WINDOW must be >= 1")
    # Liquidity filter validations
    if not (0.0 <= MAX_SPREAD <= 1.0):
        errors.append("MAX_SPREAD must be between 0 and 1")
    if MIN_YES_DEPTH < 0:
        errors.append("MIN_YES_DEPTH must be >= 0")
    if MIN_NO_DEPTH < 0:
        errors.append("MIN_NO_DEPTH must be >= 0")
    if not (0.0 <= DEPTH_BAND <= 1.0):
        errors.append("DEPTH_BAND must be between 0 and 1")
    if ORDER_SIZE_FP is not None and ORDER_SIZE_FP < 1:
        errors.append("ORDER_SIZE_FP must be >= 1 when set")
    if errors:
        raise EnvironmentError(
            "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    log = logging.getLogger(__name__)
    log.info("KALSHI_ENV            : %s", KALSHI_ENV)
    log.info("BASE_URL              : %s", BASE_URL)
    log.info("KALSHI_API_KEY_ID     : %s", "SET" if KALSHI_API_KEY_ID else "NOT SET")
    log.info("KALSHI_PRIVATE_KEY    : %s", KALSHI_PRIVATE_KEY_PATH)
    log.info("BTC_SERIES_TICKER     : %s", BTC_SERIES_TICKER)
    log.info("DRY_RUN               : %s", DRY_RUN)
    log.info("MAX_TRADE_DOLLARS     : $%s", MAX_TRADE_DOLLARS)
    log.info("MAX_OPEN_POSITIONS    : %s", MAX_OPEN_POSITIONS)
    log.info("MAX_TOTAL_EXPOSURE    : $%s", MAX_TOTAL_EXPOSURE)
    log.info("MAX_DAILY_LOSS_CENTS  : %sc", MAX_DAILY_LOSS_CENTS)
    log.info("MAX_DAILY_TRADES      : %s", MAX_DAILY_TRADES)
    log.info("STOP_LOSS_CENTS       : %sc", STOP_LOSS_CENTS)
    log.info("TAKE_PROFIT_CENTS     : %sc", TAKE_PROFIT_CENTS)
    log.info("SIGNAL_REVERSAL_EXIT  : %s", SIGNAL_REVERSAL_EXIT)
    log.info("MIN_EDGE_PCT          : %.2f", MIN_EDGE_PCT)
    log.info("FORBIDDEN_PRICE_BAND  : %.2f – %.2f", FORBIDDEN_PRICE_LOW, FORBIDDEN_PRICE_HIGH)
    log.info("MIN_EV_NET/CONTRACT   : $%.3f", MIN_EXPECTED_NET_PER_CONTRACT)
    log.info("BASE_SIZE / MAX_SIZE  : %d / %d", BASE_SIZE, MAX_SIZE)
    log.info("MAX_EDGE_PCT          : %.2f", MAX_EDGE_PCT)
    log.info("LOOP_INTERVAL_SECONDS : %ss", LOOP_INTERVAL_SECONDS)
    log.info("EXPIRY_EXIT_SECONDS   : %ss", EXPIRY_EXIT_SECONDS)
    log.info("REQUEST_TIMEOUT_SECS  : %ss", REQUEST_TIMEOUT_SECONDS)
    log.info("REQUEST_MAX_RETRIES   : %s", REQUEST_MAX_RETRIES)
    log.info("TRADE_LOG_FILE        : %s", TRADE_LOG_FILE)
    log.info("STRATEGY_MODE         : %s", STRATEGY_MODE)
    log.info("TRIGGER_POINT_PRICE   : %.2f", TRIGGER_POINT_PRICE)
    log.info("EXIT_POINT_PRICE      : %.2f", EXIT_POINT_PRICE)
    log.info("TRIGGER_MINUTE_REMAINING : %d", TRIGGER_MINUTE_REMAINING)
    log.info("MAX_TRADES_PER_WINDOW : %d", MAX_TRADES_PER_WINDOW)
    try:
        validate()
        log.info("\nConfig validation: PASSED")
    except EnvironmentError as e:
        log.error("\nConfig validation: FAILED\n%s", e)
