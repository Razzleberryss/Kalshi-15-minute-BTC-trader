"""
tests/test_enhancements.py – Tests for hardening improvements.

Covers:
  - KalshiClient._request retry logic (timeout, connection error, 5xx, 4xx)
  - Daily limit boundary edge cases (at-limit and just-past-limit)
  - Strategy helpers with empty / invalid orderbook data
  - manage_positions with missing market fields
  - config.EXPIRY_EXIT_SECONDS honoured by manage_positions
"""
import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, call
from pathlib import Path

import requests

os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    str(Path(tempfile.gettempdir()) / f"openclaw_stop_file_tests_{os.getpid()}"),
)
os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("KALSHI_TRADING_LIVE", "1")

import config
from bot import manage_positions
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import Signal, get_orderbook_skew, suggest_limit_price


_TICKER = "BTCZ-TEST"


def _future_close_time(seconds: int = 3600) -> str:
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=seconds)
    ).isoformat()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_mock_client():
    client = MagicMock(spec=KalshiClient)
    client.close_position.return_value = None
    client.contracts_held_on_side.return_value = 999
    return client


def _make_risk_with_position(ticker=_TICKER, side="yes", qty=2, entry_price=55):
    risk = RiskManager()
    risk.record_open_position(ticker, side, qty, entry_price)
    return risk


def _make_market(ticker=_TICKER, yes_bid=55, no_bid=45, close_time=None):
    return {
        "ticker": ticker,
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "close_time": close_time or _future_close_time(),
    }


# ── KalshiClient retry logic ───────────────────────────────────────────────────

