"""
test_liquidity_filters.py - Tests for orderbook depth/spread liquidity filters.

Verifies that:
1. Spread and depth are correctly computed from orderbooks
2. Liquidity filters block trades when spread is too wide (MAX_SLIPPAGE)
3. Liquidity filters block trades when YES depth is too thin (MIN_YES_DEPTH)
4. Ghost markets (0¢/100¢ spread) are always skipped
"""
import importlib
import os
import unittest
import unittest.mock
from unittest.mock import MagicMock, patch

from kalshi_client import KalshiClient
from strategy import generate_signal, _extract_best_bid_depth
import config


class TestSpreadAndDepthCalculations(unittest.TestCase):
    """Test spread and depth calculations in get_market_quotes."""

    def setUp(self):
        """Create a mock KalshiClient instance."""
        with patch.object(KalshiClient, '_load_private_key', return_value=None):
            self.client = KalshiClient()

    def test_spread_calculation(self):
        """Test that spread is correctly calculated as (ask - bid) / 100."""
        orderbook = {
            "orderbook": {
                "yes": [[52, 10]],  # YES bid at 52c
                "no": [[46, 8]],    # NO bid at 46c
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # YES ask = 100 - 46 = 54c
        # Spread = (54 - 52) / 100 = 0.02
        self.assertEqual(quotes["spread"], 0.02)

    def test_depth_calculation_near_mid(self):
        """Test that depth counts contracts within DEPTH_BAND of mid price."""
        # Save original DEPTH_BAND
        original_depth_band = config.DEPTH_BAND
        config.DEPTH_BAND = 0.05  # 5 cents

        orderbook = {
            "orderbook": {
                # best_yes_bid=53, best_no_bid=47
                # best_yes_ask=100-47=53, best_no_ask=100-53=47
                # Mid = (53+53)/2 = 53c, so depth band is 48c-58c
                "yes": [
                    [53, 20],  # Within band (53c)
                    [52, 15],  # Within band (52c)
                    [48, 10],  # Within band (48c)
                    [40, 100],  # Outside band (too low, 40c < 48c)
                ],
                "no": [
                    [47, 25],  # 47c - this is the bid, so it's the NO price
                    [46, 30],  # 46c
                    [60, 100],  # Outside band
                ],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Mid = (53+53)/2 = 53c
        # Depth band: 48c to 58c
        # YES depth: 53c (20) + 52c (15) + 48c (10) = 45
        # NO depth: we need to check which NO bids fall in the band
        # NO bid at 47c is within 48-58? No, it's below 48
        # Actually, let me recalculate: mid is 53, band is ±5, so 48-58
        # NO prices at 47, 46, 60: none are in 48-58 range
        self.assertEqual(quotes["yes_depth_near_mid"], 45)
        # NO: 47, 46, 60 - none in 48-58 range
        self.assertEqual(quotes["no_depth_near_mid"], 0)

        # Restore original DEPTH_BAND
        config.DEPTH_BAND = original_depth_band

    def test_empty_orderbook_depth(self):
        """Test that empty orderbook returns zero depth."""
        orderbook = {
            "orderbook": {
                "yes": [],
                "no": [],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["yes_depth_near_mid"], 0)
        self.assertEqual(quotes["no_depth_near_mid"], 0)
        self.assertIsNone(quotes["spread"])


class TestLiquidityFilters(unittest.TestCase):
    """Test that liquidity filters block trades appropriately."""

    def setUp(self):
        """Save original config values."""
        self.original_max_spread = config.MAX_SPREAD
        self.original_min_yes_depth = config.MIN_YES_DEPTH
        self.original_min_no_depth = config.MIN_NO_DEPTH
        self.original_min_confidence = config.MIN_CONFIDENCE
        self.original_max_slippage = config.MAX_SLIPPAGE
        self.original_max_price_deviation = config.MAX_PRICE_DEVIATION

    def tearDown(self):
        """Restore original config values."""
        config.MAX_SPREAD = self.original_max_spread
        config.MIN_YES_DEPTH = self.original_min_yes_depth
        config.MIN_NO_DEPTH = self.original_min_no_depth
        config.MIN_CONFIDENCE = self.original_min_confidence
        config.MAX_SLIPPAGE = self.original_max_slippage
        config.MAX_PRICE_DEVIATION = self.original_max_price_deviation

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_wide_spread_blocks_trade(self, mock_skew, mock_momentum):
        """Test that trades are blocked when spread (from orderbook) exceeds MAX_SLIPPAGE."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        config.MIN_CONFIDENCE = 0.001
        # Set MAX_SLIPPAGE to 0.05 (5 cents); orderbook spread will be 10 cents
        config.MAX_SLIPPAGE = 0.05

        # Market with wide spread: YES bid=45, NO bid=45 → YES ask=55, spread=0.10
        market = {
            "best_yes_bid": 45,
            "best_yes_ask": 55,
            "best_no_bid": 45,
            "best_no_ask": 55,
        }

        orderbook = {
            "orderbook": {"yes": [[45, 100]], "no": [[45, 100]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return None because orderbook spread (0.10) > MAX_SLIPPAGE (0.05)
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_low_yes_depth_blocks_trade(self, mock_skew, mock_momentum):
        """Test that trades are blocked when YES depth (from orderbook) is below MIN_YES_DEPTH."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        config.MIN_CONFIDENCE = 0.001
        config.MAX_SLIPPAGE = 0.20  # Wide enough to not block
        config.MIN_YES_DEPTH = 100  # Require 100 contracts

        # Orderbook has only 30 YES contracts at best bid
        market = {
            "best_yes_bid": 48,
            "best_yes_ask": 52,
            "best_no_bid": 48,
            "best_no_ask": 52,
        }

        orderbook = {
            "orderbook": {"yes": [[48, 30]], "no": [[48, 100]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return None: YES depth from orderbook (30) < MIN_YES_DEPTH (100)
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_low_no_depth_does_not_block_trade(self, mock_skew, mock_momentum):
        """NO depth is no longer a blocking filter; only YES depth matters."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        config.MIN_CONFIDENCE = 0.001
        config.MAX_SLIPPAGE = 0.20
        config.MIN_YES_DEPTH = 5   # Low threshold → YES depth will satisfy this
        config.MIN_NO_DEPTH = 100  # High threshold, but no longer enforced

        # YES depth = 100 (satisfies MIN_YES_DEPTH=5), NO depth = 1 (would have
        # failed the old MIN_NO_DEPTH=100 check, but that check is removed).
        market = {
            "best_yes_bid": 48,
            "best_yes_ask": 52,
            "best_no_bid": 48,
            "best_no_ask": 52,
        }

        orderbook = {
            "orderbook": {"yes": [[48, 100]], "no": [[48, 1]]}
        }

        # generate_signal is not mocked for decide_trade_fee_aware here — it may
        # return None due to the fee/band filters, but it must NOT return None
        # solely because NO depth is low.  We verify by asserting the signal
        # is not blocked at the NO-depth stage (price/fee filters may still kick in).
        # We mock decide_trade to force a tradeable signal.
        with unittest.mock.patch('strategy.decide_trade_fee_aware', return_value=("BUY_YES", 5)):
            config.MAX_PRICE_DEVIATION = 0.50
            signal = generate_signal(market, orderbook)

        # Signal should not be None because NO depth is not a blocking filter any more
        self.assertIsNotNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    @patch('strategy.decide_trade_fee_aware')
    def test_good_liquidity_allows_trade(self, mock_decide, mock_skew, mock_momentum):
        """Test that trades proceed when liquidity is sufficient."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3
        mock_decide.return_value = ("BUY_YES", 5)

        config.MIN_CONFIDENCE = 0.001
        config.MAX_SPREAD = 0.10
        config.MIN_YES_DEPTH = 50
        config.MIN_NO_DEPTH = 50
        config.MAX_SLIPPAGE = 0.10  # Allow up to 10 cent spread for MAX_SLIPPAGE filter
        config.MAX_PRICE_DEVIATION = 0.50  # Allow wide deviations

        # Market with good liquidity
        market = {
            "best_yes_bid": 48,
            "best_yes_ask": 52,
            "best_no_bid": 48,
            "best_no_ask": 52,
            "spread": 0.04,  # Within MAX_SPREAD
            "yes_depth_near_mid": 100,  # Above MIN_YES_DEPTH
            "no_depth_near_mid": 100,  # Above MIN_NO_DEPTH
        }

        orderbook = {
            "orderbook": {"yes": [[48, 100]], "no": [[48, 100]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return a signal (not None) because liquidity is good
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "yes")
        self.assertEqual(signal.size, 5)


class TestEmptyWebSocketOrderbook(unittest.TestCase):
    """Test that empty WebSocket orderbooks are handled gracefully."""

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_empty_websocket_orderbook_returns_no_trade(self, mock_skew, mock_momentum):
        """Test that empty WebSocket orderbook results in NO_TRADE, not an exception."""
        # Set up mocks for a bullish signal
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        # Market data with all None values (as would happen with empty orderbook)
        market = {
            "best_yes_bid": None,
            "best_yes_ask": None,
            "best_no_bid": None,
            "best_no_ask": None,
            "mid_price": None,
            "spread": None,
            "yes_depth_near_mid": 0,
            "no_depth_near_mid": 0,
        }

        # Empty orderbook structure (as returned by WebSocket with no data)
        orderbook = {
            "orderbook": {
                "yes": [],
                "no": []
            }
        }

        # This should NOT raise an exception, but should return None (NO_TRADE)
        signal = generate_signal(market, orderbook)

        # Verify no exception was raised and NO_TRADE was returned
        self.assertIsNone(signal)


class TestExtractBestBidDepth(unittest.TestCase):
    """Unit tests for _extract_best_bid_depth()."""

    def test_integer_cents_format(self):
        """Handles [[price_cents_int, count]] (WebSocket normalized format)."""
        price, count = _extract_best_bid_depth([[55, 10], [54, 5]])
        self.assertAlmostEqual(price, 0.55)
        self.assertEqual(count, 10)

    def test_string_dollar_format(self):
        """Handles [["0.55", "10"]] (REST API yes_dollars format)."""
        price, count = _extract_best_bid_depth([["0.55", "10"], ["0.54", "5"]])
        self.assertAlmostEqual(price, 0.55)
        self.assertEqual(count, 10)

    def test_float_dollar_format(self):
        """Handles [[0.55, 10]] (float dollar format)."""
        price, count = _extract_best_bid_depth([[0.55, 10]])
        self.assertAlmostEqual(price, 0.55)
        self.assertEqual(count, 10)

    def test_empty_array(self):
        """Returns (None, 0) for empty input."""
        price, count = _extract_best_bid_depth([])
        self.assertIsNone(price)
        self.assertEqual(count, 0)

    def test_none_input(self):
        """Returns (None, 0) for None input."""
        price, count = _extract_best_bid_depth(None)
        self.assertIsNone(price)
        self.assertEqual(count, 0)

    def test_skips_zero_count_entries(self):
        """Skips entries with count=0."""
        price, count = _extract_best_bid_depth([[55, 0], [54, 8]])
        self.assertAlmostEqual(price, 0.54)
        self.assertEqual(count, 8)

    def test_picks_highest_price(self):
        """Returns the highest-priced level (best bid) regardless of order."""
        price, count = _extract_best_bid_depth([[40, 3], [55, 7], [48, 5]])
        self.assertAlmostEqual(price, 0.55)
        self.assertEqual(count, 7)

    def test_dict_entry_format(self):
        """Handles dict entries with price/size keys."""
        price, count = _extract_best_bid_depth([{"price": 55, "size": 12}])
        self.assertAlmostEqual(price, 0.55)
        self.assertEqual(count, 12)


class TestGhostMarketDetection(unittest.TestCase):
    """Verify that ghost markets (0¢/100¢ books) are always skipped."""

    def setUp(self):
        self.original_min_confidence = config.MIN_CONFIDENCE
        self.original_max_slippage = config.MAX_SLIPPAGE
        self.original_min_yes_depth = config.MIN_YES_DEPTH

    def tearDown(self):
        config.MIN_CONFIDENCE = self.original_min_confidence
        config.MAX_SLIPPAGE = self.original_max_slippage
        config.MIN_YES_DEPTH = self.original_min_yes_depth

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_ghost_book_blocked(self, mock_skew, mock_momentum):
        """YES bid=1¢, NO bid=1¢ → YES ask=99¢ → spread=0.98 ≈ ghost market."""
        mock_momentum.return_value = 0.8
        mock_skew.return_value = 0.5
        config.MIN_CONFIDENCE = 0.001
        config.MAX_SLIPPAGE = 1.0      # Very permissive — should still block
        config.MIN_YES_DEPTH = 1

        market = {"best_yes_bid": 1, "best_yes_ask": 99}
        # YES bid=0.01, NO bid=0.01 → YES ask=0.99, spread=0.98
        orderbook = {"orderbook": {"yes": [[1, 5]], "no": [[1, 5]]}}

        signal = generate_signal(market, orderbook)
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_zero_yes_bid_blocked(self, mock_skew, mock_momentum):
        """YES bid=0 is treated as ghost/dead book and skipped by spread."""
        mock_momentum.return_value = 0.8
        mock_skew.return_value = 0.5
        config.MIN_CONFIDENCE = 0.001
        config.MAX_SLIPPAGE = 0.08
        config.MIN_YES_DEPTH = 1

        # YES bid=0, NO bid=0 → spread would be ~1.0
        market = {"best_yes_bid": 1, "best_yes_ask": 99}
        orderbook = {"orderbook": {"yes": [[0, 5]], "no": [[0, 5]]}}

        signal = generate_signal(market, orderbook)
        # spread falls back to (99-1)/100=0.98 > 0.08, so trade is skipped
        self.assertIsNone(signal)


class TestOrderbookDepthComputedDirectly(unittest.TestCase):
    """Verify YES depth is read from the raw orderbook, not from market dict fields."""

    def setUp(self):
        self.original_min_yes_depth = config.MIN_YES_DEPTH
        self.original_max_slippage = config.MAX_SLIPPAGE
        self.original_min_confidence = config.MIN_CONFIDENCE
        self.original_max_price_deviation = config.MAX_PRICE_DEVIATION

    def tearDown(self):
        config.MIN_YES_DEPTH = self.original_min_yes_depth
        config.MAX_SLIPPAGE = self.original_max_slippage
        config.MIN_CONFIDENCE = self.original_min_confidence
        config.MAX_PRICE_DEVIATION = self.original_max_price_deviation

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_depth_from_orderbook_not_market_dict(self, mock_skew, mock_momentum):
        """Depth comes from orderbook, not market dict. Market dict has no depth fields."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3
        config.MIN_CONFIDENCE = 0.001
        config.MAX_SLIPPAGE = 0.20
        config.MIN_YES_DEPTH = 100   # Require 100 contracts
        config.MAX_PRICE_DEVIATION = 0.5

        # market dict deliberately has NO yes_depth_near_mid field
        market = {"best_yes_bid": 48, "best_yes_ask": 52}

        # Orderbook only has 30 contracts — should block
        orderbook = {"orderbook": {"yes": [[48, 30]], "no": [[48, 100]]}}
        signal = generate_signal(market, orderbook)
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    @patch('strategy.decide_trade_fee_aware')
    def test_sufficient_depth_from_orderbook_allows_trade(
        self, mock_decide, mock_skew, mock_momentum
    ):
        """When orderbook has enough depth, trade proceeds even without market dict depth."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3
        mock_decide.return_value = ("BUY_YES", 5)
        config.MIN_CONFIDENCE = 0.001
        config.MAX_SLIPPAGE = 0.20
        config.MIN_YES_DEPTH = 5    # Just 5 contracts required
        config.MAX_PRICE_DEVIATION = 0.5

        # market dict has NO yes_depth_near_mid
        market = {"best_yes_bid": 48, "best_yes_ask": 52}
        # Orderbook has 10 contracts > MIN_YES_DEPTH(5)
        orderbook = {"orderbook": {"yes": [[48, 10]], "no": [[48, 10]]}}
        signal = generate_signal(market, orderbook)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.size, 5)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_default_min_yes_depth_is_5(self, mock_skew, mock_momentum):
        """Default MIN_YES_DEPTH is 5 (reduced from 50 for more aggressive trading)."""
        env_backup = os.environ.pop("MIN_YES_DEPTH", None)
        try:
            import config as _cfg
            importlib.reload(_cfg)
            self.assertEqual(_cfg.MIN_YES_DEPTH, 5)
        finally:
            if env_backup is not None:
                os.environ["MIN_YES_DEPTH"] = env_backup
            importlib.reload(config)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_default_min_no_depth_is_5(self, mock_skew, mock_momentum):
        """Default MIN_NO_DEPTH is 5 (reduced from 50 for more aggressive trading)."""
        env_backup = os.environ.pop("MIN_NO_DEPTH", None)
        try:
            import config as _cfg
            importlib.reload(_cfg)
            self.assertEqual(_cfg.MIN_NO_DEPTH, 5)
        finally:
            if env_backup is not None:
                os.environ["MIN_NO_DEPTH"] = env_backup
            importlib.reload(config)


if __name__ == "__main__":
    unittest.main()
