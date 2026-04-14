"""
Tests for openclaw_kalshi.py — series resolution, orderbook routing, safety gates,
and the structured response envelope contract.

Response contract (every JSON object to stdout):
  Success: {"ok": true, "code": str, "result": dict, "warnings": list}
  Failure: {"ok": false, "code": str, "error": str, "details": dict}

  - warnings is always present on success ([] when empty).
  - details is always present on failure ({} when empty).
  - The two key-sets are disjoint except for ok and code.

  Decision semantics (nested inside result / details):
    retryable, halt_trading, requires_human_review are always present as bools.
    They are independent of ok — see DECISION_POLICY in openclaw_kalshi.py.
"""

import datetime
import inspect
import io
import json
import os
import re
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("BTC_SERIES_TICKER", "KXBTCD")
os.environ.setdefault("KALSHI_API_KEY_ID", "test-key")
os.environ.setdefault("KALSHI_PRIVATE_KEY_PATH", __file__)
os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    os.path.join(tempfile.gettempdir(), f"openclaw_stop_file_tests_{os.getpid()}"),
)

import openclaw_kalshi as cli

_STOP_PATH = os.environ["OPENCLAW_STOP_FILE"]
try:
    os.remove(_STOP_PATH)
except FileNotFoundError:
    pass

# ── Envelope invariant assertions ──────────────────────────────────────────────

_SUCCESS_KEYS = {"ok", "code", "result", "warnings"}
_FAILURE_KEYS = {"ok", "code", "error", "details"}
_DECISION_KEYS = {"retryable", "halt_trading", "requires_human_review"}


def _assert_success_envelope(tc: unittest.TestCase, env: dict, code: str):
    """Assert that env is a well-formed success envelope with expected code."""
    tc.assertEqual(set(env.keys()), _SUCCESS_KEYS, "success envelope key-set mismatch")
    tc.assertIs(env["ok"], True)
    tc.assertEqual(env["code"], code)
    tc.assertIsInstance(env["result"], dict)
    tc.assertIsInstance(env["warnings"], list)
    for field in _DECISION_KEYS:
        tc.assertIn(field, env["result"], f"missing decision field '{field}' in result")
        tc.assertIsInstance(
            env["result"][field], bool, f"decision field '{field}' must be bool"
        )


def _assert_failure_envelope(tc: unittest.TestCase, env: dict, code: str):
    """Assert that env is a well-formed failure envelope with expected code."""
    tc.assertEqual(set(env.keys()), _FAILURE_KEYS, "failure envelope key-set mismatch")
    tc.assertIs(env["ok"], False)
    tc.assertEqual(env["code"], code)
    tc.assertIsInstance(env["error"], str)
    tc.assertIsInstance(env["details"], dict)
    for field in _DECISION_KEYS:
        tc.assertIn(field, env["details"], f"missing decision field '{field}' in details")
        tc.assertIsInstance(
            env["details"][field], bool, f"decision field '{field}' must be bool"
        )


# ── Test helpers ───────────────────────────────────────────────────────────────


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


# ── Envelope helper unit tests ────────────────────────────────────────────────


class TestResponseEnvelopeHelpers(unittest.TestCase):
    """_success and _failure produce the canonical, fixed-shape envelopes."""

    def test_success_minimal(self):
        env = cli._success("STATUS_OK", {"balance": 100})
        _assert_success_envelope(self, env, "STATUS_OK")
        self.assertEqual(env["result"]["balance"], 100)
        self.assertEqual(env["warnings"], [])

    def test_success_with_warnings(self):
        env = cli._success(
            "SELL_CLAMPED",
            {"count": 2},
            warnings=[{"code": "POSITION_CLAMPED", "message": "clamped"}],
        )
        _assert_success_envelope(self, env, "SELL_CLAMPED")
        self.assertEqual(len(env["warnings"]), 1)
        self.assertEqual(env["warnings"][0]["code"], "POSITION_CLAMPED")

    def test_success_empty_warnings_list_stays_empty(self):
        env = cli._success("OK", {}, warnings=[])
        _assert_success_envelope(self, env, "OK")
        self.assertEqual(env["warnings"], [])

    def test_success_none_warnings_becomes_empty(self):
        env = cli._success("OK", {}, warnings=None)
        _assert_success_envelope(self, env, "OK")
        self.assertEqual(env["warnings"], [])

    def test_failure_minimal(self):
        env = cli._failure("NO_POSITION", "no contracts")
        _assert_failure_envelope(self, env, "NO_POSITION")
        self.assertEqual(env["error"], "no contracts")
        self.assertEqual(set(env["details"].keys()), _DECISION_KEYS)

    def test_failure_with_details(self):
        env = cli._failure("COMMAND_FAILED", "boom", details={"exc": "ValueError"})
        _assert_failure_envelope(self, env, "COMMAND_FAILED")
        self.assertEqual(env["details"]["exc"], "ValueError")

    def test_failure_none_details_becomes_decision_only(self):
        env = cli._failure("ERR", "msg", details=None)
        _assert_failure_envelope(self, env, "ERR")
        self.assertEqual(set(env["details"].keys()), _DECISION_KEYS)


