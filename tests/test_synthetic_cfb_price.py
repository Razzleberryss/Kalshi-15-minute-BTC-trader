"""
tests/test_synthetic_cfb_price.py – Unit tests for synthetic_cfb_price module.

Covers:
- price parsing from markdown string with $66,870.79
- scrape helper success using mocked Firecrawl response
- scrape helper failure using mocked exception
- snapshot success with 5 mocked observations
- outlier rejection
- fewer than 3 valid sources returns ok=False
- confidence classification: high / medium / low
- immature window lowers confidence
- rolling buffer: eviction, average, sample_count
- empty / missing FIRECRAWL_API_KEY: no network, failed snapshot / observation
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synthetic_cfb_price import (
    PriceObservation,
    RollingSyntheticCfbBuffer,
    build_synthetic_cfb_snapshot,
    extract_price_usd,
    scrape_price_source,
    utc_now_iso,
    _apply_window_confidence_cap,
    _classify_confidence,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_obs(price: float | None, ok: bool = True, source_name: str = "TestSource") -> PriceObservation:
    return PriceObservation(
        source_name=source_name,
        source_url="https://example.com",
        price_usd=price,
        scraped_at=utc_now_iso(),
        ok=ok,
        error=None if ok else "mock error",
        raw_excerpt="",
    )


def _mock_scrape(prices: list[float | None]):
    """Return a side_effect function that yields each price in turn."""
    iter_prices = iter(prices)

    def _side_effect(api_key, source_name, source_url):
        try:
            p = next(iter_prices)
        except StopIteration:
            p = None
        ok = p is not None
        return PriceObservation(
            source_name=source_name,
            source_url=source_url,
            price_usd=p,
            scraped_at=utc_now_iso(),
            ok=ok,
            error=None if ok else "no price",
            raw_excerpt="",
        )

    return _side_effect


# ---------------------------------------------------------------------------
# utc_now_iso
# ---------------------------------------------------------------------------

class TestUtcNowIso(unittest.TestCase):
    def test_returns_iso_string(self):
        ts = utc_now_iso()
        self.assertIsInstance(ts, str)
        self.assertIn("T", ts)
        self.assertIn("+", ts)


# ---------------------------------------------------------------------------
# extract_price_usd
# ---------------------------------------------------------------------------

class TestExtractPriceUsd(unittest.TestCase):

    def test_parses_dollar_amount_with_commas(self):
        md = "Bitcoin price today: **$66,870.79** USD"
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 66870.79)

    def test_parses_dollar_amount_without_commas(self):
        md = "BTC is trading at $67000.00 right now."
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 67000.0)

    def test_ignores_non_btc_range_prices(self):
        # $100 is below the 1_000 minimum – should be ignored
        md = "Fee: $100  BTC price: $66,500.00"
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 66500.0)

    def test_returns_none_when_no_price_found(self):
        md = "No prices here, just text."
        result = extract_price_usd(md)
        self.assertIsNone(result)

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(extract_price_usd(""))

    def test_returns_none_for_none_input(self):
        # extract_price_usd should handle None gracefully (callers may pass None)
        self.assertIsNone(extract_price_usd(None))  # type: ignore[arg-type]

    def test_large_million_dollar_btc_price(self):
        md = "BTC all time high: $1,000,000.00"
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 1_000_000.0)


# ---------------------------------------------------------------------------
# scrape_price_source
# ---------------------------------------------------------------------------

class TestScrapePriceSource(unittest.TestCase):

    def _mock_firecrawl_result(self, price_text: str) -> dict:
        return {"markdown": f"Bitcoin current price: **{price_text}**"}

    def test_success_returns_parsed_price(self):
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = self._mock_firecrawl_result("$66,870.79")
        MockApp = MagicMock(return_value=mock_app)
        mock_firecrawl = MagicMock()
        mock_firecrawl.FirecrawlApp = MockApp
        with patch.dict("sys.modules", {"firecrawl": mock_firecrawl}):
            obs = scrape_price_source("test-key", "TestSource", "https://example.com")

        self.assertIsInstance(obs, PriceObservation)
        self.assertTrue(obs.ok)
        self.assertAlmostEqual(obs.price_usd, 66870.79)
        self.assertIsNone(obs.error)

    def test_empty_api_key_returns_ok_false_without_firecrawl(self):
        with patch.dict("sys.modules", {"firecrawl": MagicMock()}):
            obs = scrape_price_source("", "TestSource", "https://example.com")
        self.assertFalse(obs.ok)
        self.assertIsNone(obs.price_usd)
        self.assertIn("FIRECRAWL", obs.error or "")

    def test_failure_on_exception_returns_ok_false(self):
        """If Firecrawl raises an exception the helper returns ok=False, never raises."""
        with patch("builtins.__import__", side_effect=ImportError("firecrawl not installed")):
            obs = scrape_price_source("", "TestSource", "https://example.com")
        self.assertIsInstance(obs, PriceObservation)
        self.assertFalse(obs.ok)
        self.assertIsNotNone(obs.error)

    def test_failure_returns_valid_observation_structure(self):
        """Even on failure the returned object has the expected fields."""
        # Force ImportError so the test is hermetic (no real network call)
        with patch.dict("sys.modules", {"firecrawl": None}):
            obs = scrape_price_source("", "FailSource", "https://bad.url")
        self.assertEqual(obs.source_name, "FailSource")
        self.assertEqual(obs.source_url, "https://bad.url")
        self.assertIsNone(obs.price_usd)
        self.assertFalse(obs.ok)


# ---------------------------------------------------------------------------
# RollingSyntheticCfbBuffer
# ---------------------------------------------------------------------------

class TestRollingSyntheticCfbBuffer(unittest.TestCase):

    def _buf(self, window: int = 60) -> RollingSyntheticCfbBuffer:
        return RollingSyntheticCfbBuffer(window_seconds=window)

    def test_empty_buffer_average_is_none(self):
        buf = self._buf()
        self.assertIsNone(buf.average())

    def test_empty_buffer_sample_count_is_zero(self):
        buf = self._buf()
        self.assertEqual(buf.sample_count(), 0)

    def test_single_sample_average_equals_price(self):
        buf = self._buf()
        buf.append(66800.0)
        self.assertAlmostEqual(buf.average(), 66800.0)
        self.assertEqual(buf.sample_count(), 1)

    def test_multiple_samples_simple_mean(self):
        buf = self._buf()
        buf.append(66800.0)
        buf.append(66900.0)
        buf.append(67000.0)
        self.assertAlmostEqual(buf.average(), 66900.0)
        self.assertEqual(buf.sample_count(), 3)

    def test_evicts_entries_older_than_window(self):
        buf = self._buf(window=60)
        t0 = 1_000_000.0  # arbitrary epoch start
        buf.append(66800.0, _timestamp=t0)
        buf.append(66900.0, _timestamp=t0 + 30)
        buf.append(67000.0, _timestamp=t0 + 61)  # this append evicts t0 entry
        # t0 entry is 61 s old relative to latest timestamp → evicted
        self.assertEqual(buf.sample_count(), 2)

    def test_evicts_all_stale_entries(self):
        buf = self._buf(window=60)
        t0 = 1_000_000.0
        buf.append(66800.0, _timestamp=t0)
        buf.append(66900.0, _timestamp=t0 + 1)
        # Jump 120 s ahead; both previous entries are now stale
        buf.append(67000.0, _timestamp=t0 + 120)
        self.assertEqual(buf.sample_count(), 1)
        self.assertAlmostEqual(buf.average(_timestamp=t0 + 120), 67000.0)

    def test_average_only_uses_in_window_samples(self):
        buf = self._buf(window=60)
        t0 = 2_000_000.0
        buf.append(50000.0, _timestamp=t0)        # stale after t0+61
        buf.append(66800.0, _timestamp=t0 + 30)   # in window at t0+61
        buf.append(66900.0, _timestamp=t0 + 50)   # in window at t0+61
        buf.append(67000.0, _timestamp=t0 + 61)   # the trigger sample, evicts t0
        expected_avg = (66800.0 + 66900.0 + 67000.0) / 3
        self.assertAlmostEqual(buf.average(_timestamp=t0 + 61), expected_avg)

    def test_average_evicts_stale_without_new_append(self):
        """average() must evict before the mean so idle buffers do not use expired samples."""
        buf = self._buf(window=60)
        t0 = 4_000_000.0
        buf.append(66800.0, _timestamp=t0)
        buf.append(66900.0, _timestamp=t0 + 10)
        self.assertIsNone(buf.average(_timestamp=t0 + 120))
        self.assertEqual(buf.sample_count(), 0)

    def test_window_seconds_property(self):
        buf = self._buf(window=90)
        self.assertEqual(buf.window_seconds, 90)

    def test_sample_count_correct_after_eviction(self):
        buf = self._buf(window=60)
        t0 = 3_000_000.0
        for i in range(5):
            buf.append(66000.0 + i * 100, _timestamp=t0 + i * 10)
        # Now append at t0+70 to evict t0 entry (70 s old)
        buf.append(67000.0, _timestamp=t0 + 70)
        # t0 entry (70 s old) is evicted; 4 + 1 = 5 remaining
        self.assertEqual(buf.sample_count(), 5)


# ---------------------------------------------------------------------------
# build_synthetic_cfb_snapshot – rolling buffer integration
# ---------------------------------------------------------------------------

class TestBuildSyntheticCfbSnapshot(unittest.TestCase):

    def test_success_with_five_observations(self):
        prices = [66800.0, 66820.0, 66850.0, 66870.0, 66890.0]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")

        self.assertTrue(snap.ok)
        self.assertIsNotNone(snap.synthetic_cfb_mid)
        self.assertIsNotNone(snap.synthetic_cfb_spot)
        self.assertEqual(snap.source_count, 5)
        self.assertIsNotNone(snap.min_price)
        self.assertIsNotNone(snap.max_price)
        self.assertIsNotNone(snap.spread_dollars)
        self.assertIsNotNone(snap.spread_bps)
        self.assertAlmostEqual(snap.synthetic_cfb_mid, 66850.0)  # median of 5
        self.assertEqual(len(snap.observations), 5)
        self.assertIsNone(snap.error)

    def test_spot_equals_mid(self):
        prices = [66800.0, 66820.0, 66850.0, 66870.0, 66890.0]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")
        self.assertAlmostEqual(snap.synthetic_cfb_spot, snap.synthetic_cfb_mid)

    def test_outlier_rejection(self):
        tight = [66800.0, 66820.0, 66840.0, 66860.0]
        outlier = 70000.0  # ~4700 bps above median
        prices = tight + [outlier]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key", outlier_threshold_bps=40.0)

        self.assertTrue(snap.ok)
        self.assertEqual(snap.source_count, 4)
        self.assertLess(snap.max_price, 70000.0)  # type: ignore[operator]

    def test_fewer_than_3_valid_returns_ok_false(self):
        prices = [66800.0, None, None, None, None]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")

        self.assertFalse(snap.ok)
        self.assertIsNone(snap.synthetic_cfb_mid)
        self.assertIsNotNone(snap.error)
        self.assertEqual(snap.source_count, 1)

    def test_exactly_two_valid_returns_ok_false(self):
        prices = [66800.0, 66820.0, None, None, None]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")
        self.assertFalse(snap.ok)

    def test_snapshot_never_raises(self):
        """build_synthetic_cfb_snapshot must not propagate exceptions."""
        with patch(
            "synthetic_cfb_price.scrape_price_source",
            side_effect=RuntimeError("unexpected crash"),
        ):
            snap = build_synthetic_cfb_snapshot("test-key")
        self.assertFalse(snap.ok)
        self.assertIsNotNone(snap.error)

    def test_with_buffer_sets_avg_60s(self):
        prices = [66800.0, 66820.0, 66850.0, 66870.0, 66890.0]
        buf = RollingSyntheticCfbBuffer(window_seconds=60)
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key", buffer=buf)
        self.assertTrue(snap.ok)
        self.assertIsNotNone(snap.synthetic_cfb_avg_60s)
        self.assertEqual(snap.sample_count_60s, 1)
        self.assertEqual(snap.window_seconds, 60)

    def test_without_buffer_avg_60s_falls_back_to_spot(self):
        prices = [66800.0, 66820.0, 66850.0, 66870.0, 66890.0]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")
        self.assertTrue(snap.ok)
        # No buffer provided → avg_60s falls back to spot
        self.assertAlmostEqual(snap.synthetic_cfb_avg_60s, snap.synthetic_cfb_spot)
        self.assertEqual(snap.sample_count_60s, 1)

    def test_buffer_accumulates_across_calls(self):
        buf = RollingSyntheticCfbBuffer(window_seconds=60)
        for price_set in [
            [66800.0, 66810.0, 66820.0, 66830.0, 66840.0],
            [66900.0, 66910.0, 66920.0, 66930.0, 66940.0],
            [67000.0, 67010.0, 67020.0, 67030.0, 67040.0],
        ]:
            with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(price_set)):
                snap = build_synthetic_cfb_snapshot("test-key", buffer=buf)
        # After 3 calls, buffer should have 3 samples
        self.assertEqual(snap.sample_count_60s, 3)
        self.assertIsNotNone(snap.synthetic_cfb_avg_60s)

    def test_empty_api_key_returns_failed_snapshot_without_scraping(self):
        with patch("synthetic_cfb_price.scrape_price_source") as mock_scrape:
            snap = build_synthetic_cfb_snapshot("", buffer=None)
        mock_scrape.assert_not_called()
        self.assertFalse(snap.ok)
        self.assertIsNotNone(snap.error)
        self.assertIn("FIRECRAWL", snap.error or "")

    def test_skip_firecrawl_uses_api_only_no_firecrawl_key(self):
        def _mock_api(source_name, source_url, json_path):
            return PriceObservation(
                source_name=source_name,
                source_url=source_url,
                price_usd=65000.0,
                scraped_at="t",
                ok=True,
                error=None,
                raw_excerpt=None,
            )

        with patch("synthetic_cfb_price.scrape_price_source") as mock_scrape:
            with patch(
                "synthetic_cfb_price.fetch_price_api",
                side_effect=_mock_api,
            ):
                snap = build_synthetic_cfb_snapshot("", skip_firecrawl=True)
        mock_scrape.assert_not_called()
        self.assertTrue(snap.ok)
        self.assertAlmostEqual(snap.synthetic_cfb_spot or 0, 65000.0)


# ---------------------------------------------------------------------------
# Immature window confidence cap
# ---------------------------------------------------------------------------

class TestApplyWindowConfidenceCap(unittest.TestCase):

    def test_three_or_more_samples_no_cap(self):
        label, score = _apply_window_confidence_cap("high", 0.9, sample_count_60s=3)
        self.assertEqual(label, "high")
        self.assertAlmostEqual(score, 0.9)

    def test_two_samples_caps_high_to_medium(self):
        label, score = _apply_window_confidence_cap("high", 0.9, sample_count_60s=2)
        self.assertEqual(label, "medium")
        self.assertAlmostEqual(score, 0.6)

    def test_two_samples_does_not_raise_medium(self):
        # Already at medium → no change
        label, score = _apply_window_confidence_cap("medium", 0.6, sample_count_60s=2)
        self.assertEqual(label, "medium")
        self.assertAlmostEqual(score, 0.6)

    def test_one_sample_caps_high_to_low(self):
        label, score = _apply_window_confidence_cap("high", 0.9, sample_count_60s=1)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_one_sample_caps_medium_to_low(self):
        label, score = _apply_window_confidence_cap("medium", 0.6, sample_count_60s=1)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_one_sample_leaves_low_unchanged(self):
        label, score = _apply_window_confidence_cap("low", 0.3, sample_count_60s=1)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_immature_window_in_snapshot(self):
        """build_synthetic_cfb_snapshot confidence is capped when buffer has 1 sample."""
        prices = [66800.0, 66800.0, 66800.0, 66800.0, 66800.0]  # tight → normally "high"
        buf = RollingSyntheticCfbBuffer(window_seconds=60)
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key", buffer=buf)
        # 1 sample in buffer → confidence must be "low"
        self.assertEqual(snap.confidence, "low")
        self.assertAlmostEqual(snap.confidence_score, 0.3)

    def test_mature_window_allows_high_confidence(self):
        """After 3+ samples the window cap no longer limits confidence."""
        buf = RollingSyntheticCfbBuffer(window_seconds=60)
        for _ in range(3):
            prices = [66800.0, 66800.0, 66800.0, 66800.0, 66800.0]
            with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
                snap = build_synthetic_cfb_snapshot("test-key", buffer=buf)
        # 3 samples → cap no longer applies; tight spread → "high"
        self.assertIn(snap.confidence, ("high", "medium"))  # depends on spread
        self.assertGreater(snap.confidence_score, 0.3)


# ---------------------------------------------------------------------------
# confidence classification
# ---------------------------------------------------------------------------

class TestClassifyConfidence(unittest.TestCase):

    def test_high_confidence(self):
        label, score = _classify_confidence(source_count=4, spread_bps=8.0)
        self.assertEqual(label, "high")
        self.assertAlmostEqual(score, 0.9)

    def test_high_confidence_five_sources(self):
        label, score = _classify_confidence(source_count=5, spread_bps=5.0)
        self.assertEqual(label, "high")
        self.assertAlmostEqual(score, 0.9)

    def test_medium_confidence(self):
        label, score = _classify_confidence(source_count=3, spread_bps=15.0)
        self.assertEqual(label, "medium")
        self.assertAlmostEqual(score, 0.6)

    def test_low_confidence_wide_spread(self):
        label, score = _classify_confidence(source_count=5, spread_bps=30.0)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_low_confidence_few_sources(self):
        label, score = _classify_confidence(source_count=2, spread_bps=5.0)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_low_confidence_no_spread(self):
        label, score = _classify_confidence(source_count=5, spread_bps=None)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_boundary_high_exactly_10_bps(self):
        label, score = _classify_confidence(source_count=4, spread_bps=10.0)
        self.assertEqual(label, "high")

    def test_boundary_medium_exactly_25_bps(self):
        label, score = _classify_confidence(source_count=3, spread_bps=25.0)
        self.assertEqual(label, "medium")

    def test_boundary_just_over_high_threshold_drops_to_medium(self):
        label, score = _classify_confidence(source_count=4, spread_bps=11.0)
        self.assertEqual(label, "medium")


if __name__ == "__main__":
    unittest.main()

