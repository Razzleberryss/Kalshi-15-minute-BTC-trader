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
        """Test orderbook with only YES bids (no NO bids)."""
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
        # YES ask cannot be computed without NO bids
        self.assertIsNone(quotes["best_yes_ask"])
        # NO ask = 100 - 60 = 40
        self.assertEqual(quotes["best_no_ask"], 40)
        # Mid cannot be computed without YES ask
        self.assertIsNone(quotes["mid_price"])

    def test_only_no_bids(self):
        """Test orderbook with only NO bids (no YES bids)."""
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
        # NO ask cannot be computed without YES bids
        self.assertIsNone(quotes["best_no_ask"])
        # Mid cannot be computed without YES bid
        self.assertIsNone(quotes["mid_price"])

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


if __name__ == "__main__":
    unittest.main()
