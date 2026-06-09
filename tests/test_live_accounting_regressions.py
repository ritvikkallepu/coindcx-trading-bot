from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.execution.live import LivePositionSnapshot
from app.live.accounting import (
    calculate_closed_position_accounting,
    matching_transaction_totals,
    normalize_position_direction,
    order_fee_in_margin_currency,
    select_exit_orders,
    transaction_identity,
)
from app.live.live_loop import LiveTradingLoop
from app.strategies.base import SignalDirection


def _order(
    *,
    order_id: str,
    side: str,
    avg_price: str,
    quantity: str = "119",
    updated_at: int = 1,
    fee_amount: str = "0",
    conversion: str = "102",
    group_id: str | None = None,
) -> dict[str, object]:
    return {
        "id": order_id,
        "pair": "B-BSB_USDT",
        "side": side,
        "status": "filled",
        "avg_price": avg_price,
        "total_quantity": quantity,
        "remaining_quantity": "0",
        "cancelled_quantity": "0",
        "updated_at": updated_at,
        "fee_amount": fee_amount,
        "settlement_currency_conversion_price": conversion,
        "group_id": group_id,
    }


class LiveAccountingRegressionTests(unittest.TestCase):
    def test_lowercase_long_loss_stays_negative(self) -> None:
        accounting = calculate_closed_position_accounting(
            position={
                "pair": "B-BSB_USDT",
                "direction": "long",
                "active_pos": "119",
                "avg_price": "0.3483736",
            },
            exit_orders=[
                _order(order_id="exit-1", side="sell", avg_price="0.32929"),
            ],
            quote_to_margin_rate=Decimal("102"),
            unit_contract_value=Decimal("1"),
            margin_currency="INR",
        )

        self.assertEqual(accounting.direction, SignalDirection.LONG)
        self.assertEqual(accounting.gross_pnl, Decimal("-231.6367368"))
        self.assertLess(accounting.net_pnl, 0)

    def test_uppercase_long_is_normalized(self) -> None:
        self.assertEqual(
            normalize_position_direction({"direction": "LONG", "active_pos": "1"}),
            SignalDirection.LONG,
        )

    def test_short_profit_stays_positive(self) -> None:
        accounting = calculate_closed_position_accounting(
            position={
                "pair": "B-BSB_USDT",
                "direction": "short",
                "active_pos": "-10",
                "avg_price": "10",
            },
            exit_orders=[
                _order(
                    order_id="exit-1",
                    side="buy",
                    avg_price="8",
                    quantity="10",
                    conversion="1",
                ),
            ],
            quote_to_margin_rate=Decimal("1"),
            unit_contract_value=Decimal("1"),
            margin_currency="USDT",
        )

        self.assertEqual(accounting.gross_pnl, Decimal("20"))

    def test_signed_quantity_is_direction_fallback(self) -> None:
        self.assertEqual(
            normalize_position_direction({"active_pos": "-2"}),
            SignalDirection.SHORT,
        )

    def test_long_selects_sell_exit_not_newer_buy_entry(self) -> None:
        orders = [
            _order(order_id="entry", side="buy", avg_price="0.348", updated_at=20),
            _order(order_id="exit", side="sell", avg_price="0.329", updated_at=10),
        ]

        selected = select_exit_orders(
            orders,
            pair="B-BSB_USDT",
            direction=SignalDirection.LONG,
        )

        self.assertEqual([order["id"] for order in selected], ["exit"])

    def test_short_selects_buy_exit(self) -> None:
        orders = [
            _order(order_id="entry", side="sell", avg_price="10", updated_at=10),
            _order(order_id="exit", side="buy", avg_price="8", updated_at=20),
        ]

        selected = select_exit_orders(
            orders,
            pair="B-BSB_USDT",
            direction=SignalDirection.SHORT,
        )

        self.assertEqual([order["id"] for order in selected], ["exit"])

    def test_order_fee_is_converted_from_usdt_to_inr(self) -> None:
        fee = order_fee_in_margin_currency(
            _order(
                order_id="exit",
                side="sell",
                avg_price="1",
                fee_amount="0.02313725490196078431372549020",
            ),
            quote_to_margin_rate=Decimal("100"),
            margin_currency="INR",
        )

        self.assertEqual(fee, Decimal("2.360000000000000000000000000"))

    def test_exact_transactions_match_coindcx_history_breakdown(self) -> None:
        gross, fee, matched = matching_transaction_totals(
            [
                {
                    "pair": "B-BSB_USDT",
                    "parent_id": "exit-1",
                    "amount": "-231.64",
                    "fee_amount": "2.36",
                },
            ],
            pair="B-BSB_USDT",
            exit_order_ids={"exit-1"},
        )
        accounting = calculate_closed_position_accounting(
            position={
                "pair": "B-BSB_USDT",
                "direction": "long",
                "active_pos": "119",
                "avg_price": "0.3483736",
            },
            exit_orders=[
                _order(order_id="exit-1", side="sell", avg_price="0.32929"),
            ],
            quote_to_margin_rate=Decimal("102"),
            unit_contract_value=Decimal("1"),
            margin_currency="INR",
            entry_fee=Decimal("1.40"),
            transaction_gross_pnl=gross,
            transaction_exit_fee=fee,
        )

        self.assertEqual(matched, 1)
        self.assertEqual(accounting.gross_pnl, Decimal("-231.64"))
        self.assertEqual(accounting.exit_fee, Decimal("2.36"))
        self.assertEqual(accounting.net_pnl, Decimal("-235.40"))
        self.assertEqual(accounting.source, "exchange_transactions")

    def test_unknown_direction_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            normalize_position_direction({"direction": "sideways", "active_pos": "0"})

    def test_transaction_identity_is_stable_without_exchange_id(self) -> None:
        transaction = {
            "pair": "B-BSB_USDT",
            "stage": "funding",
            "amount": "-2.23",
            "created_at": 123,
        }
        self.assertEqual(
            transaction_identity(transaction),
            transaction_identity(dict(transaction)),
        )


