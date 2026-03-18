"""
risk_manager.py  -  Position sizing and trade gating.

Responsibilities:
  - Check max open positions
  - Check total exposure vs MAX_TOTAL_EXPOSURE
  - Check available balance vs MAX_TRADE_DOLLARS
  - Calculate contract count for a given dollar risk
  - Log every trade decision to CSV
  - Detect if we already have a position in this market (avoid doubling)
"""

import csv
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import config
from strategy import Signal

log = logging.getLogger(__name__)


class RiskManager:
    """Stateful risk guard. One instance lives for the duration of the bot run."""

    def __init__(self):
        self._today = datetime.now(timezone.utc).date()
        self._daily_trade_count = 0
        self._daily_realized_pnl_cents = 0
        # Tracks only positions opened by this bot instance (keyed by ticker)
        self._open_positions: dict[str, dict] = {}
        # Cache for current datetime to reduce redundant calls
        self._cached_now: Optional[datetime] = None
        # Trade log buffer for batch writes (reduces file I/O overhead)
        self._trade_log_buffer: list[dict] = []
        self._ensure_log_file()
        self._load_daily_stats_from_log()

    def _get_current_datetime(self) -> datetime:
        """Get current datetime, cached within a single operation."""
        if self._cached_now is None:
            self._cached_now = datetime.now(timezone.utc)
        return self._cached_now

    def _clear_datetime_cache(self):
        """Clear the datetime cache and flush trade log buffer. Call at the end of each bot cycle."""
        self._cached_now = None
        self._flush_trade_log_buffer()

    # ── Trade approval ───────────────────────────────────────────────────────────

    def approve_trade(
        self,
        signal: Signal,
        balance: float,
        positions: list,
        market_ticker: str,
    ) -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        Gates the trade against all risk limits.
        """
        # 1. Reset daily stats once per cycle (not on every call)
        self._reset_daily_if_needed()

        # 2. Already have a position in this market (opened by this bot)?
        if market_ticker in self._open_positions:
            return False, f"Already have a position in {market_ticker}"

        if self._daily_trade_count >= config.MAX_DAILY_TRADES:
            return False, (
                f"MAX_DAILY_TRADES reached ({self._daily_trade_count}/{config.MAX_DAILY_TRADES})"
            )
        if self._daily_realized_pnl_cents <= -config.MAX_DAILY_LOSS_CENTS:
            return False, (
                f"MAX_DAILY_LOSS_CENTS reached ({self._daily_realized_pnl_cents} <= -{config.MAX_DAILY_LOSS_CENTS})"
            )

        # 2. Max open positions
        open_count = len(positions)
        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, f"At max open positions ({open_count}/{config.MAX_OPEN_POSITIONS})"

        # 3. Available balance check
        if balance < config.MAX_TRADE_DOLLARS:
            return False, f"Insufficient balance ${balance:.2f} < ${config.MAX_TRADE_DOLLARS}"

        # 4. Total exposure check
        # Estimate current deployed capital: count all positions * avg price
        deployed = self._estimate_deployed(positions)
        if deployed + config.MAX_TRADE_DOLLARS > config.MAX_TOTAL_EXPOSURE:
            return False, (
                f"Would exceed MAX_TOTAL_EXPOSURE: "
                f"${deployed:.2f} + ${config.MAX_TRADE_DOLLARS:.2f} > ${config.MAX_TOTAL_EXPOSURE:.2f}"
            )

        # 5. Price sanity
        p = signal.price_cents
        if not (config.MIN_CONTRACT_PRICE_CENTS <= p <= config.MAX_CONTRACT_PRICE_CENTS):
            return False, f"Price {p}c outside allowed range [{config.MIN_CONTRACT_PRICE_CENTS},{config.MAX_CONTRACT_PRICE_CENTS}]"

        return True, "All risk checks passed"

    # ── Position sizing ───────────────────────────────────────────────────────────

    def calculate_contracts(
        self,
        price_cents: int,
        max_dollars: Optional[float] = None,
    ) -> int:
        """
        Calculate how many contracts to buy.

        Each contract costs `price_cents` cents.
        We never risk more than MAX_TRADE_DOLLARS per trade.
        Returns at least 1 or 0 if the price exceeds the budget.
        """
        budget_cents = (max_dollars or config.MAX_TRADE_DOLLARS) * 100
        if price_cents <= 0:
            return 0
        contracts = int(budget_cents // price_cents)
        return max(0, contracts)

    # ── Trade logging ─────────────────────────────────────────────────────────────

    def log_entry_trade(
        self,
        market: str,
        side: str,
        size: int,
        entry_price: int,
    ) -> None:
        # Daily count increment (reset already done in approve_trade)
        self._daily_trade_count += 1
        row = {
            "timestamp": self._get_current_datetime().isoformat(),
            "market": market,
            "side": side.upper(),
            "size": size,
            "entry_price": entry_price,
            "exit_price": "",
            "pnl": "",
            "exit_reason": "entry",
        }
        self._append_trade_row(row)

    def log_exit_trade(
        self,
        market: str,
        side: str,
        size: int,
        entry_price: int,
        exit_price: int,
        exit_reason: str,
    ) -> int:
        pnl_cents = (exit_price - entry_price) * size if side.lower() == "yes" else (entry_price - exit_price) * size
        # PnL update (reset not needed here, done once per cycle)
        self._daily_realized_pnl_cents += pnl_cents
        row = {
            "timestamp": self._get_current_datetime().isoformat(),
            "market": market,
            "side": side.upper(),
            "size": size,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "pnl": pnl_cents,
            "exit_reason": exit_reason,
        }
        self._append_trade_row(row)
        return pnl_cents

    def _append_trade_row(self, row: dict) -> None:
        """Buffer trade row for later flush instead of immediate write."""
        self._trade_log_buffer.append(row)
        log.info("Trade logged (buffered): %s", row)

    def _flush_trade_log_buffer(self) -> None:
        """Flush buffered trade rows to disk in a single write operation."""
        if not self._trade_log_buffer:
            return
        try:
            with open(config.TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self._trade_log_headers())
                writer.writerows(self._trade_log_buffer)
            log.debug("Flushed %d trade log entries to disk", len(self._trade_log_buffer))
            self._trade_log_buffer.clear()
        except Exception as exc:
            log.error("Failed to flush trade log buffer: %s", exc)

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._today:
            self._today = today
            self._daily_trade_count = 0
            self._daily_realized_pnl_cents = 0

    def _load_daily_stats_from_log(self) -> None:
        """Load today's trade stats from CSV log. Optimized to read only recent entries."""
        if not os.path.exists(config.TRADE_LOG_FILE):
            return
        try:
            today_iso = self._today.isoformat()

            # For small files, read all. For large files (>1000 lines), read only last 500 lines
            # since today's trades are likely at the end
            with open(config.TRADE_LOG_FILE, "rb") as f:
                # Get file size
                f.seek(0, 2)  # Seek to end
                file_size = f.tell()

                # If file is small (<50KB), read all lines normally
                if file_size < 50000:
                    f.seek(0)
                    lines = f.read().decode("utf-8").splitlines()
                else:
                    # For large files, read last ~500 lines (approximate)
                    # Average line length ~100 bytes, so read last 50KB
                    f.seek(max(0, file_size - 50000))
                    partial = f.read().decode("utf-8")
                    # Skip first incomplete line
                    lines = partial.split("\n", 1)[-1].splitlines()

            # Parse CSV from lines
            if not lines:
                return

            import io
            csv_content = "\n".join(lines)
            reader = csv.DictReader(io.StringIO(csv_content))

            for row in reader:
                ts = row.get("timestamp", "")
                if not ts.startswith(today_iso):
                    continue
                if row.get("exit_reason") == "entry":
                    self._daily_trade_count += 1
                pnl_str = row.get("pnl", "")
                if pnl_str not in ("", None):
                    self._daily_realized_pnl_cents += int(float(pnl_str))
        except Exception as exc:
            log.warning("Unable to load daily stats from trade log: %s", exc)

    # ── Bot position tracking ─────────────────────────────────────────────────

    def record_open_position(
        self,
        ticker: str,
        side: str,
        quantity: int,
        entry_price: int,
    ) -> None:
        """Record a position opened by this bot so we can manage it later."""
        self._open_positions[ticker] = {
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
        }
        log.debug(
            "Tracking new position: %s %s x%d @ %dc",
            ticker, side, quantity, entry_price,
        )

    def record_closed_position(self, ticker: str) -> None:
        """Remove a position from tracking after it has been closed."""
        self._open_positions.pop(ticker, None)
        log.debug("Removed closed position from tracking: %s", ticker)

    def get_open_positions(self) -> dict[str, dict]:
        """Return a deep snapshot of positions opened by this bot instance."""
        return {ticker: dict(pos) for ticker, pos in self._open_positions.items()}

    # ── Helpers ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _estimate_deployed(positions: list) -> float:
        """Rough estimate of total dollars currently at risk in open positions."""
        total_cents = 0
        for pos in positions:
            # Kalshi position: quantity * average cost
            qty = abs(pos.get("position", 0))
            avg_price = pos.get("average_price", 50)  # default 50c
            total_cents += qty * avg_price
        return total_cents / 100

    def _ensure_log_file(self):
        """Create the CSV with headers if it doesn't already exist."""
        if not os.path.exists(config.TRADE_LOG_FILE):
            with open(config.TRADE_LOG_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=self._trade_log_headers())
                writer.writeheader()
            log.info("Created trade log: %s", config.TRADE_LOG_FILE)

    @staticmethod
    def _trade_log_headers() -> list[str]:
        return [
            "timestamp",
            "market",
            "side",
            "size",
            "entry_price",
            "exit_price",
            "pnl",
            "exit_reason",
        ]
