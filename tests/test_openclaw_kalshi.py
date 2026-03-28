"""
tests/test_openclaw_kalshi.py – Unit tests for openclaw_kalshi.py

Tests cover:
  - _parse_iso_datetime()      (timezone handling)
  - find_active_market()       (market-selection logic)
  - _trim_orderbook()          (output truncation)
  - cmd_orderbook()            (integration of the above with a mocked client)
"""
import datetime
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Minimal stub so that "import config" inside openclaw_kalshi works even
# when .env / credentials are absent in the test environment.
# ---------------------------------------------------------------------------
_config_stub = types.ModuleType("config")
_config_stub.KALSHI_API_KEY_ID = "test-key"
_config_stub.KALSHI_PRIVATE_KEY_PATH = "/dev/null"
_config_stub.KALSHI_ENV = "demo"
_config_stub.BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"
_config_stub.KALSHI_BASE_URL = _config_stub.BASE_URL
_config_stub.BTC_SERIES_TICKER = "BTCZ"
_config_stub.REQUEST_TIMEOUT_SECONDS = 5
_config_stub.REQUEST_MAX_RETRIES = 0
_config_stub.DEPTH_BAND = 0.20
sys.modules.setdefault("config", _config_stub)

# Now it is safe to import the module under test.
import openclaw_kalshi  # noqa: E402 – must come after stub setup
from openclaw_kalshi import (  # noqa: E402
    _parse_iso_datetime,
    _trim_orderbook,
    cmd_orderbook,
    find_active_market,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

UTC = datetime.timezone.utc


def _utc(year, month, day, hour=0, minute=0) -> datetime.datetime:
    return datetime.datetime(year, month, day, hour, minute, tzinfo=UTC)


def _make_market(ticker, close_time_dt, open_time_dt=None, status="open", provisional=False):
    m = {
        "ticker": ticker,
        "status": status,
        "close_time": close_time_dt.isoformat(),
        "is_provisional": provisional,
    }
    if open_time_dt is not None:
        m["open_time"] = open_time_dt.isoformat()
    return m


def _mock_client(markets_by_status=None, orderbook=None):
    """
    Return a MagicMock that behaves like KalshiClient.

    ``markets_by_status``: dict mapping status -> list of market dicts.
      None means return an empty list for every status.
    ``orderbook``: dict returned by get_orderbook().
    """
    client = MagicMock()

    def _get_markets(series, status=None, limit=20):
        if markets_by_status is None:
            return []
        return markets_by_status.get(status, [])

    client.get_markets.side_effect = _get_markets
    client.get_orderbook.return_value = orderbook or {}
    return client


# ---------------------------------------------------------------------------
# _parse_iso_datetime
# ---------------------------------------------------------------------------

class TestParseIsoDatetime(unittest.TestCase):
    def test_z_suffix(self):
        dt = _parse_iso_datetime("2026-03-28T02:00:00Z")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo, UTC)
        self.assertEqual(dt.hour, 2)

    def test_plus_offset(self):
        dt = _parse_iso_datetime("2026-03-28T02:00:00+00:00")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.tzinfo.utcoffset(dt), datetime.timedelta(0))

    def test_empty_string(self):
        self.assertIsNone(_parse_iso_datetime(""))

    def test_none(self):
        self.assertIsNone(_parse_iso_datetime(None))

    def test_bad_value(self):
        self.assertIsNone(_parse_iso_datetime("not-a-date"))

    def test_naive_datetime_normalized_to_utc(self):
        """A naive datetime (no tz info) should be returned as UTC-aware."""
        dt = _parse_iso_datetime("2026-03-28T02:00:00")
        self.assertIsNotNone(dt)
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.utcoffset(), datetime.timedelta(0))


# ---------------------------------------------------------------------------
# find_active_market
# ---------------------------------------------------------------------------

