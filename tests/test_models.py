from __future__ import annotations

import unittest
from decimal import Decimal

from app.exchange.models import FuturesOrderRequest


class FuturesOrderRequestTests(unittest.TestCase):
    def test_market_order_omits_time_in_force_and_none_prices(self) -> None:
        order = FuturesOrderRequest(
            side="buy",
            pair="B-BTC_USDT",
            order_type="market_order",
            total_quantity=Decimal("0.01"),
            time_in_force=None,
        )

        payload = order.to_api_order()
        self.assertNotIn("time_in_force", payload)
        self.assertNotIn("price", payload)
        self.assertNotIn("stop_price", payload)
        self.assertEqual(payload["margin_currency_short_name"], "USDT")

    def test_market_order_rejects_time_in_force(self) -> None:
        with self.assertRaises(ValueError):
            FuturesOrderRequest(
                side="buy",
                pair="B-BTC_USDT",
                order_type="market_order",
                total_quantity=Decimal("0.01"),
            )

    def test_market_order_rejects_time_in_force_case_insensitive(self) -> None:
        with self.assertRaises(ValueError):
            FuturesOrderRequest(
                side="BUY",
                pair="B-BTC_USDT",
                order_type="MARKET_ORDER",
                total_quantity=Decimal("0.01"),
            )

    def test_limit_order_requires_price(self) -> None:
        with self.assertRaises(ValueError):
            FuturesOrderRequest(
                side="buy",
                pair="B-BTC_USDT",
                order_type="limit_order",
                total_quantity=Decimal("0.01"),
            )

    def test_limit_order_requires_price_case_insensitive(self) -> None:
        with self.assertRaises(ValueError):
            FuturesOrderRequest(
                side="BUY",
                pair="B-BTC_USDT",
                order_type="LIMIT_ORDER",
                total_quantity=Decimal("0.01"),
            )


if __name__ == "__main__":
    unittest.main()
