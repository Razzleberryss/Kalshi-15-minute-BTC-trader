# Kalshi 15-Minute BTC Trader

A rule-based Python trading bot for Kalshi's BTC Up/Down 15-minute prediction markets. Trades using the official Kalshi REST API v2 with RSA-PSS authentication.

---

## New Features: Early Exit Logic
The bot now supports managing open positions before they expire:
- **Stop-Loss:** Automatically sells a position if the contract price drops below your entry price by a set amount.
- **Take-Profit:** Automatically sells a position if the contract price rises above your entry price by a set amount.
- **Signal Reversal:** Sells an open position if the latest strategy signal flips against your current holding (e.g., holding YES but signal becomes NO).
- **Daily Risk Limits:** Blocks new entries after hitting `MAX_DAILY_LOSS_CENTS` or `MAX_DAILY_TRADES`, while continuing to manage/exit open positions.
- **Strict Market Scope:** Discovery and order placement are restricted to `BTC_SERIES_TICKER` markets only.

---

## Project Structure

```
.
├── bot.py             # Main loop - manages positions and enters trades
├── kalshi_client.py   # Kalshi API wrapper (auth, orders, positions, selling)
├── strategy.py        # Signal generation (momentum + orderbook skew)
├── risk_manager.py    # Risk checks, position sizing, CSV trade log
├── config.py          # Config loader (reads from .env)
├── .env.example       # Copy to .env and fill in your keys
├── requirements.txt   # Python dependencies
└── .gitignore
```

## Quick Start

### 1. Clone and set up environment

```bash
git clone https://github.com/Razzleberryss/Kalshi-15-minute-BTC-trader.git
cd Kalshi-15-minute-BTC-trader
python3 -m venv venv
source venv/bin/activate      # Mac/Linux
# venv\Scripts\activate      # WindowsREADME.md: document new early exit features (stop-loss, take-profit, reversal)
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
- `STOP_LOSS_CENTS`: (New) Max cents to lose before exiting (default: 20)
- `TAKE_PROFIT_CENTS`: (New) Target profit cents before exiting (default: 30)
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

## Local Web Dashboard

A simple browser dashboard lets you monitor the bot in real time without reading log files.

### How it works

After every bot cycle `bot.py` writes a small JSON snapshot to `dashboard_state.json` in the project root.
`dashboard.py` is a tiny Flask server that reads that file and renders an auto-refreshing HTML page.

### Run the dashboard

**Terminal 1 – start the bot:**
```bash
python bot.py
```

**Terminal 2 – start the dashboard:**
```bash
python dashboard.py
```

Open **http://127.0.0.1:8000** in your browser.
The page auto-refreshes every 5 seconds and shows:

| Field | Description |
|---|---|
| Active market | Current Kalshi ticker being traded |
| YES / NO bid & ask | Best quotes from the orderbook |
| Mid price | Midpoint of the YES spread |
| Spread | YES ask − YES bid (in cents) |
| Signal composite / momentum / skew / confidence | Strategy signal components |
| Position size | Contracts currently held |
| Realized PnL today | Today's closed-trade P&L in cents |

> Errors writing or reading `dashboard_state.json` are logged at DEBUG level and **never** crash the bot or the dashboard.

---

## Disclaimer
This bot is for educational purposes only. Trading involves risk. Use at your own risk.
