"""
agent_decision_engine.py — Deterministic agent-side interpreter for CLI response envelopes.

Consumes the structured JSON envelope produced by ``openclaw_kalshi.py`` and
emits a normalized ``DecisionOutcome`` describing the single next action the
agent should take.

Design principles:
  - Agent-first: this is an internal control-plane module, not a UI feature.
  - Deterministic: every input maps to exactly one output via explicit rules.
  - Fail-closed: malformed envelopes halt trading and require human review.
  - Thin: stdlib-only, no scheduler, no queue, no side effects.

Precedence rules (highest to lowest):
  1. ``halt_trading`` — always wins.  Action: HALT_TRADING.
  2. ``requires_human_review`` — escalation semantics.
       a. If also ``retryable`` and retry budget remains: RETRY_AND_ESCALATE.
       b. Otherwise: ESCALATE_TO_HUMAN.
  3. ``retryable`` (without human review) — transient failure.
       If retry budget remains: RETRY.
       Else: ESCALATE_TO_HUMAN (budget exhausted is notable).
  4. ``ok=True`` with no flags — normal success.  Action: CONTINUE.
  5. ``ok=False``, non-retryable, non-halt — terminal failure.
       Action: ESCALATE_TO_HUMAN (safe path; surface the issue).

The interpreter trusts the nested decision flags as the source of truth.
Response code names are *not* used for branching; they are preserved for
logging and commander context only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, unique
from typing import Any, Optional

# ── Action vocabulary ─────────────────────────────────────────────────────────

@unique
class AgentAction(Enum):
    """Deterministic action the agent must take next."""
    CONTINUE = "CONTINUE"
    RETRY = "RETRY"
    HALT_TRADING = "HALT_TRADING"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    RETRY_AND_ESCALATE = "RETRY_AND_ESCALATE"


# ── Retry policy ──────────────────────────────────────────────────────────────

DEFAULT_MAX_RETRY_ATTEMPTS = 3
DEFAULT_BASE_DELAY_SECONDS = 2.0

@dataclass(frozen=True)
class RetryPolicy:
    """Lightweight retry-budget gating.  No scheduling — just a gate and a
    suggested delay for callers that want simple exponential backoff."""

    max_attempts: int = DEFAULT_MAX_RETRY_ATTEMPTS
    base_delay_seconds: float = DEFAULT_BASE_DELAY_SECONDS

    def can_retry(self, retryable: bool, attempt: int) -> bool:
        return retryable and attempt < self.max_attempts

    def next_delay_seconds(self, attempt: int) -> float:
        """Exponential backoff: base * 2^attempt, capped at 60 s."""
        return min(self.base_delay_seconds * (2 ** attempt), 60.0)


_DEFAULT_RETRY_POLICY = RetryPolicy()

# ── Escalation context ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EscalationContext:
    """Structured context preserved for human-facing notification/log.

    Carries enough detail for a commander to understand what happened without
    needing to parse the raw envelope again.
    """
    code: str
    ok: bool
    error: Optional[str] = None
    warnings: list = field(default_factory=list)
    payload: dict = field(default_factory=dict)


# ── Decision outcome ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DecisionOutcome:
    """Normalized result of interpreting one CLI response envelope."""
    ok: bool
    code: str
    action: AgentAction
    retry_allowed: bool
    halt_trading: bool
    requires_human_review: bool
    escalation: Optional[EscalationContext] = None
    malformed: bool = False


# ── Envelope helpers ──────────────────────────────────────────────────────────

_DECISION_FIELDS = ("retryable", "halt_trading", "requires_human_review")

_SUCCESS_KEYS = {"ok", "code", "result", "warnings"}
_FAILURE_KEYS = {"ok", "code", "error", "details"}


def _extract_flags(envelope: dict) -> Optional[dict[str, bool]]:
    """Pull the three decision booleans from the nested payload.

    Returns None if the envelope is structurally invalid or any flag
    is missing / wrong type.
    """
    ok = envelope.get("ok")
    if ok is True:
        payload = envelope.get("result")
    elif ok is False:
        payload = envelope.get("details")
    else:
        return None

    if not isinstance(payload, dict):
        return None

    flags: dict[str, bool] = {}
    for f in _DECISION_FIELDS:
        val = payload.get(f)
        if not isinstance(val, bool):
            return None
        flags[f] = val
    return flags


def _is_well_formed(envelope: dict) -> bool:
    """Validate top-level shape without inspecting nested payload."""
    if not isinstance(envelope, dict):
        return False
    ok = envelope.get("ok")
    if ok is True:
        return set(envelope.keys()) == _SUCCESS_KEYS
    if ok is False:
        return set(envelope.keys()) == _FAILURE_KEYS
    return False


def _build_escalation(envelope: dict) -> EscalationContext:
    """Build an EscalationContext from whatever we can salvage."""
    ok = envelope.get("ok")
    code = envelope.get("code", "UNKNOWN")

    if ok is True:
        payload = envelope.get("result", {})
        warnings = envelope.get("warnings", [])
        error = None
    elif ok is False:
        payload = envelope.get("details", {})
        warnings = []
        error = envelope.get("error")
    else:
        payload = dict(envelope)
        warnings = []
        error = str(envelope) if envelope else "empty envelope"

    clean_payload = {
        k: v for k, v in (payload if isinstance(payload, dict) else {}).items()
        if k not in _DECISION_FIELDS
    }

    return EscalationContext(
        code=str(code),
        ok=bool(ok),
        error=error,
        warnings=list(warnings) if isinstance(warnings, list) else [],
        payload=clean_payload,
    )


# ── Interpreter ───────────────────────────────────────────────────────────────

def interpret_cli_response(
    envelope: dict,
    *,
    retry_attempt: int = 0,
    retry_policy: Optional[RetryPolicy] = None,
) -> DecisionOutcome:
    """Interpret a parsed CLI response envelope into a deterministic decision.

    Parameters
    ----------
    envelope : dict
        One already-parsed CLI response (``json.loads(stdout)``).
    retry_attempt : int
        Zero-based attempt counter for the current operation.
    retry_policy : RetryPolicy or None
        Retry budget/backoff parameters.  Uses module default when None.

    Returns
    -------
    DecisionOutcome
        Normalized decision with exactly one ``action`` selected via the
        precedence rules documented in the module docstring.
    """
    policy = retry_policy if retry_policy is not None else _DEFAULT_RETRY_POLICY

    # ── Malformed envelope → fail closed ──────────────────────────────────
    if not isinstance(envelope, dict) or not _is_well_formed(envelope):
        return DecisionOutcome(
            ok=False,
            code=str(envelope.get("code", "MALFORMED")) if isinstance(envelope, dict) else "MALFORMED",
            action=AgentAction.HALT_TRADING,
            retry_allowed=False,
            halt_trading=True,
            requires_human_review=True,
            escalation=_build_escalation(envelope if isinstance(envelope, dict) else {}),
            malformed=True,
        )

    flags = _extract_flags(envelope)
    if flags is None:
        return DecisionOutcome(
            ok=False,
            code=str(envelope.get("code", "MALFORMED")),
            action=AgentAction.HALT_TRADING,
            retry_allowed=False,
            halt_trading=True,
            requires_human_review=True,
            escalation=_build_escalation(envelope),
            malformed=True,
        )

    ok: bool = envelope["ok"]
    code: str = envelope["code"]
    f_retryable: bool = flags["retryable"]
    f_halt: bool = flags["halt_trading"]
    f_review: bool = flags["requires_human_review"]

    retry_allowed = policy.can_retry(f_retryable, retry_attempt)
    escalation: Optional[EscalationContext] = None

    # ── Precedence 1: halt outranks everything ────────────────────────────
    if f_halt:
        escalation = _build_escalation(envelope)
        return DecisionOutcome(
            ok=ok,
            code=code,
            action=AgentAction.HALT_TRADING,
            retry_allowed=False,
            halt_trading=True,
            requires_human_review=f_review,
            escalation=escalation,
        )

    # ── Precedence 2: human review ────────────────────────────────────────
    if f_review:
        escalation = _build_escalation(envelope)
        if retry_allowed:
            action = AgentAction.RETRY_AND_ESCALATE
        else:
            action = AgentAction.ESCALATE_TO_HUMAN
        return DecisionOutcome(
            ok=ok,
            code=code,
            action=action,
            retry_allowed=retry_allowed,
            halt_trading=False,
            requires_human_review=True,
            escalation=escalation,
        )

    # ── Precedence 3: retryable (no review, no halt) ─────────────────────
    if f_retryable:
        if retry_allowed:
            return DecisionOutcome(
                ok=ok,
                code=code,
                action=AgentAction.RETRY,
                retry_allowed=True,
                halt_trading=False,
                requires_human_review=False,
            )
        escalation = _build_escalation(envelope)
        return DecisionOutcome(
            ok=ok,
            code=code,
            action=AgentAction.ESCALATE_TO_HUMAN,
            retry_allowed=False,
            halt_trading=False,
            requires_human_review=f_review,
            escalation=escalation,
        )

    # ── Precedence 4: clean success ───────────────────────────────────────
    if ok:
        return DecisionOutcome(
            ok=True,
            code=code,
            action=AgentAction.CONTINUE,
            retry_allowed=False,
            halt_trading=False,
            requires_human_review=False,
        )

    # ── Precedence 5: non-retryable, non-halt failure ─────────────────────
    escalation = _build_escalation(envelope)
    return DecisionOutcome(
        ok=False,
        code=code,
        action=AgentAction.ESCALATE_TO_HUMAN,
        retry_allowed=False,
        halt_trading=False,
        requires_human_review=f_review,
        escalation=escalation,
    )
