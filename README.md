# Kalshi 15-Minute BTC Trader

A rule-based Python trading bot for Kalshi's BTC Up/Down 15-minute prediction markets. Trades using the official Kalshi REST API v2 with RSA-PSS authentication.

---

## New Features: Early Exit Logic
The bot now supports managing open positions before they expire:
- **Stop-Loss:** Automatically sells a position if the contract price drops below your entry price by a set amount.
- **Take-Profit:** Automatically sells a position if the contract price rises above your entry price by a set amount.
- **Signal Reversal:** Sells an open position if the latest strategy signal flips against your current holding (e.g., holding YES but signal becomes NO).

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

### 3. Run the bot

```bash
# Default runs in DRY_RUN mode (logs trades but doesn't place them)
python bot.py

# To trade for real
DRY_RUN=false python bot.py
```

---

## Disclaimer
This bot is for educational purposes only. Trading involves risk. Use at your own risk.
