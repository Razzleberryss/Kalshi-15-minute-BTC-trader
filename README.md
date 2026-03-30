# Kalshi 15-Minute BTC Trader

A rule-based Python trading bot for Kalshi's BTC Up/Down 15-minute prediction markets. Trades using the official Kalshi REST API v2 with RSA-PSS authentication. Now integrates with **OpenClaw Agent Trading** for autonomous, AI-driven signal enrichment and trade execution.

---

## New Features

### 🤖 OpenClaw / Agent Trading Integration *(New)*
The bot now natively integrates with **OpenClaw Agent Trading**, enabling autonomous AI-driven market analysis and trade execution on top of the existing rule-based strategy.

- **AI-Powered Signal Enrichment:** OpenClaw's agent layer cross-references Kalshi orderbook data with external momentum signals, sentiment feeds, and multi-timeframe BTC price context to score trade confidence more accurately.
- **Autonomous Agent Mode:** When `OPENCLAW_AGENT_MODE=true`, the OpenClaw agent can independently decide whether to act on a signal based on configurable risk thresholds — no manual approval needed.
- **Orderbook Intelligence:** Pulls enriched market depth data from OpenClaw's data layer to supplement Kalshi's native orderbook, providing tighter spread analysis and better entry timing.
- **Seamless Pipeline Integration:** Plugs directly into `strategy.py` — the existing momentum + orderbook skew logic feeds into OpenClaw's scoring model, and the agent returns an enriched composite signal back to `bot.py`.
- **Configurable Agent Autonomy:** Set `OPENCLAW_CONFIDENCE_THRESHOLD` to control at what confidence level the agent auto-executes vs. defers to the rule-based fallback.

---

### Early Exit Logic
The bot actively manages open positions before expiry to lock in gains and cut losses:

- **Stop-Loss:** Automatically sells a position if the contract price drops below your entry price by a set amount.
- **Take-Profit:** Automatically sells a position if the contract price rises above your entry price by a set amount.
- **Signal Reversal:** Sells an open position if the latest strategy signal flips against your current holding (e.g., holding YES but signal becomes NO).
- **Daily Risk Limits:** Blocks new entries after hitting `MAX_DAILY_LOSS_CENTS` or `MAX_DAILY_TRADES`, while continuing to manage/exit open positions.
- **Strict Market Scope:** Discovery and order placement are restricted to `BTC_SERIES_TICKER` markets only.

### Cursor Plugin Support
This project is fully optimized for development inside **Cursor** with AI-assisted coding enabled:

- `.cursor` config included for instant project context — no manual setup needed.
- AI-aware file structure and inline comments make Copilot/Cursor suggestions faster and more accurate.
- Modular design means Cursor can autocomplete, refactor, and reason about `bot.py`, `strategy.py`, and `risk_manager.py` independently.
- Faster, cleaner code iteration directly from your editor with context-aware suggestions tailored to this trading bot's architecture.

### Performance & Code Quality
- Refactored async loop in `bot.py` for lower latency between signal generation and order placement.
- Cleaner separation of concerns across all modules — easier to extend or swap out strategy logic.
- Improved logging with structured output for faster debugging and trade review.
- Reduced boilerplate in `kalshi_client.py` for a leaner, more readable API wrapper.

---

## Project Structure

```
.
├── bot.py              # Main loop - manages positions and enters trades
├── kalshi_client.py    # Kalshi API wrapper (auth, orders, positions, selling)
├── strategy.py         # Signal generation (momentum + orderbook skew + OpenClaw)
├── openclaw_client.py  # OpenClaw agent interface (signal enrichment + agent mode)
├── risk_manager.py     # Risk checks, position sizing, CSV trade log
├── config.py           # Config loader (reads from .env)
├── .env.example        # Copy to .env and fill in your keys
├── requirements.txt    # Python dependencies
└── .gitignore
```

## Quick Start

### 1. Clone and set up environment

```bash
git clone https://github.com/Razzleberryss/Kalshi-15-minute-BTC-trader.git
cd Kalshi-15-minute-BTC-trader
python3 -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 2. Configure your credentials

```bash
cp .env.example .env
```

Edit `.env` and provide your Kalshi API details:

- `KALSHI_API_KEY_ID`: Your API Key ID
- `KALSHI_PRIVATE_KEY_PATH`: Path to your RSA private key (e.g., `./kalshi_private_key.pem`)
- `KALSHI_ENV`: `demo` or `prod`
- `STOP_LOSS_CENTS`: Max cents to lose before exiting (default: 20)
- `TAKE_PROFIT_CENTS`: Target profit cents before exiting (default: 30)
- `MAX_DAILY_LOSS_CENTS`: Max realized daily loss before stopping new entries (default: 1000)
- `MAX_DAILY_TRADES`: Max new entries per day (default: 20)

### 3. Run the bot

```bash
# Default runs in DRY_RUN mode (logs trades but doesn't place them)
python bot.py

