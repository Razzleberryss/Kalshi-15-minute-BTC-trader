"""
config.py  -  Central configuration loader.

Loads all settings from .env and exposes them as typed constants.
Import this module everywhere instead of calling os.getenv() directly.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# -- Load .env file from project root ----------------------------------------
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


# -- Kalshi API ---------------------------------------------------------------
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
KALSHI_ENV: str = os.getenv("KALSHI_ENV", "prod").lower()  # 'demo' or 'prod'

# Base URLs
if KALSHI_ENV == "prod":
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
else:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


# -- Risk Controls ------------------------------------------------------------
MAX_TRADE_DOLLARS: float = float(os.getenv("MAX_TRADE_DOLLARS", "10"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_TOTAL_EXPOSURE: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "50"))

# -- Strategy -----------------------------------------------------------------
BTC_SERIES_TICKER: str = os.getenv("BTC_SERIES_TICKER", "KXBTC15M")
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.05"))
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))

# -- Logging ------------------------------------------------------------------
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
