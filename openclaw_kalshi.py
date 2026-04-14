#!/usr/bin/env python3
"""
openclaw_kalshi.py - CLI for OpenClaw agent to trade Kalshi hourly BTC markets.

Designed to be invoked by an OpenClaw skill via shell commands.
All output is JSON by default; use --human for readable output.

Flag convention (no ambiguity):
  --series   Series code (e.g. KXBTCD).  Always auto-resolves to the best
             live market ticker before any market-specific API call.
  --ticker   Exact market ticker (e.g. KXBTCD-28MAR2615-B85000).
             Used directly — must include the date and strike suffix.
  When --ticker is omitted, the command resolves from --series automatically.
  A bare series code passed as --ticker is always rejected.

Safety gates (enforced in code, not optional prompt text):
  - KALSHI_TRADING_LIVE=1 env var required for real orders (or --dry-run)
  - ~/.openclaw/workspace/STOP_TRADING file presence halts all trading
  - All risk limits from config.py are enforced by KalshiClient / CLI

Subcommands:
  status              Balance, positions, active hourly market
  markets             List open markets in the configured series
  orderbook           Orderbook for a live market
  buy                 Buy contracts
  sell                Sell contracts

Invariant:
  No command may pass a series code to get_orderbook(), get_market(),
  place_order(), or sell_position().  All resolution goes through
  _resolve_ticker_from_args() → resolve_live_market_ticker().

Response contract (agent-facing API):
  Every CLI invocation writes exactly one JSON object to stdout, then exits.
  The envelope shape is fixed per ok value — agents MUST NOT probe for
  optional top-level keys.

  Success (exit 0):
    {
      "ok":       true,                       // bool, always true
      "code":     "SELL_PLACED",              // str, non-empty uppercase
      "result":   { ... },                    // dict, command-specific payload
      "warnings": [ {"code": "...", "message": "..."}, ... ]  // list, may be []
    }

  Failure (exit 1):
    {
      "ok":      false,                       // bool, always false
      "code":    "NO_POSITION",               // str, non-empty uppercase
      "error":   "human-readable message",    // str, non-empty
      "details": { ... }                      // dict, may be {}
    }

  Invariants:
    - ok is always bool.
    - code is always a non-empty uppercase string.
    - Success always has exactly {ok, code, result, warnings}.
    - Failure always has exactly {ok, code, error, details}.
    - The two key-sets are disjoint except for ok and code.
    - warnings is always a list; details is always a dict.

  Success codes:
    STATUS_OK, MARKETS_OK, ORDERBOOK_OK, ORDERBOOK_EMPTY,
    BUY_DRY_RUN, BUY_PLACED, SELL_DRY_RUN, SELL_PLACED, SELL_CLAMPED

  Failure codes:
    STOP_TRADING, LIVE_TRADING_BLOCKED, CONFIG_ERROR, COMMAND_FAILED,
    INVALID_TICKER, SERIES_RESOLUTION_FAILED, SERIES_RESOLUTION_NETWORK_ERROR,
    ORDERBOOK_FETCH_ERROR, INVALID_SIDE, INVALID_COUNT, INVALID_PRICE_RANGE,
    PRICE_OUTSIDE_CONFIG_RANGE, EXCEEDS_MAX_TRADE_DOLLARS, NO_POSITION

  Warning codes:
    ORDERBOOK_EMPTY, BIDS_UNPARSEABLE, POSITION_CLAMPED

  Buy/sell result fields (fixed set, null when not applicable):
    action, ticker, side, count, price_cents, client_order_id, mode,
    order_id (null on dry run), order_status (null on dry run).
    buy adds: cost_dollars.
    sell adds: requested_count, position_held.

  Decision semantics (agent orchestration layer):
    Every response carries three boolean decision fields inside the nested
    payload (``result`` for success, ``details`` for failure):
      retryable              — may the agent re-attempt this operation?
      halt_trading           — should the agent stop all trading?
      requires_human_review  — should a human commander review this event?

    These are independent of ``ok``:
      - A response may be ok=true and still require human review
        (e.g. SELL_CLAMPED).
      - A response may be ok=false and still be retryable
        (e.g. ORDERBOOK_FETCH_ERROR).
    Warnings remain advisory only and do not affect decision semantics.

    Merge precedence: the centralized DECISION_POLICY always overwrites any
    same-named fields in result/details — no mixed ownership.

    Unmapped/unknown response codes fall back to the safest posture:
      retryable=false, halt_trading=true, requires_human_review=true.
"""

