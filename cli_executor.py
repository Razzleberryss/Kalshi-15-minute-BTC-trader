"""
cli_executor.py — Deterministic CLI execution with decision-engine-driven
retry, halt, and escalation handling.

Wraps subprocess calls to openclaw_kalshi.py, parses JSON envelopes from
stdout, and routes through agent_decision_engine.interpret_cli_response()
for deterministic action branching.

Thread model: single-threaded, blocking.  Retry delays are synchronous sleeps.
Retry state is always local to a single execute_with_decision_engine() call
and never shared across unrelated operations.

STOP_TRADING file path matches the CLI's own STOP_FILE so both the bot loop
and manual CLI invocations observe the same halt signal.
"""

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Callable

from agent_decision_engine import (
    AgentAction,
    DecisionOutcome,
    RetryPolicy,
    interpret_cli_response,
)

log = logging.getLogger("cli_executor")

CLI_SCRIPT = str(Path(__file__).parent / "openclaw_kalshi.py")
# Backward-compatible constant (tests may patch this), but prefer reading the
# environment at call time so test modules can set OPENCLAW_STOP_FILE even if
# cli_executor was imported earlier.
_DEFAULT_STOP_TRADING_FILE = Path.home() / ".openclaw" / "workspace" / "STOP_TRADING"
STOP_TRADING_FILE = _DEFAULT_STOP_TRADING_FILE


def _stop_trading_path() -> Path:
    # If tests patched STOP_TRADING_FILE, honor that over the environment.
    if STOP_TRADING_FILE != _DEFAULT_STOP_TRADING_FILE:
        return STOP_TRADING_FILE
    return Path(os.environ.get("OPENCLAW_STOP_FILE", str(STOP_TRADING_FILE)))


def execute_cli(args: list, timeout: int = 30) -> dict:
    """Run openclaw_kalshi.py as a subprocess and return the parsed JSON envelope.

    Returns a well-formed failure envelope (suitable for interpret_cli_response)
    for any subprocess or JSON-parse error, so callers always get a dict.
    """
    cmd = [sys.executable, CLI_SCRIPT] + [str(a) for a in args]
    log.debug("CLI exec: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return _synthetic_failure(
            "CLI_TIMEOUT",
            f"CLI timed out after {timeout}s",
            retryable=True,
        )
    except Exception as exc:
        return _synthetic_failure(
            "CLI_EXEC_ERROR",
            f"Failed to execute CLI: {exc}",
            retryable=False,
        )

    stdout = result.stdout.strip()
    if not stdout:
        return _synthetic_failure(
            "EMPTY_STDOUT",
            f"CLI produced no stdout (exit {result.returncode})",
            retryable=True,
            stderr=result.stderr[:500] if result.stderr else "",
        )

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        return _synthetic_failure(
            "JSON_PARSE_ERROR",
            f"Failed to parse CLI stdout: {exc}",
            retryable=False,
            raw_stdout=stdout[:500],
        )


def _synthetic_failure(code: str, error: str, retryable: bool = False, **extra) -> dict:
    """Build a well-formed failure envelope for errors outside the CLI's control.

    The envelope matches the CLI contract so interpret_cli_response() can
    process it through the normal precedence rules.  Transient retryable
    errors don't require human review; non-retryable ones halt and escalate.
    """
    details = dict(extra)
    details["retryable"] = retryable
    details["halt_trading"] = not retryable
    details["requires_human_review"] = not retryable
    return {"ok": False, "code": code, "error": error, "details": details}


def log_escalation(outcome: DecisionOutcome) -> None:
    """Log a structured escalation event for human commander review.

    Includes escalation code, error, and warnings so the commander can
    triage without re-parsing the raw envelope.
    """
    esc = outcome.escalation
    event = {
        "event": "ESCALATION",
        "action": outcome.action.value,
        "code": outcome.code,
        "ok": outcome.ok,
        "malformed": outcome.malformed,
    }
    if esc:
        event["escalation_code"] = esc.code
        event["escalation_error"] = esc.error
        event["escalation_warnings"] = esc.warnings
        event["escalation_payload"] = esc.payload
    log.warning("ESCALATION %s", json.dumps(event, default=str))


