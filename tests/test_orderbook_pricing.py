"""
test_orderbook_pricing.py - Tests for orderbook-based price fetching.

Verifies that get_market_quotes correctly computes best bid/ask prices
from the orderbook structure and that strategy functions work with both
old and new field names.
"""
import unittest
import logging
from unittest.mock import MagicMock, patch

from kalshi_client import KalshiClient
from strategy import suggest_limit_price


class TestGetMarketQuotes(unittest.TestCase):
    """Test the get_market_quotes method in KalshiClient."""

    def setUp(self):
        """Create a mock KalshiClient instance."""
        with patch.object(KalshiClient, '_load_private_key', return_value=None):
            self.client = KalshiClient()

    def test_orderbook_with_both_sides(self):
        """Test quote computation when both YES and NO bids are present."""
        orderbook = {
            "orderbook": {
                "yes": [[55, 10], [54, 5]],  # YES bids at 55c and 54c
                "no": [[45, 8], [44, 3]],    # NO bids at 45c and 44c
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 55)
        self.assertEqual(quotes["best_no_bid"], 45)
        # YES ask = 100 - best_no_bid = 100 - 45 = 55
        self.assertEqual(quotes["best_yes_ask"], 55)
        # NO ask = 100 - best_yes_bid = 100 - 55 = 45
        self.assertEqual(quotes["best_no_ask"], 45)
        # Mid = (yes_bid + yes_ask) / 2 = (55 + 55) / 2 = 55
        self.assertEqual(quotes["mid_price"], 55)

    def test_orderbook_with_spread(self):
        """Test quote computation with a realistic spread."""
        orderbook = {
            "orderbook": {
                "yes": [[52, 10]],  # YES bid at 52c
                "no": [[46, 8]],    # NO bid at 46c
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 52)
        self.assertEqual(quotes["best_no_bid"], 46)
        # YES ask = 100 - 46 = 54
        self.assertEqual(quotes["best_yes_ask"], 54)
        # NO ask = 100 - 52 = 48
        self.assertEqual(quotes["best_no_ask"], 48)
        # Mid = (52 + 54) / 2 = 53
        self.assertEqual(quotes["mid_price"], 53)

    def test_empty_orderbook(self):
        """Test that empty orderbook returns None for all fields."""
        orderbook = {
            "orderbook": {
                "yes": [],
                "no": [],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertIsNone(quotes["best_yes_bid"])
        self.assertIsNone(quotes["best_yes_ask"])
        self.assertIsNone(quotes["best_no_bid"])
        self.assertIsNone(quotes["best_no_ask"])
        self.assertIsNone(quotes["mid_price"])

    def test_only_yes_bids(self):
        """Test orderbook with only YES bids (no NO bids).

        With the new inference logic, YES ask should be inferred even without NO bids.
        """
        orderbook = {
            "orderbook": {
                "yes": [[60, 10]],
                "no": [],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 60)
        self.assertIsNone(quotes["best_no_bid"])
        # YES ask should be inferred (60 + 1 = 61) since NO side is empty
        self.assertEqual(quotes["best_yes_ask"], 61)
        # NO ask = 100 - 60 = 40
        self.assertEqual(quotes["best_no_ask"], 40)
        # Mid should be computed from YES bid/ask
        self.assertEqual(quotes["mid_price"], (60 + 61) // 2)

    def test_only_no_bids(self):
        """Test orderbook with only NO bids (no YES bids).

        With the new inference logic, NO ask should be inferred even without YES bids.
        """
        orderbook = {
            "orderbook": {
                "yes": [],
                "no": [[40, 10]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertIsNone(quotes["best_yes_bid"])
        self.assertEqual(quotes["best_no_bid"], 40)
        # YES ask = 100 - 40 = 60
        self.assertEqual(quotes["best_yes_ask"], 60)
        # NO ask should be inferred (40 + 1 = 41) since YES side is empty
        self.assertEqual(quotes["best_no_ask"], 41)
        # Mid should be computed from NO bid/ask: 100 - ((40 + 41) // 2)
        expected_mid = 100 - ((40 + 41) // 2)
        self.assertEqual(quotes["mid_price"], expected_mid)

    def test_error_handling(self):
        """Test that errors in get_orderbook are caught and return None values."""
        with patch.object(self.client, 'get_orderbook', side_effect=Exception("API Error")):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertIsNone(quotes["best_yes_bid"])
        self.assertIsNone(quotes["best_yes_ask"])
        self.assertIsNone(quotes["best_no_bid"])
        self.assertIsNone(quotes["best_no_ask"])
        self.assertIsNone(quotes["mid_price"])


class TestSuggestLimitPriceWithOrderbook(unittest.TestCase):
    """Test suggest_limit_price works with orderbook-based field names."""

    def test_suggest_limit_price_with_new_field_names(self):
        """Test that suggest_limit_price works with best_yes_bid/best_yes_ask."""
        market = {
            "best_yes_bid": 52,
            "best_yes_ask": 54,
            "best_no_bid": 46,
            "best_no_ask": 48,
        }

        # YES side: should suggest between bid+2 and ask
        yes_price = suggest_limit_price(market, "yes")
        self.assertGreaterEqual(yes_price, 52)
        self.assertLessEqual(yes_price, 54)

        # NO side: should suggest between bid+2 and ask
        no_price = suggest_limit_price(market, "no")
        self.assertGreaterEqual(no_price, 46)
        self.assertLessEqual(no_price, 48)

    def test_suggest_limit_price_with_old_field_names(self):
        """Test that suggest_limit_price still works with old yes_bid/yes_ask."""
        market = {
            "yes_bid": 52,
            "yes_ask": 54,
            "no_bid": 46,
            "no_ask": 48,
        }

        # YES side
        yes_price = suggest_limit_price(market, "yes")
        self.assertGreaterEqual(yes_price, 52)
        self.assertLessEqual(yes_price, 54)

        # NO side
        no_price = suggest_limit_price(market, "no")
        self.assertGreaterEqual(no_price, 46)
        self.assertLessEqual(no_price, 48)

    def test_suggest_limit_price_prefers_new_field_names(self):
        """Test that new field names take precedence when both are present."""
        market = {
            "best_yes_bid": 60,
            "best_yes_ask": 62,
            "yes_bid": 50,  # Old value - should be ignored
            "yes_ask": 52,  # Old value - should be ignored
        }

        yes_price = suggest_limit_price(market, "yes")
        # Should use new values (60-62 range), not old values (50-52 range)
        self.assertGreaterEqual(yes_price, 60)
        self.assertLessEqual(yes_price, 62)


class TestOneSidedOrderbooks(unittest.TestCase):
    """Test handling of one-sided orderbooks with inference logic."""

    def setUp(self):
        """Create a mock KalshiClient instance."""
        with patch.object(KalshiClient, '_load_private_key', return_value=None):
            self.client = KalshiClient()

    def test_only_yes_bids_with_inference(self):
        """Test that YES ask is inferred when only YES bids present."""
        orderbook = {
            "orderbook": {
                "yes": [[60, 10]],  # YES bid at 60c
                "no": [],           # No NO bids
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # YES bid should be extracted
        self.assertEqual(quotes["best_yes_bid"], 60)
        # YES ask should be inferred (60 + 1 = 61)
        self.assertEqual(quotes["best_yes_ask"], 61)
        # NO bid should be None
        self.assertIsNone(quotes["best_no_bid"])
        # NO ask should be computed from YES bid: 100 - 60 = 40
        self.assertEqual(quotes["best_no_ask"], 40)
        # Mid should be computed from YES bid/ask
        self.assertEqual(quotes["mid_price"], (60 + 61) // 2)

    def test_only_no_bids_with_inference(self):
        """Test that NO ask is inferred when only NO bids present."""
        orderbook = {
            "orderbook": {
                "yes": [],           # No YES bids
                "no": [[40, 10]],    # NO bid at 40c
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # NO bid should be extracted
        self.assertEqual(quotes["best_no_bid"], 40)
        # NO ask should be inferred (40 + 1 = 41)
        self.assertEqual(quotes["best_no_ask"], 41)
        # YES bid should be None
        self.assertIsNone(quotes["best_yes_bid"])
        # YES ask should be computed from NO bid: 100 - 40 = 60
        self.assertEqual(quotes["best_yes_ask"], 60)
        # Mid should be computed from NO bid/ask: 100 - ((40 + 41) // 2)
        expected_mid = 100 - ((40 + 41) // 2)
        self.assertEqual(quotes["mid_price"], expected_mid)

    def test_yes_dollars_format_with_strings(self):
        """Test parsing of yes_dollars/no_dollars format with string prices."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.55", "10"], ["0.54", "5"]],  # String format
                "no_dollars": [["0.45", "8"]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Should parse "0.55" as 55 cents
        self.assertEqual(quotes["best_yes_bid"], 55)
        # Should parse "0.45" as 45 cents
        self.assertEqual(quotes["best_no_bid"], 45)
        # YES ask = 100 - 45 = 55
        self.assertEqual(quotes["best_yes_ask"], 55)
        # NO ask = 100 - 55 = 45
        self.assertEqual(quotes["best_no_ask"], 45)

    def test_yes_dollars_one_sided_with_inference(self):
        """Test yes_dollars format with only one side present."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.65", "15"]],
                "no_dollars": [],  # Empty NO side
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # YES bid from yes_dollars
        self.assertEqual(quotes["best_yes_bid"], 65)
        # YES ask inferred (65 + 1 = 66)
        self.assertEqual(quotes["best_yes_ask"], 66)
        # NO bid should be None
        self.assertIsNone(quotes["best_no_bid"])
        # NO ask from YES bid
        self.assertEqual(quotes["best_no_ask"], 35)  # 100 - 65

    def test_fallback_to_standard_format(self):
        """Test that standard format is used when orderbook_fp is not present."""
        orderbook = {
            "orderbook": {
                "yes": [[52, 10]],
                "no": [[48, 8]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 52)
        self.assertEqual(quotes["best_no_bid"], 48)
        self.assertEqual(quotes["best_yes_ask"], 52)  # 100 - 48
        self.assertEqual(quotes["best_no_ask"], 48)  # 100 - 52

    def test_yes_dollars_fp_format(self):
        """Test parsing of the new yes_dollars_fp / no_dollars_fp fixed-point fields."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.8500", "300.00"], ["0.8400", "150.00"]],
                "no_dollars_fp": [["0.1400", "200.00"]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # "0.8500" -> 85 cents
        self.assertEqual(quotes["best_yes_bid"], 85)
        # "0.1400" -> 14 cents
        self.assertEqual(quotes["best_no_bid"], 14)
        # YES ask = 100 - 14 = 86
        self.assertEqual(quotes["best_yes_ask"], 86)
        # NO ask = 100 - 85 = 15
        self.assertEqual(quotes["best_no_ask"], 15)
        # Mid = (85 + 86) // 2 = 85
        self.assertEqual(quotes["mid_price"], (85 + 86) // 2)

    def test_yes_dollars_fp_preferred_over_yes_dollars(self):
        """Test that yes_dollars_fp takes priority over yes_dollars when both are present."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.7000", "100.00"]],
                "no_dollars_fp": [["0.2500", "50.00"]],
                "yes_dollars": [["0.5000", "10"]],  # Should be ignored
                "no_dollars": [["0.4500", "5"]],    # Should be ignored
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Should use yes_dollars_fp values (70, 25), not yes_dollars values (50, 45)
        self.assertEqual(quotes["best_yes_bid"], 70)
        self.assertEqual(quotes["best_no_bid"], 25)

    def test_yes_dollars_fp_fallback_to_yes_dollars(self):
        """Test fallback to yes_dollars when yes_dollars_fp is absent."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.6000", "20"]],
                "no_dollars": [["0.3500", "10"]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 60)
        self.assertEqual(quotes["best_no_bid"], 35)

    def test_yes_dollars_fp_one_sided(self):
        """Test yes_dollars_fp with only YES side present uses 100 - bid for NO ask."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.7500", "500.00"]],
                "no_dollars_fp": [],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # YES bid from yes_dollars_fp
        self.assertEqual(quotes["best_yes_bid"], 75)
        # NO bid is absent
        self.assertIsNone(quotes["best_no_bid"])
        # NO ask = 100 - best_yes_bid = 100 - 75 = 25
        self.assertEqual(quotes["best_no_ask"], 25)
        # YES ask inferred = 75 + 1 = 76
        self.assertEqual(quotes["best_yes_ask"], 76)

    def test_rounding_in_fp_conversion(self):
        """Test that dollar string to cents conversion rounds correctly."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars_fp": [["0.3000", "100.00"]],
                "no_dollars_fp": [["0.6500", "100.00"]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # "0.3000" must round to 30, not 29 (float truncation bug guard)
        self.assertEqual(quotes["best_yes_bid"], 30)
        # "0.6500" -> 65
        self.assertEqual(quotes["best_no_bid"], 65)


class TestFixedPointMigration(unittest.TestCase):
    """Tests for fixed-point migration: top-level _dollars and WebSocket-wrapped _dollars fields."""

    def setUp(self):
        """Create a mock KalshiClient instance."""
        with patch.object(KalshiClient, '_load_private_key', return_value=None):
            self.client = KalshiClient()

    def test_top_level_yes_dollars_no_dollars(self):
        """Test that top-level yes_dollars/no_dollars fields are parsed (step 5 in priority chain)."""
        orderbook = {
            "yes_dollars": [["0.5500", "10"], ["0.5400", "5"]],
            "no_dollars": [["0.4500", "8"]],
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 55)
        self.assertEqual(quotes["best_no_bid"], 45)
        self.assertEqual(quotes["best_yes_ask"], 55)   # 100 - 45
        self.assertEqual(quotes["best_no_ask"], 45)    # 100 - 55
        self.assertEqual(quotes["mid_price"], 55)

    def test_top_level_yes_dollars_one_sided(self):
        """Test top-level yes_dollars with only YES side present uses inference."""
        orderbook = {
            "yes_dollars": [["0.6500", "15"]],
            "no_dollars": [],
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 65)
        self.assertIsNone(quotes["best_no_bid"])
        self.assertEqual(quotes["best_yes_ask"], 66)   # inferred: 65 + 1
        self.assertEqual(quotes["best_no_ask"], 35)    # 100 - 65

    def test_websocket_wrapped_yes_dollars(self):
        """Test that WebSocket-wrapped yes_dollars/no_dollars (nested under 'orderbook') is parsed."""
        # Simulate what bot.py does: wraps ws_orderbook in {"orderbook": ws_orderbook}
        ws_orderbook = {
            "yes_dollars": [["0.7000", "20"]],
            "no_dollars": [["0.2800", "12"]],
        }
        orderbook = {"orderbook": ws_orderbook}

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 70)
        self.assertEqual(quotes["best_no_bid"], 28)
        self.assertEqual(quotes["best_yes_ask"], 72)   # 100 - 28
        self.assertEqual(quotes["best_no_ask"], 30)    # 100 - 70

    def test_websocket_wrapped_yes_dollars_fp(self):
        """Test that WebSocket-wrapped yes_dollars_fp/no_dollars_fp is parsed."""
        ws_orderbook = {
            "yes_dollars_fp": [["0.8200", "300.00"]],
            "no_dollars_fp": [["0.1600", "150.00"]],
        }
        orderbook = {"orderbook": ws_orderbook}

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 82)
        self.assertEqual(quotes["best_no_bid"], 16)
        self.assertEqual(quotes["best_yes_ask"], 84)   # 100 - 16
        self.assertEqual(quotes["best_no_ask"], 18)    # 100 - 82

    def test_orderbook_fp_preferred_over_top_level_dollars(self):
        """Test that orderbook_fp fields take priority over top-level yes_dollars."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.6000", "10"]],
                "no_dollars": [["0.3500", "8"]],
            },
            "yes_dollars": [["0.4000", "5"]],   # lower priority – should be ignored
            "no_dollars": [["0.5500", "3"]],    # lower priority – should be ignored
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Should use orderbook_fp values (60, 35), not top-level values (40, 55)
        self.assertEqual(quotes["best_yes_bid"], 60)
        self.assertEqual(quotes["best_no_bid"], 35)

    def test_websocket_wrapped_dollars_preferred_over_top_level_legacy(self):
        """Test that _dollars inside 'orderbook' wrapper beats top-level legacy 'yes'."""
        orderbook = {
            "orderbook": {
                "yes_dollars": [["0.5800", "20"]],
                "no_dollars": [["0.4000", "15"]],
                "yes": [[40, 5]],   # lower priority – should be ignored
                "no": [[55, 5]],    # lower priority – should be ignored
            },
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Should use yes_dollars (58, 40), not legacy yes (40, 55)
        self.assertEqual(quotes["best_yes_bid"], 58)
        self.assertEqual(quotes["best_no_bid"], 40)


class TestAscendingSortOrderbookFp(unittest.TestCase):
    """Test that ascending-sorted orderbook_fp arrays are parsed correctly.

    Kalshi API returns orderbook_fp.yes_dollars / no_dollars sorted ascending
    by price. The best (highest) bid is the LAST element, not the first.
    """

    def setUp(self):
        with patch.object(KalshiClient, '_load_private_key', return_value=None):
            self.client = KalshiClient()

    def test_ascending_orderbook_fp_both_sides(self):
        """Realistic ascending-sorted orderbook_fp with both YES and NO bids."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [
                    ["0.42", "5"],
                    ["0.44", "10"],
                    ["0.46", "15"],
                ],
                "no_dollars": [
                    ["0.50", "8"],
                    ["0.52", "12"],
                    ["0.54", "20"],
                ],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Best YES bid = last entry = 46c, size = 15
        self.assertEqual(quotes["best_yes_bid"], 46)
        self.assertEqual(quotes["best_yes_bid_size"], 15)
        # Best NO bid = last entry = 54c, size = 20
        self.assertEqual(quotes["best_no_bid"], 54)
        self.assertEqual(quotes["best_no_bid_size"], 20)
        # Derived asks: yes_ask = 100 - 54 = 46, no_ask = 100 - 46 = 54
        self.assertEqual(quotes["best_yes_ask"], 46)
        self.assertEqual(quotes["best_no_ask"], 54)
        # Mid = (46 + 46) // 2 = 46
        self.assertEqual(quotes["mid_price"], 46)

    def test_ascending_orderbook_fp_single_entry_each(self):
        """Single entry on each side, ascending order trivially works."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.60", "25"]],
                "no_dollars": [["0.38", "30"]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 60)
        self.assertEqual(quotes["best_yes_bid_size"], 25)
        self.assertEqual(quotes["best_no_bid"], 38)
        self.assertEqual(quotes["best_no_bid_size"], 30)
        # Derived asks
        self.assertEqual(quotes["best_yes_ask"], 62)  # 100 - 38
        self.assertEqual(quotes["best_no_ask"], 40)   # 100 - 60

    def test_ascending_orderbook_fp_yes_only(self):
        """Only YES side present; NO ask derived from YES bid."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [["0.30", "5"], ["0.35", "10"], ["0.40", "20"]],
                "no_dollars": [],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid"], 40)
        self.assertEqual(quotes["best_yes_bid_size"], 20)
        self.assertIsNone(quotes["best_no_bid"])
        self.assertEqual(quotes["best_no_bid_size"], 0)
        # NO ask = 100 - 40 = 60
        self.assertEqual(quotes["best_no_ask"], 60)
        # YES ask inferred (40 + 1 = 41) since NO side is empty
        self.assertEqual(quotes["best_yes_ask"], 41)

    def test_ascending_orderbook_fp_no_only(self):
        """Only NO side present; YES ask derived from NO bid."""
        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [],
                "no_dollars": [["0.20", "5"], ["0.25", "10"], ["0.30", "15"]],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertIsNone(quotes["best_yes_bid"])
        self.assertEqual(quotes["best_yes_bid_size"], 0)
        self.assertEqual(quotes["best_no_bid"], 30)
        self.assertEqual(quotes["best_no_bid_size"], 15)
        # YES ask = 100 - 30 = 70
        self.assertEqual(quotes["best_yes_ask"], 70)
        # NO ask inferred (30 + 1 = 31) since YES side is empty
        self.assertEqual(quotes["best_no_ask"], 31)

    def test_bid_sizes_in_error_fallback(self):
        """Error fallback should include zero bid sizes."""
        with patch.object(self.client, 'get_orderbook', side_effect=Exception("API Error")):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["best_yes_bid_size"], 0)
        self.assertEqual(quotes["best_no_bid_size"], 0)


class TestQuotesFromOrderbookAscending(unittest.TestCase):
    """Test bot.py _quotes_from_orderbook with ascending-sorted REST data."""

    def test_ascending_rest_orderbook_fp(self):
        """REST API orderbook_fp format with ascending sort order."""
        from bot import _quotes_from_orderbook

        orderbook = {
            "orderbook_fp": {
                "yes_dollars": [
                    ["0.42", "5"],
                    ["0.44", "10"],
                    ["0.46", "15"],
                ],
                "no_dollars": [
                    ["0.50", "8"],
                    ["0.52", "12"],
                    ["0.54", "20"],
                ],
            }
        }

        quotes = _quotes_from_orderbook(orderbook)

        self.assertEqual(quotes["best_yes_bid"], 46)
        self.assertEqual(quotes["best_yes_bid_size"], 15)
        self.assertEqual(quotes["best_no_bid"], 54)
        self.assertEqual(quotes["best_no_bid_size"], 20)
        self.assertEqual(quotes["best_yes_ask"], 46)   # 100 - 54
        self.assertEqual(quotes["best_no_ask"], 54)    # 100 - 46

    def test_descending_websocket_orderbook(self):
        """WebSocket normalized format with descending sort order."""
        from bot import _quotes_from_orderbook

        orderbook = {
            "orderbook": {
                "yes": [[55, 10], [54, 5], [53, 3]],
                "no": [[45, 8], [44, 3]],
            }
        }

        quotes = _quotes_from_orderbook(orderbook)

        # max() should correctly find 55 regardless of sort order
        self.assertEqual(quotes["best_yes_bid"], 55)
        self.assertEqual(quotes["best_yes_bid_size"], 10)
        self.assertEqual(quotes["best_no_bid"], 45)
        self.assertEqual(quotes["best_no_bid_size"], 8)
        self.assertEqual(quotes["best_yes_ask"], 55)   # 100 - 45
        self.assertEqual(quotes["best_no_ask"], 45)    # 100 - 55

    def test_mixed_format_still_finds_max(self):
        """Even with non-monotonic entries, max() finds the best bid."""
        from bot import _quotes_from_orderbook

        orderbook = {
            "orderbook": {
                "yes": [[40, 3], [55, 7], [48, 5]],
                "no": [[30, 2], [45, 6], [35, 4]],
            }
        }

        quotes = _quotes_from_orderbook(orderbook)

        self.assertEqual(quotes["best_yes_bid"], 55)
        self.assertEqual(quotes["best_yes_bid_size"], 7)
        self.assertEqual(quotes["best_no_bid"], 45)
        self.assertEqual(quotes["best_no_bid_size"], 6)


class _RaisingHandler(logging.Handler):
    def emit(self, record):
        # Force message formatting; raises if %-format placeholders mismatch args.
        record.getMessage()


class TestBotLoggingDoesNotCrashOnNone(unittest.TestCase):
    def test_active_market_log_safe_with_none_orderbook_side(self):
        from bot import _quotes_from_orderbook
        from kalshi_money import fmt_cents

        # NO side empty → best_no_bid=None; this must not crash logging formatting.
        orderbook = {"orderbook": {"yes": [[60, 10]], "no": []}}
        quotes = _quotes_from_orderbook(orderbook)

        self.assertIsNone(quotes["best_no_bid"])

        logger = logging.getLogger("bot")
        handler = _RaisingHandler()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.INFO)
        try:
            logger.info(
                "Active market: %s | last=%sc yes=%sc/%sc no=%sc/%sc mid=%sc (from orderbook)",
                "TEST-TICKER",
                fmt_cents(100),
                fmt_cents(quotes.get("best_yes_bid")),
                fmt_cents(quotes.get("best_yes_ask")),
                fmt_cents(quotes.get("best_no_bid")),
                fmt_cents(quotes.get("best_no_ask")),
                fmt_cents(quotes.get("mid_price")),
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)


if __name__ == "__main__":
    unittest.main()
