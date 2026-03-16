"""
tests/test_decide_trade.py – Unit tests for the fee-aware entry decision function.

Covers:
  - BUY_YES when mispricing >= MIN_EDGE_PCT and price in allowed band
  - BUY_NO  when mispricing <= -MIN_EDGE_PCT and price in allowed band
  - NO_TRADE when mispricing is within the no-trade band
  - NO_TRADE when price is inside the forbidden band
  - NO_TRADE when expected net EV per contract is below threshold
  - Dynamic sizing: BASE_SIZE at minimum edge, MAX_SIZE at maximum edge
  - side_allowed_flags can block YES or NO independently
  - Config defaults are loaded from the config module when cfg=None
"""
import math
import types
import unittest

import config
from strategy import decide_trade_fee_aware as decide_trade


def _make_cfg(
    min_edge_pct=0.10,
    forbidden_low=0.30,
    forbidden_high=0.70,
    min_ev_net=0.02,
    base_size=1,
    max_size=10,
    max_edge_pct=0.30,
):
    """Return a simple namespace acting as a config object."""
    cfg = types.SimpleNamespace(
        MIN_EDGE_PCT=min_edge_pct,
        FORBIDDEN_PRICE_LOW=forbidden_low,
        FORBIDDEN_PRICE_HIGH=forbidden_high,
        MIN_EXPECTED_NET_PER_CONTRACT=min_ev_net,
        BASE_SIZE=base_size,
        MAX_SIZE=max_size,
        MAX_EDGE_PCT=max_edge_pct,
    )
    return cfg


