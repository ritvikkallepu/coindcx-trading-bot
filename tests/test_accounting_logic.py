from __future__ import annotations

import unittest
from decimal import Decimal
from app.config import Settings
from app.live.live_loop import LiveTradingLoop

class TestAccountingLogic(unittest.TestCase):
    def setUp(self):
        self.settings = Settings()
        # Ensure compound_profits is False to test legacy profit locking
        from dataclasses import replace
        self.settings = replace(self.settings, risk=replace(self.settings.risk, compound_profits=False))
        
        self.loop = LiveTradingLoop(self.settings)
        # Reset state for each test
        self.loop.local_state["tradable_base"] = "100000"
        self.loop.local_state["locked_profit"] = "0"
        self.loop.local_state["daily_loss_from_tradable_base"] = "0"

    def test_profit_adds_to_locked_profit(self):
        self.loop._update_accounting_balances(Decimal("5000"))
        self.assertEqual(self.loop.local_state["locked_profit"], "5000")
        self.assertEqual(self.loop.local_state["tradable_base"], "100000")
        self.assertEqual(self.loop.local_state["daily_loss_from_tradable_base"], "0")

    def test_loss_reduces_tradable_base_and_increases_daily_loss(self):
        self.loop._update_accounting_balances(Decimal("-3000"))
        self.assertEqual(self.loop.local_state["locked_profit"], "0")
        self.assertEqual(self.loop.local_state["tradable_base"], "97000")
        self.assertEqual(self.loop.local_state["daily_loss_from_tradable_base"], "3000")

    def test_locked_profit_remains_unchanged_after_loss(self):
        self.loop.local_state["locked_profit"] = "10000"
        self.loop._update_accounting_balances(Decimal("-4000"))
        self.assertEqual(self.loop.local_state["locked_profit"], "10000")
        self.assertEqual(self.loop.local_state["tradable_base"], "96000")
        self.assertEqual(self.loop.local_state["daily_loss_from_tradable_base"], "4000")

    def test_two_losses_accumulate_correctly(self):
        self.loop._update_accounting_balances(Decimal("-2000"))
        self.loop._update_accounting_balances(Decimal("-1500"))
        self.assertEqual(self.loop.local_state["tradable_base"], "96500")
        self.assertEqual(self.loop.local_state["daily_loss_from_tradable_base"], "3500")

    def test_tradable_base_never_goes_below_zero(self):
        self.loop.local_state["tradable_base"] = "1000"
        self.loop._update_accounting_balances(Decimal("-5000"))
        self.assertEqual(self.loop.local_state["tradable_base"], "0")
        self.assertEqual(self.loop.local_state["daily_loss_from_tradable_base"], "5000")

    def test_clean_string_formatting(self):
        # Test that 5000.00 becomes "5000"
        self.loop._update_accounting_balances(Decimal("5000.0000"))
        self.assertEqual(self.loop.local_state["locked_profit"], "5000")
        
        # Test that 8311.946144... stays as is but without scientific notation
        pnl = Decimal("-3311.946144533870410685529918")
        self.loop.local_state["daily_loss_from_tradable_base"] = "5000"
        self.loop._update_accounting_balances(pnl)
        self.assertEqual(self.loop.local_state["daily_loss_from_tradable_base"], "8311.946144533870410685529918")

    def test_idempotent_order_processing(self):
        # Reset processed list for isolation
        self.loop.local_state["processed_pnl_order_ids"] = []
        
        # Mock client.list_orders
        from unittest.mock import MagicMock
        exit_order = {
            "id": "order-123",
            "pair": "B-BTC_USDT",
            "side": "sell",
            "status": "filled",
            "avg_price": "110",
            "total_quantity": "1",
            "remaining_quantity": "0",
            "fee_amount": "0.05",
            "settlement_currency_conversion_price": "100",
            "updated_at": 123456789,
            "stage": "exit",
        }
        self.loop.client.list_orders = MagicMock(
            side_effect=lambda **kwargs: [exit_order] if kwargs["side"] == "sell" else []
        )
        self.loop.client.list_position_transactions = MagicMock(return_value=[
            {
                "id": "transaction-123",
                "pair": "B-BTC_USDT",
                "parent_id": "order-123",
                "amount": "1000",
                "fee_amount": "5",
            }
        ])
        local_position = {
            "pair": "B-BTC_USDT",
            "source": "exchange",
            "direction": "long",
            "active_pos": "1",
            "avg_price": "100",
        }
        
        # First call
        self.assertTrue(self.loop._process_closed_position("B-BTC_USDT", local_position))
        self.assertEqual(self.loop.local_state["locked_profit"], "995")
        self.assertIn("order-123", self.loop.local_state["processed_pnl_order_ids"])
        
        # Second call with same order ID should be ignored
        self.assertTrue(self.loop._process_closed_position("B-BTC_USDT", local_position))
        self.assertEqual(self.loop.local_state["locked_profit"], "995") # No change
        self.assertEqual(len(self.loop.local_state["processed_pnl_order_ids"]), 1)

if __name__ == "__main__":
    unittest.main()