class TestKalshiClientRetries(unittest.TestCase):
    """Verify that _request retries on transient errors but not on 4xx."""

    def _make_client(self):
        client = object.__new__(KalshiClient)
        client.base_url = config.BASE_URL
        client.api_key_id = "test-key"
        client.session = MagicMock()
        client._private_key = MagicMock()
        client._auth_headers = MagicMock(return_value={})
        return client

    def _ok_response(self, payload=None):
        resp = MagicMock()
        resp.ok = True
        resp.json.return_value = payload or {}
        resp.raise_for_status.return_value = None
        return resp

    def _error_response(self, status_code: int):
        resp = MagicMock()
        resp.ok = False
        resp.status_code = status_code
        resp.text = "error"
        http_err = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = http_err
        return resp

    def test_succeeds_on_first_attempt(self):
        client = self._make_client()
        client.session.request.return_value = self._ok_response({"key": "value"})

        result = client._request("GET", "/test")

        self.assertEqual(result, {"key": "value"})
        self.assertEqual(client.session.request.call_count, 1)

    @patch("time.sleep", return_value=None)
    @patch("kalshi_client.random.uniform", return_value=1.0)
    def test_retries_on_timeout(self, mock_uniform, mock_sleep):
        client = self._make_client()
        orig_retries = config.REQUEST_MAX_RETRIES
        config.REQUEST_MAX_RETRIES = 3
        try:
            ok = self._ok_response({"ok": True})
            client.session.request.side_effect = [
                requests.exceptions.Timeout("timed out"),
                ok,
            ]

            result = client._request("GET", "/test")

            self.assertEqual(result, {"ok": True})
            self.assertEqual(client.session.request.call_count, 2)
            # random.uniform is called with (0.1, max(0.2, 2**0)) = (0.1, 1.0)
            mock_uniform.assert_called_once_with(0.1, 1.0)
            mock_sleep.assert_called_once_with(1.0)
        finally:
            config.REQUEST_MAX_RETRIES = orig_retries

    @patch("time.sleep", return_value=None)
    @patch("kalshi_client.random.uniform", return_value=0.5)
    def test_retries_on_connection_error(self, mock_uniform, mock_sleep):
        client = self._make_client()
        orig_retries = config.REQUEST_MAX_RETRIES
        config.REQUEST_MAX_RETRIES = 3
        try:
            ok = self._ok_response({"ok": True})
            client.session.request.side_effect = [
                requests.exceptions.ConnectionError("refused"),
                ok,
            ]

            result = client._request("GET", "/test")

            self.assertEqual(result, {"ok": True})
            self.assertEqual(client.session.request.call_count, 2)
        finally:
            config.REQUEST_MAX_RETRIES = orig_retries

    @patch("time.sleep", return_value=None)
    @patch("kalshi_client.random.uniform", return_value=0.5)
    def test_raises_after_max_retries_exceeded(self, mock_uniform, mock_sleep):
        client = self._make_client()
        orig_retries = config.REQUEST_MAX_RETRIES
        config.REQUEST_MAX_RETRIES = 3
        try:
            client.session.request.side_effect = requests.exceptions.Timeout("always")

            with self.assertRaises(requests.exceptions.Timeout):
                client._request("GET", "/test")

            # 1 initial attempt + 3 retries = 4 total attempts
            self.assertEqual(client.session.request.call_count, 4)
        finally:
            config.REQUEST_MAX_RETRIES = orig_retries

    def test_does_not_retry_on_4xx(self):
        client = self._make_client()
        orig_retries = config.REQUEST_MAX_RETRIES
        config.REQUEST_MAX_RETRIES = 3
        try:
            client.session.request.return_value = self._error_response(400)

            with self.assertRaises(requests.exceptions.HTTPError):
                client._request("GET", "/test")

            # 4xx must not retry — exactly 1 attempt expected
            self.assertEqual(client.session.request.call_count, 1)
        finally:
            config.REQUEST_MAX_RETRIES = orig_retries

    @patch("time.sleep", return_value=None)
    @patch("kalshi_client.random.uniform", return_value=0.5)
    def test_retries_on_5xx_server_error(self, mock_uniform, mock_sleep):
        client = self._make_client()
        orig_retries = config.REQUEST_MAX_RETRIES
        config.REQUEST_MAX_RETRIES = 3
        try:
            ok = self._ok_response({"ok": True})
            bad = self._error_response(503)
            client.session.request.side_effect = [bad, ok]

            result = client._request("GET", "/test")

            self.assertEqual(result, {"ok": True})
            self.assertEqual(client.session.request.call_count, 2)
        finally:
            config.REQUEST_MAX_RETRIES = orig_retries

    def test_uses_config_timeout(self):
        """_request must pass REQUEST_TIMEOUT_SECONDS to session.request."""
        client = self._make_client()
        orig_timeout = config.REQUEST_TIMEOUT_SECONDS
        config.REQUEST_TIMEOUT_SECONDS = 7
        try:
            client.session.request.return_value = self._ok_response()

            client._request("GET", "/test")

            _, kwargs = client.session.request.call_args
            self.assertEqual(kwargs.get("timeout"), 7)
        finally:
            config.REQUEST_TIMEOUT_SECONDS = orig_timeout

    def test_url_uses_demo_host_when_env_is_demo(self):
        """URL must target demo-api.kalshi.co when BASE_URL is the demo endpoint."""
        client = self._make_client()
        orig_base_url = config.BASE_URL
        config.BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
        try:
            client.session.request.return_value = self._ok_response()

            client._request("GET", "/test")

            pos_args, _ = client.session.request.call_args
            url = pos_args[1]
            self.assertIn("demo-api.kalshi.co", url)
        finally:
            config.BASE_URL = orig_base_url

    def test_url_uses_prod_host_when_env_is_prod(self):
        """URL must target trading-api.kalshi.com when BASE_URL is the prod endpoint."""
        client = self._make_client()
        orig_base_url = config.BASE_URL
        config.BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
        try:
            client.session.request.return_value = self._ok_response()

            client._request("GET", "/test")

            pos_args, _ = client.session.request.call_args
            url = pos_args[1]
            self.assertIn("trading-api.kalshi.com", url)
        finally:
            config.BASE_URL = orig_base_url


# ── Daily limit boundary tests ─────────────────────────────────────────────────