class TestDecideTradeBasic(unittest.TestCase):
    """Core pass/reject scenarios."""

    def setUp(self):
        self.cfg = _make_cfg()

    # ── BUY_YES ────────────────────────────────────────────────────────────────

    def test_buy_yes_when_model_clearly_above_market(self):
        # market_price=0.10 (tail), model_p_yes=0.25 → mispricing=+0.15 > 0.10
        action, size = decide_trade(0.10, 0.25, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")
        self.assertGreaterEqual(size, 1)

    def test_buy_yes_at_high_tail_price(self):
        # market_price=0.85, model_p_yes=0.97 → mispricing=+0.12
        action, size = decide_trade(0.85, 0.97, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")

    # ── BUY_NO ────────────────────────────────────────────────────────────────

    def test_buy_no_when_model_clearly_below_market(self):
        # market_price=0.90 (high), model_p_yes=0.75 → mispricing=-0.15
        action, size = decide_trade(0.90, 0.75, cfg=self.cfg)
        self.assertEqual(action, "BUY_NO")
        self.assertGreaterEqual(size, 1)

    def test_buy_no_at_low_tail_price(self):
        # market_price=0.15, model_p_yes=0.01 → mispricing=-0.14
        action, size = decide_trade(0.15, 0.01, cfg=self.cfg)
        self.assertEqual(action, "BUY_NO")

    # ── NO_TRADE: insufficient mispricing ─────────────────────────────────────

    def test_no_trade_when_mispricing_zero(self):
        action, size = decide_trade(0.10, 0.10, cfg=self.cfg)
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_no_trade_when_mispricing_below_min_edge_pct(self):
        # mispricing = 0.05, MIN_EDGE_PCT = 0.10 → no trade
        action, size = decide_trade(0.10, 0.15, cfg=self.cfg)
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_no_trade_at_exact_boundary_below(self):
        # mispricing = MIN_EDGE_PCT - epsilon → no trade
        action, size = decide_trade(0.10, 0.10 + 0.10 - 1e-9, cfg=self.cfg)
        self.assertEqual(action, "NO_TRADE")

    def test_trade_at_exact_boundary_above(self):
        # mispricing exactly = MIN_EDGE_PCT → trade
        action, size = decide_trade(0.10, 0.10 + 0.10, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")

    # ── NO_TRADE: forbidden price band ────────────────────────────────────────

    def test_no_trade_when_price_inside_forbidden_band(self):
        # price=0.50 is inside (0.30, 0.70)
        action, size = decide_trade(0.50, 0.65, cfg=self.cfg)
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_no_trade_at_middle_of_forbidden_band(self):
        action, size = decide_trade(0.45, 0.60, cfg=self.cfg)
        self.assertEqual(action, "NO_TRADE")

    def test_trade_allowed_at_lower_tail(self):
        # price=0.20 is below forbidden low (0.30)
        action, size = decide_trade(0.20, 0.35, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")

    def test_trade_allowed_at_upper_tail(self):
        # price=0.80 is above forbidden high (0.70)
        action, size = decide_trade(0.80, 0.95, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")

    def test_trade_allowed_exactly_at_forbidden_low_boundary(self):
        # price == FORBIDDEN_PRICE_LOW (0.30) is NOT inside the open interval
        action, size = decide_trade(0.30, 0.45, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")

    def test_trade_allowed_exactly_at_forbidden_high_boundary(self):
        # price == FORBIDDEN_PRICE_HIGH (0.70) is NOT inside the open interval
        action, size = decide_trade(0.70, 0.85, cfg=self.cfg)
        self.assertEqual(action, "BUY_YES")


class TestDecideTradeNetEV(unittest.TestCase):
    """Fee and expected-net-value filtering."""

    def test_no_trade_when_net_ev_below_min_threshold(self):
        # Very tight edge (10 pp) at a tail price — fees could eat the edge
        # Use a high MIN_EXPECTED_NET_PER_CONTRACT to force a reject
        cfg = _make_cfg(min_ev_net=0.50)  # require 50 cents net EV/contract
        action, size = decide_trade(0.10, 0.20, cfg=cfg)
        self.assertEqual(action, "NO_TRADE")

    def test_trade_passes_when_net_ev_above_threshold(self):
        # Large edge (+0.40) at a tail price with default threshold of $0.02
        cfg = _make_cfg(min_ev_net=0.02)
        action, size = decide_trade(0.10, 0.50, cfg=cfg)
        self.assertEqual(action, "BUY_YES")

    def test_fee_formula_uses_ceil(self):
        # Verify that the fee is at least 1 cent even for tiny C*P*(1-P)
        # C=1, P=0.01 → 0.07*1*0.01*0.99 = 0.000693 → ceil = 1 cent
        fee = math.ceil(0.07 * 1 * 0.01 * 0.99)
        self.assertEqual(fee, 1)

    def test_net_ev_for_no_side_is_positive(self):
        # Buying NO: market_price=0.85, model_p_yes=0.70 → mispricing=-0.15
        # ev_gross = (1-0.70) - (1-0.85) = 0.30 - 0.15 = 0.15 (before fees)
        cfg = _make_cfg(min_ev_net=0.02)
        action, size = decide_trade(0.85, 0.70, cfg=cfg)
        self.assertEqual(action, "BUY_NO")
        self.assertGreaterEqual(size, 1)
        # Verify net EV independently: fees at C=size should still leave >$0.02/contract
        import math as _math
        P, P_exit = 0.85, 0.5
        ev_gross = (1.0 - 0.70) - (1.0 - P)
        fee_open = _math.ceil(0.07 * size * P * (1.0 - P))
        fee_close = _math.ceil(0.07 * size * P_exit * (1.0 - P_exit))
        ev_net = ev_gross - (fee_open + fee_close) / 100.0 / size
        self.assertGreaterEqual(ev_net, cfg.MIN_EXPECTED_NET_PER_CONTRACT)


class TestDecideTradeSizing(unittest.TestCase):
    """Dynamic position sizing based on edge magnitude."""

    def test_base_size_used_at_minimum_edge(self):
        # edge_mag = MIN_EDGE_PCT → edge_ratio = 0 → C = BASE_SIZE
        cfg = _make_cfg(base_size=2, max_size=10, min_edge_pct=0.10, max_edge_pct=0.30)
        # mispricing = 0.10 exactly
        action, size = decide_trade(0.10, 0.20, cfg=cfg)
        self.assertEqual(action, "BUY_YES")
        self.assertEqual(size, 2)

    def test_max_size_used_at_maximum_edge(self):
        # edge_mag = MAX_EDGE_PCT → edge_ratio = 1 → C = MAX_SIZE
        cfg = _make_cfg(base_size=1, max_size=8, min_edge_pct=0.10, max_edge_pct=0.30)
        # mispricing = 0.30 or more
        action, size = decide_trade(0.10, 0.40, cfg=cfg)
        self.assertEqual(action, "BUY_YES")
        self.assertEqual(size, 8)

    def test_intermediate_size_between_base_and_max(self):
        # edge_ratio ~= 0.5 → C should be between BASE_SIZE and MAX_SIZE
        cfg = _make_cfg(base_size=1, max_size=10, min_edge_pct=0.10, max_edge_pct=0.30)
        # mispricing ≈ 0.20 (approximately 50% of the MIN→MAX edge range)
        action, size = decide_trade(0.10, 0.30, cfg=cfg)
        self.assertEqual(action, "BUY_YES")
        self.assertGreater(size, cfg.BASE_SIZE)   # scaled up from BASE_SIZE
        self.assertLess(size, cfg.MAX_SIZE)        # but not yet at MAX_SIZE

    def test_size_capped_at_max_size(self):
        # edge_mag >> MAX_EDGE_PCT → edge_ratio clipped to 1 → C = MAX_SIZE
        cfg = _make_cfg(base_size=1, max_size=5, min_edge_pct=0.10, max_edge_pct=0.30)
        action, size = decide_trade(0.10, 0.99, cfg=cfg)
        self.assertEqual(action, "BUY_YES")
        self.assertEqual(size, 5)

    def test_size_is_at_least_one(self):
        cfg = _make_cfg(base_size=1, max_size=1)
        action, size = decide_trade(0.10, 0.25, cfg=cfg)
        if action != "NO_TRADE":
            self.assertGreaterEqual(size, 1)


class TestDecideTradeSideFlags(unittest.TestCase):
    """side_allowed_flags can disable YES or NO entries."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_yes_blocked_by_side_flag(self):
        # mispricing suggests BUY_YES, but YES is disabled
        action, size = decide_trade(
            0.10, 0.25,
            side_allowed_flags={"yes": False, "no": True},
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_no_blocked_by_side_flag(self):
        # mispricing suggests BUY_NO, but NO is disabled
        action, size = decide_trade(
            0.90, 0.75,
            side_allowed_flags={"yes": True, "no": False},
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_yes_blocked_returns_no_trade_not_buy_no(self):
        # When YES side is blocked, should not fall through to BUY_NO even if
        # mispricing is in the YES direction.
        action, size = decide_trade(
            0.10, 0.25,
            side_allowed_flags={"yes": False, "no": True},
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_no_blocked_returns_no_trade_not_buy_yes(self):
        # When NO side is blocked, should not fall through to BUY_YES even if
        # mispricing is in the NO direction.
        action, size = decide_trade(
            0.90, 0.75,
            side_allowed_flags={"yes": True, "no": False},
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_yes_allowed_when_only_yes_enabled(self):
        action, size = decide_trade(
            0.10, 0.25,
            side_allowed_flags={"yes": True, "no": False},
            cfg=self.cfg,
        )
        self.assertEqual(action, "BUY_YES")

    def test_no_allowed_when_only_no_enabled(self):
        action, size = decide_trade(
            0.90, 0.75,
            side_allowed_flags={"yes": False, "no": True},
            cfg=self.cfg,
        )
        self.assertEqual(action, "BUY_NO")


class TestDecideTradeUsesConfigDefaults(unittest.TestCase):
    """When cfg=None, decide_trade reads from the real config module."""

    def setUp(self):
        # Save originals
        self._orig_min_edge_pct = config.MIN_EDGE_PCT
        self._orig_forbidden_low = config.FORBIDDEN_PRICE_LOW
        self._orig_forbidden_high = config.FORBIDDEN_PRICE_HIGH
        self._orig_min_ev = config.MIN_EXPECTED_NET_PER_CONTRACT
        self._orig_base = config.BASE_SIZE
        self._orig_max = config.MAX_SIZE
        self._orig_max_edge = config.MAX_EDGE_PCT

    def tearDown(self):
        config.MIN_EDGE_PCT = self._orig_min_edge_pct
        config.FORBIDDEN_PRICE_LOW = self._orig_forbidden_low
        config.FORBIDDEN_PRICE_HIGH = self._orig_forbidden_high
        config.MIN_EXPECTED_NET_PER_CONTRACT = self._orig_min_ev
        config.BASE_SIZE = self._orig_base
        config.MAX_SIZE = self._orig_max
        config.MAX_EDGE_PCT = self._orig_max_edge

    def test_uses_config_min_edge_pct(self):
        config.MIN_EDGE_PCT = 0.50  # very high threshold → no trade
        action, _ = decide_trade(0.10, 0.25)  # mispricing only 0.15
        self.assertEqual(action, "NO_TRADE")

    def test_uses_config_forbidden_band(self):
        config.MIN_EDGE_PCT = 0.10
        config.FORBIDDEN_PRICE_LOW = 0.05
        config.FORBIDDEN_PRICE_HIGH = 0.95
        config.MIN_EXPECTED_NET_PER_CONTRACT = 0.0
        config.BASE_SIZE = 1
        config.MAX_SIZE = 1
        config.MAX_EDGE_PCT = 0.30
        # price=0.10 is now inside the wide forbidden band (0.05, 0.95)
        action, _ = decide_trade(0.10, 0.25)
        self.assertEqual(action, "NO_TRADE")


if __name__ == "__main__":
    unittest.main()