class TestFindActiveMarket(unittest.TestCase):
    """Tests for the market-selection logic in find_active_market()."""

    def _now(self):
        return datetime.datetime(2026, 3, 28, 5, 30, tzinfo=UTC)

    def test_selects_spanning_market(self):
        """Should prefer a market whose window spans 'now'."""
        now = self._now()
        markets = [
            _make_market(
                "KXBTCD-28MAR2606",
                close_time_dt=now + datetime.timedelta(hours=1),
                open_time_dt=now - datetime.timedelta(minutes=30),
            ),
            _make_market(
                "KXBTCD-28MAR2607",
                close_time_dt=now + datetime.timedelta(hours=2),
                open_time_dt=now + datetime.timedelta(hours=1),
            ),
        ]
        client = _mock_client({"open": markets})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "KXBTCD-28MAR2606")
        self.assertIn("nearest active", reason)

    def test_prefers_soonest_close_when_multiple_spanning(self):
        """When multiple markets span now, pick the one with the soonest close."""
        now = self._now()
        m1 = _make_market(
            "KXBTCD-EARLY",
            close_time_dt=now + datetime.timedelta(minutes=30),
            open_time_dt=now - datetime.timedelta(hours=1),
        )
        m2 = _make_market(
            "KXBTCD-LATE",
            close_time_dt=now + datetime.timedelta(hours=2),
            open_time_dt=now - datetime.timedelta(hours=1),
        )
        client = _mock_client({"open": [m2, m1]})  # deliberately out of order

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, _ = find_active_market(client, "KXBTCD")

        self.assertEqual(result["ticker"], "KXBTCD-EARLY")

    def test_fallback_to_future_when_none_span_now(self):
        """If no market spans now, pick the one with the soonest future close."""
        now = self._now()
        # Both markets open in the future
        m1 = _make_market(
            "KXBTCD-NEXT",
            close_time_dt=now + datetime.timedelta(hours=1),
            open_time_dt=now + datetime.timedelta(minutes=10),
        )
        m2 = _make_market(
            "KXBTCD-AFTER",
            close_time_dt=now + datetime.timedelta(hours=2),
            open_time_dt=now + datetime.timedelta(hours=1),
        )
        client = _mock_client({"open": [m2, m1]})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "KXBTCD-NEXT")
        self.assertIn("upcoming", reason)

    def test_returns_none_when_no_markets(self):
        """Should return (None, error) when the API returns no markets at all."""
        client = _mock_client(markets_by_status=None)

        with patch("openclaw_kalshi.datetime") as mock_dt:
            now = self._now()
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNone(result)
        self.assertIn("No markets", reason)

    def test_provisional_markets_excluded(self):
        """Provisional markets should be filtered out."""
        now = self._now()
        prov = _make_market(
            "KXBTCD-PROV",
            close_time_dt=now + datetime.timedelta(minutes=30),
            open_time_dt=now - datetime.timedelta(minutes=30),
            provisional=True,
        )
        real = _make_market(
            "KXBTCD-REAL",
            close_time_dt=now + datetime.timedelta(hours=2),
            open_time_dt=now - datetime.timedelta(minutes=10),
            provisional=False,
        )
        client = _mock_client({"open": [prov, real]})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, _ = find_active_market(client, "KXBTCD")

        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "KXBTCD-REAL")

    def test_api_error_returns_none(self):
        """API exceptions should be caught and returned as (None, error_message)."""
        client = MagicMock()
        client.get_markets.side_effect = RuntimeError("network timeout")

        result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNone(result)
        self.assertIn("API error", reason)

    def test_open_empty_falls_back_to_no_status(self):
        """When status=open returns nothing, the no-status retry should be used."""
        now = self._now()
        fallback_market = _make_market(
            "KXBTCD-28MAR2606",
            close_time_dt=now + datetime.timedelta(hours=1),
            open_time_dt=now - datetime.timedelta(minutes=30),
        )
        # status=open → empty; no-status → has a market
        def _get_markets(series, status=None, limit=20):
            if status == "open":
                return []
            return [fallback_market]

        client = MagicMock()
        client.get_markets.side_effect = _get_markets

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "KXBTCD-28MAR2606")

    def test_all_provisional_returns_none(self):
        """When every returned market is provisional, find_active_market returns (None, reason)."""
        now = self._now()
        markets = [
            _make_market(
                "KXBTCD-PROV1",
                close_time_dt=now + datetime.timedelta(hours=1),
                open_time_dt=now - datetime.timedelta(minutes=30),
                provisional=True,
            ),
            _make_market(
                "KXBTCD-PROV2",
                close_time_dt=now + datetime.timedelta(hours=2),
                open_time_dt=now - datetime.timedelta(minutes=10),
                provisional=True,
            ),
        ]
        client = _mock_client({"open": markets})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNone(result)
        self.assertIn("provisional", reason)

    def test_all_expired_returns_none(self):
        """When all markets are expired (close_time in the past), return (None, reason)."""
        now = self._now()
        markets = [
            _make_market(
                "KXBTCD-OLD1",
                close_time_dt=now - datetime.timedelta(hours=2),
                open_time_dt=now - datetime.timedelta(hours=3),
            ),
            _make_market(
                "KXBTCD-OLD2",
                close_time_dt=now - datetime.timedelta(hours=1),
                open_time_dt=now - datetime.timedelta(hours=2),
            ),
        ]
        client = _mock_client({"open": markets})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNone(result)
        self.assertIn("expired", reason)

    def test_markets_with_no_close_time_returns_none(self):
        """When markets exist but none have close_time, return (None, reason)."""
        now = self._now()
        # Markets with no close_time
        m1 = {"ticker": "KXBTCD-NOTS1", "status": "open", "is_provisional": False}
        m2 = {"ticker": "KXBTCD-NOTS2", "status": "open", "is_provisional": False}
        client = _mock_client({"open": [m1, m2]})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNone(result)
        self.assertIn("expired or missing close_time", reason)

    def test_naive_datetime_treated_as_utc(self):
        """A close_time without timezone info should be treated as UTC (no TypeError)."""
        now = self._now()
        # close_time without timezone offset → naive datetime after fromisoformat
        naive_close = (now + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
        naive_open = (now - datetime.timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S")
        m = {
            "ticker": "KXBTCD-NAIVE",
            "status": "open",
            "close_time": naive_close,
            "open_time": naive_open,
            "is_provisional": False,
        }
        client = _mock_client({"open": [m]})

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            # Should not raise TypeError (naive vs aware comparison)
            result, reason = find_active_market(client, "KXBTCD")

        self.assertIsNotNone(result)
        self.assertEqual(result["ticker"], "KXBTCD-NAIVE")


# ---------------------------------------------------------------------------
# _trim_orderbook
# ---------------------------------------------------------------------------

class TestTrimOrderbook(unittest.TestCase):
    def test_trims_nested_yes_no(self):
        ob = {
            "orderbook": {
                "yes": [[55, 10], [54, 5], [53, 2]],
                "no": [[45, 8], [44, 3]],
            }
        }
        result = _trim_orderbook(ob, limit=2)
        self.assertEqual(len(result["orderbook"]["yes"]), 2)
        self.assertEqual(len(result["orderbook"]["no"]), 2)

    def test_does_not_mutate_original(self):
        yes_list = [[55, 10], [54, 5], [53, 2]]
        ob = {"orderbook": {"yes": yes_list, "no": []}}
        _trim_orderbook(ob, limit=1)
        self.assertEqual(len(yes_list), 3)  # original unchanged

    def test_trims_top_level_arrays(self):
        ob = {"yes": [[55, 10], [54, 5]], "no": [[45, 8]]}
        result = _trim_orderbook(ob, limit=1)
        self.assertEqual(len(result["yes"]), 1)
        self.assertEqual(len(result["no"]), 1)

    def test_empty_response(self):
        self.assertEqual(_trim_orderbook({}, 5), {})
        self.assertIsNone(_trim_orderbook(None, 5))


# ---------------------------------------------------------------------------
# cmd_orderbook
# ---------------------------------------------------------------------------

class TestCmdOrderbook(unittest.TestCase):
    def _args(self, ticker=None, series=None, limit=10):
        args = MagicMock()
        args.ticker = ticker
        args.series = series
        args.limit = limit
        return args

    def test_direct_ticker_uses_ticker(self):
        client = _mock_client(orderbook={"orderbook": {"yes": [[55, 10]], "no": [[45, 8]]}})
        args = self._args(ticker="KXBTCD-26MAR2802")

        result = cmd_orderbook(client, args)

        client.get_orderbook.assert_called_once_with("KXBTCD-26MAR2802")
        self.assertEqual(result["ticker"], "KXBTCD-26MAR2802")
        self.assertIn("explicit --ticker", result["reason"])
        self.assertIn("orderbook", result)

    def test_ticker_wins_over_series(self):
        """When both --ticker and --series are given, --ticker is used."""
        client = _mock_client(orderbook={"orderbook": {}})
        args = self._args(ticker="KXBTCD-26MAR2802", series="KXBTCD")

        result = cmd_orderbook(client, args)

        client.get_orderbook.assert_called_once_with("KXBTCD-26MAR2802")
        # get_markets should NOT have been called
        client.get_markets.assert_not_called()

    def test_series_auto_select_returns_selected_ticker(self):
        """With only --series, the auto-selected ticker is shown in the output."""
        now = datetime.datetime(2026, 3, 28, 5, 30, tzinfo=UTC)
        markets = [
            _make_market(
                "KXBTCD-28MAR2606",
                close_time_dt=now + datetime.timedelta(hours=1),
                open_time_dt=now - datetime.timedelta(minutes=30),
            ),
        ]
        client = _mock_client(
            markets_by_status={"open": markets},
            orderbook={"orderbook": {"yes": [[60, 5]], "no": [[40, 3]]}},
        )
        args = self._args(series="KXBTCD")

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = cmd_orderbook(client, args)

        self.assertEqual(result["series"], "KXBTCD")
        self.assertEqual(result["selected_ticker"], "KXBTCD-28MAR2606")
        self.assertIn("reason", result)
        self.assertIn("orderbook", result)

    def test_series_no_active_market_returns_error(self):
        """When no active market is found, the response must contain an 'error' key."""
        client = _mock_client(markets_by_status=None)
        args = self._args(series="KXBTCD")

        result = cmd_orderbook(client, args)

        self.assertIn("error", result)
        self.assertIn("KXBTCD", result["error"])
        self.assertEqual(result["series"], "KXBTCD")
        client.get_orderbook.assert_not_called()

    def test_neither_ticker_nor_series_returns_error(self):
        client = MagicMock()
        args = self._args()  # both None

        result = cmd_orderbook(client, args)

        self.assertIn("error", result)
        client.get_orderbook.assert_not_called()

    def test_orderbook_api_error_returns_error(self):
        """If get_orderbook() raises, the error is captured in the JSON response."""
        now = datetime.datetime(2026, 3, 28, 5, 30, tzinfo=UTC)
        markets = [
            _make_market(
                "KXBTCD-28MAR2606",
                close_time_dt=now + datetime.timedelta(hours=1),
                open_time_dt=now - datetime.timedelta(minutes=30),
            ),
        ]
        client = _mock_client(markets_by_status={"open": markets})
        client.get_orderbook.side_effect = RuntimeError("connection refused")
        args = self._args(series="KXBTCD")

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = cmd_orderbook(client, args)

        self.assertIn("error", result)
        self.assertIn("connection refused", result["error"])

    def test_limit_applied_to_orderbook(self):
        """The --limit argument must cap bid-level arrays in the output."""
        now = datetime.datetime(2026, 3, 28, 5, 30, tzinfo=UTC)
        markets = [
            _make_market(
                "KXBTCD-28MAR2606",
                close_time_dt=now + datetime.timedelta(hours=1),
                open_time_dt=now - datetime.timedelta(minutes=30),
            ),
        ]
        client = _mock_client(
            markets_by_status={"open": markets},
            orderbook={
                "orderbook": {
                    "yes": [[price, 1] for price in range(99, 49, -1)],  # 50 entries
                    "no": [[price, 1] for price in range(1, 51)],        # 50 entries
                }
            },
        )
        args = self._args(series="KXBTCD", limit=3)

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = cmd_orderbook(client, args)

        self.assertEqual(len(result["orderbook"]["orderbook"]["yes"]), 3)
        self.assertEqual(len(result["orderbook"]["orderbook"]["no"]), 3)

    def test_missing_ticker_in_market_returns_error(self):
        """If the selected market dict has no 'ticker' key, return a structured error."""
        now = datetime.datetime(2026, 3, 28, 5, 30, tzinfo=UTC)
        # A market dict that is missing the 'ticker' field
        market_no_ticker = {
            "status": "open",
            "close_time": (now + datetime.timedelta(hours=1)).isoformat(),
            "open_time": (now - datetime.timedelta(minutes=30)).isoformat(),
            "is_provisional": False,
        }
        client = _mock_client(markets_by_status={"open": [market_no_ticker]})
        args = self._args(series="KXBTCD")

        with patch("openclaw_kalshi.datetime") as mock_dt:
            mock_dt.datetime.now.return_value = now
            mock_dt.timezone.utc = UTC
            mock_dt.datetime.fromisoformat = datetime.datetime.fromisoformat
            mock_dt.timedelta = datetime.timedelta
            result = cmd_orderbook(client, args)

        self.assertIn("error", result)
        self.assertEqual(result["series"], "KXBTCD")
        client.get_orderbook.assert_not_called()


# ---------------------------------------------------------------------------
# _positive_int (argparse type helper)
# ---------------------------------------------------------------------------

class TestPositiveInt(unittest.TestCase):
    def test_valid_positive(self):
        from openclaw_kalshi import _positive_int
        self.assertEqual(_positive_int("5"), 5)
        self.assertEqual(_positive_int("1"), 1)

    def test_zero_raises(self):
        from openclaw_kalshi import _positive_int
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            _positive_int("0")

    def test_negative_raises(self):
        from openclaw_kalshi import _positive_int
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            _positive_int("-1")

    def test_non_integer_raises(self):
        from openclaw_kalshi import _positive_int
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            _positive_int("abc")


if __name__ == "__main__":
    unittest.main()