class LiveLoopRegressionTests(unittest.TestCase):
    def test_closed_position_uses_exact_coindcx_transaction_values(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.settings = SimpleNamespace(
            quote_to_margin_rate=Decimal("102"),
            futures_margin_currency="INR",
            risk=SimpleNamespace(
                compound_profits=False,
                live_max_daily_loss_inr=Decimal("1000"),
            ),
        )
        loop.starting_equity = Decimal("1000")
        loop.kill_switch_active = False
        loop._instruments = {}
        loop.alert = MagicMock()
        loop.client = MagicMock()
        entry_order = _order(
            order_id="entry-1",
            side="buy",
            avg_price="0.3483736",
            updated_at=1,
        )
        exit_order = _order(
            order_id="exit-1",
            side="sell",
            avg_price="0.32929",
            updated_at=2,
        )
        exit_order["stage"] = "exit"
        loop.client.list_orders.side_effect = (
            lambda **kwargs: [entry_order] if kwargs["side"] == "buy" else [exit_order]
        )
        loop.client.list_position_transactions.return_value = [
            {
                "id": "close-transaction",
                "pair": "B-BSB_USDT",
                "parent_id": "exit-1",
                "amount": "-231.64",
                "fee_amount": "2.36",
            }
        ]
        loop.local_state = {
            "tradable_base": "1000",
            "locked_profit": "0",
            "daily_loss_from_tradable_base": "0",
            "processed_pnl_order_ids": [],
            "position_metadata": {
                "B-BSB_USDT": {
                    "entry_fee_margin_currency": "1.40",
                },
            },
        }
        local_position = {
            "pair": "B-BSB_USDT",
            "source": "exchange",
            "direction": "long",
            "active_pos": "119",
            "avg_price": "0.3483736",
        }

        self.assertTrue(loop._process_closed_position("B-BSB_USDT", local_position))

        self.assertEqual(loop.local_state["tradable_base"], "764.6")
        self.assertEqual(loop.local_state["daily_loss_from_tradable_base"], "235.4")
        self.assertIn("exit-1", loop.local_state["processed_pnl_order_ids"])

    def test_dry_run_accounting_uses_inr_conversion(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.settings = SimpleNamespace(
            quote_to_margin_rate=Decimal("102"),
            risk=SimpleNamespace(
                taker_fee_rate=Decimal("0.0005"),
                fee_gst_rate=Decimal("0.18"),
            ),
        )
        loop._instruments = {}

        gross, fees = loop._simulated_close_accounting(
            "B-BSB_USDT",
            {
                "direction": "long",
                "active_pos": "119",
                "avg_price": "0.3483736",
            },
            Decimal("0.32929"),
        )

        self.assertEqual(gross, Decimal("-231.6367368"))
        self.assertEqual(
            fees,
            (Decimal("0.3483736") + Decimal("0.32929"))
            * Decimal("119")
            * Decimal("102")
            * Decimal("0.00059"),
        )

    def test_strategy_features_include_normalized_open_position(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.settings = SimpleNamespace(execution_interval="15m")
        loop.strategy_name = "hybrid_meta_v2"
        series = CandleSeries(maxlen=10)
        series.add(
            OHLCVCandle(
                pair="B-BSB_USDT",
                interval="15m",
                open_time_ms=0,
                close_time_ms=899999,
                open=Decimal("1"),
                high=Decimal("1.1"),
                low=Decimal("0.9"),
                close=Decimal("1"),
                volume=Decimal("10"),
            )
        )
        loop.execution_series_by_pair = {"B-BSB_USDT": series}
        loop.local_state = {
            "positions": {
                "B-BSB_USDT": {
                    "pair": "B-BSB_USDT",
                    "direction": "LONG",
                    "active_pos": "2",
                    "avg_price": "1",
                    "stop_loss_trigger": "0.9",
                },
            },
            "position_metadata": {
                "B-BSB_USDT": {
                    "strategy_name": "hybrid_meta_v2",
                    "opened_at_ms": 123,
                },
            },
        }

        features = loop._strategy_features("B-BSB_USDT")

        self.assertEqual(features["open_position"]["direction"], "long")
        self.assertEqual(features["open_position"]["opened_at_ms"], 123)
        self.assertEqual(len(features["execution_candles"]), 1)

    def test_pending_bot_entry_is_not_marked_manual(self) -> None:
        loop = object.__new__(LiveTradingLoop)
        loop.local_state = {"positions": {}, "position_metadata": {}}
        loop._pending_entry_pairs = {"B-BSB_USDT"}
        loop.settings = SimpleNamespace(
            live_trading_allowed=True,
            live_pilot_dry_run=False,
            risk=SimpleNamespace(
                live_require_stop_loss=False,
                live_close_on_kill_switch=False,
                live_cancel_orders_on_kill_switch=False,
            ),
        )
        position = LivePositionSnapshot.from_mapping(
            {
                "id": "position-1",
                "pair": "B-BSB_USDT",
                "active_pos": "119",
                "avg_price": "0.3483736",
                "stop_loss_trigger": "0.33",
            }
        )
        loop.execution_engine = SimpleNamespace(
            sync=SimpleNamespace(fetch_position=MagicMock(return_value=position))
        )
        loop.alert = MagicMock()
        loop.client = MagicMock()
        loop.kill_switch_active = False

        loop._reconcile_pair("B-BSB_USDT")

        self.assertFalse(loop.local_state["positions"]["B-BSB_USDT"]["manual_entry"])
        loop.alert.alert.assert_not_called()

    def test_funding_reconciliation_baselines_then_applies_only_new_rows(self) -> None:
        existing = {
            "id": "funding-1",
            "pair": "B-BSB_USDT",
            "stage": "funding",
            "amount": "-2.23",
            "fee_amount": "0",
            "updated_at": 1,
        }
        new = {
            "id": "funding-2",
            "pair": "B-BSB_USDT",
            "stage": "funding",
            "amount": "-0.16",
            "fee_amount": "0",
            "updated_at": 2,
        }
        loop = object.__new__(LiveTradingLoop)
        loop.settings = SimpleNamespace(
            live_pilot_dry_run=False,
            live_trading_allowed=True,
            futures_margin_currency="INR",
            risk=SimpleNamespace(
                compound_profits=False,
                live_max_daily_loss_inr=Decimal("1000"),
            ),
        )
        loop.pairs = ["B-BSB_USDT"]
        loop.starting_equity = Decimal("1000")
        loop.kill_switch_active = False
        loop.alert = MagicMock()
        loop.client = MagicMock()
        loop.client.list_position_transactions.side_effect = [[existing], [existing, new]]
        loop.local_state = {
            "tradable_base": "1000",
            "locked_profit": "0",
            "daily_loss_from_tradable_base": "0",
            "processed_funding_transaction_ids": [],
            "funding_reconciliation_initialized": False,
        }

        loop._reconcile_funding_transactions()
        self.assertEqual(loop.local_state["tradable_base"], "1000")

        loop._reconcile_funding_transactions()
        self.assertEqual(loop.local_state["tradable_base"], "999.84")
        self.assertEqual(loop.local_state["daily_loss_from_tradable_base"], "0.16")


if __name__ == "__main__":
    unittest.main()