# ── Series resolution tests (unchanged logic) ─────────────────────────────────


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


# ── Orderbook tests ────────────────────────────────────────────────────────────


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
        _assert_success_envelope(self, output, "ORDERBOOK_OK")
        self.assertEqual(output["result"]["ticker"], "KXBTCD-26MAR2815-B87500")
        self.assertEqual(output["result"]["resolved_from_series"], "KXBTCD")
        self.assertEqual(output["result"]["best_yes_bid"], 55)
        self.assertEqual(output["result"]["best_no_bid"], 45)
        self.assertEqual(output["result"]["best_yes_ask"], 55)
        self.assertEqual(output["result"]["best_no_ask"], 45)
        self.assertEqual(output["warnings"], [])

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

        buf = io.StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit):
            cli.cmd_orderbook(client, args)

        err = json.loads(buf.getvalue())
        _assert_failure_envelope(self, err, "ORDERBOOK_FETCH_ERROR")

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
        _assert_success_envelope(self, output, "ORDERBOOK_OK")
        self.assertEqual(output["result"]["ticker"], "KXBTCD-26MAR2815-B87500")
        self.assertIsNone(output["result"]["resolved_from_series"])
        self.assertEqual(output["warnings"], [])
        client.list_markets.assert_not_called()

    def test_empty_orderbook_is_success_with_warning(self):
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
        _assert_success_envelope(self, output, "ORDERBOOK_EMPTY")
        self.assertEqual(len(output["warnings"]), 1)
        self.assertEqual(output["warnings"][0]["code"], "ORDERBOOK_EMPTY")
        self.assertIn("zero bids", output["warnings"][0]["message"])

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
        buf = io.StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit):
            cli.cmd_orderbook(client, args)

        err = json.loads(buf.getvalue())
        _assert_failure_envelope(self, err, "INVALID_TICKER")

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
        _assert_success_envelope(self, output, "ORDERBOOK_OK")
        self.assertEqual(output["result"]["best_yes_bid"], 55)
        self.assertEqual(output["result"]["best_no_bid"], 45)
        self.assertEqual(output["result"]["yes_bid_levels"], 2)
        self.assertEqual(output["result"]["no_bid_levels"], 1)
        self.assertEqual(output["warnings"], [])


# ── Ticker resolution from args tests ──────────────────────────────────────────


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


# ── Buy / Sell envelope and ticker normalization tests ─────────────────────────


