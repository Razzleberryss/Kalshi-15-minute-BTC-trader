"""
synthetic_cfb_price.py – Synthetic CF Benchmarks BTC price estimator.

Kalshi's BTC markets reference CF Benchmarks pricing (BRTI), specifically the
*simple average of the last 60 seconds* of the BRTI feed.  Without direct BRTI
feed access this module builds a best-effort synthetic estimate by:

  1. Scraping several public BTC spot pages with Firecrawl each cycle to
     produce an instantaneous synthetic spot price.
  2. Accumulating those spot samples in a ``RollingSyntheticCfbBuffer`` that
     keeps only the last 60 seconds of observations.
  3. Exposing the rolling simple-mean as ``synthetic_cfb_avg_60s``, which is
     the closest proxy available to Kalshi's actual settlement reference.

Public entry points
-------------------
build_synthetic_cfb_snapshot(api_key, buffer, outlier_threshold_bps) -> SyntheticCfbSnapshot
classify_price_regime(kalshi_reference_usd, cfb_avg_60s, threshold_bps) -> str

Helper functions (also tested individually)
-------------------------------------------
utc_now_iso() -> str
extract_price_usd(markdown_text) -> float | None
scrape_price_source(api_key, source_name, source_url) -> PriceObservation

Rolling buffer
--------------
RollingSyntheticCfbBuffer(window_seconds=60)
"""

from __future__ import annotations

import re
import statistics
import datetime
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source list
# ---------------------------------------------------------------------------

BTC_SOURCES: list[tuple[str, str]] = [
    ("CoinGecko Bitcoin",   "https://www.coingecko.com/en/coins/bitcoin"),
    ("Coinbase BTC-USD",    "https://www.coinbase.com/price/bitcoin"),
    ("Kraken BTC/USD",      "https://www.kraken.com/prices/btc-bitcoin-price-chart/usd-us-dollar"),
    ("Binance BTC/USDT",    "https://www.binance.com/en/price/bitcoin"),
    ("TradingView BTCUSD",  "https://www.tradingview.com/symbols/BTCUSD/"),
]

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PriceObservation:
    source_name: str
    source_url: str
    price_usd: Optional[float]
    scraped_at: str
    ok: bool
    error: Optional[str]
    raw_excerpt: str


@dataclass
class SyntheticCfbSnapshot:
    # Instantaneous multi-source median for this cycle
    synthetic_cfb_mid: Optional[float]
    # Alias kept separate to make the rolling-vs-spot distinction explicit
    synthetic_cfb_spot: Optional[float]
    # Rolling 60-second simple mean – closest proxy to Kalshi's settlement ref
    synthetic_cfb_avg_60s: Optional[float]
    sample_count_60s: int
    window_seconds: int
    price_regime: str
    # Cross-source quality metrics
    source_count: int
    min_price: Optional[float]
    max_price: Optional[float]
    spread_dollars: Optional[float]
    spread_bps: Optional[float]
    confidence: str
    confidence_score: float
    observations: list[PriceObservation] = field(default_factory=list)
    scraped_at: str = ""
    ok: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Rolling buffer
# ---------------------------------------------------------------------------

class RollingSyntheticCfbBuffer:
    """
    In-memory rolling buffer of synthetic CFB spot samples for the last
    *window_seconds* seconds (default 60).

    Kalshi settles BTC markets on the simple average of the last 60 seconds
    of the CF Benchmarks BRTI feed.  This buffer accumulates our synthetic
    spot estimates at each bot cycle and exposes their simple mean as
    ``average()`` — the closest proxy we can build for that settlement ref.

    Thread-safety: not required (single-threaded bot loop).
    """

    def __init__(self, window_seconds: int = 60) -> None:
        self._window_seconds = window_seconds
        # Each entry: (price: float, timestamp: float) where timestamp is
        # time.time() unless overridden via _timestamp in append().
        self._samples: deque[tuple[float, float]] = deque()

    @property
    def window_seconds(self) -> int:
        return self._window_seconds

    def append(self, price: float, _timestamp: Optional[float] = None) -> None:
        """
        Add *price* to the buffer and evict stale entries.

        ``_timestamp`` is a POSIX timestamp (``time.time()`` epoch seconds).
        It defaults to ``time.time()`` and is exposed only for deterministic
        unit tests – callers should never pass it in production.
        """
        ts = _timestamp if _timestamp is not None else time.time()
        self._samples.append((price, ts))
        self._evict(ts)

    def _evict(self, now: float) -> None:
        """Remove entries older than window_seconds relative to *now*."""
        cutoff = now - self._window_seconds
        while self._samples and self._samples[0][1] < cutoff:
            self._samples.popleft()

    def average(self, _timestamp: Optional[float] = None) -> Optional[float]:
        """Simple arithmetic mean of buffered prices, or None if empty.

        Evicts stale entries before computing so that callers always get a
        mean over the live window, even if ``append`` hasn't been called
        recently.

        ``_timestamp`` defaults to ``time.time()`` and is exposed only for
        deterministic unit tests — callers should never pass it in production.
        """
        self._evict(_timestamp if _timestamp is not None else time.time())
        if not self._samples:
            return None
        return statistics.mean(p for p, _ in self._samples)

    def sample_count(self) -> int:
        """Number of samples currently in the rolling window."""
        return len(self._samples)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Matches dollar amounts like $66,870.79 or $66870.79 or $1,234,567.00