import argparse
import datetime
import json
import logging
import os
import re
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

STOP_FILE = Path(os.environ.get("OPENCLAW_STOP_FILE", str(Path.home() / ".openclaw" / "workspace" / "STOP_TRADING")))
PROJECT_DIR = Path(__file__).resolve().parent

# Override BTC_SERIES_TICKER before config import so KalshiClient's
# safety prefix check matches hourly tickers.
_series_override = os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD")
os.environ.setdefault("BTC_SERIES_TICKER", _series_override)

sys.path.insert(0, str(PROJECT_DIR))
import config  # noqa: E402
from kalshi_client import KalshiClient  # noqa: E402
from kalshi_money import enrich_market_quotes_from_dollar_fields  # noqa: E402

log = logging.getLogger("openclaw_kalshi")

from kalshi_agent_envelope import (  # noqa: E402
    DECISION_POLICY,
    DECISION_POLICY,
    failure_envelope as _failure,
    success_envelope as _success,
)


def _stop_file() -> Path:
    return Path(os.environ.get("OPENCLAW_STOP_FILE", str(STOP_FILE)))


def _check_stop_file():
    stop_file = _stop_file()
    if stop_file.exists():
        _die(
            "STOP_TRADING",
            f"STOP_TRADING file exists at {stop_file}. Remove it to resume trading.",
        )


def _check_live_gate(args):
    """Refuse real orders unless KALSHI_TRADING_LIVE=1 or --dry-run."""
    if getattr(args, "dry_run", False):
        return True
    if os.environ.get("KALSHI_TRADING_LIVE") != "1":
        _die(
            "LIVE_TRADING_BLOCKED",
            "Real trading blocked. Set KALSHI_TRADING_LIVE=1 to enable, "
            "or pass --dry-run.",
        )
    return False


def _die(code: str, error: str, *, details: dict = None, exit_code: int = 1):
    """Print a failure envelope to stdout and terminate."""
    print(json.dumps(_failure(code, error, details)))
    sys.exit(exit_code)


def _out(envelope: dict, human: bool = False):
    """Print a response envelope (success or failure) to stdout."""
    if human:
        if envelope.get("ok"):
            print(f"[{envelope['code']}]")
            for k, v in envelope.get("result", {}).items():
                print(f"  {k}: {v}")
            for w in envelope.get("warnings", []):
                print(f"  WARNING {w.get('code', '')}: {w.get('message', '')}")
        else:
            print(f"ERROR [{envelope['code']}]: {envelope.get('error', '')}")
            for k, v in envelope.get("details", {}).items():
                print(f"  {k}: {v}")
    else:
        print(json.dumps(envelope, indent=2, default=str))


def _debug_print(msg: str, debug: bool):
    """Print debug info to stderr so it doesn't corrupt JSON stdout."""
    if debug:
        print(f"[DEBUG] {msg}", file=sys.stderr)


def _is_exact_market_ticker(value: str, series: str) -> bool:
    """True if value looks like 'SERIES-...' (exact market ticker, not bare series)."""
    return value.startswith(f"{series}-") and len(value) > len(series) + 1