class TestCmdBuySellEnvelope(unittest.TestCase):
    """Buy/sell produce deterministic envelopes with fixed result field-sets."""

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

    # ── Ticker validation ──────────────────────────────────────────────────────

    def test_buy_rejects_bare_series(self):
        client = MagicMock()
        args = self._make_args(ticker="KXBTCD")
        buf = io.StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit):
            cli.cmd_buy(client, args)
        _assert_failure_envelope(self, json.loads(buf.getvalue()), "INVALID_TICKER")

    def test_sell_rejects_bare_series(self):
        client = MagicMock()
        args = self._make_args(ticker="KXBTCD")
        buf = io.StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit):
            cli.cmd_sell(client, args)
        _assert_failure_envelope(self, json.loads(buf.getvalue()), "INVALID_TICKER")

    # ── Buy envelope tests ─────────────────────────────────────────────────────

    def test_buy_dry_run_envelope(self):
        client = MagicMock()
        client.list_markets.return_value = [
            _make_market("KXBTCD-28MAR2615-B85000", _future_iso(1))
        ]
        args = self._make_args(ticker=None)

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_buy(client, args)

        output = json.loads(captured.getvalue())
        _assert_success_envelope(self, output, "BUY_DRY_RUN")
        r = output["result"]
        self.assertEqual(r["ticker"], "KXBTCD-28MAR2615-B85000")
        self.assertEqual(r["mode"], "DRY_RUN")
        self.assertIsNone(r["order_id"])
        self.assertIsNone(r["order_status"])
        self.assertEqual(output["warnings"], [])

    def test_buy_accepts_exact_ticker(self):
        client = MagicMock()
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000")

        captured = io.StringIO()
        with patch("sys.stdout", captured):
            cli.cmd_buy(client, args)

        output = json.loads(captured.getvalue())
        _assert_success_envelope(self, output, "BUY_DRY_RUN")
        self.assertEqual(output["result"]["ticker"], "KXBTCD-28MAR2615-B85000")
        client.list_markets.assert_not_called()

    # ── Sell price validation ──────────────────────────────────────────────────

    def test_sell_rejects_price_outside_config_range(self):
        client = MagicMock()
        client.contracts_held_on_side.return_value = 10
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", price=15)
        buf = io.StringIO()
        with patch.object(cli.config, "MIN_CONTRACT_PRICE_CENTS", 20), patch.object(
            cli.config, "MAX_CONTRACT_PRICE_CENTS", 80
        ):
            with patch("sys.stdout", buf), self.assertRaises(SystemExit):
                cli.cmd_sell(client, args)
        _assert_failure_envelope(
            self, json.loads(buf.getvalue()), "PRICE_OUTSIDE_CONFIG_RANGE"
        )

    # ── Sell position semantics ────────────────────────────────────────────────

    def test_sell_no_position_hard_failure(self):
        """Selling with zero held contracts is ok:false / NO_POSITION."""
        client = MagicMock()
        client.contracts_held_on_side.return_value = 0
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000")
        buf = io.StringIO()
        with patch("sys.stdout", buf), self.assertRaises(SystemExit) as ctx:
            cli.cmd_sell(client, args)
        self.assertEqual(ctx.exception.code, 1)
        err = json.loads(buf.getvalue())
        _assert_failure_envelope(self, err, "NO_POSITION")
        self.assertIn("KXBTCD-28MAR2615-B85000", err["error"])
        client.sell_position.assert_not_called()

    def test_sell_within_held_amount_success_no_warnings(self):
        """Selling <= held contracts: ok:true, SELL_DRY_RUN, warnings=[]."""
        client = MagicMock()
        client.contracts_held_on_side.return_value = 10
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", count=3, dry_run=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.cmd_sell(client, args)
        out = json.loads(buf.getvalue())
        _assert_success_envelope(self, out, "SELL_DRY_RUN")
        self.assertEqual(out["result"]["count"], 3)
        self.assertEqual(out["result"]["requested_count"], 3)
        self.assertEqual(out["result"]["position_held"], 10)
        self.assertIsNone(out["result"]["order_id"])
        self.assertIsNone(out["result"]["order_status"])
        self.assertEqual(out["warnings"], [])
        client.sell_position.assert_not_called()

    def test_sell_exact_held_amount_success_no_warnings(self):
        """Selling exactly the held amount: ok:true, SELL_DRY_RUN, warnings=[]."""
        client = MagicMock()
        client.contracts_held_on_side.return_value = 5
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", count=5, dry_run=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.cmd_sell(client, args)
        out = json.loads(buf.getvalue())
        _assert_success_envelope(self, out, "SELL_DRY_RUN")
        self.assertEqual(out["result"]["count"], 5)
        self.assertEqual(out["result"]["requested_count"], 5)
        self.assertEqual(out["result"]["position_held"], 5)
        self.assertEqual(out["warnings"], [])

    def test_sell_clamps_dry_run_success_with_warning(self):
        """Requesting more than held in dry run: ok:true, SELL_CLAMPED, warning."""
        client = MagicMock()
        client.contracts_held_on_side.return_value = 2
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", count=5, dry_run=True)
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            cli.cmd_sell(client, args)
        out = json.loads(buf.getvalue())
        _assert_success_envelope(self, out, "SELL_CLAMPED")
        self.assertEqual(out["result"]["count"], 2)
        self.assertEqual(out["result"]["requested_count"], 5)
        self.assertEqual(out["result"]["position_held"], 2)
        self.assertIsNone(out["result"]["order_id"])
        self.assertEqual(len(out["warnings"]), 1)
        self.assertEqual(out["warnings"][0]["code"], "POSITION_CLAMPED")
        self.assertIn("clamped", out["warnings"][0]["message"].lower())
        client.sell_position.assert_not_called()

    def test_sell_clamps_live_success_with_warning_and_api_call(self):
        """Requesting more than held live: ok:true, SELL_CLAMPED, API called with clamped count."""
        client = MagicMock()
        client.contracts_held_on_side.return_value = 2
        client.sell_position.return_value = {
            "order": {"order_id": "ord-1", "status": "resting"},
        }
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", count=5, dry_run=False)
        buf = io.StringIO()
        with (
            patch("sys.stdout", buf),
            patch.dict(os.environ, {"KALSHI_TRADING_LIVE": "1"}, clear=False),
            patch.object(cli, "_check_stop_file"),
        ):
            cli.cmd_sell(client, args)
        out = json.loads(buf.getvalue())
        _assert_success_envelope(self, out, "SELL_CLAMPED")
        self.assertEqual(out["result"]["count"], 2)
        self.assertEqual(out["result"]["requested_count"], 5)
        self.assertEqual(out["result"]["order_id"], "ord-1")
        self.assertEqual(out["result"]["order_status"], "resting")
        self.assertEqual(len(out["warnings"]), 1)
        self.assertEqual(out["warnings"][0]["code"], "POSITION_CLAMPED")
        client.sell_position.assert_called_once_with(
            "KXBTCD-28MAR2615-B85000", "yes", 2, 45, dry_run=False
        )

    def test_sell_within_held_live_no_warning(self):
        """Selling within held amount live: ok:true, SELL_PLACED, warnings=[]."""
        client = MagicMock()
        client.contracts_held_on_side.return_value = 10
        client.sell_position.return_value = {
            "order": {"order_id": "ord-2", "status": "resting"},
        }
        args = self._make_args(ticker="KXBTCD-28MAR2615-B85000", count=3, dry_run=False)
        buf = io.StringIO()
        with (
            patch("sys.stdout", buf),
            patch.dict(os.environ, {"KALSHI_TRADING_LIVE": "1"}, clear=False),
            patch.object(cli, "_check_stop_file"),
        ):
            cli.cmd_sell(client, args)
        out = json.loads(buf.getvalue())
        _assert_success_envelope(self, out, "SELL_PLACED")
        self.assertEqual(out["result"]["count"], 3)
        self.assertEqual(out["result"]["order_id"], "ord-2")
        self.assertEqual(out["result"]["order_status"], "resting")
        self.assertEqual(out["warnings"], [])
        client.sell_position.assert_called_once_with(
            "KXBTCD-28MAR2615-B85000", "yes", 3, 45, dry_run=False
        )

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
        _assert_success_envelope(self, output, "SELL_DRY_RUN")
        self.assertEqual(output["result"]["ticker"], "KXBTCD-28MAR2615-B85000")
        self.assertEqual(output["result"]["mode"], "DRY_RUN")


