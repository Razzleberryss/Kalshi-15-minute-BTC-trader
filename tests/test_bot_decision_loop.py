"""
Tests for bot ↔ decision-engine integration.

Verifies that each AgentAction triggers the correct behavior in the
execution loop:

  - CONTINUE:           proceed to next trade cycle normally
  - RETRY:              wait and re-execute with incrementing attempt counter
  - HALT_TRADING:       log, write STOP_TRADING file, stop loop
  - ESCALATE_TO_HUMAN:  log escalation event, abort current operation
  - RETRY_AND_ESCALATE: log escalation event *and* retry the operation

Layers tested:
  1. cli_executor.execute_cli      — subprocess ↔ JSON parsing
  2. cli_executor.execute_with_decision_engine — full retry/halt/escalate loop
  3. cli_executor.log_escalation   — structured escalation logging
  4. cli_executor.write_stop_trading_file — halt flag persistence
  5. bot._cli_buy / bot._cli_sell  — arg formatting + passthrough
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from agent_decision_engine import (
    AgentAction,
    DecisionOutcome,
    EscalationContext,
    RetryPolicy,
    interpret_cli_response,
)
import cli_executor


# ── Envelope factories (mirror CLI contract) ──────────────────────────────

def _ok_envelope(code, result=None, warnings=None):
    r = dict(result) if result else {}
    r.setdefault("retryable", False)
    r.setdefault("halt_trading", False)
    r.setdefault("requires_human_review", False)
    return {"ok": True, "code": code, "result": r, "warnings": warnings or []}


def _fail_envelope(code, error="error", details=None):
    d = dict(details) if details else {}
    d.setdefault("retryable", False)
    d.setdefault("halt_trading", False)
    d.setdefault("requires_human_review", False)
    return {"ok": False, "code": code, "error": error, "details": d}


# ══════════════════════════════════════════════════════════════════════════
# Layer 1: execute_cli — subprocess wrapper
# ══════════════════════════════════════════════════════════════════════════

class TestExecuteCli(unittest.TestCase):
    """Subprocess wrapper must always return a dict suitable for
    interpret_cli_response, even on errors."""

    @patch("cli_executor.subprocess.run")
    def test_successful_call_returns_parsed_json(self, mock_run):
        envelope = _ok_envelope("BUY_PLACED", {"order_id": "abc123"})
        mock_run.return_value = MagicMock(
            stdout=json.dumps(envelope), returncode=0, stderr="",
        )
        result = cli_executor.execute_cli(["buy", "yes", "1", "50"])
        self.assertEqual(result, envelope)

    @patch("cli_executor.subprocess.run")
    def test_empty_stdout_returns_retryable_failure(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", returncode=1, stderr="oops")
        result = cli_executor.execute_cli(["buy", "yes", "1", "50"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "EMPTY_STDOUT")
        self.assertTrue(result["details"]["retryable"])
        self.assertFalse(result["details"]["requires_human_review"])

    @patch("cli_executor.subprocess.run")
    def test_timeout_returns_retryable_failure(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="test", timeout=30)
        result = cli_executor.execute_cli(["buy", "yes", "1", "50"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLI_TIMEOUT")
        self.assertTrue(result["details"]["retryable"])

    @patch("cli_executor.subprocess.run")
    def test_invalid_json_returns_non_retryable_failure(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout="not-json{", returncode=0, stderr="",
        )
        result = cli_executor.execute_cli(["buy", "yes", "1", "50"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "JSON_PARSE_ERROR")
        self.assertFalse(result["details"]["retryable"])
        self.assertTrue(result["details"]["halt_trading"])

    @patch("cli_executor.subprocess.run")
    def test_subprocess_exception_returns_non_retryable_failure(self, mock_run):
        mock_run.side_effect = OSError("command not found")
        result = cli_executor.execute_cli(["buy", "yes", "1", "50"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "CLI_EXEC_ERROR")
        self.assertFalse(result["details"]["retryable"])

    @patch("cli_executor.subprocess.run")
    def test_whitespace_only_stdout_treated_as_empty(self, mock_run):
        mock_run.return_value = MagicMock(stdout="  \n  ", returncode=0, stderr="")
        result = cli_executor.execute_cli(["status"])
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "EMPTY_STDOUT")

    @patch("cli_executor.subprocess.run")
    def test_passes_args_to_subprocess(self, mock_run):
        envelope = _ok_envelope("STATUS_OK")
        mock_run.return_value = MagicMock(
            stdout=json.dumps(envelope), returncode=0, stderr="",
        )
        cli_executor.execute_cli(["status", "--series", "KXBTCD"])
        cmd = mock_run.call_args[0][0]
        self.assertIn("status", cmd)
        self.assertIn("--series", cmd)
        self.assertIn("KXBTCD", cmd)


# ══════════════════════════════════════════════════════════════════════════
# Layer 2: execute_with_decision_engine — retry/halt/escalate loop
# ══════════════════════════════════════════════════════════════════════════

class TestContinueAction(unittest.TestCase):
    """CONTINUE → return immediately with (outcome, envelope)."""

    @patch("cli_executor.execute_cli")
    def test_continue_returns_on_first_call(self, mock_cli):
        envelope = _ok_envelope("BUY_PLACED", {"order_id": "abc"})
        mock_cli.return_value = envelope

        outcome, env = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"],
        )
        self.assertEqual(outcome.action, AgentAction.CONTINUE)
        self.assertEqual(env, envelope)
        mock_cli.assert_called_once()


class TestRetryAction(unittest.TestCase):
    """RETRY → wait backoff delay, re-execute, increment attempt counter."""

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.execute_cli")
    def test_retry_then_succeed(self, mock_cli, mock_sleep):
        retry_env = _fail_envelope("FETCH_ERR", "err", {"retryable": True})
        ok_env = _ok_envelope("BUY_PLACED", {"order_id": "abc"})
        mock_cli.side_effect = [retry_env, ok_env]

        outcome, env = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"],
        )
        self.assertEqual(outcome.action, AgentAction.CONTINUE)
        self.assertEqual(mock_cli.call_count, 2)
        mock_sleep.assert_called_once()

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.execute_cli")
    def test_retry_uses_exponential_backoff(self, mock_cli, mock_sleep):
        retry_env = _fail_envelope("ERR", "err", {"retryable": True})
        ok_env = _ok_envelope("OK")
        mock_cli.side_effect = [retry_env, retry_env, ok_env]

        policy = RetryPolicy(base_delay_seconds=1.0, max_attempts=5)
        cli_executor.execute_with_decision_engine(
            ["status"], retry_policy=policy,
        )
        delays = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertAlmostEqual(delays[0], 1.0)   # 1 * 2^0
        self.assertAlmostEqual(delays[1], 2.0)   # 1 * 2^1

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.execute_cli")
    def test_retry_exhaustion_escalates_to_human(self, mock_cli, mock_sleep):
        retry_env = _fail_envelope("FETCH_ERR", "err", {"retryable": True})
        mock_cli.return_value = retry_env

        policy = RetryPolicy(max_attempts=2)
        outcome, _ = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"], retry_policy=policy,
        )
        self.assertEqual(outcome.action, AgentAction.ESCALATE_TO_HUMAN)
        # 3 CLI calls: attempt 0, 1, 2 (exhausted at attempt=2)
        self.assertEqual(mock_cli.call_count, 3)

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.execute_cli")
    def test_retry_counter_is_local_to_call(self, mock_cli, mock_sleep):
        """Retry state must reset between unrelated operations."""
        retry_env = _fail_envelope("ERR", "err", {"retryable": True})
        ok_env = _ok_envelope("OK")

        mock_cli.side_effect = [retry_env, ok_env]
        outcome1, _ = cli_executor.execute_with_decision_engine(["status"])
        self.assertEqual(outcome1.action, AgentAction.CONTINUE)

        mock_cli.side_effect = [retry_env, ok_env]
        outcome2, _ = cli_executor.execute_with_decision_engine(["status"])
        self.assertEqual(outcome2.action, AgentAction.CONTINUE)


class TestHaltTradingAction(unittest.TestCase):
    """HALT_TRADING → log escalation, write STOP_TRADING file, return."""

    @patch("cli_executor.write_stop_trading_file")
    @patch("cli_executor.log_escalation")
    @patch("cli_executor.execute_cli")
    def test_halt_writes_stop_file_and_logs(self, mock_cli, mock_log, mock_write):
        halt_env = _fail_envelope("STOP_TRADING", "stop file present", {
            "halt_trading": True, "requires_human_review": True,
        })
        mock_cli.return_value = halt_env

        outcome, env = cli_executor.execute_with_decision_engine(["status"])
        self.assertEqual(outcome.action, AgentAction.HALT_TRADING)
        mock_write.assert_called_once()
        mock_log.assert_called_once()
        mock_cli.assert_called_once()

    @patch("cli_executor.write_stop_trading_file")
    @patch("cli_executor.execute_cli")
    def test_halt_does_not_retry(self, mock_cli, mock_write):
        halt_env = _fail_envelope("CONFIG_ERROR", "bad config", {
            "halt_trading": True, "retryable": True,
            "requires_human_review": True,
        })
        mock_cli.return_value = halt_env

        outcome, _ = cli_executor.execute_with_decision_engine(["buy", "yes", "1", "50"])
        self.assertEqual(outcome.action, AgentAction.HALT_TRADING)
        mock_cli.assert_called_once()

    @patch("cli_executor.write_stop_trading_file")
    @patch("cli_executor.execute_cli")
    def test_halt_invokes_on_escalation_callback(self, mock_cli, mock_write):
        halt_env = _fail_envelope("STOP_TRADING", "stop", {
            "halt_trading": True, "requires_human_review": True,
        })
        mock_cli.return_value = halt_env
        callback = MagicMock()

        cli_executor.execute_with_decision_engine(
            ["status"], on_escalation=callback,
        )
        callback.assert_called_once()


class TestEscalateToHumanAction(unittest.TestCase):
    """ESCALATE_TO_HUMAN → log escalation event, return without retry."""

    @patch("cli_executor.log_escalation")
    @patch("cli_executor.execute_cli")
    def test_escalate_logs_and_returns(self, mock_cli, mock_log):
        esc_env = _fail_envelope("INVALID_TICKER", "bad ticker", {
            "requires_human_review": True,
        })
        mock_cli.return_value = esc_env

        outcome, _ = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"],
        )
        self.assertEqual(outcome.action, AgentAction.ESCALATE_TO_HUMAN)
        mock_log.assert_called_once()
        mock_cli.assert_called_once()

    @patch("cli_executor.log_escalation")
    @patch("cli_executor.execute_cli")
    def test_escalate_invokes_callback(self, mock_cli, mock_log):
        esc_env = _fail_envelope("NO_POSITION", "no pos", {
            "requires_human_review": True,
        })
        mock_cli.return_value = esc_env
        callback = MagicMock()

        cli_executor.execute_with_decision_engine(
            ["sell", "yes", "1", "50"], on_escalation=callback,
        )
        callback.assert_called_once()


class TestRetryAndEscalateAction(unittest.TestCase):
    """RETRY_AND_ESCALATE → log escalation, then retry the operation."""

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.log_escalation")
    @patch("cli_executor.execute_cli")
    def test_retry_and_escalate_logs_then_retries(self, mock_cli, mock_log, mock_sleep):
        esc_retry_env = _fail_envelope("SOME_ERR", "err", {
            "retryable": True, "requires_human_review": True,
        })
        ok_env = _ok_envelope("BUY_PLACED", {"order_id": "abc"})
        mock_cli.side_effect = [esc_retry_env, ok_env]

        outcome, _ = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"],
        )
        self.assertEqual(outcome.action, AgentAction.CONTINUE)
        mock_log.assert_called_once()
        mock_sleep.assert_called_once()

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.log_escalation")
    @patch("cli_executor.execute_cli")
    def test_retry_and_escalate_exhaustion_escalates(self, mock_cli, mock_log, mock_sleep):
        esc_retry_env = _fail_envelope("ERR", "err", {
            "retryable": True, "requires_human_review": True,
        })
        mock_cli.return_value = esc_retry_env

        policy = RetryPolicy(max_attempts=1)
        outcome, _ = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"], retry_policy=policy,
        )
        self.assertEqual(outcome.action, AgentAction.ESCALATE_TO_HUMAN)
        # Logged on the RETRY_AND_ESCALATE attempt, then again on final ESCALATE_TO_HUMAN
        self.assertGreaterEqual(mock_log.call_count, 1)

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.execute_cli")
    def test_retry_and_escalate_invokes_callback_each_attempt(self, mock_cli, mock_sleep):
        esc_retry_env = _fail_envelope("ERR", "err", {
            "retryable": True, "requires_human_review": True,
        })
        ok_env = _ok_envelope("OK")
        mock_cli.side_effect = [esc_retry_env, esc_retry_env, ok_env]

        callback = MagicMock()
        cli_executor.execute_with_decision_engine(
            ["status"], on_escalation=callback,
        )
        self.assertEqual(callback.call_count, 2)


# ══════════════════════════════════════════════════════════════════════════
# Layer 3: log_escalation — structured commander intel
# ══════════════════════════════════════════════════════════════════════════

class TestLogEscalation(unittest.TestCase):

    @patch("cli_executor.log")
    def test_logs_with_full_escalation_context(self, mock_log):
        esc = EscalationContext(
            code="CONFIG_ERROR", ok=False, error="bad config",
            warnings=[{"code": "W1", "message": "warning"}],
            payload={"extra": "data"},
        )
        outcome = DecisionOutcome(
            ok=False, code="CONFIG_ERROR",
            action=AgentAction.HALT_TRADING,
            retry_allowed=False, halt_trading=True,
            requires_human_review=True, escalation=esc,
        )
        cli_executor.log_escalation(outcome)
        mock_log.warning.assert_called_once()
        log_args = mock_log.warning.call_args[0]
        parsed = json.loads(log_args[1])
        self.assertEqual(parsed["escalation_code"], "CONFIG_ERROR")
        self.assertEqual(parsed["escalation_error"], "bad config")
        self.assertEqual(parsed["escalation_warnings"], [{"code": "W1", "message": "warning"}])
        self.assertEqual(parsed["action"], "HALT_TRADING")

    @patch("cli_executor.log")
    def test_logs_without_escalation_context(self, mock_log):
        outcome = DecisionOutcome(
            ok=False, code="UNKNOWN",
            action=AgentAction.ESCALATE_TO_HUMAN,
            retry_allowed=False, halt_trading=False,
            requires_human_review=True,
        )
        cli_executor.log_escalation(outcome)
        mock_log.warning.assert_called_once()
        parsed = json.loads(mock_log.warning.call_args[0][1])
        self.assertEqual(parsed["action"], "ESCALATE_TO_HUMAN")
        self.assertNotIn("escalation_code", parsed)


# ══════════════════════════════════════════════════════════════════════════
# Layer 4: write_stop_trading_file — halt flag persistence
# ══════════════════════════════════════════════════════════════════════════

class TestWriteStopTradingFile(unittest.TestCase):

    def setUp(self):
        self._original = cli_executor.STOP_TRADING_FILE
        self._test_file = Path(f"/tmp/test_stop_trading_{os.getpid()}")
        cli_executor.STOP_TRADING_FILE = self._test_file

    def tearDown(self):
        cli_executor.STOP_TRADING_FILE = self._original
        if self._test_file.exists():
            self._test_file.unlink()

    def test_creates_file_with_context(self):
        outcome = DecisionOutcome(
            ok=False, code="STOP_TRADING",
            action=AgentAction.HALT_TRADING,
            retry_allowed=False, halt_trading=True,
            requires_human_review=True,
            escalation=EscalationContext(
                code="STOP_TRADING", ok=False, error="stop file present",
                warnings=[{"code": "W", "message": "w"}],
            ),
        )
        cli_executor.write_stop_trading_file(outcome)
        self.assertTrue(self._test_file.exists())
        content = json.loads(self._test_file.read_text())
        self.assertEqual(content["code"], "STOP_TRADING")
        self.assertEqual(content["action"], "HALT_TRADING")
        self.assertFalse(content["ok"])
        self.assertIn("halted_at", content)
        self.assertEqual(content["escalation_code"], "STOP_TRADING")
        self.assertEqual(content["escalation_error"], "stop file present")

    def test_creates_file_without_escalation(self):
        outcome = DecisionOutcome(
            ok=False, code="UNKNOWN",
            action=AgentAction.HALT_TRADING,
            retry_allowed=False, halt_trading=True,
            requires_human_review=True,
        )
        cli_executor.write_stop_trading_file(outcome)
        self.assertTrue(self._test_file.exists())
        content = json.loads(self._test_file.read_text())
        self.assertEqual(content["code"], "UNKNOWN")
        self.assertNotIn("escalation_code", content)


# ══════════════════════════════════════════════════════════════════════════
# Layer 5: bot._cli_buy / bot._cli_sell — arg formatting
# ══════════════════════════════════════════════════════════════════════════

class TestCliBuyArgs(unittest.TestCase):
    """_cli_buy must format args correctly for the CLI."""

    @patch("cli_executor.execute_with_decision_engine")
    def test_builds_buy_args(self, mock_exec):
        ok_outcome = DecisionOutcome(
            ok=True, code="BUY_PLACED", action=AgentAction.CONTINUE,
            retry_allowed=False, halt_trading=False,
            requires_human_review=False,
        )
        mock_exec.return_value = (ok_outcome, _ok_envelope("BUY_PLACED"))

        import bot
        bot._cli_buy("KXBTCD-28MAR2615-B85000", "yes", 5, 45)
        mock_exec.assert_called_once_with(
            ["buy", "--ticker", "KXBTCD-28MAR2615-B85000", "yes", "5", "45"],
        )

    @patch("cli_executor.execute_with_decision_engine")
    def test_builds_buy_args_with_dry_run(self, mock_exec):
        ok_outcome = DecisionOutcome(
            ok=True, code="BUY_DRY_RUN", action=AgentAction.CONTINUE,
            retry_allowed=False, halt_trading=False,
            requires_human_review=False,
        )
        mock_exec.return_value = (ok_outcome, _ok_envelope("BUY_DRY_RUN"))

        import bot
        bot._cli_buy("KXBTCD-28MAR2615-B85000", "no", 10, 50, dry_run=True)
        mock_exec.assert_called_once_with(
            ["buy", "--ticker", "KXBTCD-28MAR2615-B85000", "no", "10", "50", "--dry-run"],
        )


class TestCliSellArgs(unittest.TestCase):
    """_cli_sell must format args correctly for the CLI."""

    @patch("cli_executor.execute_with_decision_engine")
    def test_builds_sell_args(self, mock_exec):
        ok_outcome = DecisionOutcome(
            ok=True, code="SELL_PLACED", action=AgentAction.CONTINUE,
            retry_allowed=False, halt_trading=False,
            requires_human_review=False,
        )
        mock_exec.return_value = (ok_outcome, _ok_envelope("SELL_PLACED"))

        import bot
        bot._cli_sell("KXBTCD-28MAR2615-B85000", "yes", 3, 55)
        mock_exec.assert_called_once_with(
            ["sell", "--ticker", "KXBTCD-28MAR2615-B85000", "yes", "3", "55"],
        )

    @patch("cli_executor.execute_with_decision_engine")
    def test_builds_sell_args_with_dry_run(self, mock_exec):
        ok_outcome = DecisionOutcome(
            ok=True, code="SELL_DRY_RUN", action=AgentAction.CONTINUE,
            retry_allowed=False, halt_trading=False,
            requires_human_review=False,
        )
        mock_exec.return_value = (ok_outcome, _ok_envelope("SELL_DRY_RUN"))

        import bot
        bot._cli_sell("KXBTCD-28MAR2615-B85000", "no", 2, 40, dry_run=True)
        mock_exec.assert_called_once_with(
            ["sell", "--ticker", "KXBTCD-28MAR2615-B85000", "no", "2", "40", "--dry-run"],
        )


# ══════════════════════════════════════════════════════════════════════════
# Integration: synthetic failure envelopes route through decision engine
# ══════════════════════════════════════════════════════════════════════════

class TestSyntheticEnvelopes(unittest.TestCase):
    """Verify _synthetic_failure envelopes are well-formed for
    interpret_cli_response and route to the correct action."""

    def test_timeout_envelope_routes_to_retry(self):
        env = cli_executor._synthetic_failure(
            "CLI_TIMEOUT", "timed out", retryable=True,
        )
        outcome = interpret_cli_response(env)
        self.assertEqual(outcome.action, AgentAction.RETRY)

    def test_exec_error_envelope_routes_to_halt(self):
        env = cli_executor._synthetic_failure(
            "CLI_EXEC_ERROR", "not found", retryable=False,
        )
        outcome = interpret_cli_response(env)
        self.assertEqual(outcome.action, AgentAction.HALT_TRADING)

    def test_json_parse_error_envelope_routes_to_halt(self):
        env = cli_executor._synthetic_failure(
            "JSON_PARSE_ERROR", "bad json", retryable=False,
        )
        outcome = interpret_cli_response(env)
        self.assertEqual(outcome.action, AgentAction.HALT_TRADING)

    def test_empty_stdout_envelope_routes_to_retry(self):
        env = cli_executor._synthetic_failure(
            "EMPTY_STDOUT", "no output", retryable=True,
        )
        outcome = interpret_cli_response(env)
        self.assertEqual(outcome.action, AgentAction.RETRY)


# ══════════════════════════════════════════════════════════════════════════
# End-to-end: full decision engine loop scenarios
# ══════════════════════════════════════════════════════════════════════════

class TestEndToEndScenarios(unittest.TestCase):
    """Full-path scenarios combining execute_cli mock → decision engine → action."""

    @patch("cli_executor.time.sleep")
    @patch("cli_executor.subprocess.run")
    def test_transient_failure_retry_then_success(self, mock_run, mock_sleep):
        """Network error → RETRY → success on second attempt."""
        retry_env = _fail_envelope("ORDERBOOK_FETCH_ERROR", "timeout", {
            "retryable": True,
        })
        ok_env = _ok_envelope("BUY_PLACED", {"order_id": "xyz"})
        mock_run.side_effect = [
            MagicMock(stdout=json.dumps(retry_env), returncode=1, stderr=""),
            MagicMock(stdout=json.dumps(ok_env), returncode=0, stderr=""),
        ]

        outcome, env = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"],
        )
        self.assertEqual(outcome.action, AgentAction.CONTINUE)
        self.assertEqual(env["result"]["order_id"], "xyz")
        mock_sleep.assert_called_once()

    @patch("cli_executor.write_stop_trading_file")
    @patch("cli_executor.subprocess.run")
    def test_stop_trading_halts_immediately(self, mock_run, mock_write):
        """STOP_TRADING file detected → HALT_TRADING, no retry."""
        halt_env = _fail_envelope("STOP_TRADING", "stop file exists", {
            "halt_trading": True, "requires_human_review": True,
        })
        mock_run.return_value = MagicMock(
            stdout=json.dumps(halt_env), returncode=1, stderr="",
        )

        outcome, _ = cli_executor.execute_with_decision_engine(["buy", "yes", "1", "50"])
        self.assertEqual(outcome.action, AgentAction.HALT_TRADING)
        mock_write.assert_called_once()
        mock_run.assert_called_once()

    @patch("cli_executor.subprocess.run")
    def test_validation_error_escalates_without_retry(self, mock_run):
        """INVALID_TICKER → ESCALATE_TO_HUMAN, no retry."""
        esc_env = _fail_envelope("INVALID_TICKER", "bad ticker", {
            "requires_human_review": True,
        })
        mock_run.return_value = MagicMock(
            stdout=json.dumps(esc_env), returncode=1, stderr="",
        )

        outcome, _ = cli_executor.execute_with_decision_engine(
            ["buy", "yes", "1", "50"],
        )
        self.assertEqual(outcome.action, AgentAction.ESCALATE_TO_HUMAN)
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