def _resolve_ticker_from_args(
    client: KalshiClient,
    args,
    caller: str,
) -> str:
    """Shared ticker resolution used by orderbook, buy, and sell.

    Rules (single source of truth for --series / --ticker):
      - If --ticker is given → validate exact market ticker, use directly.
      - If --ticker is omitted → resolve from --series via resolve_live_market_ticker().
      - A bare series code passed as --ticker is always rejected.
    Returns the validated exact market ticker string.
    """
    series = args.series
    debug = getattr(args, "debug", False)
    explicit_ticker = getattr(args, "ticker", None)

    if explicit_ticker:
        ticker = explicit_ticker.upper()
        _debug_print(f"[{caller}] Using explicit --ticker: {ticker}", debug)
        if not _is_exact_market_ticker(ticker, series):
            _die(
                "INVALID_TICKER",
                f"--ticker '{ticker}' is not a valid market ticker for series '{series}'. "
                f"Expected format: {series}-<date>-<strike> "
                f"(e.g. {series}-28MAR2615-B85000). "
                f"To auto-resolve the live market, omit --ticker and use --series.",
            )
        return ticker

    _debug_print(f"[{caller}] No --ticker; resolving from --series '{series}'", debug)
    try:
        ticker = resolve_live_market_ticker(client, series, debug=debug)
    except RuntimeError as exc:
        _die("SERIES_RESOLUTION_FAILED", str(exc))
    except Exception as exc:
        _die(
            "SERIES_RESOLUTION_NETWORK_ERROR",
            f"Could not reach Kalshi API to resolve series '{series}': {exc}",
        )
    return ticker


# ── Series-to-ticker resolution ───────────────────────────────────────────────


def resolve_live_market_ticker(
    client: KalshiClient,
    series: str,
    debug: bool = False,
) -> str:
    """
    Resolve a series code (e.g. KXBTCD) to the best currently-live market ticker.

    Flow:
      1. GET /markets?series_ticker=SERIES&status=open
      2. Filter to tickers that start with '{SERIES}-'
      3. Drop provisional markets
      4. Drop markets whose close_time is in the past (UTC)
      5. Sort ascending by close_time (soonest = current live window)
      6. Return the first ticker

    Raises RuntimeError with a detailed message if no live market is found.
    """
    _debug_print(f"Resolving series '{series}' to live market ticker...", debug)

    all_markets = client.list_markets(series, status="open", limit=100)

    _debug_print(
        f"API returned {len(all_markets)} markets for series_ticker={series}",
        debug,
    )

    if not all_markets:
        raise RuntimeError(
            f"No markets returned by Kalshi API for series_ticker={series} "
            f"with status=open. The series may be inactive or misspelled."
        )

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    _debug_print(f"Current UTC time: {now_utc.isoformat()}", debug)

    candidates = []
    filtered_out = []

    for m in all_markets:
        ticker = m.get("ticker", "")
        status = m.get("status", "")
        close_time_str = m.get("close_time", "")
        is_provisional = m.get("is_provisional", False)

        reasons = []

        if not ticker.startswith(f"{series}-"):
            reasons.append(f"ticker '{ticker}' missing prefix '{series}-'")

        if is_provisional:
            reasons.append("is_provisional=True")

        close_dt = None
        if close_time_str:
            try:
                close_dt = datetime.datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
                if close_dt <= now_utc:
                    reasons.append(f"close_time {close_time_str} is in the past")
            except (ValueError, TypeError):
                reasons.append(f"unparseable close_time '{close_time_str}'")

        if reasons:
            filtered_out.append({"ticker": ticker, "reasons": reasons})
            _debug_print(f"  FILTERED OUT: {ticker} — {'; '.join(reasons)}", debug)
        else:
            candidates.append(m)
            _debug_print(
                f"  CANDIDATE: {ticker} | status={status} "
                f"| close_time={close_time_str} | title={m.get('title', '')!r}",
                debug,
            )

    if not candidates:
        detail_lines = [
            f"No live markets found for series '{series}'.",
            f"Total markets from API: {len(all_markets)}",
            f"All were filtered out:",
        ]
        for fo in filtered_out[:10]:
            detail_lines.append(f"  {fo['ticker']}: {'; '.join(fo['reasons'])}")
        if len(filtered_out) > 10:
            detail_lines.append(f"  ... and {len(filtered_out) - 10} more")
        raise RuntimeError("\n".join(detail_lines))

    candidates.sort(key=lambda m: (m.get("close_time", ""), -(m.get("volume", 0) or 0)))
    chosen = candidates[0]
    chosen_ticker = chosen["ticker"]

    _debug_print(
        f"RESOLVED: {chosen_ticker} (close_time={chosen.get('close_time')}, "
        f"volume={chosen.get('volume', 0)}, out of {len(candidates)} candidates)",
        debug,
    )

    return chosen_ticker


