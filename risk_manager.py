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
        self._ensure_log_file()
        self._load_daily_stats_from_log()

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
        # 1. Already have a position in this market?
        self._reset_daily_if_needed()
        existing = [p for p in positions if p.get("ticker") == market_ticker]
        if existing:
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
        self._reset_daily_if_needed()
        self._daily_trade_count += 1
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        self._reset_daily_if_needed()
        self._daily_realized_pnl_cents += pnl_cents
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        with open(config.TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self._trade_log_headers())
            writer.writerow(row)
        log.info("Trade logged: %s", row)

    def _reset_daily_if_needed(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self._today:
            self._today = today
            self._daily_trade_count = 0
            self._daily_realized_pnl_cents = 0

    def _load_daily_stats_from_log(self) -> None:
        if not os.path.exists(config.TRADE_LOG_FILE):
            return
        try:
            today_iso = self._today.isoformat()
            with open(config.TRADE_LOG_FILE, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
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
