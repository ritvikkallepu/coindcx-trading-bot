from __future__ import annotations

import unittest
from decimal import Decimal

from app.config import Settings
from app.execution.live import (
    LiveExecutionEngine,
    LiveOrderSnapshot,
    LivePositionSnapshot,
    build_live_entry_order,
)
from app.risk.models import RiskDecision
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _signal(
    *,
    action: SignalAction = SignalAction.ENTER_LONG,
    direction: SignalDirection = SignalDirection.LONG,
    stop_loss: Decimal | None = Decimal("95"),
    metadata: dict[str, object] | None = None,
) -> StrategySignal:
    return StrategySignal(
        strategy_name="test",
        pair="B-BTC_USDT",
        interval="1m",
        action=action,
        direction=direction,
        confidence=Decimal("1"),
        reason="test",
        timestamp_ms=1,
        entry_price=Decimal("100"),
        stop_loss=stop_loss,
        take_profit=Decimal("110"),
        metadata=metadata or {},
    )


def _decision(signal: StrategySignal | None = None) -> RiskDecision:
    return RiskDecision(
        approved=True,
        reason="approved",
        signal=signal or _signal(),
        position_size=Decimal("2"),
        notional=Decimal("200"),
        leverage=Decimal("2"),
        max_loss=Decimal("10"),
    )


class FakeClient:
    def __init__(self, positions=None, orders=None) -> None:
        self.orders = []
        self.positions = list(positions or [[]])
        self.order_rows = list(orders or [])
        self.tpsl_calls = []
        self.exit_calls = []
        self.cancel_position_calls = []

    def place_order(self, order):
        self.orders.append(order)
        return [{"id": "live-order-1"}]

    def list_positions(self, **kwargs):
        if len(self.positions) > 1:
            return self.positions.pop(0)
        return self.positions[0]

    def list_orders(self, **kwargs):
        return self.order_rows

    def create_position_tpsl(self, **kwargs):
        self.tpsl_calls.append(kwargs)
        if self.positions:
            self.positions[0] = [
                {
                    **row,
                    "take_profit_trigger": kwargs.get("take_profit_stop_price"),
                    "stop_loss_trigger": kwargs.get("stop_loss_stop_price"),
                }
                for row in self.positions[0]
            ]
        return {"success": True}

    def exit_position(self, position_id: str):
        self.exit_calls.append(position_id)
        return {"id": position_id, "status": "exiting"}

    def cancel_all_open_orders_for_position(self, position_id: str):
        self.cancel_position_calls.append(position_id)
        return {"message": "success"}


def _closed_position_row() -> dict[str, object]:
    return {
        "id": "pos-1",
        "pair": "B-BTC_USDT",
        "active_pos": "0",
        "avg_price": "0",
        "take_profit_trigger": None,
        "stop_loss_trigger": None,
        "leverage": "2",
        "margin_type": "isolated",
        "margin_currency_short_name": "INR",
    }


def _open_position_row(
    *,
    active_pos: str = "2",
    stop_loss_trigger: str | None = "95",
    take_profit_trigger: str | None = "110",
) -> dict[str, object]:
    return {
        "id": "pos-1",
        "pair": "B-BTC_USDT",
        "active_pos": active_pos,
        "avg_price": "100",
        "liquidation_price": "60",
        "locked_margin": "100",
        "locked_order_margin": "0",
        "take_profit_trigger": take_profit_trigger,
        "stop_loss_trigger": stop_loss_trigger,
        "leverage": "2",
        "margin_type": "isolated",
        "margin_currency_short_name": "INR",
        "settlement_currency_avg_price": "98",
        "updated_at": 1700000000000,
    }


def _order_row() -> dict[str, object]:
    return {
        "id": "live-order-1",
        "pair": "B-BTC_USDT",
        "side": "buy",
        "status": "filled",
        "order_type": "market_order",
        "total_quantity": "2",
        "remaining_quantity": "0",
        "avg_price": "100",
        "fee_amount": "0.1",
        "updated_at": 1700000000000,
    }


