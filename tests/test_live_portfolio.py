from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.live.live_loop import LiveTradingLoop, _validate_live_intervals
from app.live.portfolio import (
    build_futures_portfolio_snapshot,
    risk_capacity_before_reservations,
)


class LivePortfolioTests(unittest.TestCase):
    def test_portfolio_equity_includes_open_unrealized_pnl(self) -> None:
        snapshot = build_futures_portfolio_snapshot(
            wallets=[
                {
                    "currency_short_name": "INR",
                    "balance": "1000.10288496021732",
                    "locked_balance": "659.98953035319906",
                    "cross_order_margin": "0",
                    "cross_user_margin": "0",
                }
            ],
            positions=[
                {
                    "pair": "B-BSB_USDT",
                    "active_pos": "74",
                    "avg_price": "0.31352",
                    "mark_price": "0.25704673",
                    "settlement_currency_avg_price": "102",
                }
            ],
            margin_currency="INR",
            fallback_quote_to_margin_rate=Decimal("98"),
        )

        expected_unrealized = (
            Decimal("0.25704673") - Decimal("0.31352")
        ) * Decimal("74") * Decimal("102")
        self.assertEqual(snapshot.unrealized_pnl, expected_unrealized)
        expected_total_wallet = (
            Decimal("1000.10288496021732") + Decimal("659.98953035319906")
        )
        self.assertEqual(snapshot.wallet_balance, expected_total_wallet)
        self.assertEqual(
            snapshot.portfolio_equity,
            expected_total_wallet + expected_unrealized,
        )
        self.assertEqual(
            snapshot.free_collateral,
            Decimal("1000.10288496021732"),
        )

    def test_risk_capacity_respects_allocation_and_exchange_free_collateral(self) -> None:
        snapshot = build_futures_portfolio_snapshot(
            wallets=[
                {
                    "currency_short_name": "INR",
                    "balance": "1000",
                    "locked_balance": "660",
                }
            ],
            positions=[],
            margin_currency="INR",
            fallback_quote_to_margin_rate=Decimal("98"),
        )

        capacity = risk_capacity_before_reservations(
            allocated_capital=Decimal("1000"),
            existing_required_margin=Decimal("660"),
            portfolio=snapshot,
        )

        self.assertEqual(capacity, Decimal("1660"))
        self.assertEqual(capacity - Decimal("660"), Decimal("1000"))

    def test_risk_capacity_caps_new_usable_amount_to_allocation(self) -> None:
        snapshot = build_futures_portfolio_snapshot(
            wallets=[
                {
                    "currency_short_name": "INR",
                    "balance": "10000",
                    "locked_balance": "0",
                }
            ],
            positions=[],
            margin_currency="INR",
            fallback_quote_to_margin_rate=Decimal("98"),
        )

        capacity = risk_capacity_before_reservations(
            allocated_capital=Decimal("1000"),
            existing_required_margin=Decimal("250"),
            portfolio=snapshot,
        )
        self.assertEqual(capacity, Decimal("1250"))
        self.assertEqual(capacity - Decimal("250"), Decimal("1000"))

    def test_risk_capacity_caps_new_trade_to_exchange_usable_balance(self) -> None:
        snapshot = build_futures_portfolio_snapshot(
            wallets=[
                {
                    "currency_short_name": "INR",
                    "balance": "400",
                    "locked_balance": "600",
                }
            ],
            positions=[],
            margin_currency="INR",
            fallback_quote_to_margin_rate=Decimal("98"),
        )

        capacity = risk_capacity_before_reservations(
            allocated_capital=Decimal("1000"),
            existing_required_margin=Decimal("600"),
            portfolio=snapshot,
        )

        self.assertEqual(capacity, Decimal("1000"))
        self.assertEqual(capacity - Decimal("600"), Decimal("400"))

    def test_live_intervals_accept_one_hour_five_minute(self) -> None:
        _validate_live_intervals("1h", "5m")

    def test_live_intervals_reject_non_intrabar_execution(self) -> None:
        with self.assertRaises(ValueError):
            _validate_live_intervals("1h", "1h")

    def test_refresh_portfolio_normalizes_exchange_positions_for_dashboard(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.settings = SimpleNamespace(
            futures_margin_currency="INR",
            quote_to_margin_rate=Decimal("98"),
        )
        loop.client = MagicMock()
        loop.client.get_wallets.return_value = [
            {
                "currency_short_name": "INR",
                "balance": "1000",
                "locked_balance": "100",
            }
        ]
        loop.client.list_positions.return_value = [
            {
                "pair": "B-BSB_USDT",
                "active_pos": "2",
                "avg_price": "1",
                "mark_price": "1.1",
                "settlement_currency_avg_price": "98",
            }
        ]
        loop.local_state = {}
        loop._portfolio_snapshot = None
        loop._portfolio_positions = {}

        loop._refresh_portfolio_snapshot()

        position = loop.local_state["portfolio_positions"]["B-BSB_USDT"]
        self.assertEqual(position["direction"], "long")
        self.assertEqual(position["source"], "exchange")
        self.assertEqual(loop.local_state["wallet_balance"], "1100")
        self.assertEqual(loop.local_state["wallet_free_collateral"], "1000")


if __name__ == "__main__":
    unittest.main()