# ── Subcommands ─────────────────────────────────────────────────────────────


def cmd_status(client: KalshiClient, args):
    series = args.series
    balance = client.get_balance()
    positions = client.get_positions()

    markets = client.list_markets(series, status="open", limit=20)
    hourly_markets = [
        m
        for m in markets
        if m.get("ticker", "").startswith(f"{series}-")
        and not m.get("is_provisional", False)
    ]
    hourly_markets.sort(key=lambda m: m.get("close_time", ""))

    btc_positions = [
        p for p in positions if p.get("ticker", "").startswith(f"{series}-")
    ]

    result = {
        "balance_dollars": balance,
        "series_ticker": series,
        "open_hourly_markets": len(hourly_markets),
        "next_close": hourly_markets[0].get("close_time") if hourly_markets else None,
        "btc_positions": [
            {
                "ticker": p.get("ticker"),
                "side": "yes" if p.get("position", 0) > 0 else "no",
                "quantity": abs(p.get("position", 0)),
            }
            for p in btc_positions
            if p.get("position", 0) != 0
        ],
        "total_positions": len([p for p in positions if p.get("position", 0) != 0]),
        "live_trading": os.environ.get("KALSHI_TRADING_LIVE") == "1",
        "dry_run_config": config.DRY_RUN,
        "stop_file_present": _stop_file().exists(),
    }
    _out(_success("STATUS_OK", result), args.human)


