from __future__ import annotations

import unittest
from decimal import Decimal

from app.broker.models import PaperExecutionStatus, PaperOrderSide
from app.broker.paper import PaperBroker
from app.config import RiskSettings
from app.data.candle_builder import OHLCVCandle
from app.execution.engine import PaperExecutionEngine
from app.risk.manager import RiskManager
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _entry_signal(
    *,
    strategy_name: str = "test",
    direction: SignalDirection = SignalDirection.LONG,
    entry_price: Decimal = Decimal("100"),
    stop_loss: Decimal = Decimal("95"),
    take_profit: Decimal = Decimal("110"),
    metadata: dict[str, object] | None = None,
) -> StrategySignal:
    action = (
        SignalAction.ENTER_LONG
        if direction == SignalDirection.LONG
        else SignalAction.ENTER_SHORT
    )
    return StrategySignal(
        strategy_name=strategy_name,
        pair="B-BTC_USDT",
        interval="1h",
        action=action,
        direction=direction,
        confidence=Decimal("0.8"),
        reason="test entry",
        timestamp_ms=1,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        metadata=metadata or {},
    )


def _exit_signal(direction: SignalDirection, price: Decimal) -> StrategySignal:
    action = (
        SignalAction.EXIT_LONG
        if direction == SignalDirection.LONG
        else SignalAction.EXIT_SHORT
    )
    return StrategySignal(
        strategy_name="test",
        pair="B-BTC_USDT",
        interval="1h",
        action=action,
        direction=direction,
        confidence=Decimal("0.6"),
        reason="test exit",
        timestamp_ms=2,
        entry_price=price,
    )


def _candle(
    *,
    low: Decimal,
    high: Decimal,
    close: Decimal,
    open_price: Decimal = Decimal("100"),
) -> OHLCVCandle:
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="1h",
        open_time_ms=3_600_000,
        close_time_ms=7_199_999,
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=Decimal("10"),
    )


def _decision(signal: StrategySignal):
    return RiskManager(
        RiskSettings(
            max_risk_per_trade_pct=Decimal("1"),
            max_daily_loss_pct=Decimal("3"),
            max_open_positions=1,
            max_leverage=2,
        )
    ).evaluate_signal(
        signal,
        account_equity=Decimal("1000"),
        requested_leverage=Decimal("1"),
    )


