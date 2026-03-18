"""
test_orderbook_pricing.py - Tests for orderbook-based price fetching.

Verifies that get_market_quotes correctly computes best bid/ask prices
from the orderbook structure and that strategy functions work with both
old and new field names.
"""
import unittest
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


if __name__ == "__main__":
    unittest.main()