def cmd_markets(client: KalshiClient, args):
    series = args.series
    markets = client.list_markets(series, status="open", limit=1000)
    markets = [
        m
        for m in markets
        if m.get("ticker", "").startswith(f"{series}-")
        and not m.get("is_provisional", False)
    ]
    for m in markets:
        enrich_market_quotes_from_dollar_fields(m)
    markets.sort(key=lambda m: m.get("close_time", ""))

    rows = []
    for m in markets:
        rows.append(
            {
                "ticker": m.get("ticker"),
                "title": m.get("title", ""),
                "subtitle": m.get("subtitle", ""),
                "status": m.get("status", ""),
                "close_time": m.get("close_time"),
                "yes_ask": m.get("yes_ask"),
                "yes_bid": m.get("yes_bid"),
                "no_ask": m.get("no_ask"),
                "no_bid": m.get("no_bid"),
                "last_price": m.get("last_price"),
                "volume": m.get("volume"),
            }
        )

    # Enrich markets with live orderbook prices.
    # The /markets list endpoint frequently returns null bid/ask even when
    # markets are live; the /orderbook endpoint always has real data.
    # Strategy:
    #   1. Find the soonest close_time (active settlement window).
    #   2. Among all markets in that window, fetch orderbooks and find the
    #      one with mid_price closest to 50 (most uncertain / near-spot).
    #      This is the tradeable market — the one Kalshi shows on the UI.
    #   3. Move that market to rows[0] so the skill sees it first.
    if rows:
        active_close = rows[0]["close_time"]
        window_markets = [r for r in rows if r.get("close_time") == active_close]

        def _strike(ticker):
            m = re.search(r'-T(\d+)', ticker or '')
            return int(m.group(1)) if m else 0

        window_markets.sort(key=lambda r: _strike(r["ticker"]))
        mid_idx = len(window_markets) // 2
        candidates = window_markets[max(0, mid_idx - 10):mid_idx + 10]

        # Fetch orderbooks in parallel (I/O-bound network calls)
        enriched: list[tuple[float, dict]] = []
        n_workers = min(10, len(candidates))
        with ThreadPoolExecutor(max_workers=n_workers) as pool:
            future_to_cand = {
                pool.submit(client.get_orderbook, cand["ticker"]): cand
                for cand in candidates
            }
            for future in as_completed(future_to_cand):
                cand = future_to_cand[future]
                try:
                    raw_ob = future.result()
                    yes_raw, no_raw = _extract_raw_bids(raw_ob)
                    yes_bids = _parse_bid_array(yes_raw)
                    no_bids  = _parse_bid_array(no_raw)
                    _yes_best = max(yes_bids, key=lambda x: x[0]) if yes_bids else None
                    _no_best  = max(no_bids,  key=lambda x: x[0]) if no_bids  else None
                    byb = _yes_best[0] if _yes_best else None
                    bnb = _no_best[0]  if _no_best  else None
                    bya = (100 - bnb)  if bnb is not None else None
                    bna = (100 - byb)  if byb is not None else None
                    if byb is not None or bya is not None:
                        # Compute mid safely for one-sided books.
                        # Kalshi only returns YES bids; NO bids are separate.
                        # When BTC << strike, yes_dollars is empty (byb=None)
                        # but no_dollars has real bids → bya = 100 - bnb.
                        # Treat the absent side as 0 for mid estimation.
                        if byb is not None and bya is not None:
                            mid = (byb + bya) / 2
                        elif bya is not None:
                            # Only NO side present; YES is near-zero.
                            # Estimate: mid ≈ bya / 2 (YES bid implied ~0)
                            mid = bya / 2.0
                        else:
                            # Only YES side present; NO is near-zero.
                            # Estimate: mid ≈ (100 + byb) / 2 (NO bid implied ~0)
                            mid = (100 + byb) / 2.0
                        cand["yes_bid"]      = byb
                        cand["yes_ask"]      = bya
                        cand["no_bid"]       = bnb
                        cand["no_ask"]       = bna
                        cand["mid_price"]    = int(mid)
                        cand["_ob_enriched"] = True
                        enriched.append((abs(mid - 50), cand))
                except Exception as exc:
                    log.debug("Orderbook fetch failed for %s: %s", cand["ticker"], exc)

        if enriched:
            best_market = min(enriched, key=lambda x: x[0])[1]
            rows.remove(best_market)
            rows.insert(0, best_market)

    result = {"series": series, "count": len(rows), "markets": rows}
    _out(_success("MARKETS_OK", result), args.human)


def _parse_bid_array(bid_array):
    """Parse bid arrays from Kalshi orderbook formats into [(price_cents, size), ...]."""
    if not bid_array:
        return []
    parsed = []
    for entry in bid_array:
        if isinstance(entry, (list, tuple)) and len(entry) >= 2:
            if isinstance(entry[0], str):
                try:
                    price_cents = round(float(entry[0]) * 100)
                    size = int(float(entry[1]))
                    parsed.append((price_cents, size))
                except (ValueError, TypeError):
                    continue
            else:
                price = entry[0]
                if isinstance(price, float) and 0.0 <= price <= 1.0:
                    price_cents = int(round(price * 100))
                else:
                    price_cents = int(price)
                parsed.append((price_cents, int(entry[1])))
    return parsed


def _extract_raw_bids(raw_ob: dict) -> tuple:
    """Extract (yes_bids_raw, no_bids_raw) from orderbook response."""
    from orderbook_utils import extract_raw_arrays
    return extract_raw_arrays(raw_ob)


