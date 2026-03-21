"""
dashboard.py – Simple local web dashboard for the Kalshi 15-minute BTC bot.

Reads dashboard_state.json written by the bot each cycle and renders a
live-refreshing HTML page at http://127.0.0.1:8000.

Usage:
    python dashboard.py

Then open http://127.0.0.1:8000 in your browser while the bot is running.
"""
import json
import logging
from pathlib import Path

from flask import Flask, render_template_string

STATE_FILE = Path(__file__).parent / "dashboard_state.json"

log = logging.getLogger(__name__)

app = Flask(__name__)

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Kalshi BTC Bot Dashboard</title>
  <meta http-equiv="refresh" content="5">
  <style>
    body { font-family: monospace; background: #1a1a2e; color: #e0e0e0;
           padding: 24px; max-width: 640px; }
    h1   { color: #00d4aa; margin-bottom: 4px; }
    .sub { color: #666; font-size: 0.85em; margin-bottom: 20px; }
    table { border-collapse: collapse; width: 100%; }
    td, th { padding: 7px 12px; text-align: left; border-bottom: 1px solid #2a2a3e; }
    .label  { color: #888; width: 220px; }
    .sect   { color: #555; font-size: 0.8em; padding-top: 14px; }
    .pos    { color: #00d4aa; }
    .neg    { color: #ff6b6b; }
    .na     { color: #555; }
    .warn   { color: #ff6b6b; margin-top: 12px; }
  </style>
</head>
<body>
  <h1>&#x1F916; Kalshi BTC Bot Dashboard</h1>
  <p class="sub">Auto-refreshes every 5 seconds.</p>

  {% if error %}
    <p class="warn">{{ error }}</p>
  {% elif not state %}
    <p class="na">Waiting for bot data&hellip; (dashboard_state.json not found yet)</p>
  {% else %}
  <table>
    <tr><td class="label">Last updated</td>
        <td>{{ state.get('timestamp') or 'N/A' }}</td></tr>
    <tr><td class="label">Active market</td>
        <td>{{ state.get('active_market_ticker') or 'N/A' }}</td></tr>

    <tr><td colspan="2" class="sect">&#x2500; Quotes</td></tr>
    <tr><td class="label">YES bid / ask</td>
        <td>{{ fmt(state.get('yes_bid')) }}&#162; / {{ fmt(state.get('yes_ask')) }}&#162;</td></tr>
    <tr><td class="label">NO bid / ask</td>
        <td>{{ fmt(state.get('no_bid')) }}&#162; / {{ fmt(state.get('no_ask')) }}&#162;</td></tr>
    <tr><td class="label">Mid price</td>
        <td>{{ fmt(state.get('mid_price')) }}&#162;</td></tr>
    <tr><td class="label">Spread</td>
        <td>{{ fmt(state.get('spread')) }}&#162;</td></tr>

    <tr><td colspan="2" class="sect">&#x2500; Signal</td></tr>
    <tr><td class="label">Composite</td>
        <td>{{ fmt4(state.get('signal_composite')) }}</td></tr>
    <tr><td class="label">Momentum</td>
        <td>{{ fmt4(state.get('signal_momentum')) }}</td></tr>
    <tr><td class="label">Skew</td>
        <td>{{ fmt4(state.get('signal_skew')) }}</td></tr>
    <tr><td class="label">Confidence</td>
        <td>{{ fmt4(state.get('signal_confidence')) }}</td></tr>

    <tr><td colspan="2" class="sect">&#x2500; Position &amp; P&amp;L</td></tr>
    <tr><td class="label">Position size</td>
        <td>{{ state.get('position_size', 0) }}</td></tr>
    <tr><td class="label">Realized PnL today</td>
        <td class="{{ pnl_cls(state.get('realized_pnl_cents', 0)) }}">
          {{ state.get('realized_pnl_cents', 0) }}&#162;
        </td></tr>
  </table>
  {% endif %}
</body>
</html>"""


@app.route("/")
def index():
    state = None
    error = None
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("Error reading dashboard_state.json: %s", exc)
        error = f"Error reading dashboard_state.json: {exc}"

    def fmt(v):
        return "N/A" if v is None else str(v)

    def fmt4(v):
        return "N/A" if v is None else f"{v:.4f}"

    def pnl_cls(v):
        if v is None or v == 0:
            return ""
        return "pos" if v > 0 else "neg"

    return render_template_string(
        _HTML,
        state=state,
        error=error,
        fmt=fmt,
        fmt4=fmt4,
        pnl_cls=pnl_cls,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=8000, debug=False)
