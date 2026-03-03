import os
import tempfile
import unittest

import config
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import Signal


class BotRequirementsTests(unittest.TestCase):
    def setUp(self):
        self._orig_trade_log = config.TRADE_LOG_FILE
        self._orig_max_daily_trades = config.MAX_DAILY_TRADES
        self._orig_max_daily_loss = config.MAX_DAILY_LOSS_CENTS
        self.trade_log = tempfile.NamedTemporaryFile(delete=False)
        self.trade_log.close()
        os.unlink(self.trade_log.name)
        config.TRADE_LOG_FILE = self.trade_log.name

    def tearDown(self):
        config.TRADE_LOG_FILE = self._orig_trade_log
        config.MAX_DAILY_TRADES = self._orig_max_daily_trades
        config.MAX_DAILY_LOSS_CENTS = self._orig_max_daily_loss
        if os.path.exists(self.trade_log.name):
            os.remove(self.trade_log.name)

    def test_daily_trade_limit_blocks_new_entry(self):
        config.MAX_DAILY_TRADES = 1
        risk = RiskManager()
        risk.log_entry_trade("BTCZ-TEST", "yes", 1, 50)
        signal = Signal(side="yes", confidence=0.9, price_cents=50, reason="test")
        approved, reason = risk.approve_trade(signal, balance=100, positions=[], market_ticker="BTCZ-TEST2")
        self.assertFalse(approved)
        self.assertIn("MAX_DAILY_TRADES", reason)

    def test_daily_loss_limit_blocks_new_entry(self):
        config.MAX_DAILY_TRADES = 10
        config.MAX_DAILY_LOSS_CENTS = 5
        risk = RiskManager()
        risk.log_exit_trade("BTCZ-TEST", "yes", 1, 50, 40, "stop loss")
        signal = Signal(side="yes", confidence=0.9, price_cents=50, reason="test")
        approved, reason = risk.approve_trade(signal, balance=100, positions=[], market_ticker="BTCZ-TEST2")
        self.assertFalse(approved)
        self.assertIn("MAX_DAILY_LOSS_CENTS", reason)

    def test_client_rejects_non_btc_market(self):
        client = object.__new__(KalshiClient)
        with self.assertRaises(ValueError):
            client._ensure_btc_market("NOTBTC-TEST")


if __name__ == "__main__":
    unittest.main()