def cmd_orderbook(client: KalshiClient, args):
    series = args.series
    debug = getattr(args, "debug", False)
    explicit_ticker = getattr(args, "ticker", None)

    ticker = _resolve_ticker_from_args(client, args, caller="orderbook")
    _debug_print(f"Final orderbook request target: {ticker}", debug)

    try:
        raw_ob = client.get_orderbook(ticker)
    except Exception as exc:
        _die(
            "ORDERBOOK_FETCH_ERROR",
            f"GET /markets/{ticker}/orderbook failed: {exc}",
        )

    _debug_print(f"Raw orderbook response keys: {list(raw_ob.keys())}", debug)
    ob_data = raw_ob.get("orderbook", {})
    ob_fp = raw_ob.get("orderbook_fp", {})
    _debug_print(
        f"orderbook sub-keys: {list(ob_data.keys()) if ob_data else '(missing)'}",
        debug,
    )
    _debug_print(
        f"orderbook_fp sub-keys: {list(ob_fp.keys()) if ob_fp else '(missing)'}",
        debug,
    )
    if debug:
        _debug_print(
            f"Raw orderbook (truncated): {json.dumps(raw_ob, default=str)[:2000]}",
            True,
        )

    yes_raw, no_raw = _extract_raw_bids(raw_ob)
    _debug_print(f"yes entries: {len(yes_raw)}, no entries: {len(no_raw)}", debug)

    market_data = {}
    try:
        market_info = client.get_market(ticker)
        market_data = market_info.get("market", market_info)
    except Exception as exc:
        _debug_print(f"Could not fetch market details: {exc}", debug)

    if not yes_raw and not no_raw:
        result = {
            "ticker": ticker,
            "resolved_from_series": series if not explicit_ticker else None,
            "title": market_data.get("title", ""),
            "close_time": market_data.get("close_time"),
            "raw_response_keys": list(raw_ob.keys()),
            "orderbook_subkeys": list(ob_data.keys()) if ob_data else [],
        }
        _out(
            _success("ORDERBOOK_EMPTY", result, warnings=[{
                "code": "ORDERBOOK_EMPTY",
                "message": (
                    f"Orderbook for '{ticker}' has zero bids on both sides. "
                    f"The market may have just opened or have no liquidity."
                ),
            }]),
            args.human,
        )
        return

    yes_bids = _parse_bid_array(yes_raw)
    no_bids = _parse_bid_array(no_raw)

    # Kalshi arrays are sorted ascending; best (highest) bid is the last entry.
    _yes_best = max(yes_bids, key=lambda x: x[0]) if yes_bids else None
    _no_best = max(no_bids, key=lambda x: x[0]) if no_bids else None
    best_yes_bid = _yes_best[0] if _yes_best else None
    best_yes_bid_size = _yes_best[1] if _yes_best else 0
    best_no_bid = _no_best[0] if _no_best else None
    best_no_bid_size = _no_best[1] if _no_best else 0
    # Asks are derived: yes_ask = 1.00 - best_no_bid, no_ask = 1.00 - best_yes_bid
    best_yes_ask = (100 - best_no_bid) if best_no_bid is not None else None
    best_no_ask = (100 - best_yes_bid) if best_yes_bid is not None else None

    if best_yes_bid is not None and best_yes_ask is not None:
        mid_price = (best_yes_bid + best_yes_ask) // 2
        spread = (best_yes_ask - best_yes_bid) / 100.0
    elif best_no_bid is not None and best_no_ask is not None:
        mid_price = 100 - ((best_no_bid + best_no_ask) // 2)
        spread = None
    else:
        mid_price = None
        spread = None

    result = {
        "ticker": ticker,
        "resolved_from_series": series if not explicit_ticker else None,
        "title": market_data.get("title", ""),
        "close_time": market_data.get("close_time"),
        "best_yes_bid": best_yes_bid,
        "best_yes_ask": best_yes_ask,
        "best_no_bid": best_no_bid,
        "best_no_ask": best_no_ask,
        "best_yes_bid_size": best_yes_bid_size,
        "best_no_bid_size": best_no_bid_size,
        "mid_price": mid_price,
        "spread": spread,
        "yes_bid_levels": len(yes_bids),
        "no_bid_levels": len(no_bids),
    }

    warnings = []
    if best_yes_bid is None and best_no_bid is None:
        warnings.append({
            "code": "BIDS_UNPARSEABLE",
            "message": (
                f"Raw orderbook had {len(yes_raw)} YES and {len(no_raw)} NO entries "
                f"but none could be parsed. Possible response format mismatch."
            ),
        })

    _out(_success("ORDERBOOK_OK", result, warnings or None), args.human)


def cmd_buy(client: KalshiClient, args):
    _check_stop_file()
    dry_run = _check_live_gate(args)

    ticker = _resolve_ticker_from_args(client, args, caller="buy")
    side = args.side.lower()
    count = args.count
    price_cents = args.price

    if side not in ("yes", "no"):
        _die("INVALID_SIDE", "side must be 'yes' or 'no'")
    if count < 1:
        _die("INVALID_COUNT", "count must be >= 1")
    if not (1 <= price_cents <= 99):
        _die("INVALID_PRICE_RANGE", "price must be 1-99 (cents)")
    if not (
        config.MIN_CONTRACT_PRICE_CENTS
        <= price_cents
        <= config.MAX_CONTRACT_PRICE_CENTS
    ):
        _die(
            "PRICE_OUTSIDE_CONFIG_RANGE",
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]",
        )

    cost_dollars = count * price_cents / 100
    if cost_dollars > config.MAX_TRADE_DOLLARS:
        _die(
            "EXCEEDS_MAX_TRADE_DOLLARS",
            f"Order cost ${cost_dollars:.2f} exceeds MAX_TRADE_DOLLARS "
            f"${config.MAX_TRADE_DOLLARS:.2f}",
        )

    client_order_id = str(uuid.uuid4())
    mode = "DRY_RUN" if dry_run else "LIVE"

    audit = {
        "action": "BUY",
        "ticker": ticker,
        "side": side,
        "count": count,
        "price_cents": price_cents,
        "cost_dollars": cost_dollars,
        "client_order_id": client_order_id,
        "mode": mode,
        "order_id": None,
        "order_status": None,
    }

    if dry_run:
        _out(_success("BUY_DRY_RUN", audit), args.human)
        return

    api_result = client.place_order(ticker, side, count, price_cents, dry_run=False)
    order = api_result.get("order", {}) if api_result else {}
    audit["order_id"] = order.get("order_id")
    audit["order_status"] = order.get("status")
    _out(_success("BUY_PLACED", audit), args.human)