_PRICE_RE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")


def extract_price_usd(markdown_text: Optional[str]) -> Optional[float]:
    """
    Extract the first BTC-range USD price from markdown text.

    Looks for dollar-formatted numbers (e.g. ``$66,870.79``) and returns the
    first value that falls in a plausible BTC price range
    (1 000 – 10 000 000 USD).  Returns ``None`` if nothing plausible is found.
    """
    if not markdown_text:
        return None
    for match in _PRICE_RE.finditer(markdown_text):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        # Filter to plausible BTC range
        if 1_000.0 <= value <= 10_000_000.0:
            return value
    return None


def scrape_price_source(
    api_key: str,
    source_name: str,
    source_url: str,
) -> PriceObservation:
    """
    Scrape *source_url* with Firecrawl and extract a USD price.

    Never raises – all exceptions are caught and reflected in the returned
    ``PriceObservation`` with ``ok=False``.
    """
    now = utc_now_iso()
    try:
        from firecrawl import FirecrawlApp  # type: ignore[import]
        app = FirecrawlApp(api_key=api_key)
        result = app.scrape_url(source_url, formats=["markdown"])
        markdown: str = ""
        if isinstance(result, dict):
            markdown = result.get("markdown") or result.get("content") or ""
        elif hasattr(result, "markdown"):
            markdown = result.markdown or ""
        elif hasattr(result, "content"):
            markdown = result.content or ""
        raw_excerpt = str(markdown)[:1000]
        price = extract_price_usd(markdown)
        if price is None:
            return PriceObservation(
                source_name=source_name,
                source_url=source_url,
                price_usd=None,
                scraped_at=now,
                ok=False,
                error="No BTC price found in scraped content",
                raw_excerpt=raw_excerpt,
            )
        return PriceObservation(
            source_name=source_name,
            source_url=source_url,
            price_usd=price,
            scraped_at=now,
            ok=True,
            error=None,
            raw_excerpt=raw_excerpt,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("scrape_price_source failed for %s: %s", source_name, exc)
        return PriceObservation(
            source_name=source_name,
            source_url=source_url,
            price_usd=None,
            scraped_at=now,
            ok=False,
            error=str(exc),
            raw_excerpt="",
        )


# ---------------------------------------------------------------------------
# Confidence classification
# ---------------------------------------------------------------------------

# Ordered rank map used to apply the immature-window cap.
_CONFIDENCE_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}
_CONFIDENCE_BY_RANK: dict[int, tuple[str, float]] = {3: ("high", 0.9), 2: ("medium", 0.6), 1: ("low", 0.3)}


def _classify_confidence(source_count: int, spread_bps: Optional[float]) -> tuple[str, float]:
    """Return (confidence_label, confidence_score) from source_count and spread_bps."""
    if spread_bps is None:
        return "low", 0.3
    if source_count >= 4 and spread_bps <= 10.0:
        return "high", 0.9
    if source_count >= 3 and spread_bps <= 25.0:
        return "medium", 0.6
    return "low", 0.3


def _apply_window_confidence_cap(
    confidence: str,
    confidence_score: float,
    sample_count_60s: int,
) -> tuple[str, float]:
    """
    Cap confidence down when the rolling window is immature.

    - 1 sample  → cap at "low"   (0.3)
    - 2 samples → cap at "medium" (0.6)
    - ≥3 samples → no cap (full confidence from multi-source quality)
    """
    if sample_count_60s >= 3:
        return confidence, confidence_score
    cap_rank = max(1, sample_count_60s)  # 1 → low, 2 → medium
    current_rank = _CONFIDENCE_RANK.get(confidence, 1)
    if current_rank > cap_rank:
        capped_label, capped_score = _CONFIDENCE_BY_RANK[cap_rank]
        return capped_label, capped_score
    return confidence, confidence_score


# ---------------------------------------------------------------------------
# Price regime classification
# ---------------------------------------------------------------------------

