from __future__ import annotations

import unittest
from decimal import Decimal
from app.config import load_settings
from app.dashboard.server import DashboardRequestHandler
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.models import FuturesOrderRequest
from unittest.mock import MagicMock

class INRMarginParityTests(unittest.TestCase):
    def test_inr_is_default_margin_currency(self) -> None:
        settings = load_settings()
        self.assertEqual(settings.futures_margin_currency, "INR")

    def test_client_defaults_to_inr(self) -> None:
        client = CoinDCXFuturesClient()
        # Verify that internal request logic uses INR if not specified
        client._request_json = MagicMock()
        client.get_active_instruments()
        
        args = client._request_json.call_args
        # query={"margin_currency_short_name[]": ["INR"]}
        self.assertEqual(args[1]["query"]["margin_currency_short_name[]"], ["INR"])

    def test_order_request_defaults_to_inr(self) -> None:
        order = FuturesOrderRequest(
            side="buy",
            pair="B-BTC_USDT",
            order_type="market_order",
            total_quantity=Decimal("1"),
            time_in_force=None # For market orders
        )
        self.assertEqual(order.margin_currency_short_name, "INR")
        api_order = order.to_api_order()
        self.assertEqual(api_order["margin_currency_short_name"], "INR")

    def test_pair_normalization(self) -> None:
        # This tests the conceptual normalization required for the dashboard
        # display name -> internal name
        def normalize(display):
            if display.startswith("B-"): return display
            return "B-" + display.replace("-", "_").upper()
            
        self.assertEqual(normalize("BTC-USDT"), "B-BTC_USDT")
        self.assertEqual(normalize("BSB-USDT"), "B-BSB_USDT")
        self.assertEqual(normalize("B-SOL_USDT"), "B-SOL_USDT")

if __name__ == "__main__":
    unittest.main()
