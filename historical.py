"""
historical.py - Historical market data fetcher for Kalshi.

Provides functionality to fetch historical market data from Kalshi's API
for backtesting and analysis purposes.

Usage:
    python -m historical --series KXBTC15M --start 2025-12-01 --end 2025-12-31
"""
import argparse
import datetime
import json
import logging
import sys
from typing import List, Dict, Optional

from kalshi_client import KalshiClient
import config

log = logging.getLogger(__name__)


def fetch_historical_markets(
    series_ticker: str,
    start_date: str,
    end_date: str,
    client: Optional[KalshiClient] = None
) -> List[Dict]:
    """
    Fetch historical markets for a given series between start_date and end_date.

    Args:
        series_ticker: The series ticker (e.g., "KXBTC15M" or "BTCZ")
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format
        client: Optional KalshiClient instance. If not provided, creates a new one.

    Returns:
        List of market dicts with prices, outcomes, and metadata.
        Each dict contains:
            - ticker: str
            - series_ticker: str
            - title: str
            - subtitle: str
            - open_time: str (ISO 8601)
            - close_time: str (ISO 8601)
            - expected_expiration_time: str (ISO 8601)
            - result: str ("yes", "no", or empty if unresolved)
            - last_price: int (cents)
            - yes_price: int (cents, if available)
            - no_price: int (cents, if available)
            - status: str ("open", "closed", "settled")
            - volume: int (if available)
            - liquidity: int (if available)

    Raises:
        ValueError: If date format is invalid
        requests.exceptions.RequestException: If API call fails
    """
    # Validate date format
    try:
        start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d")
        end_dt = datetime.datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Invalid date format. Use YYYY-MM-DD: {exc}")

    if start_dt > end_dt:
        raise ValueError("start_date must be before or equal to end_date")

    # Create client if not provided
    if client is None:
        client = KalshiClient()

    # Fetch markets for the series
    # Kalshi's /markets endpoint accepts min_close_ts and max_close_ts parameters
    # to filter by close time
    params = {
        "series_ticker": series_ticker,
        "min_close_ts": int(start_dt.timestamp()),
        "max_close_ts": int(end_dt.timestamp()),
        "limit": 200,  # Max per page
    }

    all_markets = []
    cursor = None

    log.info(
        "Fetching historical markets for series %s from %s to %s",
        series_ticker, start_date, end_date
    )

    while True:
        if cursor:
            params["cursor"] = cursor

        try:
            response = client.fetch_markets(params)
        except Exception as exc:
            log.error("Failed to fetch markets: %s", exc)
            raise

        markets = response.get("markets", [])
        if not markets:
            break

        all_markets.extend(markets)
        log.info("Fetched %d markets (total: %d)", len(markets), len(all_markets))

        # Check if there are more pages
        cursor = response.get("cursor")
        if not cursor:
            break

    log.info("Total markets fetched: %d", len(all_markets))

    # Extract relevant fields for each market
    structured_markets = []
    for market in all_markets:
        structured_market = {
            "ticker": market.get("ticker"),
            "series_ticker": market.get("series_ticker"),
            "title": market.get("title", ""),
            "subtitle": market.get("subtitle", ""),
            "open_time": market.get("open_time", ""),
            "close_time": market.get("close_time", ""),
            "expected_expiration_time": market.get("expected_expiration_time", ""),
            "result": market.get("result", ""),
            "last_price": market.get("last_price"),
            "yes_price": market.get("yes_price"),
            "no_price": market.get("no_price"),
            "status": market.get("status", ""),
            "volume": market.get("volume"),
            "liquidity": market.get("liquidity"),
        }
        structured_markets.append(structured_market)

    return structured_markets


def main():
    """CLI entrypoint for historical data fetching."""
    parser = argparse.ArgumentParser(
        description="Fetch historical Kalshi market data for a series"
    )
    parser.add_argument(
        "--series",
        type=str,
        required=True,
        help="Series ticker (e.g., KXBTC15M or BTCZ)"
    )
    parser.add_argument(
        "--start",
        type=str,
        required=True,
        help="Start date in YYYY-MM-DD format"
    )
    parser.add_argument(
        "--end",
        type=str,
        required=True,
        help="End date in YYYY-MM-DD format"
    )
    parser.add_argument(
        "--output",
        type=str,
        help="Output file path (if not specified, prints to stdout)"
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    try:
        # Validate config
        config.validate()

        # Fetch historical data
        markets = fetch_historical_markets(
            series_ticker=args.series,
            start_date=args.start,
            end_date=args.end
        )

        # Output results
        output_data = {
            "series_ticker": args.series,
            "start_date": args.start,
            "end_date": args.end,
            "market_count": len(markets),
            "markets": markets
        }

        json_output = json.dumps(output_data, indent=2)

        if args.output:
            with open(args.output, "w") as f:
                f.write(json_output)
            log.info("Historical data written to %s", args.output)
        else:
            print(json_output)

    except Exception as exc:
        log.error("Error: %s", exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