# To trade for real
DRY_RUN=false python bot.py
```

When running with `DRY_RUN=false` and `KALSHI_ENV=prod`, the bot logs a prominent live-trading warning banner at startup.

---

## OpenClaw / Agent Trading Setup

### Step 1: Get your OpenClaw API key

1. Sign up or log in at [openclaw.io](https://openclaw.io) (or your OpenClaw instance URL).
2. Navigate to **Settings → API Keys** and generate a new key.
3. Copy the key — you'll add it to `.env` in the next step.

### Step 2: Add OpenClaw credentials to `.env`

Open your `.env` file and add the following variables:

```env
# OpenClaw / Agent Trading
OPENCLAW_API_KEY=your_openclaw_api_key_here
OPENCLAW_BASE_URL=https://api.openclaw.io/v1   # Update if self-hosting
OPENCLAW_AGENT_MODE=false                       # Set true to enable autonomous agent execution
OPENCLAW_CONFIDENCE_THRESHOLD=0.72              # Agent auto-executes only above this confidence (0.0–1.0)
```

> **Note:** Keep `OPENCLAW_AGENT_MODE=false` while testing. In agent mode, OpenClaw can place trades autonomously — only enable it once you've validated signal quality in dry-run.

### Step 3: Install the OpenClaw client dependency

The OpenClaw client is included via `requirements.txt`. If you need to install it manually:

```bash
pip install openclaw-sdk
```

Or if OpenClaw is accessed via direct HTTP (no SDK):

```bash
pip install httpx  # already included in requirements.txt
```

### Step 4: Verify the connection

Run the connection test to confirm your API key and endpoint are working:

```bash
python -c "from openclaw_client import OpenClawClient; c = OpenClawClient(); print(c.ping())"
```

Expected output:

```
✅ OpenClaw connected — agent ready.
```

If you see an authentication error, double-check `OPENCLAW_API_KEY` and `OPENCLAW_BASE_URL` in `.env`.

### Step 5: Run the bot with OpenClaw enabled

```bash
# Dry run with OpenClaw signal enrichment active
python bot.py

# Live trading with OpenClaw enrichment (agent mode off)
DRY_RUN=false python bot.py

# Live trading with full autonomous agent mode
DRY_RUN=false OPENCLAW_AGENT_MODE=true python bot.py
```

### How the OpenClaw integration works

```
Kalshi Orderbook
      │
      ▼
 strategy.py  ──► momentum score + orderbook skew
      │
      ▼
openclaw_client.py  ──► enriches signal with AI context, cross-refs external BTC data
      │
      ├─ AGENT_MODE=false: returns enriched composite_signal → bot.py decides
      └─ AGENT_MODE=true:  agent evaluates confidence → auto-executes if above threshold
```

### OpenClaw `.env` reference

| Variable | Default | Description |
|---|---|---|
| `OPENCLAW_API_KEY` | *(required)* | Your OpenClaw API key |
| `OPENCLAW_BASE_URL` | `https://api.openclaw.io/v1` | API base URL (update if self-hosted) |
| `OPENCLAW_AGENT_MODE` | `false` | Enable autonomous agent trade execution |
| `OPENCLAW_CONFIDENCE_THRESHOLD` | `0.72` | Min confidence score for agent auto-execution |

---

## Local Web Dashboard

A simple browser dashboard lets you monitor the bot in real time without reading log files.

### How it works

After every bot cycle `bot.py` writes a small JSON snapshot to `dashboard_state.json` in the project root. `dashboard.py` is a tiny Flask server that reads that file and renders an auto-refreshing HTML page.

### Run the dashboard

**Terminal 1 – start the bot:**

```bash
python bot.py
```

**Terminal 2 – start the dashboard:**

```bash
python dashboard.py
```

Open **http://127.0.0.1:8000** in your browser. The page auto-refreshes every 5 seconds and shows:

| Field | Description |
|---|---|
| Active market | Current Kalshi ticker being traded |
| YES / NO bid & ask | Best quotes from the orderbook |
| Mid price | Midpoint of the YES spread |
| Spread | YES ask − YES bid (in cents) |
| Signal composite / momentum / skew / confidence | Strategy signal components |
| OpenClaw enriched confidence | AI-enriched confidence score from OpenClaw agent |
| Position size | Contracts currently held |
| Realized PnL today | Today's closed-trade P&L in cents |

> Errors writing or reading `dashboard_state.json` are logged at DEBUG level and **never** crash the bot or the dashboard.

---

## Disclaimer

This bot is for educational purposes only. Trading involves risk. Use at your own risk.
