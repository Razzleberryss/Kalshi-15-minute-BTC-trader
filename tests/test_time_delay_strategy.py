"""
tests/test_time_delay_strategy.py – Unit tests for the reddit_time_delay strategy.

Covers all six required cases from the problem statement:
  1. No trade when minutes_to_expiry > TRIGGER_MINUTE_REMAINING and no position.
  2. Enter YES when minutes_to_expiry <= TRIGGER_MINUTE_REMAINING, up_price >= TRIGGER_POINT_PRICE,
     and no previous trade in this window.
  3. Enter NO under the symmetric conditions for down_price.
  4. No trade when trades_in_current_window >= MAX_TRADES_PER_WINDOW.
  5. Exit position when current_position_side == "YES" and up_price <= EXIT_POINT_PRICE.
  6. Exit position when current_position_side == "NO" and down_price <= EXIT_POINT_PRICE.

Plus additional boundary and edge cases including:
  - Bid vs. ask price distinction for entry/exit
  - MAX_TRADES_PER_WINDOW > 1 correctly enforced via counter
  - Invalid/unexpected current_position_side returns NO_TRADE safely
  - _compute_minutes_to_expiry helper edge cases
  - _compute_window_id helper edge cases
"""
import datetime
import types
import unittest
from unittest.mock import patch

from strategy import decide_trade_time_delay, decide_trade
from bot import _compute_minutes_to_expiry, _compute_window_id


def _make_cfg(
    strategy_mode="reddit_time_delay",
    trigger_point_price=0.90,
    exit_point_price=0.40,
    trigger_minute_remaining=14,
    max_trades_per_window=1,
    base_size=1,
):
    """Return a SimpleNamespace acting as a config object for the time-delay strategy."""
    return types.SimpleNamespace(
        STRATEGY_MODE=strategy_mode,
        TRIGGER_POINT_PRICE=trigger_point_price,
        EXIT_POINT_PRICE=exit_point_price,
        TRIGGER_MINUTE_REMAINING=trigger_minute_remaining,
        MAX_TRADES_PER_WINDOW=max_trades_per_window,
        BASE_SIZE=base_size,
    )


WINDOW_A = "2025-01-01T15:15:00+00:00"
WINDOW_B = "2025-01-01T15:30:00+00:00"