def write_stop_trading_file(outcome: DecisionOutcome) -> None:
    """Write STOP_TRADING flag file so both the bot loop and CLI halt.

    Uses the same path as openclaw_kalshi.STOP_FILE to ensure a single
    source of truth for the halt signal.
    """
    try:
        path = _stop_trading_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        context = {
            "action": outcome.action.value,
            "code": outcome.code,
            "ok": outcome.ok,
            "halted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if outcome.escalation:
            context["escalation_code"] = outcome.escalation.code
            context["escalation_error"] = outcome.escalation.error
            context["escalation_warnings"] = outcome.escalation.warnings
        path.write_text(
            json.dumps(context, indent=2), encoding="utf-8",
        )
        log.critical("STOP_TRADING file written: %s", path)
    except Exception as exc:
        log.error("Failed to write STOP_TRADING file: %s", exc)


def execute_with_decision_engine(
    args: list,
    *,
    retry_policy: Optional[RetryPolicy] = None,
    timeout: int = 30,
    on_escalation: Optional[Callable[[DecisionOutcome], None]] = None,
    envelope_fn: Optional[Callable[[], Any]] = None,
) -> tuple:
    """Execute a CLI command with full decision-engine-driven retry/escalate loop.

    Returns ``(DecisionOutcome, envelope_dict)``.

    If *envelope_fn* is set, it is called instead of ``execute_cli`` to obtain
    the JSON envelope (in-process orders). *args* is ignored in that case.

    Retry state is local to this invocation — the attempt counter starts at 0
    and is never shared with other calls.  On CONTINUE the counter is implicitly
    reset because a new call creates a fresh counter.
    """
    policy = retry_policy or RetryPolicy()
    attempt = 0

    while True:
        if envelope_fn is not None:
            try:
                envelope = envelope_fn()
            except Exception as exc:
                log.exception("envelope_fn() failed during execute_with_decision_engine")
                envelope = {
                    "ok": False,
                    "code": "ENVELOPE_FN_EXCEPTION",
                    "error": f"{type(exc).__name__}: {exc}",
                    "details": {
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc),
                        "retryable": False,
                        "escalate": True,
                        "halt_trading": False,
                    },
                }
        else:
            envelope = execute_cli(args, timeout=timeout)
        outcome = interpret_cli_response(
            envelope, retry_attempt=attempt, retry_policy=policy,
        )

        if outcome.action == AgentAction.CONTINUE:
            return outcome, envelope

        if outcome.action == AgentAction.RETRY:
            delay = policy.next_delay_seconds(attempt)
            log.warning(
                "RETRY attempt=%d/%d delay=%.1fs code=%s",
                attempt + 1, policy.max_attempts, delay, outcome.code,
            )
            time.sleep(delay)
            attempt += 1
            continue

        if outcome.action == AgentAction.RETRY_AND_ESCALATE:
            log_escalation(outcome)
            if on_escalation:
                on_escalation(outcome)
            delay = policy.next_delay_seconds(attempt)
            log.warning(
                "RETRY_AND_ESCALATE attempt=%d/%d delay=%.1fs code=%s",
                attempt + 1, policy.max_attempts, delay, outcome.code,
            )
            time.sleep(delay)
            attempt += 1
            continue

        if outcome.action == AgentAction.HALT_TRADING:
            log_escalation(outcome)
            if on_escalation:
                on_escalation(outcome)
            write_stop_trading_file(outcome)
            log.critical("HALT_TRADING — stopping. code=%s", outcome.code)
            return outcome, envelope

        if outcome.action == AgentAction.ESCALATE_TO_HUMAN:
            log_escalation(outcome)
            if on_escalation:
                on_escalation(outcome)
            return outcome, envelope

        # Unreachable for known actions — fail closed
        log.critical("Unknown action %s — treating as HALT_TRADING", outcome.action)
        write_stop_trading_file(outcome)
        return outcome, envelope
