"""
Tests for openclaw_kalshi.py — series resolution, orderbook routing, and safety gates.
"""

import datetime
import io
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("BTC_SERIES_TICKER", "KXBTCD")
os.environ.setdefault("KALSHI_API_KEY_ID", "test-key")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", __file__)

import openclaw_kalshi as cli


def _make_market(
    ticker,
    close_time,
    status="open",
    is_provisional=False,
    title="",
    volume=100,
):
    return {
        "ticker": ticker,
        "status": status,
        "close_time": close_time,
        "is_provisional": is_provisional,
        "title": title,
        "yes_ask": 55,
        "yes_bid": 45,
        "no_ask": 55,
        "no_bid": 45,
        "last_price": 50,
        "volume": volume,
    }


def _future_iso(hours_ahead=1):
    dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        hours=hours_ahead
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _past_iso(hours_ago=1):
    dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        hours=hours_ago
    )
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestResolveLiveMarketTicker(unittest.TestCase):
    """Tests for resolve_live_market_ticker()."""

    def _mock_client(self, markets):
        client = MagicMock()
        client.list_markets.return_value = markets
        return client

    def test_resolves_single_live_market(self):
        markets = [_make_market("KXBTCD-26MAR2815-B87500", _future_iso(1))]
        client = self._mock_client(markets)
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-26MAR2815-B87500")

    def test_picks_soonest_expiring_from_multiple(self):
        markets = [
            _make_market("KXBTCD-26MAR2816-B87500", _future_iso(2)),
            _make_market("KXBTCD-26MAR2815-B87500", _future_iso(1)),
            _make_market("KXBTCD-26MAR2817-B87500", _future_iso(3)),
        ]
        client = self._mock_client(markets)
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-26MAR2815-B87500")

    def test_filters_out_provisional(self):
        markets = [
            _make_market(
                "KXBTCD-26MAR2815-B87500", _future_iso(1), is_provisional=True
            ),
            _make_market("KXBTCD-26MAR2815-B88000", _future_iso(1)),
        ]
        client = self._mock_client(markets)
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-26MAR2815-B88000")

    def test_filters_out_past_close_time(self):
        markets = [
            _make_market("KXBTCD-26MAR2812-B87500", _past_iso(1)),
            _make_market("KXBTCD-26MAR2815-B87500", _future_iso(1)),
        ]
        client = self._mock_client(markets)
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-26MAR2815-B87500")

    def test_filters_out_wrong_prefix(self):
        markets = [
            _make_market("OTHERSERIES-26MAR2815-B87500", _future_iso(1)),
            _make_market("KXBTCD-26MAR2815-B88000", _future_iso(1)),
        ]
        client = self._mock_client(markets)
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-26MAR2815-B88000")

    def test_raises_when_no_markets_from_api(self):
        client = self._mock_client([])
        with self.assertRaises(RuntimeError) as ctx:
            cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertIn("No markets returned", str(ctx.exception))

    def test_raises_when_all_filtered_out(self):
        markets = [
            _make_market("KXBTCD-26MAR2812-B87500", _past_iso(1)),
            _make_market("KXBTCD-26MAR2811-B87500", _past_iso(2), is_provisional=True),
        ]
        client = self._mock_client(markets)
        with self.assertRaises(RuntimeError) as ctx:
            cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertIn("No live markets found", str(ctx.exception))
        self.assertIn("filtered out", str(ctx.exception).lower())

    def test_api_receives_correct_params(self):
        client = self._mock_client([_make_market("KXBTCD-TEST", _future_iso(1))])
        cli.resolve_live_market_ticker(client, "KXBTCD")
        client.list_markets.assert_called_once_with("KXBTCD", status="open", limit=100)


class TestIsExactMarketTicker(unittest.TestCase):
    """Ensure bare series codes are never confused with exact tickers."""

    def test_bare_series_is_not_exact(self):
        self.assertFalse(cli._is_exact_market_ticker("KXBTCD", "KXBTCD"))

    def test_series_with_dash_only_is_not_exact(self):
        self.assertFalse(cli._is_exact_market_ticker("KXBTCD-", "KXBTCD"))

    def test_full_ticker_is_exact(self):
        self.assertTrue(
            cli._is_exact_market_ticker("KXBTCD-26MAR2815-B87500", "KXBTCD")
        )


class TestResolverPrefersHighVolume(unittest.TestCase):
    """When multiple strikes share the same close_time, pick highest volume."""

    def test_same_close_time_picks_highest_volume(self):
        ct = _future_iso(1)
        markets = [
            _make_market("KXBTCD-28MAR2615-B84000", ct, volume=10),
            _make_market("KXBTCD-28MAR2615-B85000", ct, volume=500),
            _make_market("KXBTCD-28MAR2615-B86000", ct, volume=200),
        ]
        client = MagicMock()
        client.list_markets.return_value = markets
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-28MAR2615-B85000")

    def test_earlier_close_time_wins_over_higher_volume(self):
        markets = [
            _make_market("KXBTCD-28MAR2616-B85000", _future_iso(2), volume=999),
            _make_market("KXBTCD-28MAR2615-B84000", _future_iso(1), volume=50),
        ]
        client = MagicMock()
        client.list_markets.return_value = markets
        result = cli.resolve_live_market_ticker(client, "KXBTCD")
        self.assertEqual(result, "KXBTCD-28MAR2615-B84000")


