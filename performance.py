"""
Performance monitoring utilities for tracking function execution times and bot metrics.

Use these decorators and classes to identify performance bottlenecks and monitor
system health in production.
"""

import time
import functools
import logging
from typing import Callable, Any, Optional
from collections import deque

log = logging.getLogger(__name__)


def monitor_performance(threshold_ms: float = 100.0, log_all: bool = False):
    """
    Decorator to monitor and log function execution time.

    Logs a warning when execution exceeds threshold_ms, or always logs in debug mode.
    Useful for identifying slow operations in hot paths.

    Args:
        threshold_ms: Warn if function takes longer than this (milliseconds)
        log_all: If True, log all executions regardless of threshold

    Example:
        @monitor_performance(threshold_ms=50.0)
        def get_btc_momentum():
            # ... implementation ...

        @monitor_performance(log_all=True)  # Log every execution
        def critical_path_function():
            # ... implementation ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            start = time.perf_counter()
            try:
                result = func(*args, **kwargs)
                return result
            finally:
                elapsed_ms = (time.perf_counter() - start) * 1000

                if log_all:
                    log.info(f"{func.__name__} took {elapsed_ms:.1f}ms")
                elif elapsed_ms > threshold_ms:
                    log.warning(
                        f"{func.__name__} took {elapsed_ms:.1f}ms (threshold: {threshold_ms}ms)"
                    )
                else:
                    log.debug(f"{func.__name__} took {elapsed_ms:.1f}ms")

        return wrapper
    return decorator


class BotMetrics:
    """
    Track bot performance metrics over time.

    Maintains rolling windows of timing data for bot cycles, signal generation,
    order placement, and other critical operations.
    """

    def __init__(self, max_samples: int = 100):
        """
        Initialize metrics tracker.

        Args:
            max_samples: Number of samples to keep in rolling windows
        """
        self.max_samples = max_samples
        self.cycle_times = deque(maxlen=max_samples)
        self.signal_gen_times = deque(maxlen=max_samples)
        self.order_placement_times = deque(maxlen=max_samples)
        self.orderbook_fetch_times = deque(maxlen=max_samples)

    def record_cycle_time(self, duration_ms: float) -> None:
        """Record bot cycle execution time"""
        self.cycle_times.append(duration_ms)

    def record_signal_gen_time(self, duration_ms: float) -> None:
        """Record signal generation execution time"""
        self.signal_gen_times.append(duration_ms)

    def record_order_placement_time(self, duration_ms: float) -> None:
        """Record order placement execution time"""
        self.order_placement_times.append(duration_ms)

    def record_orderbook_fetch_time(self, duration_ms: float) -> None:
        """Record orderbook fetch execution time"""
        self.orderbook_fetch_times.append(duration_ms)

    def _compute_stats(self, data: deque) -> Optional[dict]:
        """Compute statistics for a data series"""
        if not data:
            return None

        sorted_data = sorted(data)
        n = len(sorted_data)

        return {
            "count": n,
            "avg_ms": sum(data) / n,
            "min_ms": sorted_data[0],
            "max_ms": sorted_data[-1],
            "p50_ms": sorted_data[n // 2],
            "p95_ms": sorted_data[int(n * 0.95)] if n > 1 else sorted_data[0],
            "p99_ms": sorted_data[int(n * 0.99)] if n > 1 else sorted_data[0],
        }

    def get_cycle_stats(self) -> Optional[dict]:
        """Get statistics for bot cycle times"""
        return self._compute_stats(self.cycle_times)

    def get_signal_gen_stats(self) -> Optional[dict]:
        """Get statistics for signal generation times"""
        return self._compute_stats(self.signal_gen_times)

    def get_order_placement_stats(self) -> Optional[dict]:
        """Get statistics for order placement times"""
        return self._compute_stats(self.order_placement_times)

    def get_orderbook_fetch_stats(self) -> Optional[dict]:
        """Get statistics for orderbook fetch times"""
        return self._compute_stats(self.orderbook_fetch_times)

    def get_all_stats(self) -> dict:
        """Get all performance statistics"""
        return {
            "cycle": self.get_cycle_stats(),
            "signal_generation": self.get_signal_gen_stats(),
            "order_placement": self.get_order_placement_stats(),
            "orderbook_fetch": self.get_orderbook_fetch_stats(),
        }

    def log_summary(self) -> None:
        """Log a summary of all performance metrics"""
        stats = self.get_all_stats()

        log.info("=== Performance Summary ===")
        for category, data in stats.items():
            if data:
                log.info(
                    f"{category}: avg={data['avg_ms']:.1f}ms, "
                    f"p95={data['p95_ms']:.1f}ms, max={data['max_ms']:.1f}ms "
                    f"(n={data['count']})"
                )
            else:
                log.info(f"{category}: no data")


class TimingContext:
    """
    Context manager for timing code blocks.

    Example:
        metrics = BotMetrics()

        with TimingContext() as timer:
            # ... do work ...

        metrics.record_cycle_time(timer.elapsed_ms)

        # Or use with callback
        with TimingContext(callback=metrics.record_cycle_time):
            # ... do work ...
        # Automatically records timing when exiting context
    """

    def __init__(self, callback: Optional[Callable[[float], None]] = None):
        """
        Initialize timing context.

        Args:
            callback: Optional function to call with elapsed_ms on exit
        """
        self.callback = callback
        self.start_time = None
        self.end_time = None
        self.elapsed_ms = None

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.end_time = time.perf_counter()
        self.elapsed_ms = (self.end_time - self.start_time) * 1000

        if self.callback:
            self.callback(self.elapsed_ms)

        return False  # Don't suppress exceptions


# Example usage in bot.py:
#
# from performance import BotMetrics, TimingContext, monitor_performance
#
# metrics = BotMetrics()
#
# def run_bot():
#     while True:
#         with TimingContext(callback=metrics.record_cycle_time):
#             # ... bot cycle code ...
#
#         # Log summary every 100 cycles
#         if len(metrics.cycle_times) == 100:
#             metrics.log_summary()
#
# @monitor_performance(threshold_ms=50.0)
# def fetch_orderbook(ticker: str):
#     # ... implementation ...