class TestDailyLimitBoundaries(unittest.TestCase):

    def setUp(self):
        self._orig_trade_log = config.TRADE_LOG_FILE
        self._orig_max_daily_trades = config.MAX_DAILY_TRADES
        self._orig_max_daily_loss = config.MAX_DAILY_LOSS_CENTS
        self.trade_log = tempfile.NamedTemporaryFile(delete=False)
        self.trade_log.close()
        os.unlink(self.trade_log.name)
        config.TRADE_LOG_FILE = self.trade_log.name

    def tearDown(self):
        config.TRADE_LOG_FILE = self._orig_trade_log
        config.MAX_DAILY_TRADES = self._orig_max_daily_trades
        config.MAX_DAILY_LOSS_CENTS = self._orig_max_daily_loss
        if os.path.exists(self.trade_log.name):
            os.remove(self.trade_log.name)

    def _signal(self, ticker="BTCZ-OTHER"):
        return Signal(side="yes", confidence=0.9, price_cents=50, reason="test")

    def test_trade_allowed_one_below_daily_limit(self):
        config.MAX_DAILY_TRADES = 3
        risk = RiskManager()
        for i in range(2):
            risk.log_entry_trade(f"BTCZ-T{i}", "yes", 1, 50)
        approved, _ = risk.approve_trade(self._signal(), balance=100, positions=[], market_ticker="BTCZ-NEW")
        self.assertTrue(approved)

    def test_trade_blocked_at_daily_limit(self):
        config.MAX_DAILY_TRADES = 2
        risk = RiskManager()
        for i in range(2):
            risk.log_entry_trade(f"BTCZ-T{i}", "yes", 1, 50)
        approved, reason = risk.approve_trade(self._signal(), balance=100, positions=[], market_ticker="BTCZ-NEW")
        self.assertFalse(approved)
        self.assertIn("MAX_DAILY_TRADES", reason)

    def test_loss_allowed_one_cent_below_daily_limit(self):
        config.MAX_DAILY_LOSS_CENTS = 100
        risk = RiskManager()
        # entry=100, exit=1, qty=1 → pnl = (1-100)*1 = -99  (one cent below -100 limit)
        risk.log_exit_trade("BTCZ-T1", "yes", 1, 100, 1, "stop_loss")
        approved, _ = risk.approve_trade(self._signal(), balance=100, positions=[], market_ticker="BTCZ-NEW")
        self.assertTrue(approved)

    def test_loss_blocked_at_daily_limit(self):
        config.MAX_DAILY_LOSS_CENTS = 100
        risk = RiskManager()
        # entry=100, exit=1, qty=2 → pnl = (1-100)*2 = -198 which exceeds -100 limit
        risk.log_exit_trade("BTCZ-T1", "yes", 2, 100, 1, "stop_loss")
        approved, reason = risk.approve_trade(self._signal(), balance=100, positions=[], market_ticker="BTCZ-NEW")
        self.assertFalse(approved)
        self.assertIn("MAX_DAILY_LOSS_CENTS", reason)

    def test_daily_stats_reset_on_new_utc_day(self):
        """Daily trade count and PnL should reset when the UTC day rolls over."""
        config.MAX_DAILY_TRADES = 1
        risk = RiskManager()
        risk.log_entry_trade("BTCZ-T1", "yes", 1, 50)
        # Simulate a new day by backdating _today using the same UTC clock that
        # RiskManager itself uses (datetime.now(timezone.utc).date()), so this
        # test stays correct on systems where local time differs from UTC.
        risk._today = datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=1)
        # After reset, trade count should be 0 so a new trade is allowed
        approved, _ = risk.approve_trade(self._signal(), balance=100, positions=[], market_ticker="BTCZ-NEW")
        self.assertTrue(approved)


# ── Strategy helper tests ─────────────────────────────────────────────────────