def _make_raw_orderbook(yes_bids=None, no_bids=None):
    return {
        "orderbook": {
            "yes": yes_bids or [],
            "no": no_bids or [],
        }
    }


class TestCmdOrderbookDirectFetch(unittest.TestCase):
    """cmd_orderbook fetches raw orderbook (not get_market_quotes)."""

    def test_resolves_series_and_fetches_orderbook(self):
        client = MagicMock()
        client.list_markets.return_value = [
            _make_market("KXBTCD-26MAR2815-B87500", _future_iso(1))
        ]
        client.get_orderbook.return_value = _make_raw_orderbook(
            yes_bids=[[55, 10], [50, 20]],
            no_bids=[[45, 15]],
        )
        client.get_market.return_value = {
            "market": {"title": "BTC Above 87500", "close_time": _future_iso(1)}
        }

        args = SimpleNamespace(
            series="KXBTCD",
            ticker=None,
            human=False,
            debug=False,
            limit=None,
            json=False,
        )

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_orderbook(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["ticker"], "KXBTCD-26MAR2815-B87500")
        self.assertEqual(output["resolved_from_series"], "KXBTCD")
        self.assertEqual(output["best_yes_bid"], 55)
        self.assertEqual(output["best_no_bid"], 45)
        self.assertEqual(output["best_yes_ask"], 55)
        self.assertEqual(output["best_no_ask"], 45)
        self.assertNotIn("error", output)

        client.get_orderbook.assert_called_once_with("KXBTCD-26MAR2815-B87500")
        client.get_market_quotes.assert_not_called()

    def test_http_error_surfaces_not_swallowed(self):
        import requests

        client = MagicMock()
        client.list_markets.return_value = [
            _make_market("KXBTCD-26MAR2815-B87500", _future_iso(1))
        ]
        resp = MagicMock()
        resp.status_code = 404
        client.get_orderbook.side_effect = requests.exceptions.HTTPError(
            "404 Not Found", response=resp
        )

        args = SimpleNamespace(
            series="KXBTCD",
            ticker=None,
            human=False,
            debug=False,
            limit=None,
            json=False,
        )

        with self.assertRaises(SystemExit):
            cli.cmd_orderbook(client, args)

    def test_explicit_ticker_bypasses_resolution(self):
        client = MagicMock()
        client.get_orderbook.return_value = _make_raw_orderbook(
            yes_bids=[[60, 5]], no_bids=[[40, 5]]
        )
        client.get_market.return_value = {
            "market": {"title": "Test", "close_time": _future_iso(1)}
        }

        args = SimpleNamespace(
            series="KXBTCD",
            ticker="KXBTCD-26MAR2815-B87500",
            human=False,
            debug=False,
            limit=None,
            json=False,
        )

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_orderbook(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["ticker"], "KXBTCD-26MAR2815-B87500")
        self.assertIsNone(output["resolved_from_series"])
        client.list_markets.assert_not_called()

    def test_empty_orderbook_reports_keys(self):
        client = MagicMock()
        client.get_orderbook.return_value = _make_raw_orderbook()
        client.get_market.return_value = {
            "market": {"title": "T", "close_time": _future_iso(1)}
        }

        args = SimpleNamespace(
            series="KXBTCD",
            ticker="KXBTCD-26MAR2815-B87500",
            human=False,
            debug=False,
            limit=None,
            json=False,
        )

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_orderbook(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["error"], "orderbook_empty")
        self.assertIn("zero bids", output["message"])

    def test_bare_series_as_ticker_is_rejected(self):
        client = MagicMock()
        args = SimpleNamespace(
            series="KXBTCD",
            ticker="KXBTCD",
            human=False,
            debug=False,
            limit=None,
            json=False,
        )
        with self.assertRaises(SystemExit):
            cli.cmd_orderbook(client, args)

    def test_fp_format_orderbook_parsed(self):
        client = MagicMock()
        client.get_orderbook.return_value = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.55", "10"], ["0.50", "20"]],
                "no_dollars_fp": [["0.45", "15"]],
            },
            "orderbook": {},
        }
        client.get_market.return_value = {
            "market": {"title": "T", "close_time": _future_iso(1)}
        }

        args = SimpleNamespace(
            series="KXBTCD",
            ticker="KXBTCD-28MAR2615-B85000",
            human=False,
            debug=False,
            limit=None,
            json=False,
        )

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_orderbook(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["best_yes_bid"], 55)
        self.assertEqual(output["best_no_bid"], 45)
        self.assertEqual(output["yes_bid_levels"], 2)
        self.assertEqual(output["no_bid_levels"], 1)


