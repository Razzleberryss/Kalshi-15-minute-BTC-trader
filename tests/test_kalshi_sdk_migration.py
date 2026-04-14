import unittest
import os
import tempfile
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    os.path.join(tempfile.gettempdir(), f"openclaw_stop_file_tests_{os.getpid()}"),
)

import config


class TestKalshiSdkInit(unittest.TestCase):
    def test_sdk_client_initialization_sets_host_and_keys(self):
        # Import inside test so patches apply to module globals.
        import kalshi_client as kc

        cfg_instance = MagicMock()

        def _cfg_ctor(*, host):
            cfg_instance.host = host
            return cfg_instance

        sdk_client_instance = MagicMock()

        with patch.object(kc, "_SdkConfiguration", side_effect=_cfg_ctor) as p_cfg, patch.object(
            kc, "_SdkKalshiClient", return_value=sdk_client_instance
        ) as p_client, patch.object(
            kc, "_read_private_key_pem", return_value="---PEM---"
        ):
            _ = kc.KalshiClient()

        p_cfg.assert_called_once()
        p_client.assert_called_once()
        self.assertTrue(cfg_instance.host.endswith("/trade-api/v2"))
        self.assertEqual(cfg_instance.api_key_id, config.KALSHI_API_KEY_ID)
        self.assertEqual(cfg_instance.private_key_pem, "---PEM---")


class TestOrderPayloadConstruction(unittest.TestCase):
    def setUp(self):
        import kalshi_client as kc

        self.kc = kc
        # Make a client with SDK mocked to avoid touching network / keys.
        with patch.object(kc, "_SdkConfiguration") as _p_cfg, patch.object(
            kc, "_SdkKalshiClient"
        ) as _p_client, patch.object(kc, "_read_private_key_pem", return_value="---PEM---"):
            self.client = kc.KalshiClient()

    def test_build_order_payload_yes_sets_yes_price_dollars(self):
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="yes",
            action="buy",
            contracts=3,
            price_cents=55,
            reduce_only=False,
            post_only=True,
            client_order_id="cid",
        )
        self.assertEqual(payload["ticker"], f"{config.BTC_SERIES_TICKER}-TEST")
        self.assertEqual(payload["side"], "yes")
        self.assertEqual(payload["action"], "buy")
        self.assertEqual(payload["type"], "limit")
        self.assertEqual(payload["count_fp"], "3.00")
        self.assertEqual(payload["client_order_id"], "cid")
        self.assertTrue(payload["post_only"])
        self.assertTrue(payload["cancel_order_on_pause"])
        self.assertEqual(payload["yes_price_dollars"], "0.5500")
        self.assertNotIn("no_price_dollars", payload)

    def test_build_order_payload_no_sets_no_price_dollars(self):
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="no",
            action="buy",
            contracts=1,
            price_cents=40,
        )
        self.assertEqual(payload["no_price_dollars"], "0.4000")
        self.assertNotIn("yes_price_dollars", payload)


class TestSdkCallPaths(unittest.TestCase):
    def setUp(self):
        import kalshi_client as kc

        self.kc = kc
        with patch.object(kc, "_SdkConfiguration"), patch.object(
            kc, "_SdkKalshiClient"
        ) as p_client, patch.object(kc, "_read_private_key_pem", return_value="---PEM---"):
            self.sdk = MagicMock()
            p_client.return_value = self.sdk
            self.client = kc.KalshiClient()

    def test_cancel_order_calls_sdk_cancel(self):
        self.sdk.cancel_order.return_value = {"ok": True}
        out = self.client.cancel_order("order123")
        self.sdk.cancel_order.assert_called()
        self.assertIsInstance(out, dict)

    def test_get_orderbook_calls_sdk_market_orderbook(self):
        self.sdk.get_market_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}
        out = self.client.get_orderbook("TICKER", depth=10)
        self.sdk.get_market_orderbook.assert_called()
        self.assertEqual(out["orderbook"]["yes"], [])


if __name__ == "__main__":
    unittest.main()