class PaperBrokerTests(unittest.TestCase):
    def test_approved_entry_opens_paper_long_position(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        report = broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.status, PaperExecutionStatus.FILLED)
        self.assertEqual(report.order.side, PaperOrderSide.BUY)  # type: ignore[union-attr]
        self.assertEqual(report.fill.price, Decimal("100"))  # type: ignore[union-attr]
        self.assertEqual(report.position.quantity, Decimal("2"))  # type: ignore[union-attr]
        self.assertEqual(broker.snapshot().open_position_count, 1)

    def test_rejected_risk_decision_does_not_create_order_or_position(self) -> None:
        hold = StrategySignal.hold(
            strategy_name="test",
            pair="B-BTC_USDT",
            interval="1h",
            timestamp_ms=1,
            reason="nothing to do",
        )
        broker = PaperBroker(starting_equity=Decimal("1000"))
        report = broker.execute_decision(
            RiskManager(
                RiskSettings(
                    max_risk_per_trade_pct=Decimal("1"),
                    max_daily_loss_pct=Decimal("3"),
                    max_open_positions=1,
                    max_leverage=2,
                )
            ).evaluate_signal(hold, account_equity=Decimal("1000")),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        self.assertFalse(report.accepted)
        self.assertEqual(report.status, PaperExecutionStatus.REJECTED)
        self.assertIsNone(report.order)
        self.assertEqual(broker.snapshot().open_position_count, 0)

    def test_exit_signal_closes_long_and_records_realized_pnl(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        report = broker.execute_decision(
            _decision(_exit_signal(SignalDirection.LONG, Decimal("110"))),
            market_price=Decimal("110"),
            timestamp_ms=20,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.fill.realized_pnl, Decimal("20"))  # type: ignore[union-attr]
        self.assertEqual(broker.realized_pnl, Decimal("20"))
        self.assertEqual(broker.snapshot().equity, Decimal("1020"))
        self.assertEqual(broker.snapshot().open_position_count, 0)

    def test_short_position_profits_when_price_falls(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        entry = _entry_signal(
            direction=SignalDirection.SHORT,
            stop_loss=Decimal("105"),
            take_profit=Decimal("90"),
        )
        broker.execute_decision(
            _decision(entry),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        report = broker.execute_decision(
            _decision(_exit_signal(SignalDirection.SHORT, Decimal("90"))),
            market_price=Decimal("90"),
            timestamp_ms=20,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.order.side, PaperOrderSide.BUY)  # type: ignore[union-attr]
        self.assertEqual(report.fill.realized_pnl, Decimal("20"))  # type: ignore[union-attr]
        self.assertEqual(broker.snapshot().equity, Decimal("1020"))

    def test_candle_reports_ambiguous_when_stop_and_take_profit_both_hit(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        engine = PaperExecutionEngine(broker)
        engine.process_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        reports = engine.process_candle(
            _candle(
                low=Decimal("94"),
                high=Decimal("111"),
                close=Decimal("101"),
            )
        )

        self.assertEqual(len(reports), 1)
        self.assertTrue(reports[0].accepted)
        self.assertEqual(
            reports[0].reason,
            "Ambiguous candle hit stop loss and take profit; conservative stop loss used.",
        )
        self.assertEqual(reports[0].fill.price, Decimal("95"))  # type: ignore[union-attr]
        self.assertEqual(reports[0].signal.metadata["ambiguous_candle"], True)  # type: ignore[union-attr]
        self.assertEqual(broker.snapshot().equity, Decimal("990"))

    def test_gap_stop_fills_at_candle_open_not_stop_level(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        engine = PaperExecutionEngine(broker)
        engine.process_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        reports = engine.process_candle(
            _candle(
                open_price=Decimal("90"),
                low=Decimal("89"),
                high=Decimal("96"),
                close=Decimal("92"),
            )
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(
            reports[0].reason,
            "Stop loss gapped through; filled at candle open.",
        )
        self.assertEqual(reports[0].fill.price, Decimal("90"))  # type: ignore[union-attr]
        self.assertEqual(reports[0].signal.metadata["gap_exit"], True)  # type: ignore[union-attr]
        self.assertEqual(
            reports[0].signal.metadata["trigger_price_source"],  # type: ignore[union-attr]
            "candle_open",
        )
        self.assertEqual(broker.snapshot().equity, Decimal("980"))

    def test_stop_slippage_uses_separate_stop_rate(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("1000"),
            slippage_pct=Decimal("0"),
            stop_slippage_pct=Decimal("1"),
        )
        engine = PaperExecutionEngine(broker)
        engine.process_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        reports = engine.process_candle(
            _candle(low=Decimal("94"), high=Decimal("100"), close=Decimal("96"))
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].fill.price, Decimal("94.05"))  # type: ignore[union-attr]
        self.assertEqual(reports[0].fill.metadata["pre_slippage_price"], Decimal("95"))  # type: ignore[union-attr]
        self.assertEqual(reports[0].fill.metadata["slippage_pct"], Decimal("1"))  # type: ignore[union-attr]
        self.assertEqual(reports[0].fill.metadata["slippage_type"], "stop")  # type: ignore[union-attr]

    def test_trailing_stop_tightens_long_and_exits_on_later_candle(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("1000"),
            trailing_stop_enabled=True,
            trailing_stop_activation_pct=Decimal("1"),
            trailing_stop_distance_pct=Decimal("2"),
        )
        engine = PaperExecutionEngine(broker)
        engine.process_decision(
            _decision(_entry_signal(take_profit=Decimal("150"))),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        first_reports = engine.process_candle(
            _candle(low=Decimal("100"), high=Decimal("104"), close=Decimal("103"))
        )
        position = broker.open_positions()[0]

        self.assertEqual(first_reports, [])
        self.assertEqual(position.stop_loss, Decimal("100.94"))
        self.assertTrue(position.metadata["trailing_stop_active"])

        second_reports = engine.process_candle(
            _candle(
                open_price=Decimal("101.5"),
                low=Decimal("100.90"),
                high=Decimal("102"),
                close=Decimal("101"),
            )
        )

        self.assertEqual(len(second_reports), 1)
        self.assertEqual(second_reports[0].reason, "Trailing stop triggered.")
        self.assertEqual(second_reports[0].fill.price, Decimal("100.94"))  # type: ignore[union-attr]

    def test_trailing_stop_tightens_short_and_exits_on_later_candle(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("1000"),
            trailing_stop_enabled=True,
            trailing_stop_activation_pct=Decimal("1"),
            trailing_stop_distance_pct=Decimal("2"),
        )
        engine = PaperExecutionEngine(broker)
        entry = _entry_signal(
            direction=SignalDirection.SHORT,
            stop_loss=Decimal("105"),
            take_profit=Decimal("50"),
        )
        engine.process_decision(
            _decision(entry),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        first_reports = engine.process_candle(
            _candle(low=Decimal("96"), high=Decimal("100"), close=Decimal("97"))
        )
        position = broker.open_positions()[0]

        self.assertEqual(first_reports, [])
        self.assertEqual(position.stop_loss, Decimal("98.94"))
        self.assertTrue(position.metadata["trailing_stop_active"])

        second_reports = engine.process_candle(
            _candle(
                open_price=Decimal("98.5"),
                low=Decimal("98"),
                high=Decimal("99"),
                close=Decimal("98.5"),
            )
        )

        self.assertEqual(len(second_reports), 1)
        self.assertEqual(second_reports[0].reason, "Trailing stop triggered.")
        self.assertEqual(second_reports[0].fill.price, Decimal("98.94"))  # type: ignore[union-attr]

    def test_dynamic_atr_exits_refresh_long_levels_and_stop_later(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        engine = PaperExecutionEngine(broker)
        engine.process_decision(
            _decision(
                _entry_signal(
                    metadata={
                        "atr_dynamic_exits_enabled": True,
                        "atr_best_price": Decimal("100"),
                    },
                )
            ),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        broker.update_dynamic_atr_exits(
            _candle(low=Decimal("101"), high=Decimal("110"), close=Decimal("108")),
            atr=Decimal("2"),
            stop_multiple=Decimal("2"),
            take_profit_multiple=Decimal("3"),
        )
        position = broker.open_positions()[0]

        self.assertEqual(position.stop_loss, Decimal("106"))
        self.assertEqual(position.take_profit, Decimal("110"))
        self.assertTrue(position.metadata["atr_dynamic_exit_active"])

        reports = engine.process_candle(
            _candle(
                open_price=Decimal("107"),
                low=Decimal("105"),
                high=Decimal("109"),
                close=Decimal("106"),
            )
        )

        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].reason, "Dynamic ATR stop triggered.")
        self.assertEqual(reports[0].fill.price, Decimal("106"))  # type: ignore[union-attr]

    def test_snapshot_includes_unrealized_pnl_at_mark_price(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        snapshot = broker.snapshot({"B-BTC_USDT": Decimal("103")})

        self.assertEqual(snapshot.unrealized_pnl, Decimal("6"))
        self.assertEqual(snapshot.equity, Decimal("1006"))
        self.assertEqual(snapshot.open_notional, Decimal("206"))

    def test_rejected_order_report_keeps_marked_unrealized_pnl(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        report = broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("103"),
            timestamp_ms=20,
        )

        self.assertFalse(report.accepted)
        self.assertEqual(report.account.unrealized_pnl, Decimal("6"))
        self.assertEqual(report.account.equity, Decimal("1006"))
        self.assertEqual(report.account.open_notional, Decimal("206"))

    def test_same_strategy_scale_in_updates_aggregate_position(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        broker.execute_decision(
            _decision(_entry_signal(metadata={"grid_entries": 1})),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        scale_signal = _entry_signal(
            entry_price=Decimal("90"),
            stop_loss=Decimal("80"),
            take_profit=None,
            metadata={
                "allow_scale_in": True,
                "grid_entries": 2,
                "last_grid_entry_price": Decimal("90"),
            },
        )
        report = broker.execute_decision(
            _decision(scale_signal),
            market_price=Decimal("90"),
            timestamp_ms=20,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.reason, "Paper grid scale-in filled.")
        self.assertEqual(report.position.quantity, Decimal("3"))  # type: ignore[union-attr]
        self.assertEqual(
            report.position.entry_price,  # type: ignore[union-attr]
            Decimal("96.66666666666666666666666667"),
        )
        self.assertEqual(report.position.metadata["grid_entries"], 2)  # type: ignore[union-attr]
        self.assertEqual(report.position.metadata["scale_in_count"], 1)  # type: ignore[union-attr]

    def test_duplicate_entry_without_scale_in_metadata_still_rejects(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        report = broker.execute_decision(
            _decision(_entry_signal(entry_price=Decimal("90"), stop_loss=Decimal("80"))),
            market_price=Decimal("90"),
            timestamp_ms=20,
        )

        self.assertFalse(report.accepted)
        self.assertIn("already open", report.reason)

    def test_slippage_and_fees_reduce_equity(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("1000"),
            taker_fee_rate=Decimal("0.001"),
            slippage_pct=Decimal("1"),
        )

        report = broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.fill.price, Decimal("101"))  # type: ignore[union-attr]
        self.assertEqual(report.fill.fee, Decimal("0.202"))  # type: ignore[union-attr]
        self.assertEqual(broker.snapshot({"B-BTC_USDT": Decimal("101")}).equity, Decimal("999.798"))

    def test_maker_entry_and_taker_exit_use_different_fee_rates(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("1000"),
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            entry_fee_type="maker",
            exit_fee_type="taker",
        )
        broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        report = broker.execute_decision(
            _decision(_exit_signal(SignalDirection.LONG, Decimal("110"))),
            market_price=Decimal("110"),
            timestamp_ms=20,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(broker.fills[0].fee, Decimal("0.04"))
        self.assertEqual(broker.fills[0].metadata["fee_type"], "maker")
        self.assertEqual(report.fill.fee, Decimal("0.1100"))  # type: ignore[union-attr]
        self.assertEqual(report.fill.metadata["fee_type"], "taker")  # type: ignore[union-attr]
        self.assertEqual(broker.fees_paid, Decimal("0.1500"))

    def test_fee_gst_is_charged_on_trading_fee_only(self) -> None:
        broker = PaperBroker(
            starting_equity=Decimal("1000"),
            maker_fee_rate=Decimal("0.0002"),
            fee_gst_rate=Decimal("0.18"),
            entry_fee_type="maker",
        )

        report = broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        self.assertTrue(report.accepted)
        self.assertEqual(report.fill.fee, Decimal("0.0472"))  # type: ignore[union-attr]
        self.assertEqual(report.fill.metadata["base_fee"], Decimal("0.0400"))  # type: ignore[union-attr]
        self.assertEqual(report.fill.metadata["gst_fee"], Decimal("0.007200"))  # type: ignore[union-attr]
        self.assertEqual(report.fill.metadata["effective_fee_rate"], Decimal("0.000236"))  # type: ignore[union-attr]
        self.assertEqual(broker.snapshot({"B-BTC_USDT": Decimal("100")}).equity, Decimal("999.9528"))

    def test_execution_report_serializes_nested_values(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("1000"))
        report = broker.execute_decision(
            _decision(_entry_signal()),
            market_price=Decimal("100"),
            timestamp_ms=10,
        )

        data = report.to_dict()

        self.assertEqual(data["status"], "FILLED")
        self.assertEqual(data["order"]["side"], "buy")
        self.assertEqual(data["position"]["direction"], "long")
        self.assertEqual(data["risk_decision"]["status"], "APPROVED")


if __name__ == "__main__":
    unittest.main()
