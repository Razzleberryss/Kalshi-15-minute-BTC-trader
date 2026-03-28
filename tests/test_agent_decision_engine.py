"""
Tests for agent_decision_engine.py — deterministic CLI envelope interpretation.

Covers:
  - Representative action outcomes (continue, retry, halt, escalate)
  - Explicit precedence when multiple decision flags are set
  - Source-of-truth: interpreter follows nested flags, not code-name heuristics
  - Retry policy gating (budget exhaustion, custom policy)
  - Escalation context preservation
  - Malformed / missing-field envelopes fail closed
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_decision_engine import (
    AgentAction,
    DecisionOutcome,
    EscalationContext,
    RetryPolicy,
    interpret_cli_response,
)


# ── Envelope factories (mirror CLI contract, no dependency on openclaw_kalshi) ─

def _ok_envelope(code, result=None, warnings=None):
    r = dict(result) if result else {}
    r.setdefault("retryable", False)
    r.setdefault("halt_trading", False)
    r.setdefault("requires_human_review", False)
    return {
        "ok": True,
        "code": code,
        "result": r,
        "warnings": warnings if warnings else [],
    }


def _fail_envelope(code, error="error", details=None):
    d = dict(details) if details else {}
    d.setdefault("retryable", False)
    d.setdefault("halt_trading", False)
    d.setdefault("requires_human_review", False)
    return {
        "ok": False,
        "code": code,
        "error": error,
        "details": d,
    }


# ── Representative action outcomes ────────────────────────────────────────────

class TestRepresentativeOutcomes(unittest.TestCase):
    """Each test mirrors a named scenario from the plan."""

    def test_normal_success_continue(self):
        env = _ok_envelope("STATUS_OK", {"balance": 100})
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.CONTINUE)
        self.assertTrue(d.ok)
        self.assertFalse(d.halt_trading)
        self.assertFalse(d.requires_human_review)
        self.assertFalse(d.retry_allowed)
        self.assertIsNone(d.escalation)
        self.assertFalse(d.malformed)

    def test_sell_clamped_escalate(self):
        env = _ok_envelope("SELL_CLAMPED", {
            "action": "SELL",
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertTrue(d.ok)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.halt_trading)
        self.assertIsNotNone(d.escalation)
        self.assertEqual(d.escalation.code, "SELL_CLAMPED")
        self.assertTrue(d.escalation.ok)

    def test_sell_clamped_escalation_captures_warnings(self):
        warnings = [{"code": "POSITION_CLAMPED", "message": "capped at 5"}]
        env = _ok_envelope("SELL_CLAMPED", {
            "action": "SELL",
            "requires_human_review": True,
        }, warnings=warnings)
        d = interpret_cli_response(env)
        self.assertEqual(d.escalation.warnings, warnings)
        self.assertIn("action", d.escalation.payload)

    def test_retryable_transient_failure_retry(self):
        env = _fail_envelope("ORDERBOOK_FETCH_ERROR", "network error", {
            "retryable": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.RETRY)
        self.assertFalse(d.ok)
        self.assertTrue(d.retry_allowed)
        self.assertFalse(d.halt_trading)
        self.assertFalse(d.requires_human_review)

    def test_hard_stop_halt_and_escalate(self):
        env = _fail_envelope("STOP_TRADING", "stop file present", {
            "halt_trading": True,
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.halt_trading)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.retry_allowed)
        self.assertIsNotNone(d.escalation)

    def test_validation_failure_escalate_no_retry_no_halt(self):
        env = _fail_envelope("INVALID_TICKER", "bad ticker", {
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(d.ok)
        self.assertFalse(d.halt_trading)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.retry_allowed)

    def test_malformed_envelope_fail_closed(self):
        d = interpret_cli_response({"garbage": True})
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.halt_trading)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.retry_allowed)
        self.assertTrue(d.malformed)

    def test_empty_dict_fail_closed(self):
        d = interpret_cli_response({})
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.malformed)

    def test_none_like_fail_closed(self):
        d = interpret_cli_response(None)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.malformed)
        self.assertTrue(d.halt_trading)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.retry_allowed)


# ── Precedence tests ──────────────────────────────────────────────────────────

class TestPrecedence(unittest.TestCase):
    """When multiple flags are set, precedence must resolve deterministically."""

    def test_halt_outranks_retry_and_review(self):
        env = _fail_envelope("COMMAND_FAILED", "boom", {
            "retryable": True,
            "halt_trading": True,
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.halt_trading)
        self.assertFalse(d.retry_allowed)

    def test_halt_outranks_retry_alone(self):
        env = _fail_envelope("X", "x", {
            "retryable": True,
            "halt_trading": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)

    def test_halt_without_review_preserves_review_false(self):
        env = _fail_envelope("X", "x", {
            "halt_trading": True,
            "requires_human_review": False,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.halt_trading)
        self.assertFalse(d.requires_human_review)
        self.assertFalse(d.retry_allowed)

    def test_review_plus_retry_gives_combined_action(self):
        env = _fail_envelope("SOME_CODE", "err", {
            "retryable": True,
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.RETRY_AND_ESCALATE)
        self.assertTrue(d.retry_allowed)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.halt_trading)

    def test_review_without_retry_gives_escalate(self):
        env = _fail_envelope("SOME_CODE", "err", {
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(d.retry_allowed)

    def test_retry_without_review_gives_retry(self):
        env = _fail_envelope("SOME_CODE", "err", {
            "retryable": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.RETRY)
        self.assertTrue(d.retry_allowed)
        self.assertFalse(d.requires_human_review)

    def test_no_flags_success_gives_continue(self):
        env = _ok_envelope("OK", {"data": 1})
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.CONTINUE)

    def test_no_flags_failure_gives_escalate(self):
        env = _fail_envelope("WEIRD", "something")
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(d.requires_human_review)
        self.assertFalse(d.halt_trading)
        self.assertFalse(d.retry_allowed)


# ── Source-of-truth: flags over code names ────────────────────────────────────

class TestFlagsOverCodeNames(unittest.TestCase):
    """The interpreter must follow nested decision flags, not infer behavior
    from the response code string."""

    def test_halt_code_name_but_flags_say_continue(self):
        env = _ok_envelope("STOP_TRADING", {"data": 1})
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.CONTINUE)
        self.assertFalse(d.halt_trading)

    def test_innocuous_code_name_but_flags_say_halt(self):
        env = _fail_envelope("STATUS_OK", "unexpected failure", {
            "halt_trading": True,
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.halt_trading)

    def test_retryable_flag_honored_regardless_of_code(self):
        env = _fail_envelope("TOTALLY_UNKNOWN", "mystery", {
            "retryable": True,
        })
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.RETRY)


# ── Decision-flag preservation (no mutation) ──────────────────────────────────

class TestFlagPreservation(unittest.TestCase):
    """No non-malformed interpreter path may mutate halt_trading or
    requires_human_review away from the envelope's decision flag values."""

    _FLAG_COMBOS = [
        (False, False, False),
        (True,  False, False),
        (False, True,  False),
        (False, False, True),
        (True,  True,  False),
        (True,  False, True),
        (False, True,  True),
        (True,  True,  True),
    ]

    def _make_env(self, ok, retryable, halt, review):
        flags = {
            "retryable": retryable,
            "halt_trading": halt,
            "requires_human_review": review,
        }
        if ok:
            return _ok_envelope("TEST_CODE", flags)
        return _fail_envelope("TEST_CODE", "err", flags)

    def test_halt_trading_preserved_on_all_success_combos(self):
        for retryable, halt, review in self._FLAG_COMBOS:
            env = self._make_env(True, retryable, halt, review)
            d = interpret_cli_response(env)
            self.assertEqual(d.halt_trading, halt,
                f"halt_trading mutated for success flags={retryable},{halt},{review}")

    def test_halt_trading_preserved_on_all_failure_combos(self):
        for retryable, halt, review in self._FLAG_COMBOS:
            env = self._make_env(False, retryable, halt, review)
            d = interpret_cli_response(env)
            self.assertEqual(d.halt_trading, halt,
                f"halt_trading mutated for failure flags={retryable},{halt},{review}")

    def test_requires_human_review_preserved_on_all_success_combos(self):
        for retryable, halt, review in self._FLAG_COMBOS:
            env = self._make_env(True, retryable, halt, review)
            d = interpret_cli_response(env)
            self.assertEqual(d.requires_human_review, review,
                f"requires_human_review mutated for success flags={retryable},{halt},{review}")

    def test_requires_human_review_preserved_on_all_failure_combos(self):
        for retryable, halt, review in self._FLAG_COMBOS:
            env = self._make_env(False, retryable, halt, review)
            d = interpret_cli_response(env)
            self.assertEqual(d.requires_human_review, review,
                f"requires_human_review mutated for failure flags={retryable},{halt},{review}")