# ── Safety gate failure envelope tests ─────────────────────────────────────────


class TestSafetyGateEnvelopes(unittest.TestCase):
    """Safety gates produce correct failure envelopes."""

    def _make_args(self, ticker="KXBTCD-28MAR2615-B85000", dry_run=False):
        return SimpleNamespace(
            series="KXBTCD",
            ticker=ticker,
            side="yes",
            count=1,
            price=45,
            dry_run=dry_run,
            human=False,
            debug=False,
            json=False,
        )

    def test_stop_file_produces_failure_envelope(self):
        args = self._make_args()
        buf = io.StringIO()
        from pathlib import Path

        with (
            patch("sys.stdout", buf),
            patch.object(Path, "exists", return_value=True),
            self.assertRaises(SystemExit),
        ):
            cli.cmd_buy(MagicMock(), args)
        err = json.loads(buf.getvalue())
        _assert_failure_envelope(self, err, "STOP_TRADING")
        self.assertIn("STOP_TRADING", err["error"])

    def test_live_gate_produces_failure_envelope(self):
        args = self._make_args(dry_run=False)
        buf = io.StringIO()
        with (
            patch("sys.stdout", buf),
            patch.dict(os.environ, {"KALSHI_TRADING_LIVE": "0"}, clear=False),
            patch.object(cli, "_check_stop_file"),
            self.assertRaises(SystemExit),
        ):
            cli.cmd_buy(MagicMock(), args)
        err = json.loads(buf.getvalue())
        _assert_failure_envelope(self, err, "LIVE_TRADING_BLOCKED")


# ── Bid parsing tests (unchanged) ─────────────────────────────────────────────


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


# ── Decision semantics tests ──────────────────────────────────────────────────


