"""
tests/test_exit_logic.py – Unit tests for the bot's exit/position-management logic.

Covers:
  - stop_loss exit condition
  - take_profit exit condition
  - reversal exit condition
  - expiry exit condition (contract close_time ≤120 seconds away)
  - baseline: no exit when conditions are not met
  - baseline: no exit when the bot has no tracked position for the market
  - record_open_position / record_closed_position tracking
  - close_position() exists and delegates to sell_position()
  - approve_trade() respects bot-tracked positions (not just API positions)
"""
import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, call, patch
from pathlib import Path

os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    os.path.join(tempfile.gettempdir(), f"openclaw_stop_file_tests_{os.getpid()}"),
)
os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("KALSHI_TRADING_LIVE", "1")

import config
from bot import manage_positions
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import Signal

_TICKER = "BTCZ-TEST"


def _future_close_time(seconds: int = 3600) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=seconds)
    ).isoformat()


def _past_close_time(seconds: int = 60) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=seconds)
    ).isoformat()


class TestExitLogic(unittest.TestCase):
    # ── Test fixtures ──────────────────────────────────────────────────────────

    def setUp(self):
        self._orig_dry_run = config.DRY_RUN
        self._orig_expiry = config.EXPIRY_EXIT_SECONDS
        self._orig_trade_log = config.TRADE_LOG_FILE
        self._orig_stop_loss = config.STOP_LOSS_CENTS
        self._orig_take_profit = config.TAKE_PROFIT_CENTS
        self._orig_signal_reversal = config.SIGNAL_REVERSAL_EXIT
        self._orig_min_edge = config.MIN_EDGE_THRESHOLD
        # Avoid cross-test leakage from bot globals (some tests intentionally
        # trigger HALT_TRADING paths).
        import bot as _bot
        _bot._halt_trading = False
        # Ensure STOP_TRADING flag from other tests doesn't block exits.
        try:
            Path(os.environ.get("OPENCLAW_STOP_FILE", "")).unlink(missing_ok=True)
        except Exception:
            pass

        self.trade_log = tempfile.NamedTemporaryFile(delete=False)
        self.trade_log.close()
        os.unlink(self.trade_log.name)
        config.TRADE_LOG_FILE = self.trade_log.name

        config.STOP_LOSS_CENTS = 20
        config.TAKE_PROFIT_CENTS = 30
        config.SIGNAL_REVERSAL_EXIT = True
        config.MIN_EDGE_THRESHOLD = 0.05
        config.DRY_RUN = True
        config.EXPIRY_EXIT_SECONDS = 120

    def tearDown(self):
        config.DRY_RUN = self._orig_dry_run
        config.EXPIRY_EXIT_SECONDS = self._orig_expiry
        config.TRADE_LOG_FILE = self._orig_trade_log
        config.STOP_LOSS_CENTS = self._orig_stop_loss
        config.TAKE_PROFIT_CENTS = self._orig_take_profit
        config.SIGNAL_REVERSAL_EXIT = self._orig_signal_reversal
        config.MIN_EDGE_THRESHOLD = self._orig_min_edge
        if os.path.exists(self.trade_log.name):
            os.remove(self.trade_log.name)

    # ── Helpers ────────────────────────────────────────────────────────────────

    def _make_client(self):
        client = MagicMock(spec=KalshiClient)
        client.close_position.return_value = None
        # manage_positions routes exits through the in-process sell envelope in DRY_RUN,
        # which consults contracts_held_on_side.
        client.contracts_held_on_side.return_value = 999
        return client

    def _make_risk(
        self,
        ticker=_TICKER,
        side="yes",
        qty=2,
        entry_price=55,
    ):
        risk = RiskManager()
        risk.record_open_position(ticker, side, qty, entry_price)
        return risk

    def _make_market(
        self,
        ticker=_TICKER,
        yes_bid=55,
        no_bid=45,
        close_time=None,
    ):
        return {
            "ticker": ticker,
            "yes_bid": yes_bid,
            "no_bid": no_bid,
            "close_time": close_time or _future_close_time(),
        }

    # ── Stop Loss ──────────────────────────────────────────────────────────────

    def test_stop_loss_triggers_when_price_drops_enough(self):
        # entry=55, bid=30, stop_loss=20 → 30 <= 55-20=35 → exit
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=30)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "stop_loss")

    def test_stop_loss_not_triggered_when_price_above_threshold(self):
        # entry=55, bid=40, stop_loss=20 → 40 > 35 → no exit
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=40)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    def test_stop_loss_disabled_when_zero(self):
        config.STOP_LOSS_CENTS = 0
        config.TAKE_PROFIT_CENTS = 0
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=1)  # extreme drop, stop-loss disabled

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    # ── Take Profit ────────────────────────────────────────────────────────────

    def test_take_profit_triggers_when_price_rises_enough(self):
        # entry=55, bid=90, take_profit=30 → 90 >= 55+30=85 → exit
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=90)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "take_profit")

    def test_take_profit_not_triggered_below_threshold(self):
        # entry=55, bid=80, take_profit=30 → 80 < 85 → no exit
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=80)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    def test_take_profit_disabled_when_zero(self):
        config.TAKE_PROFIT_CENTS = 0
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=99)  # huge gain, take-profit disabled

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    # ── Signal Reversal ────────────────────────────────────────────────────────

    def test_reversal_triggers_when_signal_opposes_position(self):
        # holding YES, strong NO signal → exit
        client = self._make_client()
        risk = self._make_risk(side="yes", entry_price=55)
        market = self._make_market(yes_bid=55)
        signal = Signal(side="no", confidence=0.9, price_cents=45, reason="test")

        results = list(manage_positions(client, market, risk, current_signal=signal))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "reversal")

    def test_reversal_not_triggered_for_same_side(self):
        # holding YES, YES signal → no exit
        client = self._make_client()
        risk = self._make_risk(side="yes", entry_price=55)
        market = self._make_market(yes_bid=55)
        signal = Signal(side="yes", confidence=0.9, price_cents=55, reason="test")

        results = list(manage_positions(client, market, risk, current_signal=signal))

        self.assertEqual(len(results), 0)

    def test_reversal_not_triggered_when_confidence_below_threshold(self):
        # opposite signal but very low confidence → no exit
        config.MIN_EDGE_THRESHOLD = 0.5
        client = self._make_client()
        risk = self._make_risk(side="yes", entry_price=55)
        market = self._make_market(yes_bid=55)
        signal = Signal(side="no", confidence=0.1, price_cents=45, reason="test")

        results = list(manage_positions(client, market, risk, current_signal=signal))

        self.assertEqual(len(results), 0)

    def test_reversal_disabled_when_config_false(self):
        config.SIGNAL_REVERSAL_EXIT = False
        client = self._make_client()
        risk = self._make_risk(side="yes", entry_price=55)
        market = self._make_market(yes_bid=55)
        signal = Signal(side="no", confidence=0.9, price_cents=45, reason="test")

        results = list(manage_positions(client, market, risk, current_signal=signal))

        self.assertEqual(len(results), 0)

    def test_reversal_triggers_with_size_zero_no_entry_signal(self):
        # A Signal with size=0 (entry blocked by fee filters) must still trigger
        # reversal so that SIGNAL_REVERSAL_EXIT remains effective for open positions.
        client = self._make_client()
        risk = self._make_risk(side="yes", entry_price=55)
        market = self._make_market(yes_bid=55)
        signal = Signal(
            side="no", confidence=0.9, price_cents=45,
            reason="NO_TRADE: composite=-0.500", size=0,
        )

        results = list(manage_positions(client, market, risk, current_signal=signal))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "reversal")

    # ── Expiry ─────────────────────────────────────────────────────────────────

    def test_expiry_triggers_when_close_time_within_2_minutes(self):
        # 90 seconds until close → exit
        close_time = _future_close_time(seconds=90)
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=55, close_time=close_time)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "expiry")

    def test_expiry_triggers_when_close_time_already_passed(self):
        # contract already closed
        close_time = _past_close_time(seconds=30)
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=55, close_time=close_time)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "expiry")

    def test_expiry_not_triggered_when_close_time_far_away(self):
        # 10 minutes until close → no exit
        close_time = _future_close_time(seconds=600)
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = self._make_market(yes_bid=55, close_time=close_time)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    def test_expiry_not_triggered_when_close_time_absent(self):
        # market dict has no close_time key → no expiry exit
        client = self._make_client()
        risk = self._make_risk(entry_price=55)
        market = {"ticker": _TICKER, "yes_bid": 55, "no_bid": 45}  # no close_time

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    # ── No exit when no bot position ───────────────────────────────────────────

    def test_no_exit_when_bot_has_no_tracked_position(self):
        # Price would normally trigger stop-loss, but bot never opened this position
        client = self._make_client()
        risk = RiskManager()  # empty — no positions recorded
        market = self._make_market(yes_bid=1)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 0)

    # ── Exit payload correctness ───────────────────────────────────────────────

    def test_exit_payload_contains_required_fields(self):
        client = self._make_client()
        risk = self._make_risk(side="yes", qty=3, entry_price=55)
        market = self._make_market(yes_bid=90)  # take profit

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertIn("market", r)
        self.assertIn("side", r)
        self.assertIn("size", r)
        self.assertIn("entry_price", r)
        self.assertIn("exit_price", r)
        self.assertIn("exit_reason", r)
        self.assertEqual(r["market"], _TICKER)
        self.assertEqual(r["side"], "yes")
        self.assertEqual(r["size"], 3)
        self.assertEqual(r["entry_price"], 55)
        self.assertEqual(r["exit_price"], 89)  # max(1, 90-1)

    def test_close_position_called_with_correct_args(self):
        client = self._make_client()
        risk = self._make_risk(side="yes", qty=2, entry_price=55)
        market = self._make_market(yes_bid=30)  # stop loss

        results = list(manage_positions(client, market, risk))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market"], _TICKER)
        self.assertEqual(results[0]["side"], "yes")
        self.assertEqual(results[0]["size"], 2)
        self.assertEqual(results[0]["exit_price"], 29)  # max(1, 30-1)

    # ── NO-side PnL is side-aware ──────────────────────────────────────────────

    def test_no_side_position_exit(self):
        # holding NO @ entry=60, bid=40, stop_loss=20 → 40 <= 60-20=40 → exit
        client = self._make_client()
        risk = self._make_risk(side="no", qty=1, entry_price=60)
        market = self._make_market(no_bid=40)

        results = list(manage_positions(client, market, risk))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["exit_reason"], "stop_loss")
        self.assertEqual(results[0]["side"], "no")

    # ── record_open_position / record_closed_position ─────────────────────────

    def test_record_open_and_closed_position(self):
        risk = RiskManager()
        self.assertNotIn(_TICKER, risk.get_open_positions())

        risk.record_open_position(_TICKER, "yes", 2, 55)
        self.assertIn(_TICKER, risk.get_open_positions())

        pos = risk.get_open_positions()[_TICKER]
        self.assertEqual(pos["side"], "yes")
        self.assertEqual(pos["quantity"], 2)
        self.assertEqual(pos["entry_price"], 55)

        risk.record_closed_position(_TICKER)
        self.assertNotIn(_TICKER, risk.get_open_positions())

    def test_record_closed_nonexistent_position_is_harmless(self):
        risk = RiskManager()
        # should not raise
        risk.record_closed_position("BTCZ-NONEXISTENT")

    def test_get_open_positions_returns_snapshot(self):
        risk = RiskManager()
        risk.record_open_position(_TICKER, "yes", 1, 50)
        snapshot = risk.get_open_positions()
        # Mutating the snapshot should not affect the manager's state
        snapshot[_TICKER]["quantity"] = 999
        self.assertEqual(risk.get_open_positions()[_TICKER]["quantity"], 1)

    # ── approve_trade uses bot-tracked positions ───────────────────────────────

    def test_approve_trade_blocks_when_bot_has_open_position(self):
        risk = self._make_risk(ticker=_TICKER)
        signal = Signal(side="yes", confidence=0.9, price_cents=50, reason="test")
        # API positions list is empty (another bot hasn't opened anything)
        approved, reason = risk.approve_trade(
            signal, balance=100, positions=[], market_ticker=_TICKER
        )
        self.assertFalse(approved)
        self.assertIn(_TICKER, reason)

    def test_approve_trade_allows_trade_when_no_bot_position(self):
        risk = RiskManager()  # no positions recorded by this bot
        # Even if API returns an existing position for this ticker
        api_positions = [{"ticker": _TICKER, "position": 1, "average_price": 50}]
        signal = Signal(side="yes", confidence=0.9, price_cents=50, reason="test")
        approved, reason = risk.approve_trade(
            signal, balance=100, positions=api_positions, market_ticker=_TICKER
        )
        # Bot hasn't opened this position itself, so "Already have" should NOT block
        self.assertNotEqual(reason, f"Already have a position in {_TICKER}")

    def test_approve_trade_blocks_when_both_api_and_bot_have_position(self):
        # API returns a position AND bot has recorded its own open position —
        # the bot-tracking check takes precedence and blocks the new trade.
        risk = self._make_risk(ticker=_TICKER)
        api_positions = [{"ticker": _TICKER, "position": 1, "average_price": 50}]
        signal = Signal(side="yes", confidence=0.9, price_cents=50, reason="test")
        approved, reason = risk.approve_trade(
            signal, balance=100, positions=api_positions, market_ticker=_TICKER
        )
        self.assertFalse(approved)
        self.assertIn(_TICKER, reason)



    def test_close_position_exists_on_client(self):
        client = object.__new__(KalshiClient)
        self.assertTrue(callable(getattr(client, "close_position", None)))

    def test_close_position_dry_run_does_not_call_request(self):
        client = object.__new__(KalshiClient)
        client._ensure_btc_market = MagicMock()
        client._request = MagicMock()
        # In dry_run mode sell_position logs and returns None without calling _request
        result = client.close_position(
            market_id="BTCZ-FAKE",
            side="yes",
            quantity=1,
            price=50,
            dry_run=True,
        )
        self.assertIsNone(result)
        client._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
