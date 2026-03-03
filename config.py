"""
config.py – Central configuration loader.

Loads all settings from .env and exposes them as typed constants.
Import this module everywhere instead of calling os.getenv() directly.
"""
import os
import logging
from pathlib import Path
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
if KALSHI_ENV == "prod":
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
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
# Strategy / Signal
# =============================================================================
# BTC_SERIES_TICKER: Kalshi 15-min BTC Up/Down series.
# The live series ticker is BTCZ (e.g. BTCZ-25DEC3100-T3PM).
# Override in .env if Kalshi changes the series name.
BTC_SERIES_TICKER: str = os.getenv("BTC_SERIES_TICKER", "BTCZ")
BTC_TICKER: str = os.getenv("BTC_TICKER", "BTC-USD")  # yfinance symbol
MOMENTUM_LOOKBACK_BARS: int = int(os.getenv("MOMENTUM_LOOKBACK_BARS", "5"))
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.05"))
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))

# =============================================================================
# Bot Loop
# =============================================================================
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOOP_INTERVAL_SECONDS: int = POLL_INTERVAL_SECONDS  # alias used in bot.py

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
    if STOP_LOSS_CENTS < 0:
        errors.append("STOP_LOSS_CENTS must be >= 0")
    if TAKE_PROFIT_CENTS < 0:
        errors.append("TAKE_PROFIT_CENTS must be >= 0")
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
    log.info("LOOP_INTERVAL_SECONDS : %ss", LOOP_INTERVAL_SECONDS)
    log.info("TRADE_LOG_FILE        : %s", TRADE_LOG_FILE)
    try:
        validate()
        log.info("\nConfig validation: PASSED")
    except EnvironmentError as e:
        log.error("\nConfig validation: FAILED\n%s", e)