class TestStrategyHelpers(unittest.TestCase):

    def test_orderbook_skew_empty_orderbook(self):
        """Skew should be 0.0 when the orderbook is empty."""
        skew = get_orderbook_skew({})
        self.assertEqual(skew, 0.0)

    def test_orderbook_skew_balanced(self):
        """Equal YES and NO liquidity should yield skew ≈ 0."""
        ob = {"orderbook": {"yes": [[50, 10]], "no": [[50, 10]]}}
        skew = get_orderbook_skew(ob)
        self.assertAlmostEqual(skew, 0.0)

    def test_orderbook_skew_all_yes(self):
        """All liquidity on YES side should yield skew = +1."""
        ob = {"orderbook": {"yes": [[50, 10]], "no": []}}
        skew = get_orderbook_skew(ob)
        self.assertAlmostEqual(skew, 1.0)

    def test_orderbook_skew_all_no(self):
        """All liquidity on NO side should yield skew = -1."""
        ob = {"orderbook": {"yes": [], "no": [[50, 10]]}}
        skew = get_orderbook_skew(ob)
        self.assertAlmostEqual(skew, -1.0)

    def test_orderbook_skew_missing_keys(self):
        """Missing yes/no keys in orderbook dict should not raise."""
        skew = get_orderbook_skew({"orderbook": {}})
        self.assertEqual(skew, 0.0)

    def test_suggest_limit_price_yes_clamped_to_range(self):
        """Suggested price must always stay within configured range."""
        market = {"yes_bid": 1, "yes_ask": 2, "no_bid": 97, "no_ask": 99}
        price = suggest_limit_price(market, "yes")
        self.assertGreaterEqual(price, config.MIN_CONTRACT_PRICE_CENTS)
        self.assertLessEqual(price, config.MAX_CONTRACT_PRICE_CENTS)

    def test_suggest_limit_price_no_clamped_to_range(self):
        market = {"yes_bid": 1, "yes_ask": 2, "no_bid": 97, "no_ask": 99}
        price = suggest_limit_price(market, "no")
        self.assertGreaterEqual(price, config.MIN_CONTRACT_PRICE_CENTS)
        self.assertLessEqual(price, config.MAX_CONTRACT_PRICE_CENTS)

    def test_suggest_limit_price_uses_ask_as_ceiling(self):
        """Price should not exceed the ask."""
        market = {"yes_bid": 50, "yes_ask": 55}
        price = suggest_limit_price(market, "yes")
        self.assertLessEqual(price, 55)


# ── manage_positions with missing market fields ───────────────────────────────

class TestManagePositionsMissingData(unittest.TestCase):

    def setUp(self):
        import bot as _bot
        _bot._halt_trading = False
        try:
            Path(os.environ.get("OPENCLAW_STOP_FILE", "")).unlink(missing_ok=True)
        except Exception:
            pass
        self._orig_trade_log = config.TRADE_LOG_FILE
        self._orig_stop_loss = config.STOP_LOSS_CENTS
        self._orig_take_profit = config.TAKE_PROFIT_CENTS
        self._orig_signal_reversal = config.SIGNAL_REVERSAL_EXIT
        self.trade_log = tempfile.NamedTemporaryFile(delete=False)
        self.trade_log.close()
        os.unlink(self.trade_log.name)
        config.TRADE_LOG_FILE = self.trade_log.name
        config.STOP_LOSS_CENTS = 20
        config.TAKE_PROFIT_CENTS = 30
        config.SIGNAL_REVERSAL_EXIT = True

    def tearDown(self):
        config.TRADE_LOG_FILE = self._orig_trade_log
        config.STOP_LOSS_CENTS = self._orig_stop_loss
        config.TAKE_PROFIT_CENTS = self._orig_take_profit
        config.SIGNAL_REVERSAL_EXIT = self._orig_signal_reversal
        if os.path.exists(self.trade_log.name):
            os.remove(self.trade_log.name)

    def test_malformed_close_time_does_not_raise(self):
        """A non-parseable close_time must not propagate an exception."""
        client = _make_mock_client()
        risk = _make_risk_with_position(entry_price=55)
        market = {
            "ticker": _TICKER,
            "yes_bid": 55,
            "no_bid": 45,
            "close_time": "not-a-date",
        }
        # Should not raise; malformed close_time is silently skipped
        results = list(manage_positions(client, market, risk))
        # No exit triggered by the bad date (stop/take-profit thresholds not hit either)
        self.assertEqual(len(results), 0)

    def test_missing_bid_falls_back_to_entry_price(self):
        """If the bid key is absent from market, entry_price is used as current_price."""
        client = _make_mock_client()
        risk = _make_risk_with_position(side="yes", entry_price=55)
        # market has no yes_bid key
        market = {"ticker": _TICKER, "no_bid": 45, "close_time": _future_close_time()}
        # With current_price == entry_price (55), stop-loss at 20 not triggered
        results = list(manage_positions(client, market, risk))
        self.assertEqual(len(results), 0)

    def test_expiry_exit_uses_config_threshold(self):
        """EXPIRY_EXIT_SECONDS in config must control the expiry exit window."""
        orig_expiry = config.EXPIRY_EXIT_SECONDS
        config.EXPIRY_EXIT_SECONDS = 300  # 5 minutes
        try:
            # 4 minutes until close — would NOT trigger with the default 120s but SHOULD
            # trigger with 300s
            close_time = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=240)
            ).isoformat()
            client = _make_mock_client()
            risk = _make_risk_with_position(entry_price=55)
            market = _make_market(yes_bid=55, close_time=close_time)
            results = list(manage_positions(client, market, risk))
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["exit_reason"], "expiry")
        finally:
            config.EXPIRY_EXIT_SECONDS = orig_expiry

    def test_expiry_exit_not_triggered_outside_threshold(self):
        """With a small EXPIRY_EXIT_SECONDS, a close_time far away should not exit."""
        orig_expiry = config.EXPIRY_EXIT_SECONDS
        config.EXPIRY_EXIT_SECONDS = 60  # 1 minute
        try:
            # 5 minutes until close — beyond the 60s window
            close_time = (
                datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(seconds=300)
            ).isoformat()
            client = _make_mock_client()
            risk = _make_risk_with_position(entry_price=55)
            market = _make_market(yes_bid=55, close_time=close_time)
            results = list(manage_positions(client, market, risk))
            self.assertEqual(len(results), 0)
        finally:
            config.EXPIRY_EXIT_SECONDS = orig_expiry