def cmd_sell(client: KalshiClient, args):
    _check_stop_file()
    dry_run = _check_live_gate(args)

    ticker = _resolve_ticker_from_args(client, args, caller="sell")
    side = args.side.lower()
    count = args.count
    price_cents = args.price

    if side not in ("yes", "no"):
        _die("INVALID_SIDE", "side must be 'yes' or 'no'")
    if count < 1:
        _die("INVALID_COUNT", "count must be >= 1")
    if not (1 <= price_cents <= 99):
        _die("INVALID_PRICE_RANGE", "price must be 1-99 (cents)")
    if not (
        config.MIN_CONTRACT_PRICE_CENTS
        <= price_cents
        <= config.MAX_CONTRACT_PRICE_CENTS
    ):
        _die(
            "PRICE_OUTSIDE_CONFIG_RANGE",
            f"price {price_cents}c outside allowed range "
            f"[{config.MIN_CONTRACT_PRICE_CENTS}, {config.MAX_CONTRACT_PRICE_CENTS}]",
        )

    requested_count = count
    held = client.contracts_held_on_side(ticker, side)
    if held == 0:
        _die(
            "NO_POSITION",
            f"No open {side.upper()} contracts for {ticker}. "
            f"Use status to inspect positions (YES = long YES, NO = long NO).",
        )

    sell_count = min(requested_count, held)
    clamped = sell_count < requested_count

    client_order_id = str(uuid.uuid4())
    mode = "DRY_RUN" if dry_run else "LIVE"

    audit = {
        "action": "SELL",
        "ticker": ticker,
        "side": side,
        "count": sell_count,
        "requested_count": requested_count,
        "price_cents": price_cents,
        "position_held": held,
        "client_order_id": client_order_id,
        "mode": mode,
        "order_id": None,
        "order_status": None,
    }

    warnings = []
    if clamped:
        warnings.append({
            "code": "POSITION_CLAMPED",
            "message": (
                f"Requested sell of {requested_count} contracts exceeds "
                f"{side.upper()} position of {held}; clamped to {sell_count}."
            ),
        })

    if dry_run:
        code = "SELL_CLAMPED" if clamped else "SELL_DRY_RUN"
        _out(_success(code, audit, warnings), args.human)
        return

    api_result = client.sell_position(
        ticker, side, sell_count, price_cents, dry_run=False,
    )
    order = api_result.get("order", {}) if api_result else {}
    audit["order_id"] = order.get("order_id")
    audit["order_status"] = order.get("status")
    code = "SELL_CLAMPED" if clamped else "SELL_PLACED"
    _out(_success(code, audit, warnings), args.human)