# ── Retry policy gating ──────────────────────────────────────────────────────

class TestRetryPolicyGating(unittest.TestCase):

    def test_default_allows_first_three_attempts(self):
        env = _fail_envelope("FETCH_ERR", "err", {"retryable": True})
        for attempt in range(3):
            d = interpret_cli_response(env, retry_attempt=attempt)
            self.assertEqual(d.action, AgentAction.RETRY, f"attempt {attempt}")
            self.assertTrue(d.retry_allowed)

    def test_default_exhausts_at_third_attempt(self):
        env = _fail_envelope("FETCH_ERR", "err", {"retryable": True})
        d = interpret_cli_response(env, retry_attempt=3)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(d.retry_allowed)

    def test_custom_max_attempts(self):
        policy = RetryPolicy(max_attempts=1)
        env = _fail_envelope("ERR", "err", {"retryable": True})
        d0 = interpret_cli_response(env, retry_attempt=0, retry_policy=policy)
        self.assertEqual(d0.action, AgentAction.RETRY)
        d1 = interpret_cli_response(env, retry_attempt=1, retry_policy=policy)
        self.assertEqual(d1.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(d1.retry_allowed)

    def test_exhausted_retryable_with_review_escalates(self):
        env = _fail_envelope("ERR", "err", {
            "retryable": True,
            "requires_human_review": True,
        })
        d = interpret_cli_response(env, retry_attempt=99)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(d.retry_allowed)
        self.assertTrue(d.requires_human_review)

    def test_non_retryable_ignores_budget(self):
        env = _fail_envelope("ERR", "err")
        d = interpret_cli_response(env, retry_attempt=0)
        self.assertNotEqual(d.action, AgentAction.RETRY)
        self.assertFalse(d.retry_allowed)

    def test_next_delay_exponential(self):
        policy = RetryPolicy(base_delay_seconds=1.0)
        self.assertAlmostEqual(policy.next_delay_seconds(0), 1.0)
        self.assertAlmostEqual(policy.next_delay_seconds(1), 2.0)
        self.assertAlmostEqual(policy.next_delay_seconds(2), 4.0)

    def test_next_delay_capped_at_60(self):
        policy = RetryPolicy(base_delay_seconds=1.0)
        self.assertAlmostEqual(policy.next_delay_seconds(100), 60.0)

    def test_can_retry_false_when_not_retryable(self):
        policy = RetryPolicy()
        self.assertFalse(policy.can_retry(False, 0))


# ── Escalation context ────────────────────────────────────────────────────────

class TestEscalationContext(unittest.TestCase):

    def test_failure_escalation_includes_error(self):
        env = _fail_envelope("CONFIG_ERROR", "bad config", {
            "halt_trading": True,
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertIsNotNone(d.escalation)
        self.assertEqual(d.escalation.error, "bad config")
        self.assertEqual(d.escalation.code, "CONFIG_ERROR")
        self.assertFalse(d.escalation.ok)

    def test_success_escalation_has_no_error(self):
        env = _ok_envelope("SELL_CLAMPED", {
            "action": "SELL",
            "requires_human_review": True,
        })
        d = interpret_cli_response(env)
        self.assertIsNone(d.escalation.error)
        self.assertTrue(d.escalation.ok)

    def test_escalation_payload_strips_decision_fields(self):
        env = _fail_envelope("ERR", "err", {
            "requires_human_review": True,
            "extra_info": "useful",
        })
        d = interpret_cli_response(env)
        self.assertIn("extra_info", d.escalation.payload)
        self.assertNotIn("retryable", d.escalation.payload)
        self.assertNotIn("halt_trading", d.escalation.payload)
        self.assertNotIn("requires_human_review", d.escalation.payload)

    def test_escalation_preserves_warnings(self):
        warnings = [{"code": "W1", "message": "watch out"}]
        env = _ok_envelope("SELL_CLAMPED", {
            "requires_human_review": True,
        }, warnings=warnings)
        d = interpret_cli_response(env)
        self.assertEqual(d.escalation.warnings, warnings)


# ── Malformed envelopes ───────────────────────────────────────────────────────

class TestMalformedEnvelopes(unittest.TestCase):

    def _assert_fail_closed(self, d: DecisionOutcome):
        self.assertEqual(d.action, AgentAction.HALT_TRADING)
        self.assertTrue(d.halt_trading)
        self.assertTrue(d.requires_human_review)
        self.assertFalse(d.retry_allowed)
        self.assertTrue(d.malformed)
        self.assertIsNotNone(d.escalation)

    def test_missing_ok(self):
        self._assert_fail_closed(interpret_cli_response({"code": "X"}))

    def test_ok_wrong_type(self):
        self._assert_fail_closed(interpret_cli_response({
            "ok": "yes", "code": "X", "result": {}, "warnings": [],
        }))

    def test_missing_result_on_success(self):
        self._assert_fail_closed(interpret_cli_response({
            "ok": True, "code": "X", "warnings": [],
        }))

    def test_missing_details_on_failure(self):
        self._assert_fail_closed(interpret_cli_response({
            "ok": False, "code": "X", "error": "e",
        }))

    def test_decision_field_not_bool(self):
        env = {
            "ok": True, "code": "X",
            "result": {"retryable": "yes", "halt_trading": False, "requires_human_review": False},
            "warnings": [],
        }
        self._assert_fail_closed(interpret_cli_response(env))

    def test_missing_decision_field(self):
        env = {
            "ok": True, "code": "X",
            "result": {"halt_trading": False, "requires_human_review": False},
            "warnings": [],
        }
        self._assert_fail_closed(interpret_cli_response(env))

    def test_extra_top_level_key(self):
        env = {
            "ok": True, "code": "X",
            "result": {"retryable": False, "halt_trading": False, "requires_human_review": False},
            "warnings": [],
            "bonus": True,
        }
        self._assert_fail_closed(interpret_cli_response(env))

    def test_none_input(self):
        self._assert_fail_closed(interpret_cli_response(None))

    def test_list_input(self):
        self._assert_fail_closed(interpret_cli_response([1, 2, 3]))

    def test_string_input(self):
        self._assert_fail_closed(interpret_cli_response("not a dict"))

    def test_malformed_preserves_code_when_available(self):
        d = interpret_cli_response({"code": "VISIBLE", "extra": True})
        self.assertTrue(d.malformed)
        self.assertEqual(d.code, "VISIBLE")


# ── Integration with real CLI contract shapes ─────────────────────────────────

class TestWithCLIContractShapes(unittest.TestCase):
    """Build envelopes that exactly match the CLI's _success/_failure output
    shapes (without importing openclaw_kalshi) and verify the interpreter
    produces correct decisions for the full policy table."""

    def _cli_success(self, code, result=None, warnings=None,
                     retryable=False, halt_trading=False, requires_human_review=False):
        r = dict(result) if result else {}
        r["retryable"] = retryable
        r["halt_trading"] = halt_trading
        r["requires_human_review"] = requires_human_review
        return {"ok": True, "code": code, "result": r, "warnings": warnings or []}

    def _cli_failure(self, code, error, details=None,
                     retryable=False, halt_trading=False, requires_human_review=False):
        d = dict(details) if details else {}
        d["retryable"] = retryable
        d["halt_trading"] = halt_trading
        d["requires_human_review"] = requires_human_review
        return {"ok": False, "code": code, "error": error, "details": d}

    def test_buy_placed(self):
        env = self._cli_success("BUY_PLACED", {"action": "BUY"})
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.CONTINUE)

    def test_orderbook_empty_retry(self):
        env = self._cli_success("ORDERBOOK_EMPTY", {"ticker": "X"}, retryable=True)
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.RETRY)

    def test_orderbook_fetch_error_retry(self):
        env = self._cli_failure("ORDERBOOK_FETCH_ERROR", "timeout", retryable=True)
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.RETRY)

    def test_stop_trading_halt(self):
        env = self._cli_failure("STOP_TRADING", "stop file",
                                halt_trading=True, requires_human_review=True)
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)

    def test_config_error_halt(self):
        env = self._cli_failure("CONFIG_ERROR", "bad config",
                                halt_trading=True, requires_human_review=True)
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.HALT_TRADING)

    def test_invalid_ticker_escalate(self):
        env = self._cli_failure("INVALID_TICKER", "bad ticker",
                                requires_human_review=True)
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)

    def test_sell_clamped_escalate(self):
        env = self._cli_success("SELL_CLAMPED", {"action": "SELL"},
                                requires_human_review=True)
        d = interpret_cli_response(env)
        self.assertEqual(d.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertTrue(d.ok)


if __name__ == "__main__":
    unittest.main()