# ── Config validation ──────────────────────────────────────────────────────────

class TestConfigValidation(unittest.TestCase):

    def _validate_with(self, overrides: dict):
        """
        Run config.validate() with temporary attribute overrides.
        Returns the error string if validation fails, or None if it passes.
        In the test environment there are always credential errors (no .env);
        callers should check whether a *specific* field name appears in the error
        rather than expecting a clean pass.
        """
        originals = {k: getattr(config, k) for k in overrides}
        for k, v in overrides.items():
            setattr(config, k, v)
        try:
            config.validate()
        except EnvironmentError as e:
            return str(e)
        finally:
            for k, v in originals.items():
                setattr(config, k, v)
        return None  # no error

    def test_negative_request_timeout_fails_validation(self):
        err = self._validate_with({"REQUEST_TIMEOUT_SECONDS": 0})
        self.assertIsNotNone(err)
        self.assertIn("REQUEST_TIMEOUT_SECONDS", err)

    def test_negative_request_max_retries_fails_validation(self):
        err = self._validate_with({"REQUEST_MAX_RETRIES": -1})
        self.assertIsNotNone(err)
        self.assertIn("REQUEST_MAX_RETRIES", err)

    def test_negative_expiry_exit_seconds_fails_validation(self):
        err = self._validate_with({"EXPIRY_EXIT_SECONDS": -1})
        self.assertIsNotNone(err)
        self.assertIn("EXPIRY_EXIT_SECONDS", err)

    def test_zero_retries_is_valid(self):
        """REQUEST_MAX_RETRIES=0 should be allowed (disables retries); must not
        appear as a validation error even if other fields (credentials) cause errors."""
        err = self._validate_with({"REQUEST_MAX_RETRIES": 0})
        # If there is an error it must be caused by something other than this field
        if err:
            self.assertNotIn("REQUEST_MAX_RETRIES", err)

    def test_zero_expiry_seconds_is_valid(self):
        """EXPIRY_EXIT_SECONDS=0 disables the expiry exit; must not appear as a
        validation error even if other fields (credentials) cause errors."""
        err = self._validate_with({"EXPIRY_EXIT_SECONDS": 0})
        if err:
            self.assertNotIn("EXPIRY_EXIT_SECONDS", err)


if __name__ == "__main__":
    unittest.main()