def classify_price_regime(
    kalshi_reference_usd: Optional[float],
    cfb_avg_60s: Optional[float],
    threshold_bps: float = 10.0,
) -> str:
    """
    Classify the relationship between the Kalshi-derived reference price and
    the 60-second rolling synthetic CFB average.

    Returns one of:
      ``"aligned"``       – Kalshi reference is within *threshold_bps* of avg_60s
      ``"kalshi_ahead"``  – Kalshi reference is meaningfully above avg_60s
      ``"kalshi_behind"`` – Kalshi reference is meaningfully below avg_60s
      ``"uncertain"``     – insufficient data (either input is None)
    """
    if kalshi_reference_usd is None or cfb_avg_60s is None or cfb_avg_60s == 0.0:
        return "uncertain"
    deviation_bps = (kalshi_reference_usd - cfb_avg_60s) / cfb_avg_60s * 10_000.0
    if abs(deviation_bps) <= threshold_bps:
        return "aligned"
    return "kalshi_ahead" if deviation_bps > 0 else "kalshi_behind"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_synthetic_cfb_snapshot(
    api_key: str,
    buffer: Optional[RollingSyntheticCfbBuffer] = None,
    outlier_threshold_bps: float = 40.0,
) -> SyntheticCfbSnapshot:
    """
    Scrape all configured sources, filter outliers, update the rolling buffer,
    and return a ``SyntheticCfbSnapshot``.

    Steps
    -----
    1. Scrape all sources in ``BTC_SOURCES``.
    2. Keep only observations with a parsed price (``ok=True``).
    3. If fewer than 3 valid prices, return ``ok=False``.
    4. Compute a first-pass median.
    5. Reject prices whose deviation from the median exceeds
       *outlier_threshold_bps* basis points.
    6. Re-compute final median (= ``synthetic_cfb_spot``) from clean prices.
    7. If *buffer* is provided, append the spot and derive ``synthetic_cfb_avg_60s``.
    8. Compute spread stats and classify confidence, capping for immature windows.

    Never raises – all exceptions are caught internally.
    """
    now = utc_now_iso()
    observations: list[PriceObservation] = []

    def _failed_snapshot(
        source_count: int,
        error: str,
        avg_60s: Optional[float] = None,
        sample_count_60s: int = 0,
    ) -> SyntheticCfbSnapshot:
        return SyntheticCfbSnapshot(
            synthetic_cfb_mid=None,
            synthetic_cfb_spot=None,
            synthetic_cfb_avg_60s=avg_60s,
            sample_count_60s=sample_count_60s,
            window_seconds=buffer.window_seconds if buffer is not None else 60,
            price_regime="uncertain",
            source_count=source_count,
            min_price=None,
            max_price=None,
            spread_dollars=None,
            spread_bps=None,
            confidence="low",
            confidence_score=0.3,
            observations=observations,
            scraped_at=now,
            ok=False,
            error=error,
        )

    try:
        for source_name, source_url in BTC_SOURCES:
            obs = scrape_price_source(api_key, source_name, source_url)
            observations.append(obs)

        valid: list[float] = [
            o.price_usd for o in observations if o.ok and o.price_usd is not None
        ]

        if len(valid) < 3:
            return _failed_snapshot(
                source_count=len(valid),
                error=f"Only {len(valid)} valid price(s) – minimum 3 required",
            )

        first_median = statistics.median(valid)

        # Reject outliers: abs deviation from median > outlier_threshold_bps
        clean: list[float] = []
        for p in valid:
            deviation_bps = abs(p - first_median) / first_median * 10_000.0
            if deviation_bps <= outlier_threshold_bps:
                clean.append(p)

        if len(clean) < 3:
            # Fall back to full valid set if filtering discards too many prices
            clean = valid

        spot = statistics.median(clean)
        min_price = min(clean)
        max_price = max(clean)
        spread_dollars = max_price - min_price
        spread_bps = (spread_dollars / spot) * 10_000.0

        # Update rolling buffer and derive 60s average
        avg_60s: Optional[float] = None
        sample_count_60s = 0
        window_seconds = 60
        if buffer is not None:
            buffer.append(spot)
            avg_60s = buffer.average()
            sample_count_60s = buffer.sample_count()
            window_seconds = buffer.window_seconds
        else:
            # No buffer: fall back to spot as the best single-sample estimate
            avg_60s = spot
            sample_count_60s = 1

        confidence, confidence_score = _classify_confidence(len(clean), spread_bps)
        confidence, confidence_score = _apply_window_confidence_cap(
            confidence, confidence_score, sample_count_60s
        )

        return SyntheticCfbSnapshot(
            synthetic_cfb_mid=spot,
            synthetic_cfb_spot=spot,
            synthetic_cfb_avg_60s=avg_60s,
            sample_count_60s=sample_count_60s,
            window_seconds=window_seconds,
            price_regime="uncertain",  # bot.py fills this after computing Kalshi reference
            source_count=len(clean),
            min_price=min_price,
            max_price=max_price,
            spread_dollars=spread_dollars,
            spread_bps=spread_bps,
            confidence=confidence,
            confidence_score=confidence_score,
            observations=observations,
            scraped_at=now,
            ok=True,
            error=None,
        )

    except Exception as exc:  # noqa: BLE001
        log.error("build_synthetic_cfb_snapshot failed unexpectedly: %s", exc)
        return SyntheticCfbSnapshot(
            synthetic_cfb_mid=None,
            synthetic_cfb_spot=None,
            synthetic_cfb_avg_60s=None,
            sample_count_60s=0,
            window_seconds=buffer.window_seconds if buffer is not None else 60,
            price_regime="uncertain",
            source_count=0,
            min_price=None,
            max_price=None,
            spread_dollars=None,
            spread_bps=None,
            confidence="low",
            confidence_score=0.3,
            observations=observations,
            scraped_at=now,
            ok=False,
            error=str(exc),
        )