class LiveExecutionTests(unittest.TestCase):
    def test_position_snapshot_reads_signed_active_position(self) -> None:
        long_position = LivePositionSnapshot.from_mapping(_open_position_row(active_pos="2"))
        short_position = LivePositionSnapshot.from_mapping(_open_position_row(active_pos="-3"))

        self.assertEqual(long_position.direction, SignalDirection.LONG)
        self.assertEqual(long_position.quantity, Decimal("2"))
        self.assertTrue(long_position.has_stop_loss)
        self.assertEqual(short_position.direction, SignalDirection.SHORT)
        self.assertEqual(short_position.quantity, Decimal("3"))

    def test_order_snapshot_tracks_filled_quantity(self) -> None:
        order = LiveOrderSnapshot.from_mapping(_order_row())

        self.assertEqual(order.filled_quantity, Decimal("2"))
        self.assertTrue(order.is_terminal)

    def test_builds_market_entry_with_attached_stop_and_tp(self) -> None:
        order = build_live_entry_order(_decision())
        payload = order.to_api_order()

        self.assertEqual(payload["side"], "buy")
        self.assertEqual(payload["order_type"], "market_order")
        self.assertNotIn("time_in_force", payload)
        self.assertEqual(payload["leverage"], 2)
        self.assertEqual(payload["stop_loss_price"], 95)
        self.assertEqual(payload["take_profit_price"], 110)
        self.assertEqual(payload["position_margin_type"], "isolated")

    def test_rejects_live_entry_without_stop_loss(self) -> None:
        with self.assertRaises(ValueError):
            build_live_entry_order(_decision(_signal(stop_loss=None)))

    def test_dry_run_does_not_call_exchange(self) -> None:
        client = FakeClient()
        engine = LiveExecutionEngine(client, Settings(), dry_run=True)  # type: ignore[arg-type]

        report = engine.process_decision(_decision())

        self.assertTrue(report.accepted)
        self.assertTrue(report.dry_run)
        self.assertEqual(client.orders, [])
        self.assertEqual(report.order_request["side"], "buy")  # type: ignore[index]

    def test_real_mode_requires_live_env_flags(self) -> None:
        client = FakeClient()
        engine = LiveExecutionEngine(client, Settings(), dry_run=False)  # type: ignore[arg-type]

        report = engine.process_decision(_decision())

        self.assertFalse(report.accepted)
        self.assertIn("TRADING_MODE must be 'live'", report.reason)
        self.assertEqual(client.orders, [])

    def test_real_mode_submits_when_live_flags_enabled(self) -> None:
        client = FakeClient(
            positions=[
                [_closed_position_row()],
                [_open_position_row()],
            ],
            orders=[_order_row()],
        )
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_confirm_i_understand_risk="YES",
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        engine = LiveExecutionEngine(client, settings, dry_run=False)  # type: ignore[arg-type]

        report = engine.process_decision(_decision())

        self.assertTrue(report.accepted)
        self.assertFalse(report.dry_run)
        self.assertEqual(report.exchange_response, [{"id": "live-order-1"}])
        self.assertEqual(len(client.orders), 1)
        self.assertTrue(report.metadata["live_sync"]["ok"])  # type: ignore[index]

    def test_real_mode_marks_report_unaccepted_when_entry_sync_fails(self) -> None:
        client = FakeClient(
            positions=[
                [_closed_position_row()],
                [],
            ],
            orders=[_order_row()],
        )
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_confirm_i_understand_risk="YES",
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        engine = LiveExecutionEngine(client, settings, dry_run=False)  # type: ignore[arg-type]

        report = engine.process_decision(_decision())

        self.assertFalse(report.accepted)
        self.assertEqual(len(client.orders), 1)
        self.assertIn("no exchange position row", report.reason)
        self.assertFalse(report.metadata["live_sync"]["ok"])  # type: ignore[index]

    def test_real_mode_rejects_when_exchange_position_already_exists(self) -> None:
        client = FakeClient(positions=[[_open_position_row()]])
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_confirm_i_understand_risk="YES",
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        engine = LiveExecutionEngine(client, settings, dry_run=False)  # type: ignore[arg-type]

        report = engine.process_decision(_decision())

        self.assertFalse(report.accepted)
        self.assertIn("live_position_conflict", report.reason)
        self.assertEqual(client.orders, [])

    def test_entry_sync_creates_position_tpsl_when_triggers_are_missing(self) -> None:
        client = FakeClient(
            positions=[
                [_closed_position_row()],
                [
                    _open_position_row(
                        stop_loss_trigger=None,
                        take_profit_trigger=None,
                    )
                ],
            ],
            orders=[_order_row()],
        )
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_confirm_i_understand_risk="YES",
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        engine = LiveExecutionEngine(client, settings, dry_run=False)  # type: ignore[arg-type]

        report = engine.process_decision(_decision())

        self.assertTrue(report.accepted)
        self.assertEqual(client.tpsl_calls[0]["position_id"], "pos-1")
        self.assertEqual(client.tpsl_calls[0]["stop_loss_stop_price"], Decimal("95"))
        self.assertEqual(client.tpsl_calls[0]["take_profit_stop_price"], Decimal("110"))

    def test_live_exit_looks_up_position_by_pair_and_confirms_closed(self) -> None:
        client = FakeClient(
            positions=[
                [_open_position_row()],
                [_closed_position_row()],
            ],
        )
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_confirm_i_understand_risk="YES",
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        engine = LiveExecutionEngine(client, settings, dry_run=False)  # type: ignore[arg-type]
        decision = _decision(
            _signal(
                action=SignalAction.EXIT_LONG,
                direction=SignalDirection.LONG,
            )
        )

        report = engine.process_decision(decision)

        self.assertTrue(report.accepted)
        self.assertEqual(client.exit_calls, ["pos-1"])
        self.assertEqual(client.cancel_position_calls, ["pos-1"])
        self.assertIn("position is closed", report.reason)


if __name__ == "__main__":
    unittest.main()