# ── Main ─────────────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw CLI for Kalshi hourly BTC trading",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Log level (default: WARNING)",
    )

    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument(
        "--series",
        default=os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD"),
        help="Kalshi series ticker (default: KXBTCD)",
    )
    shared.add_argument("--human", action="store_true", help="Human-readable output")
    shared.add_argument("--json", action="store_true", help="JSON output (default)")
    shared.add_argument(
        "--debug", action="store_true", help="Verbose debug output to stderr"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "status", parents=[shared], help="Balance, positions, active hourly market"
    )
    sub.add_parser("markets", parents=[shared], help="List open markets in the series")

    ob = sub.add_parser(
        "orderbook", parents=[shared], help="Orderbook for a live market"
    )
    ob.add_argument(
        "--ticker",
        default=None,
        help=(
            "Exact market ticker (e.g. KXBTCD-26MAR2815-B87500). "
            "If omitted, resolves the best live ticker from --series."
        ),
    )
    ob.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max orderbook levels to display (currently informational)",
    )

    buy_p = sub.add_parser("buy", parents=[shared], help="Buy contracts")
    buy_p.add_argument(
        "--ticker",
        default=None,
        help=(
            "Exact market ticker (e.g. KXBTCD-28MAR2615-B85000). "
            "If omitted, resolves the best live ticker from --series."
        ),
    )
    buy_p.add_argument("side", choices=["yes", "no"], help="Contract side")
    buy_p.add_argument("count", type=int, help="Number of contracts")
    buy_p.add_argument("price", type=int, help="Limit price in cents (1-99)")
    buy_p.add_argument("--dry-run", action="store_true", help="Simulate only")

    sell_p = sub.add_parser("sell", parents=[shared], help="Sell contracts")
    sell_p.add_argument(
        "--ticker",
        default=None,
        help=(
            "Exact market ticker (e.g. KXBTCD-28MAR2615-B85000). "
            "If omitted, resolves the best live ticker from --series."
        ),
    )
    sell_p.add_argument("side", choices=["yes", "no"], help="Contract side")
    sell_p.add_argument("count", type=int, help="Number of contracts")
    sell_p.add_argument("price", type=int, help="Limit price in cents (1-99)")
    sell_p.add_argument("--dry-run", action="store_true", help="Simulate only")

    args = parser.parse_args()

    series = getattr(args, "series", os.environ.get("HOURLY_SERIES_TICKER", "KXBTCD"))
    os.environ["BTC_SERIES_TICKER"] = series
    config.BTC_SERIES_TICKER = series

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config.validate()
    except EnvironmentError as e:
        _die("CONFIG_ERROR", f"Config error: {e}")

    client = KalshiClient()
    dispatch = {
        "status": cmd_status,
        "markets": cmd_markets,
        "orderbook": cmd_orderbook,
        "buy": cmd_buy,
        "sell": cmd_sell,
    }
    try:
        dispatch[args.command](client, args)
    except SystemExit:
        raise
    except Exception as exc:
        log.debug("Dispatch failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        print(json.dumps(_failure(
            "COMMAND_FAILED",
            f"Command failed: {exc}",
            details={"exception_type": type(exc).__name__},
        )))
        sys.exit(1)


if __name__ == "__main__":
    main()