class TestDecisionSemantics(unittest.TestCase):
    """Decision flags are present, correct, and independent of ok for all
    representative response codes."""

    def _flags(self, env):
        payload = env.get("result") if env["ok"] else env.get("details")
        return {k: payload[k] for k in _DECISION_KEYS}

    # ── success: trading outcomes ──

    def test_buy_dry_run_no_action_needed(self):
        env = cli._success("BUY_DRY_RUN", {"action": "BUY"})
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": False, "requires_human_review": False,
        })

    def test_sell_placed_no_action_needed(self):
        env = cli._success("SELL_PLACED", {"action": "SELL"})
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": False, "requires_human_review": False,
        })

    # ── success: advisory / commander intel ──

    def test_sell_clamped_requires_human_review(self):
        env = cli._success("SELL_CLAMPED", {"action": "SELL"})
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": False, "requires_human_review": True,
        })

    def test_orderbook_empty_retryable(self):
        env = cli._success("ORDERBOOK_EMPTY", {"ticker": "X"})
        self.assertEqual(self._flags(env), {
            "retryable": True, "halt_trading": False, "requires_human_review": False,
        })

    # ── failure: retryable transient ──

    def test_orderbook_fetch_error_retryable(self):
        env = cli._failure("ORDERBOOK_FETCH_ERROR", "network error")
        self.assertEqual(self._flags(env), {
            "retryable": True, "halt_trading": False, "requires_human_review": False,
        })

    def test_series_resolution_network_error_retryable(self):
        env = cli._failure("SERIES_RESOLUTION_NETWORK_ERROR", "timeout")
        self.assertEqual(self._flags(env), {
            "retryable": True, "halt_trading": False, "requires_human_review": False,
        })

    # ── failure: hard stops ──

    def test_stop_trading_halts(self):
        env = cli._failure("STOP_TRADING", "stop file present")
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": True, "requires_human_review": True,
        })

    def test_live_trading_blocked_halts(self):
        env = cli._failure("LIVE_TRADING_BLOCKED", "not enabled")
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": True, "requires_human_review": True,
        })

    def test_config_error_halts(self):
        env = cli._failure("CONFIG_ERROR", "bad config")
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": True, "requires_human_review": True,
        })

    # ── failure: caller / planning / validation ──

    def test_invalid_ticker_validation_error(self):
        env = cli._failure("INVALID_TICKER", "bad ticker")
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": False, "requires_human_review": True,
        })

    def test_invalid_side_validation_error(self):
        env = cli._failure("INVALID_SIDE", "bad side")
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": False, "requires_human_review": True,
        })

    # ── unmapped / unknown codes ──

    def test_unmapped_code_uses_safe_fallback(self):
        env = cli._failure("TOTALLY_UNKNOWN_CODE", "something weird")
        self.assertEqual(self._flags(env), {
            "retryable": False, "halt_trading": True, "requires_human_review": True,
        })

    # ── merge precedence: policy overwrites caller-provided fields ──

    def test_policy_overwrites_conflicting_caller_fields(self):
        env = cli._failure("STOP_TRADING", "stop", details={
            "retryable": True,
            "halt_trading": False,
            "requires_human_review": False,
        })
        flags = self._flags(env)
        self.assertFalse(flags["retryable"])
        self.assertTrue(flags["halt_trading"])
        self.assertTrue(flags["requires_human_review"])

    def test_policy_overwrites_on_success_too(self):
        env = cli._success("BUY_PLACED", {
            "retryable": True,
            "halt_trading": True,
            "requires_human_review": True,
        })
        flags = self._flags(env)
        self.assertFalse(flags["retryable"])
        self.assertFalse(flags["halt_trading"])
        self.assertFalse(flags["requires_human_review"])


# ── Decision-policy completeness ──────────────────────────────────────────────


class TestDecisionPolicyCoverage(unittest.TestCase):
    """Every response code emitted anywhere in the CLI must have an explicit
    DECISION_POLICY entry.  This test protects future edits from adding codes
    without decision semantics."""

    def test_all_emitted_codes_have_policy_entries(self):
        source = inspect.getsource(cli)
        direct = re.findall(r'_(?:success|failure|die)\(\s*"([A-Z][A-Z_]+)"', source)
        assigned = re.findall(r'\bcode\s*=\s*"([A-Z][A-Z_]+)"', source)
        emitted = set(direct + assigned)
        self.assertGreaterEqual(
            len(emitted), 10, "suspiciously few codes found in source"
        )
        missing = emitted - set(cli.DECISION_POLICY.keys())
        self.assertEqual(
            missing, set(),
            f"Response codes emitted by CLI but missing from DECISION_POLICY: {missing}",
        )

    def test_policy_keys_are_well_formed(self):
        for code, values in cli.DECISION_POLICY.items():
            self.assertIsInstance(code, str, f"policy key must be str: {code!r}")
            self.assertEqual(code, code.upper(), f"policy key must be uppercase: {code}")
            self.assertEqual(len(values), 3, f"policy tuple must be 3-tuple: {code}")
            for v in values:
                self.assertIsInstance(v, bool, f"policy value must be bool in {code}")


if __name__ == "__main__":
    unittest.main()
