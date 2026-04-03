"""
kalshi_agent_envelope.py — Shared JSON envelope builders for Kalshi CLI and in-process orders.

Single source of truth for DECISION_POLICY and success/failure envelope shapes used by
openclaw_kalshi.py and kalshi_inprocess_orders.py.
"""

from __future__ import annotations

from typing import Optional

# ── Decision policy: response-code → (retryable, halt_trading, requires_human_review) ─

_DECISION_FIELDS = ("retryable", "halt_trading", "requires_human_review")
_SAFE_FALLBACK = (False, True, True)

DECISION_POLICY: dict[str, tuple[bool, bool, bool]] = {
    "BUY_DRY_RUN": (False, False, False),
    "BUY_PLACED": (False, False, False),
    "SELL_DRY_RUN": (False, False, False),
    "SELL_PLACED": (False, False, False),
    "STATUS_OK": (False, False, False),
    "MARKETS_OK": (False, False, False),
    "ORDERBOOK_OK": (False, False, False),
    "ORDERBOOK_EMPTY": (True, False, False),
    "SELL_CLAMPED": (False, False, True),
    "ORDERBOOK_FETCH_ERROR": (True, False, False),
    "SERIES_RESOLUTION_NETWORK_ERROR": (True, False, False),
    "STOP_TRADING": (False, True, True),
    "LIVE_TRADING_BLOCKED": (False, True, True),
    "CONFIG_ERROR": (False, True, True),
    "COMMAND_FAILED": (False, True, True),
    "INVALID_SIDE": (False, False, True),
    "INVALID_COUNT": (False, False, True),
    "INVALID_PRICE_RANGE": (False, False, True),
    "INVALID_TICKER": (False, False, True),
    "PRICE_OUTSIDE_CONFIG_RANGE": (False, False, True),
    "EXCEEDS_MAX_TRADE_DOLLARS": (False, False, True),
    "NO_POSITION": (False, False, True),
    "SERIES_RESOLUTION_FAILED": (False, False, True),
}


def decision_flags(code: str) -> dict[str, bool]:
    """Return the three decision booleans for *code*, using safe fallback for unmapped codes."""
    return dict(zip(_DECISION_FIELDS, DECISION_POLICY.get(code, _SAFE_FALLBACK)))


def success_envelope(code: str, result: dict, warnings: Optional[list] = None) -> dict:
    """Build a successful response envelope: {ok, code, result, warnings}."""
    merged = {**result, **decision_flags(code)}
    return {
        "ok": True,
        "code": code,
        "result": merged,
        "warnings": warnings if warnings else [],
    }


def failure_envelope(code: str, error: str, details: Optional[dict] = None) -> dict:
    """Build a failed response envelope: {ok, code, error, details}."""
    merged = {**(details if details is not None else {}), **decision_flags(code)}
    return {
        "ok": False,
        "code": code,
        "error": error,
        "details": merged,
    }