class TestResolveTickerFromArgs(unittest.TestCase):
    """_resolve_ticker_from_args enforces --series / --ticker convention."""

    def test_explicit_ticker_returned_directly(self):
        client = MagicMock()
        args = SimpleNamespace(
            series="KXBTCD",
            ticker="KXBTCD-28MAR2615-B85000",
            debug=False,
        )
        result = cli._resolve_ticker_from_args(client, args, caller="test")
        self.assertEqual(result, "KXBTCD-28MAR2615-B85000")
        client.list_markets.assert_not_called()

    def test_bare_series_as_ticker_rejected(self):
        client = MagicMock()
        args = SimpleNamespace(series="KXBTCD", ticker="KXBTCD", debug=False)
        with self.assertRaises(SystemExit):
            cli._resolve_ticker_from_args(client, args, caller="test")

    def test_no_ticker_resolves_from_series(self):
        client = MagicMock()
        client.list_markets.return_value = [
            _make_market("KXBTCD-28MAR2615-B85000", _future_iso(1))
        ]
        args = SimpleNamespace(series="KXBTCD", ticker=None, debug=False)
        result = cli._resolve_ticker_from_args(client, args, caller="test")
        self.assertEqual(result, "KXBTCD-28MAR2615-B85000")

    def test_ticker_lowercased_is_uppercased(self):
        client = MagicMock()
        args = SimpleNamespace(
            series="KXBTCD",
            ticker="kxbtcd-28mar2615-b85000",
            debug=False,
        )
        result = cli._resolve_ticker_from_args(client, args, caller="test")
        self.assertEqual(result, "KXBTCD-28MAR2615-B85000")


class TestCmdBuySellTickerNormalization(unittest.TestCase):
    """Buy/sell use _resolve_ticker_from_args; bare series rejected."""

    def _make_args(self, ticker=None, side="yes", count=1, price=45, dry_run=True):
        return SimpleNamespace(
            series="KXBTCD",
            ticker=ticker,
            side=side,
            count=count,
            price=price,
            dry_run=dry_run,
            human=False,
            debug=False,
            json=False,
        )

    def test_buy_rejects_bare_series(self):
        client = MagicMock()
        args = self._make_args(ticker="KXBTCD")
        with self.assertRaises(SystemExit):
            cli.cmd_buy(client, args)

    def test_sell_rejects_bare_series(self):
        client = MagicMock()
        args = self._make_args(ticker="KXBTCD")
        with self.assertRaises(SystemExit):
            cli.cmd_sell(client, args)

    def test_buy_resolves_from_series_when_no_ticker(self):
        client = MagicMock()
        client.list_markets.return_value = [
            _make_market("KXBTCD-28MAR2615-B85000", _future_iso(1))
        ]
        args = self._make_args(ticker=None)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_buy(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["ticker"], "KXBTCD-28MAR2615-B85000")
        self.assertEqual(output["result"], "simulated")

    def test_sell_resolves_from_series_when_no_ticker(self):
        client = MagicMock()
        client.list_markets.return_value = [
            _make_market("KXBTCD-28MAR2615-B85000", _future_iso(1))
        ]
        client.contracts_held_on_side.return_value = 5
        args = self._make_args(ticker=None)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_sell(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["ticker"], "KXBTCD-28MAR2615-B85000")
        self.assertEqual(output["result"], "simulated")

    def test_buy_accepts_exact_ticker(self):
        client = MagicMock()
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000")

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_buy(client, args)

        output = json.loads(captured.getvalue())
        self.assertEqual(output["ticker"], "KXBTCD-28MAR2615-B85000")
        self.assertEqual(output["result"], "simulated")
        client.list_markets.assert_not_called()

    def test_sell_rejects_price_outside_config_range(self):
        client = MagicMock()
        client.contracts_held_on_side.return_value = 10
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", price=15)
        with patch.object(cli.config, "MIN_CONTRACT_PRICE_CENTS", 20), patch.object(
            cli.config, "MAX_CONTRACT_PRICE_CENTS", 80
        ):
            with self.assertRaises(SystemExit):
                cli.cmd_sell(client, args)

    def test_sell_rejects_no_position(self):
        client = MagicMock()
        client.contracts_held_on_side.return_value = 0
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000")
        with self.assertRaises(SystemExit) as ctx:
            cli.cmd_sell(client, args)
        self.assertEqual(ctx.exception.code, 1)

    def test_sell_rejects_oversized_count(self):
        client = MagicMock()
        client.contracts_held_on_side.return_value = 2
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", count=5, dry_run=True)
        with self.assertRaises(SystemExit):
            cli.cmd_sell(client, args)


class TestParseBidArray(unittest.TestCase):
    def test_integer_format(self):
        result = cli._parse_bid_array([[55, 10], [50, 20]])
        self.assertEqual(result, [(55, 10), (50, 20)])

    def test_string_format(self):
        result = cli._parse_bid_array([["0.55", "10"], ["0.50", "20"]])
        self.assertEqual(result, [(55, 10), (50, 20)])

    def test_float_dollars_format(self):
        result = cli._parse_bid_array([[0.55, 10], [0.50, 20]])
        self.assertEqual(result, [(55, 10), (50, 20)])

    def test_empty(self):
        self.assertEqual(cli._parse_bid_array([]), [])
        self.assertEqual(cli._parse_bid_array(None), [])


if __name__ == "__main__":
    unittest.main()