class TestTimeDelayNoPosition(unittest.TestCase):
    """Case 1: No open position – timing and trigger checks."""

    def setUp(self):
        self.cfg = _make_cfg()

    # ── Too early ──────────────────────────────────────────────────────────────

    def test_no_trade_when_too_many_minutes_remain(self):
        """Requirement 1: no trade when minutes_to_expiry > TRIGGER_MINUTE_REMAINING."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=15,          # > 14
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertIsNone(size)

    def test_no_trade_at_exact_trigger_boundary_plus_one(self):
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=15,          # one more than TRIGGER_MINUTE_REMAINING
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    # ── Exact boundary – armed ─────────────────────────────────────────────────

    def test_trade_allowed_at_exact_trigger_minute(self):
        """Armed exactly when minutes_to_expiry == TRIGGER_MINUTE_REMAINING."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=14,          # == TRIGGER_MINUTE_REMAINING
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_YES")

    # ── Enter YES ─────────────────────────────────────────────────────────────

    def test_enter_yes_when_up_price_at_trigger(self):
        """Requirement 2: Enter YES when up_price >= TRIGGER_POINT_PRICE, down_price below."""
        action, size = decide_trade_time_delay(
            up_price=0.90,
            down_price=0.10,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_YES")
        self.assertEqual(size, 1)

    def test_enter_yes_returns_base_size(self):
        cfg = _make_cfg(base_size=3)
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_YES")
        self.assertEqual(size, 3)

    # ── Enter NO ──────────────────────────────────────────────────────────────

    def test_enter_no_when_down_price_at_trigger(self):
        """Requirement 3: Enter NO when down_price >= TRIGGER_POINT_PRICE, up_price below."""
        action, size = decide_trade_time_delay(
            up_price=0.10,
            down_price=0.90,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_NO")
        self.assertEqual(size, 1)

    def test_enter_no_returns_base_size(self):
        cfg = _make_cfg(base_size=5)
        action, size = decide_trade_time_delay(
            up_price=0.05,
            down_price=0.95,
            minutes_to_expiry=2,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_NO")
        self.assertEqual(size, 5)

    # ── Neither / both qualify ─────────────────────────────────────────────────

    def test_no_trade_when_both_prices_above_trigger(self):
        action, size = decide_trade_time_delay(
            up_price=0.92,
            down_price=0.91,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_no_trade_when_neither_price_reaches_trigger(self):
        action, size = decide_trade_time_delay(
            up_price=0.70,
            down_price=0.30,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")


class TestTimeDelayWindowLimit(unittest.TestCase):
    """Requirement 4: per-window trade limit enforcement via trades_in_current_window."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_no_trade_when_already_traded_this_window(self):
        """Requirement 4: no second entry when trades_in_current_window >= MAX_TRADES_PER_WINDOW."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
            trades_in_current_window=1,   # already traded once; limit is 1
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertIsNone(size)

    def test_trade_allowed_in_new_window_after_previous(self):
        """Counter resets when window changes; trade allowed in new window."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_B,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
            trades_in_current_window=0,   # reset for new window
        )
        self.assertEqual(action, "ENTER_YES")

    def test_trade_allowed_when_max_trades_greater_than_one(self):
        """With MAX_TRADES_PER_WINDOW == 2, first same-window entry is allowed."""
        cfg = _make_cfg(max_trades_per_window=2)
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
            trades_in_current_window=1,   # 1 trade placed; limit is 2 → allowed
        )
        self.assertEqual(action, "ENTER_YES")

    def test_no_trade_when_max_trades_greater_than_one_and_limit_reached(self):
        """With MAX_TRADES_PER_WINDOW == 2, second same-window entry is blocked."""
        cfg = _make_cfg(max_trades_per_window=2)
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
            trades_in_current_window=2,   # 2 trades placed; limit is 2 → blocked
        )
        self.assertEqual(action, "NO_TRADE")

    def test_trade_allowed_when_no_prior_trade(self):
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
            trades_in_current_window=0,
        )
        self.assertEqual(action, "ENTER_YES")


class TestTimeDelayExitYes(unittest.TestCase):
    """Requirement 5: exit when holding YES and up_price drops to/below EXIT_POINT_PRICE."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_exit_yes_when_up_price_at_exit_threshold(self):
        """Requirement 5: exit YES when up_price == EXIT_POINT_PRICE."""
        action, size = decide_trade_time_delay(
            up_price=0.40,
            down_price=0.60,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")
        self.assertIsNone(size)

    def test_exit_yes_when_up_price_below_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.20,
            down_price=0.80,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_no_exit_yes_when_up_price_above_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.60,
            down_price=0.40,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_exit_yes_uses_bid_price_when_provided(self):
        """Stop-loss for YES uses the bid price (what we can sell at), not the ask."""
        # ask is above threshold — no exit if using ask
        # bid is at/below threshold — should exit when using bid
        action, size = decide_trade_time_delay(
            up_price=0.60,    # ask — above threshold; entry check would not exit
            down_price=0.40,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
            up_bid=0.38,      # bid — below EXIT_POINT_PRICE of 0.40 → should exit
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_no_exit_yes_when_bid_above_exit_threshold(self):
        """No exit when both ask and bid are above the exit threshold."""
        action, size = decide_trade_time_delay(
            up_price=0.70,
            down_price=0.30,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
            up_bid=0.65,      # bid — still above EXIT_POINT_PRICE → no exit
        )
        self.assertEqual(action, "NO_TRADE")


class TestTimeDelayExitNo(unittest.TestCase):
    """Requirement 6: exit when holding NO and down_price drops to/below EXIT_POINT_PRICE."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_exit_no_when_down_price_at_exit_threshold(self):
        """Requirement 6: exit NO when down_price == EXIT_POINT_PRICE."""
        action, size = decide_trade_time_delay(
            up_price=0.60,
            down_price=0.40,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")
        self.assertIsNone(size)

    def test_exit_no_when_down_price_below_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.80,
            down_price=0.15,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_no_exit_no_when_down_price_above_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.40,
            down_price=0.60,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_exit_no_uses_bid_price_when_provided(self):
        """Stop-loss for NO uses the bid price (what we can sell at), not the ask."""
        action, size = decide_trade_time_delay(
            up_price=0.60,
            down_price=0.60,  # ask — above threshold; would not trigger on ask
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
            down_bid=0.35,    # bid — below EXIT_POINT_PRICE of 0.40 → should exit
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_no_exit_no_when_bid_above_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.40,
            down_price=0.65,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
            down_bid=0.58,    # bid — above EXIT_POINT_PRICE → no exit
        )
        self.assertEqual(action, "NO_TRADE")


class TestTimeDelayInvalidPositionSide(unittest.TestCase):
    """Unexpected current_position_side values should return NO_TRADE safely."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_lowercase_yes_returns_no_trade(self):
        """'yes' (lowercase) is not 'YES' — treated as unknown, returns NO_TRADE."""
        action, size = decide_trade_time_delay(
            up_price=0.20,
            down_price=0.80,
            minutes_to_expiry=5,
            current_position_side="yes",  # lowercase — invalid
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_lowercase_no_returns_no_trade(self):
        action, size = decide_trade_time_delay(
            up_price=0.80,
            down_price=0.20,
            minutes_to_expiry=5,
            current_position_side="no",   # lowercase — invalid
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_arbitrary_string_returns_no_trade(self):
        action, size = decide_trade_time_delay(
            up_price=0.20,
            down_price=0.20,
            minutes_to_expiry=5,
            current_position_side="INVALID",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")


class TestDecideTradeWrapper(unittest.TestCase):
    """decide_trade() wrapper routing tests."""

    def test_routes_to_time_delay_when_mode_set(self):
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        action, size = decide_trade(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_YES")

    def test_returns_no_trade_for_fee_aware_model_mode(self):
        """For fee_aware_model mode the wrapper defers to generate_signal; returns NO_TRADE."""
        cfg = _make_cfg(strategy_mode="fee_aware_model")
        action, size = decide_trade(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_wrapper_exit_yes_via_time_delay(self):
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        action, size = decide_trade(
            up_price=0.30,
            down_price=0.70,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_wrapper_exit_no_via_time_delay(self):
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        action, size = decide_trade(
            up_price=0.70,
            down_price=0.30,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_wrapper_passes_through_bid_prices(self):
        """decide_trade wrapper forwards up_bid/down_bid to the strategy."""
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        # ask above threshold → would not exit; bid below threshold → should exit
        action, size = decide_trade(
            up_price=0.70,
            down_price=0.30,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
            up_bid=0.35,
        )
        self.assertEqual(action, "EXIT_POSITION")


# ── Tests for bot.py helpers ───────────────────────────────────────────────────

class TestComputeMinutesToExpiry(unittest.TestCase):
    """Unit tests for _compute_minutes_to_expiry edge cases."""

    def _fixed_now(self, dt_str: str):
        """Return a datetime.datetime mock that always returns the given UTC time."""
        fixed = datetime.datetime.fromisoformat(dt_str)
        return patch(
            "bot.datetime.datetime",
            wraps=datetime.datetime,
            now=lambda tz=None: fixed,
        )

    def test_returns_999_when_close_time_missing(self):
        result = _compute_minutes_to_expiry({})
        self.assertEqual(result, 999)

    def test_returns_999_when_close_time_is_none(self):
        result = _compute_minutes_to_expiry({"close_time": None})
        self.assertEqual(result, 999)

    def test_returns_999_when_close_time_is_invalid(self):
        result = _compute_minutes_to_expiry({"close_time": "not-a-date"})
        self.assertEqual(result, 999)

    def test_returns_zero_when_close_time_in_the_past(self):
        """When close_time has already passed, remaining minutes should be clamped to 0."""
        past = (
            datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(minutes=5)
        ).isoformat()
        result = _compute_minutes_to_expiry({"close_time": past})
        self.assertEqual(result, 0)

    def test_parses_z_suffix_correctly(self):
        """ISO timestamps ending in 'Z' (UTC) should be handled the same as '+00:00'."""
        future_z = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(minutes=10)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _compute_minutes_to_expiry({"close_time": future_z})
        # Should be approximately 10 minutes (allow ±1 due to wall-clock skew)
        self.assertIn(result, range(9, 12))

    def test_returns_whole_minutes(self):
        """Result must always be a whole-number integer."""
        future = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=130)
        ).isoformat()
        result = _compute_minutes_to_expiry({"close_time": future})
        self.assertIsInstance(result, int)
        self.assertEqual(result, 2)


class TestComputeWindowId(unittest.TestCase):
    """Unit tests for _compute_window_id helper."""

    def test_prefers_close_time_when_present(self):
        market = {"close_time": WINDOW_A, "ticker": "BTCZ-TEST"}
        self.assertEqual(_compute_window_id(market), WINDOW_A)

    def test_falls_back_to_ticker_when_close_time_missing(self):
        market = {"ticker": "BTCZ-TEST"}
        self.assertEqual(_compute_window_id(market), "BTCZ-TEST")

    def test_falls_back_to_ticker_when_close_time_is_none(self):
        market = {"close_time": None, "ticker": "BTCZ-TEST"}
        self.assertEqual(_compute_window_id(market), "BTCZ-TEST")

    def test_falls_back_to_unknown_when_both_missing(self):
        market = {}
        self.assertEqual(_compute_window_id(market), "unknown")

    def test_uses_close_time_as_stable_id(self):
        """Two markets with the same close_time return the same window ID."""
        m1 = {"close_time": WINDOW_A, "ticker": "BTCZ-1"}
        m2 = {"close_time": WINDOW_A, "ticker": "BTCZ-2"}
        self.assertEqual(_compute_window_id(m1), _compute_window_id(m2))

    def test_different_close_times_give_different_ids(self):
        m1 = {"close_time": WINDOW_A, "ticker": "BTCZ-1"}
        m2 = {"close_time": WINDOW_B, "ticker": "BTCZ-1"}
        self.assertNotEqual(_compute_window_id(m1), _compute_window_id(m2))


if __name__ == "__main__":
    unittest.main()

